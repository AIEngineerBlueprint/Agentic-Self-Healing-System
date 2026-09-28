"""Execution: snapshot, verify, apply, deploy, restore.

Every autonomous mutation in the demo has a known rollback path. That is not a
nice-to-have -- it is what makes the mutation defensible in the first place, so
the snapshot is taken BEFORE anything is written and the restore path is tested
in the same episode that used it.

Git is deliberately not involved yet. The apply/restore contract is expressed as
snapshot-and-restore behind two functions, so swapping in git later touches only
this module and nothing above it.

The syntax gate matters more than it looks. Model-generated Python that does not
compile would be written to a hot-reloading service and take it down -- turning
a repair attempt into an outage. Nothing is written until it compiles.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import shutil
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import httpx

from . import catalog

log = logging.getLogger(__name__)

WORKSPACE = pathlib.Path("/workspace/services")
SNAPSHOT_ROOT = pathlib.Path("/tmp/ashs-snapshots")


class ExecutionError(RuntimeError):
    """A step in the execution path failed. Callers roll back."""


# ------------------------------------------------------------------ snapshot

def snapshot(incident_id: str, paths: list[str]) -> dict:
    """Copy every target file aside before anything is modified."""
    root = SNAPSHOT_ROOT / incident_id
    root.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}

    for rel in paths:
        source = (WORKSPACE / rel).resolve()
        if not source.is_relative_to(WORKSPACE.resolve()):
            raise ExecutionError(f"path escapes the workspace: {rel}")
        # Percent-encode rather than swap "/" for "__": dunder file names such
        # as __init__.py would otherwise decode back to the wrong path.
        destination = root / quote(rel, safe="")
        if source.is_file():
            shutil.copy2(source, destination)
            saved[rel] = str(destination)
        else:
            # A file that does not exist yet (a new test) is recorded as absent
            # so restore knows to delete it rather than resurrect nothing.
            destination.with_suffix(destination.suffix + ".absent").touch()
            saved[rel] = str(destination) + ".absent"

    log.info("snapshot for %s: %d path(s)", incident_id, len(saved))
    return {"incident_id": incident_id, "root": str(root), "files": saved,
            "taken_at": datetime.now(timezone.utc).isoformat()}


def restore(snap: dict) -> dict:
    """Put every snapshotted file back exactly as it was."""
    restored, deleted = [], []
    for rel, stored in (snap.get("files") or {}).items():
        target = (WORKSPACE / rel).resolve()
        if stored.endswith(".absent"):
            if target.is_file():
                target.unlink()
                deleted.append(rel)
        else:
            shutil.copy2(stored, target)
            restored.append(rel)
    log.warning("restored %d file(s), deleted %d created file(s)", len(restored), len(deleted))
    return {"restored": restored, "deleted": deleted}


def restore_by_incident(incident_id: str) -> dict:
    """Roll back from the snapshot on disk, with nothing held in memory.

    `restore()` needs the dict `snapshot()` returned, which only exists inside
    the call that is doing the repair. If that call dies -- a hung test, the
    process restarting mid-repair -- the patch stays applied and nothing can
    put it back. Reconstructing from the snapshot directory means a later sweep
    can still unwind it.
    """
    root = SNAPSHOT_ROOT / incident_id
    if not root.is_dir():
        return {"restored": [], "deleted": [], "snapshot_found": False}

    files = {}
    for stored in root.iterdir():
        rel = stored.name[:-len(".absent")] if stored.name.endswith(".absent") else stored.name
        files[unquote(rel)] = str(stored)

    result = restore({"files": files})
    result["snapshot_found"] = True
    return result


# ------------------------------------------------------------- verify + apply

def check_syntax(path: str, content: str) -> None:
    """Refuse to write source that does not parse.

    A hot-reloading service given unparseable Python does not fail the repair --
    it fails the service. This gate is why a bad patch is a rejected patch
    rather than an outage.
    """
    if not path.endswith(".py"):
        return
    try:
        ast.parse(content)
    except SyntaxError as exc:
        raise ExecutionError(
            f"generated content for {path} is not valid Python "
            f"(line {exc.lineno}: {exc.msg}); refusing to write"
        ) from exc


def apply(path: str, content: str) -> dict:
    """Write a file, after the syntax gate. Creates parent dirs for new tests."""
    check_syntax(path, content)
    target = (WORKSPACE / path).resolve()
    if not target.is_relative_to(WORKSPACE.resolve()):
        raise ExecutionError(f"path escapes the workspace: {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content)
    log.info("applied %s (%s, %d bytes)", path, "modified" if existed else "created", len(content))
    return {"path": path, "created": not existed, "bytes": len(content)}


# --------------------------------------------------------------- deploy wait

def wait_for_healthy(service: str, timeout: float = 45.0) -> dict:
    """Wait for a hot-reloaded service to come back.

    uvicorn --reload picks up the changed file from the mounted volume, so
    "deploy" is a file write plus this wait. The wait is what turns that into a
    verifiable step rather than a hope.
    """
    url = catalog.health_url(service)
    if url is None:
        raise ExecutionError(
            f"no health endpoint known for '{service}'. Declare base_url (or "
            f"contract_url) and health_path for it in services.yaml.")

    deadline = time.time() + timeout
    last = "never responded"
    # Give the reloader a moment to notice the write before polling.
    time.sleep(2.0)

    while time.time() < deadline:
        try:
            with httpx.Client(timeout=4.0) as c:
                r = c.get(url)
            if r.status_code == 200:
                return {"service": service, "healthy": True,
                        "waited_seconds": round(timeout - (deadline - time.time()), 1)}
            last = f"HTTP {r.status_code}"
        except httpx.RequestError as exc:
            last = type(exc).__name__
        time.sleep(1.5)

    raise ExecutionError(f"{service} did not return healthy within {timeout:.0f}s ({last})")
