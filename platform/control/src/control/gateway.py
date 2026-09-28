"""The typed tool gateway.

THE LLM GETS NO RAW SHELL, NO KUBECTL, NO UNRESTRICTED FILE ACCESS.

Every capability an agent has is a typed tool registered here. Every invocation
passes three checks before it runs, and all three are enforced in code:

  1. Actor scoping   -- may THIS agent use THIS tool at all?
  2. Path allowlist  -- for anything touching source, is the path within the
                        agent's declared write scope? (from services.yaml)
  3. Protected zone  -- is the path one that is always human, whatever the
                        confidence? (from policy.yaml)

And every call -- allowed or denied -- is written to the append-only audit log
with its arguments and result. A denied call is evidence too: the demo has to be
able to show the system refusing, not just succeeding.

Guardrails enforced by prompt are not guardrails. Nothing in this module reads
a model's output as an instruction.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from . import catalog, incidents

log = logging.getLogger(__name__)

# Service repositories are mounted here read-write; the gateway is what makes
# that safe. Nothing outside WORKSPACE is reachable by any tool.
WORKSPACE = pathlib.Path("/workspace/services")


class ToolDenied(PermissionError):
    """A guardrail refused the call. This is a successful outcome, not a bug."""


class ToolFailed(RuntimeError):
    """The tool ran and failed on its own terms."""


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    mutates: bool = False           # does this change state?
    path_scoped: bool = False       # does it take a `path` needing allowlist checks?
    allowed_actors: tuple[str, ...] = ()   # empty = any registered actor


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, *, mutates: bool = False,
         path_scoped: bool = False, allowed_actors: tuple[str, ...] = ()):
    def decorate(fn):
        REGISTRY[name] = Tool(name, description, fn, mutates, path_scoped, allowed_actors)
        return fn
    return decorate


# ---------------------------------------------------------------- path safety

def resolve_in_workspace(path: str) -> pathlib.Path:
    """Resolve a repo-relative path, refusing anything that escapes the workspace.

    `path` is untrusted -- it may originate from model output. Canonicalise
    first, then verify containment. Never open a raw model-supplied path.
    """
    candidate = (WORKSPACE / path).resolve()
    workspace = WORKSPACE.resolve()
    if not candidate.is_relative_to(workspace):
        raise ToolDenied(f"path escapes the workspace: {path}")
    return candidate


def check_path(actor: str, path: str) -> None:
    """Path allowlist + protected zone. Order matters: protected wins."""
    if catalog.is_protected(path):
        zone = catalog.sensitive_zone_for(path)
        reason = zone["reason"].strip() if zone else "declared in policy.protected_paths"
        raise ToolDenied(
            f"'{path}' is in the PROTECTED ZONE and may never be modified "
            f"autonomously. Reason: {reason}"
        )
    if not catalog.agent_may_write(actor, path):
        raise ToolDenied(
            f"agent '{actor}' has no write scope covering '{path}'. "
            f"Ownership boundaries are hard boundaries."
        )


# ------------------------------------------------------------------ dispatch

def invoke(conn, incident_id: str, actor: str, name: str, **args) -> dict:
    """The single entry point for every tool call. Always audited."""
    started = datetime.now(timezone.utc)
    spec = REGISTRY.get(name)

    if spec is None:
        incidents.audit(conn, incident_id, "tool-gateway", "tool_call",
                        f"DENIED {name}: no such tool.",
                        {"actor": actor, "tool": name, "args": _safe(args),
                         "outcome": "denied", "reason": "unknown_tool"})
        raise ToolDenied(f"unknown tool '{name}'")

    try:
        if spec.allowed_actors and actor not in spec.allowed_actors:
            raise ToolDenied(
                f"agent '{actor}' may not use '{name}' "
                f"(restricted to {list(spec.allowed_actors)})"
            )
        if spec.path_scoped:
            path = args.get("path") or args.get("file_path")
            if not path:
                raise ToolDenied(f"'{name}' requires a path argument")
            check_path(actor, path)

        result = spec.handler(conn=conn, incident_id=incident_id, actor=actor, **args)

    except ToolDenied as exc:
        incidents.audit(conn, incident_id, "tool-gateway", "tool_call",
                        f"DENIED {name}: {exc}",
                        {"actor": actor, "tool": name, "args": _safe(args),
                         "outcome": "denied", "reason": str(exc),
                         "mutates": spec.mutates})
        log.warning("tool denied: %s by %s -- %s", name, actor, exc)
        raise
    except Exception as exc:  # noqa: BLE001
        incidents.audit(conn, incident_id, "tool-gateway", "tool_call",
                        f"FAILED {name}: {type(exc).__name__}: {exc}",
                        {"actor": actor, "tool": name, "args": _safe(args),
                         "outcome": "failed", "error": str(exc)})
        raise ToolFailed(str(exc)) from exc

    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    incidents.audit(conn, incident_id, "tool-gateway", "tool_call",
                    f"OK {name}",
                    {"actor": actor, "tool": name, "args": _safe(args),
                     "outcome": "ok", "mutates": spec.mutates,
                     "latency_ms": round(elapsed),
                     "result_summary": _summarise(result)})
    return result


def _safe(args: dict) -> dict:
    return {k: (v[:300] + "..." if isinstance(v, str) and len(v) > 300 else v)
            for k, v in args.items()}


def _summarise(result: Any) -> Any:
    if isinstance(result, dict):
        return {k: (f"<{len(v)} items>" if isinstance(v, (list, dict)) else
                    (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v))
                for k, v in result.items()}
    return str(result)[:200]


def manifest() -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "mutates": t.mutates,
         "path_scoped": t.path_scoped, "allowed_actors": list(t.allowed_actors)}
        for t in sorted(REGISTRY.values(), key=lambda t: t.name)
    ]


# =========================================================== READ-ONLY TOOLS

@tool("get_service_health", "Liveness and downstream dependency status for a service.")
def _get_service_health(*, service: str, **_) -> dict:
    url = catalog.health_url(service)
    if not url:
        raise ToolFailed(
            f"no health endpoint known for '{service}'. Declare base_url (or "
            f"contract_url) and health_path for it in services.yaml.")
    with httpx.Client(timeout=5.0) as c:
        r = c.get(url)
    return {"service": service, "status_code": r.status_code, "body": r.json()}


@tool("get_recent_logs", "Structured logs for a service over a time window.")
def _get_recent_logs(*, conn, service: str, minutes: int = 15, limit: int = 50, **_) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = conn.execute(
        "SELECT service_name, severity, body, trace_id, observed_at FROM tel_logs "
        "WHERE service_name = %s AND observed_at >= %s ORDER BY observed_at DESC LIMIT %s",
        (service, since, limit),
    ).fetchall()
    return {"service": service, "count": len(rows),
            "logs": [{**dict(r), "observed_at": r["observed_at"].isoformat()} for r in rows]}


@tool("get_trace", "Full span tree for one trace.")
def _get_trace(*, conn, trace_id: str, **_) -> dict:
    rows = conn.execute(
        "SELECT span_id, parent_span_id, service_name, code_repository, name, kind, "
        "status_code, duration_ms, attributes, events FROM tel_spans "
        "WHERE trace_id = %s ORDER BY start_time",
        (trace_id,),
    ).fetchall()
    return {"trace_id": trace_id, "span_count": len(rows), "spans": [dict(r) for r in rows]}


@tool("get_contract", "A service's published OpenAPI document, generated from code.")
def _get_contract(*, service: str, **_) -> dict:
    doc = catalog.contract(service)
    if doc is None:
        raise ToolFailed(f"no contract published for '{service}'")
    return {"service": service, "paths": list(doc.get("paths", {})),
            "schemas": list(doc.get("components", {}).get("schemas", {}))}


@tool("validate_against_contract", "Validate a payload against a service's published schema.")
def _validate_against_contract(*, service: str, operation: str, payload: dict,
                               status_code: int = 200, **_) -> dict:
    from . import attribution
    schema = catalog.response_schema(service, operation, status_code)
    if schema is None:
        raise ToolFailed(f"no schema resolved for {service} {operation}")
    try:
        violations = attribution._validate(payload, schema)
    except RuntimeError as exc:
        raise ToolFailed(str(exc)) from exc
    return {"service": service, "operation": operation,
            "valid": not violations, "violations": violations}


@tool("get_deployment_history", "Version timeline for a service.")
def _get_deployment_history(*, conn, service: str, limit: int = 10, **_) -> dict:
    rows = conn.execute(
        "SELECT service_name, version, ref, deployed_at, deployed_by, note "
        "FROM tel_deployments WHERE service_name = %s ORDER BY deployed_at DESC LIMIT %s",
        (service, limit),
    ).fetchall()
    return {"service": service,
            "deployments": [{**dict(r), "deployed_at": r["deployed_at"].isoformat()} for r in rows]}


@tool("read_source", "Read a source file. Allowlist-scoped, read-only.",
      path_scoped=True)
def _read_source(*, path: str, max_bytes: int = 60000, **_) -> dict:
    resolved = resolve_in_workspace(path)
    if not resolved.is_file():
        raise ToolFailed(f"not a file: {path}")
    text = resolved.read_text(errors="replace")[:max_bytes]
    return {"path": path, "lines": text.count("\n") + 1, "bytes": len(text), "content": text}


@tool("run_diagnostic", "Run a read-only check against a live target.")
def _run_diagnostic(*, check_id: str, target: str, **_) -> dict:
    """Read-only hypothesis verification. Fixed check ids -- never free-form."""
    checks = {
        "reproduce_on_target": (
            "Replay the target service's declared failing request and report "
            "the status code.",
            _probe_declared_replay,
        ),
        "downstream_contract": (
            "Replay each declared dependency's request and confirm it still "
            "answers within its own contract.",
            _probe_dependencies,
        ),
    }
    if check_id not in checks:
        raise ToolFailed(f"unknown check '{check_id}'; known: {list(checks)}")
    description, fn = checks[check_id]
    return {"check_id": check_id, "target": target, "description": description, "result": fn(target)}


def _call_declared(service: str, spec: dict) -> dict:
    """Issue one request declared in services.yaml against one service."""
    root = catalog.base_url(service)
    if not root:
        raise ToolFailed(f"'{service}' declares no base_url in services.yaml")

    method = str(spec.get("method", "GET")).upper()
    with httpx.Client(timeout=15.0) as c:
        r = c.request(method, root + spec["path"], json=spec.get("body"))

    body = {}
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            body = r.json()
        except ValueError:
            body = {}

    expected = spec.get("expect_status", 200)
    return {
        "service": service,
        "request": f"{method} {spec['path']}",
        "status_code": r.status_code,
        "matched_expectation": r.status_code == expected,
        "expected_status": expected,
        "json_matches": _json_subset(spec.get("expect_json"), body),
        "body": r.text[:500],
    }


def _json_subset(expected: dict | None, actual: dict) -> bool | None:
    """Top-level subset equality. None when nothing was declared to check."""
    if not expected:
        return None
    return all(actual.get(k) == v for k, v in expected.items())


def _probe_declared_replay(target: str) -> dict:
    spec = (catalog.validation_spec(target).get("replay")
            or catalog.validation_spec(target).get("regression_probe"))
    if not spec:
        raise ToolFailed(f"'{target}' declares no validation.replay in services.yaml")
    result = _call_declared(target, spec)
    result["reproduces_failure"] = result["status_code"] >= 500
    return result


def _probe_dependencies(target: str) -> dict:
    """Every declared dependency of the target, probed against its own spec.

    Hardcoding calc-api here made the check a Carbon Ledger check. Walking
    depends_on makes it the same check for any service in the catalog.
    """
    results = []
    for dependency in catalog.service(target).get("depends_on") or []:
        spec = (catalog.validation_spec(dependency).get("replay")
                or catalog.validation_spec(dependency).get("regression_probe"))
        if not spec:
            continue
        try:
            results.append(_call_declared(dependency, spec))
        except (ToolFailed, httpx.RequestError) as exc:
            results.append({"service": dependency, "error": str(exc), "matched_expectation": False})

    return {
        "dependencies_probed": [r["service"] for r in results],
        "all_behaved_correctly": bool(results) and all(
            r.get("matched_expectation") and r.get("json_matches") is not False
            for r in results
        ),
        "results": results,
    }


# ==================================================== MUTATING TOOLS (gated)
#
# Registered now so the policy engine has a real action surface to reason over,
# and so a denied write is demonstrable in Phase 2. Execution lands in Phase 3.

@tool("create_patch", "Produce a unified diff for a file. Does NOT apply it.",
      mutates=True, path_scoped=True, allowed_actors=("product-repair-agent",
                                                      "capability-repair-agent"))
def _create_patch(*, path: str, new_content: str, **_) -> dict:
    """Generate a diff without touching disk.

    Separating 'produce the change' from 'apply the change' is what lets a plan
    reach POLICY_APPROVED and still be reviewable before anything mutates.
    """
    import difflib

    resolved = resolve_in_workspace(path)
    original = resolved.read_text(errors="replace") if resolved.is_file() else ""
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )
    )
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return {"path": path, "diff": diff, "lines_added": added,
            "lines_removed": removed, "applied": False}


@tool("run_tests", "Run a test selector in a repository.", allowed_actors=(
      "product-repair-agent", "capability-repair-agent", "validation-agent"))
def _run_tests(*, repository: str, selector: str = "", **_) -> dict:
    repo_dir = resolve_in_workspace(repository)
    if not repo_dir.is_dir():
        raise ToolFailed(f"no such repository: {repository}")
    cmd = ["python", "-m", "pytest", "-q", "--no-header"]
    if selector:
        cmd.append(selector)
    proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=180)
    return {"repository": repository, "selector": selector,
            "exit_code": proc.returncode, "passed": proc.returncode == 0,
            "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


@tool("open_escalation", "Raise a human-readable escalation with the evidence bundle.",
      mutates=True)
def _open_escalation(*, conn, incident_id, reason: str, **_) -> dict:
    """Escalation was previously a state change and nothing else.

    No page, no ticket, no email -- so an escalation was only visible if
    somebody happened to be watching the dashboard. This records a durable,
    queryable escalation with the owning team and the evidence, which is the
    minimum honest version of "escalates to a human". Wiring it to Slack or
    PagerDuty is then one more handler, not a redesign.
    """
    row = conn.execute(
        "SELECT incident_id, fault_domain, confidence, entry_point, first_seen, "
        "diagnosis, plan FROM ash_incidents WHERE incident_id = %s",
        (incident_id,),
    ).fetchone()
    if row is None:
        raise ToolFailed(f"no incident {incident_id}")

    owner = {}
    for name, spec in catalog.services().items():
        if spec.get("repository") == row["fault_domain"]:
            owner = catalog.owner_for(name)
            break

    narrative = (row["diagnosis"] or {}).get("narrative") or {}
    conn.execute(
        """
        INSERT INTO ash_escalations (incident_id, reason, owner_team, owner_contact,
                                     fault_domain, summary, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (incident_id) DO UPDATE SET reason = EXCLUDED.reason
        """,
        (incident_id, reason, owner.get("name"), owner.get("contact"),
         row["fault_domain"], narrative.get("root_cause", "")[:500],
         incidents.J({"diagnosis": row["diagnosis"], "plan": row["plan"],
                      "entry_point": row["entry_point"]})),
    )
    return {"incident_id": incident_id, "owner": owner.get("name"),
            "contact": owner.get("contact"), "reason": reason}
