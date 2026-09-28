"""The repair loop: patch, test gate, deploy, validate, promote or roll back.

    POLICY_APPROVED -> EXECUTING -> VALIDATING -> RESOLVED
                                              -> ROLLBACK -> ESCALATED

THE TEST GATE IS THE CENTREPIECE. A patch is rejected unless the accompanying
test FAILS on the pre-patch code and PASSES after. That single rule is what
separates a fix from a plausible-looking edit, and it is enforced here in code:
if the test passes before the patch, the patch is rejected no matter how good it
looks and no matter what the model claimed.

The model writes the patch and the test. It does not decide whether either is
acceptable -- the gate does.
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import catalog, executor, gateway, incidents, llm

log = logging.getLogger(__name__)

# DEMO ONLY. Forces validation to fail so the rollback path can be exercised on
# cue (Scenario D). A repair that cannot be shown reverting is not a repair
# anyone should trust, so this is a first-class demo control rather than a
# test-only hack.
FORCE_VALIDATION_FAILURE = False


def set_force_validation_failure(enabled: bool) -> None:
    global FORCE_VALIDATION_FAILURE
    FORCE_VALIDATION_FAILURE = bool(enabled)
    log.warning("force_validation_failure -> %s", FORCE_VALIDATION_FAILURE)

PATCH_SYSTEM_TEMPLATE = """\
You are the repair agent for a single repository in an autonomous repair system.

You are given a completed diagnosis and the CURRENT FULL SOURCE of one file.
Return the COMPLETE corrected file plus a pytest test.

Hard rules for the patch:
  - Return the ENTIRE file, not a diff and not a fragment. It is written verbatim.
  - Change as little as possible. Fix the defect; do not refactor around it.
  - Do NOT widen a try/except or otherwise hide the symptom. Make the correct
    behaviour correct.
  - Preserve every existing import, function signature and return shape.
  - The file must be valid Python. It is written to a hot-reloading service.

Hard rules for the test:
  - It must FAIL against the current (broken) code and PASS after your patch.
    A test that passes before the fix proves nothing and will be rejected.
  - Assert the CORRECT behaviour, not merely the absence of an exception.
  - It is an HTTP test run from outside the service. Use `httpx` against the
    base URL in the `{env_var}` environment variable, defaulting to
    "{base_url}". Do not import the application.
  - Plain pytest functions. No fixtures beyond the standard library.
"""

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "patched_file_content": {"type": "string", "description": "The COMPLETE corrected file."},
        "test_file_content": {"type": "string", "description": "A complete pytest file."},
        "change_summary": {"type": "string", "description": "One or two sentences on what changed."},
        "why_it_fixes": {"type": "string"},
    },
    "required": ["patched_file_content", "test_file_content", "change_summary", "why_it_fixes"],
}


def _target_service(plan: dict) -> str:
    """The deployable service this plan repairs.

    The plan carries it; fall back to resolving the fault domain through the
    catalog. There is deliberately no hardcoded default -- a plan that cannot
    name its service must fail loudly rather than quietly act on cfc-api.
    """
    named = plan.get("target_service")
    if named:
        return named
    resolved = catalog.service_for_repository(plan.get("fault_domain") or "")
    if not resolved:
        raise executor.ExecutionError(
            f"no deployable service found for repository "
            f"'{plan.get('fault_domain')}' -- check services.yaml")
    return resolved


def _raise_escalation(conn, incident_id: str, reason: str) -> None:
    """Route through the gateway so the escalation is audited like any action."""
    try:
        gateway.invoke(conn, incident_id, "validation-agent", "open_escalation",
                       reason=reason)
    except Exception:  # noqa: BLE001 - never let paging failure mask the escalation
        log.exception("could not record escalation for %s", incident_id)


def _roll_back(snap: dict, service: str) -> dict:
    """Restore the snapshot, then wait for the service to come back.

    A service that stays down after the restore must not raise out of
    execute(): that would strand the incident in EXECUTING with nobody told.
    The outcome is recorded instead and the caller still escalates.
    """
    rolled = executor.restore(snap)
    try:
        executor.wait_for_healthy(service, timeout=40)
        rolled["healthy_after_restore"] = True
    except executor.ExecutionError as exc:
        log.error("service unhealthy after restore: %s", exc)
        rolled["healthy_after_restore"] = False
        rolled["health_error"] = str(exc)
    return rolled


# ------------------------------------------------------------ patch generation

def generate_patch(conn, incident_id: str) -> dict | None:
    """Ask the model for the corrected file and its test. Second model call."""
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    plan = incident["plan"] or {}
    narrative = (incident["diagnosis"] or {}).get("narrative") or {}

    source_path = plan["target_paths"][0]
    current = (executor.WORKSPACE / source_path).read_text()

    # The test the model writes has to reach the service it is testing. Both
    # the variable name and the URL come from that service's catalog entry, so
    # a second product's agent is told about its own service, not this one's.
    service = _target_service(plan)
    system = PATCH_SYSTEM_TEMPLATE.format(
        env_var=catalog.service(service).get("test_env_var") or "SERVICE_BASE_URL",
        base_url=catalog.base_url(service) or "",
    )

    prompt = "\n".join([
        f"REPOSITORY : {incident['fault_domain']}",
        f"FILE       : {source_path}",
        f"FUNCTION   : {narrative.get('target_function')}",
        "",
        "=== ROOT CAUSE ===", narrative.get("root_cause", ""),
        "", "=== FIX STRATEGY ===", narrative.get("fix_strategy", ""),
        "", "=== THE TEST MUST ASSERT ===", narrative.get("test_assertion", ""),
        "",
        "=== REPRODUCTION ===",
        f"{plan['validation_criteria']['replay_request']['method']} "
        f"{plan['validation_criteria']['replay_request']['path']}",
        f"body: {plan['validation_criteria']['replay_request']['body']}",
        f"expected after the fix: HTTP "
        f"{plan['validation_criteria']['replay_request']['expect_status']}, "
        f"{plan['validation_criteria']['replay_request']['expect']}",
        "",
        f"=== CURRENT FULL SOURCE OF {source_path} ===",
        current,
    ])

    result = llm.complete_json(system, prompt, PATCH_SCHEMA, max_tokens=16000)

    if result is None:
        incidents.audit(conn, incident_id, "product-repair-agent", "decision",
                        "Patch generation unavailable (model unreachable). "
                        "Escalating rather than guessing at a code change.",
                        {"degraded": True})
        return None

    meta = result.get("_meta", {})
    incidents.audit(conn, incident_id, "product-repair-agent", "llm_call",
                    f"Patch generated: {result.get('change_summary')}",
                    {"model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
                     "input_tokens": meta.get("input_tokens"),
                     "output_tokens": meta.get("output_tokens"),
                     "change_summary": result.get("change_summary"),
                     "why_it_fixes": result.get("why_it_fixes"),
                     "patched_bytes": len(result.get("patched_file_content", "")),
                     "test_bytes": len(result.get("test_file_content", ""))})
    return result


# -------------------------------------------------------------- the test gate

def _run_pytest(test_path: str, service: str, timeout: float = 120) -> dict:
    """Run one pytest file from the control plane against the live service.

    The service's URL is passed in from the catalog under the name that
    service declares (`test_env_var`), so the gate can test any service in the
    catalog rather than the one whose URL used to be compiled in here.
    """
    target = executor.WORKSPACE / test_path
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", str(target)],
            capture_output=True, text=True, timeout=timeout,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                 "PYTHONDONTWRITEBYTECODE": "1",
                 **catalog.test_env(service)},
        )
    except subprocess.TimeoutExpired as exc:
        # A model-written test that hangs -- an unbounded retry, a missing
        # timeout on its own HTTP call -- used to raise straight out of the
        # gate, out of execute(), and strand the incident in EXECUTING with
        # the patch possibly already applied and nothing left to roll it back.
        # A test that will not finish is a test that did not pass.
        log.warning("pytest timed out after %.0fs on %s", timeout, test_path)
        return {"exit_code": -1, "passed": False, "timed_out": True,
                "stdout": (exc.stdout or b"").decode(errors="replace")[-3000:]
                          if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-3000:],
                "stderr": f"TIMEOUT: the test did not finish within {timeout:.0f}s."}
    except OSError as exc:  # pytest missing, workspace unreadable, fork failure
        log.exception("could not run pytest for %s", test_path)
        return {"exit_code": -1, "passed": False, "stdout": "",
                "stderr": f"could not run pytest: {type(exc).__name__}: {exc}"}

    return {"exit_code": proc.returncode, "passed": proc.returncode == 0,
            "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-1500:]}


def _defect_still_reproducing(conn, incident, service: str,
                              seconds: int = 90) -> dict:
    """Is the defect still producing errors right now?

    Disambiguates the two very different things a passing pre-patch test can
    mean. If live traffic is still failing on this operation, a test that
    passes against the broken code is a bad test and the patch must be
    rejected. If nothing has errored for a while, the source moved on
    underneath the repair -- an earlier fix landed, a flag was cleared, someone
    ran reset-source -- and the incident is simply stale. Same signal, opposite
    conclusions, and only one of them deserves to page a human.

    Deliberately anchored on 'recently' rather than on the last deployment:
    source can be restored by paths that never write a deployment row, and the
    question that actually matters is whether the system is broken NOW.
    """
    if not incident["entry_point"]:
        # Without an operation to look at we cannot tell quiet from dead. Fail
        # towards "still live": the cost of being wrong is a patch rejection a
        # human reviews, versus silently closing an incident that is still
        # burning.
        return {"live": True, "recent_errors": -1, "window_seconds": seconds,
                "last_error_at": None, "quiet_seconds": None,
                "reason": "incident has no entry_point; assuming still live"}

    since = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    row = conn.execute(
        """
        SELECT count(*) AS n, max(start_time) AS last_error
        FROM tel_spans
        WHERE status_code = 'ERROR' AND kind = 'SERVER'
          AND service_name = %s AND name = %s AND start_time >= %s
        """,
        (service, incident["entry_point"], since),
    ).fetchone()

    last_ever = conn.execute(
        """
        SELECT max(start_time) AS at FROM tel_spans
        WHERE status_code = 'ERROR' AND kind = 'SERVER'
          AND service_name = %s AND name = %s
        """,
        (service, incident["entry_point"]),
    ).fetchone()["at"]

    return {
        "live": int(row["n"]) > 0,
        "recent_errors": int(row["n"]),
        "window_seconds": seconds,
        "last_error_at": (last_ever.isoformat() if last_ever else None),
        "quiet_seconds": (
            round((datetime.now(timezone.utc) - last_ever).total_seconds())
            if last_ever else None
        ),
    }


def run_test_gate(conn, incident_id: str, patch: dict, snap: dict) -> dict:
    """THE TEST GATE.

    1. Write ONLY the test against the still-broken code. It must FAIL.
    2. Apply the patch. The test must now PASS.

    Step 1 is the one that matters. A test that passes before the fix is not
    evidence of anything, and the patch is rejected on the spot.
    """
    incident = conn.execute(
        "SELECT plan, entry_point FROM ash_incidents WHERE incident_id = %s",
        (incident_id,),
    ).fetchone()
    plan = incident["plan"]
    source_path, test_path = plan["target_paths"][0], plan["target_paths"][1]
    target_service = _target_service(plan)

    # --- 1. test alone, against the broken code --------------------------
    executor.apply(test_path, patch["test_file_content"])
    pre = _run_pytest(test_path, target_service)

    incidents.audit(conn, incident_id, "test-gate", "decision",
                    f"Pre-patch run: test {'PASSED' if pre['passed'] else 'FAILED'} "
                    f"(it must FAIL here).",
                    {"phase": "pre_patch", "passed": pre["passed"],
                     "exit_code": pre["exit_code"], "output": pre["stdout"][-1200:]})

    if pre["passed"]:
        signal = _defect_still_reproducing(conn, incident, target_service)

        if not signal["live"]:
            quiet = signal["quiet_seconds"]
            return {"ok": False, "stage": "pre_patch", "stale": True,
                    "reason": ("The defect no longer reproduces. The test passes "
                               "against the current source and "
                               + (f"nothing has errored on {incident['entry_point']} "
                                  f"for {quiet}s" if quiet is not None
                                  else f"no errors have ever been recorded for "
                                       f"{incident['entry_point']}")
                               + ". The source was repaired out from under this "
                                 "incident; there is nothing left to patch."),
                    "signal": signal, "pre": pre}

        return {"ok": False, "stage": "pre_patch", "stale": False,
                "reason": (f"The test passed BEFORE the patch was applied, so it does "
                           f"not demonstrate the defect -- and "
                           f"{incident['entry_point']} is still failing "
                           f"({signal['recent_errors']} errors in the last "
                           f"{signal['window_seconds']}s). Patch rejected."),
                "signal": signal, "pre": pre}

    # --- 2. apply the patch, test must now pass --------------------------
    # Compute the diff BEFORE writing. The patch is the most concrete artifact
    # the system produces and it was previously never surfaced anywhere.
    import difflib
    before = (executor.WORKSPACE / source_path).read_text(errors="replace")
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        patch["patched_file_content"].splitlines(keepends=True),
        fromfile=f"a/{source_path}", tofile=f"b/{source_path}", n=3,
    ))
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    incidents.audit(conn, incident_id, "product-repair-agent", "patch",
                    f"Diff produced for {source_path}: +{added} / -{removed} lines.",
                    {"path": source_path, "diff": diff[:12000],
                     "lines_added": added, "lines_removed": removed,
                     "test_path": test_path,
                     "test_content": patch["test_file_content"][:8000]})

    try:
        executor.apply(source_path, patch["patched_file_content"])
    except executor.ExecutionError as exc:
        return {"ok": False, "stage": "syntax", "reason": str(exc), "pre": pre}

    try:
        executor.wait_for_healthy(target_service)
    except executor.ExecutionError as exc:
        return {"ok": False, "stage": "deploy", "reason": str(exc), "pre": pre}

    post = _run_pytest(test_path, target_service)
    incidents.audit(conn, incident_id, "test-gate", "decision",
                    f"Post-patch run: test {'PASSED' if post['passed'] else 'FAILED'} "
                    f"(it must PASS here).",
                    {"phase": "post_patch", "passed": post["passed"],
                     "exit_code": post["exit_code"], "output": post["stdout"][-1200:]})

    if not post["passed"]:
        return {"ok": False, "stage": "post_patch",
                "reason": "The test still fails after the patch. The fix does not work.",
                "pre": pre, "post": post}

    return {"ok": True, "pre": pre, "post": post,
            "summary": "Test failed pre-patch and passes post-patch."}


# --------------------------------------------------------------- validation

# Response assertions, by name. services.yaml selects which ones apply to a
# given probe; it never expresses them. Keeping the predicates in code means
# there is no expression language to sandbox and no eval anywhere near a
# response body -- the catalog picks from this list or a new function is added
# here in a reviewable change.
VALIDATION_CHECKS = {
    "total_is_positive":
        lambda body: float(body.get("total_kgco2e") or 0) > 0,
    "breakdown_percentages_sum_to_100":
        lambda body: abs(sum(e.get("percent", 0) for e in body.get("breakdown") or []) - 100) < 2,
    "breakdown_is_empty":
        lambda body: body.get("breakdown") == [],
    "body_is_non_empty":
        lambda body: bool(body),
    # A null factor is the Scenario 2 defect: HTTP 200, schema-shaped, and
    # unusable. Presence of the key is not the assertion -- numeric IS.
    "factors_all_numeric":
        lambda body: bool(body.get("factors")) and all(
            isinstance(f.get("factor"), (int, float)) for f in body["factors"]),
}


def _json_body(response) -> dict:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def _json_subset(expected: dict | None, actual: dict) -> bool | None:
    """Top-level subset equality. None when nothing was declared to check, so
    'not declared' stays distinguishable from 'declared and failed'."""
    if not expected:
        return None
    return all(actual.get(k) == v for k, v in expected.items())


def validate(conn, incident_id: str, since: datetime) -> dict:
    """Replay the ORIGINAL failing request and confirm recovery.

    Unit tests passing is not the same as the user journey working. This
    replays the exact request that produced the incident and checks the live
    error rate has returned to baseline.
    """
    incident = conn.execute(
        "SELECT plan FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    plan = incident["plan"]
    criteria = plan["validation_criteria"]
    replay = criteria["replay_request"]
    service = _target_service(plan)

    checks: list[dict] = []

    if FORCE_VALIDATION_FAILURE:
        checks.append({
            "check": "forced_failure_injected",
            "passed": False,
            "detail": ("Demo control force_validation_failure is enabled. "
                       "Validation fails deliberately so the rollback path runs."),
        })
        for c in checks:
            incidents.audit(conn, incident_id, "validation-agent", "decision",
                            f"[{c['check']}] FAIL: {c['detail']}", c)
        return {"passed": False, "checks": checks}

    root = catalog.base_url(service)
    if not root:
        checks.append({"check": "service_reachable", "passed": False,
                       "detail": f"'{service}' declares no base_url in services.yaml"})
        for c in checks:
            incidents.audit(conn, incident_id, "validation-agent", "decision",
                            f"[{c['check']}] FAIL: {c['detail']}", c)
        return {"passed": False, "checks": checks}

    # --- replay the failing journey --------------------------------------
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.request(replay.get("method", "POST"), root + replay["path"],
                          json=replay.get("body"))
        body = _json_body(r)
        status_ok = r.status_code == replay["expect_status"]
        shape_ok = _json_subset(replay.get("expect_json"), body)
        checks.append({
            "check": "replay_original_failing_request",
            "passed": status_ok and shape_ok is not False,
            "detail": f"HTTP {r.status_code} (expected {replay['expect_status']}), "
                      f"declared shape {'matched' if shape_ok else 'not matched' if shape_ok is False else 'not declared'}",
        })
    except httpx.RequestError as exc:
        checks.append({"check": "replay_original_failing_request", "passed": False,
                       "detail": f"request failed: {type(exc).__name__}: {exc}"})

    # --- the request that must KEEP working ------------------------------
    # Guards against a "fix" that makes the failing case pass by breaking
    # everything else. Which request that is, and what must hold of its
    # response, is declared per service in services.yaml.
    probe = catalog.validation_spec(service).get("regression_probe")
    if probe:
        try:
            with httpx.Client(timeout=20.0) as c:
                r = c.request(probe.get("method", "POST"), root + probe["path"],
                              json=probe.get("body"))
            body = _json_body(r)
            failures = [name for name in probe.get("checks") or []
                        if not VALIDATION_CHECKS[name](body)]
            ok = (r.status_code == probe.get("expect_status", 200)
                  and _json_subset(probe.get("expect_json"), body) is not False
                  and not failures)
            checks.append({
                "check": "no_regression_on_normal_input", "passed": ok,
                "detail": f"HTTP {r.status_code} on {probe['path']}"
                          + (f", failed: {', '.join(failures)}" if failures else ", declared checks passed"),
            })
        except httpx.RequestError as exc:
            checks.append({"check": "no_regression_on_normal_input", "passed": False,
                           "detail": f"request failed: {type(exc).__name__}"})
        except KeyError as exc:
            checks.append({"check": "no_regression_on_normal_input", "passed": False,
                           "detail": f"unknown check {exc} -- not in repair.VALIDATION_CHECKS"})

    # --- error rate back to baseline, MEASURED FROM THE DEPLOY -----------
    #
    # The window must start when the patch went live, not N seconds before now.
    # A trailing window still contains the errors that OPENED the incident, so
    # it measures the disease rather than the cure -- and rolls back a working
    # repair. `since` is the deploy timestamp.
    #
    # Drive fresh traffic first: judging a rate on an empty sample would let
    # 0/0 pass trivially, which is not evidence of anything either.
    try:
        with httpx.Client(timeout=20.0) as c:
            for _ in range(6):
                c.request(replay.get("method", "POST"), root + replay["path"],
                          json=replay.get("body"))
    except httpx.RequestError:
        pass
    time.sleep(6)  # let the collector flush those spans

    row = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE status_code = 'ERROR') AS errors, COUNT(*) AS total
        FROM tel_spans
        WHERE service_name = %s AND kind = 'SERVER' AND start_time >= %s
        """,
        (service, since),
    ).fetchone()
    errors, total_spans = int(row["errors"]), int(row["total"])
    rate = (errors / total_spans) if total_spans else 1.0
    enough = total_spans >= 3
    checks.append({
        "check": "error_rate_returned_to_baseline",
        "passed": enough and rate <= float(criteria.get("error_rate_must_return_to", 0.0)),
        "detail": (
            f"{errors}/{total_spans} error spans on {service} since the patch "
            f"deployed ({since.isoformat(timespec='seconds')}), rate {rate:.2%}"
            + ("" if enough else " -- INSUFFICIENT SAMPLE, treated as failure")
        ),
    })

    passed = all(c["passed"] for c in checks)
    for c in checks:
        incidents.audit(conn, incident_id, "validation-agent", "decision",
                        f"[{c['check']}] {'PASS' if c['passed'] else 'FAIL'}: {c['detail']}",
                        c)
    return {"passed": passed, "checks": checks}


# ------------------------------------------------------------- the repair loop

def execute(conn, incident_id: str) -> dict:
    """POLICY_APPROVED -> RESOLVED, or roll back and escalate."""
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    if incident["state"] != "POLICY_APPROVED":
        return {"skipped": True, "state": incident["state"]}

    plan = incident["plan"]
    agent = plan.get("repair_agent") or "product-repair-agent"
    started = datetime.now(timezone.utc)

    incidents.transition(conn, incident_id, "EXECUTING", agent,
                         f"Applying {plan['action_type']} to {plan['target_paths'][0]}.")

    # Snapshot BEFORE anything is written. Every mutation has a rollback path.
    snap = executor.snapshot(incident_id, plan["target_paths"])
    incidents.audit(conn, incident_id, "product-repair-agent", "tool_call",
                    f"Snapshot taken for {len(snap['files'])} path(s) before mutation.",
                    {"tool": "snapshot", "outcome": "ok", "files": list(snap["files"])})

    patch = generate_patch(conn, incident_id)
    if patch is None:
        incidents.transition(conn, incident_id, "ESCALATED", agent,
                             "Patch generation unavailable; nothing was modified.")
        _raise_escalation(conn, incident_id,
                          "Patch generation unavailable (model unreachable).")
        return {"outcome": "ESCALATED", "reason": "patch_generation_unavailable"}

    gate = run_test_gate(conn, incident_id, patch, snap)

    if not gate["ok"]:
        rolled = _roll_back(snap, _target_service(plan))
        incidents.audit(conn, incident_id, "product-repair-agent", "tool_call",
                        f"Rolled back after test gate failure at stage '{gate['stage']}'.",
                        {"tool": "restore_snapshot", "outcome": "ok", **rolled})

        # A stale incident is not a failed repair. The gate rejected a patch for
        # a defect that had already stopped reproducing, which is the system
        # working -- paging a human for it would be a false alarm, and setting
        # resolved_at stops the same pre-repair spans reopening the incident on
        # the next sweep.
        if gate.get("stale"):
            conn.execute(
                "UPDATE ash_incidents SET resolved_at = now(), outcome = 'stale' "
                "WHERE incident_id = %s", (incident_id,)
            )
            incidents.audit(conn, incident_id, "test-gate", "decision",
                            "Defect no longer reproduces; closing as stale without "
                            "a patch. No human action required.",
                            {"stage": gate["stage"], **gate.get("signal", {})})
            incidents.transition(conn, incident_id, "RESOLVED", "test-gate",
                                 gate["reason"],
                                 {"outcome": "stale", "patched": False,
                                  **gate.get("signal", {})})
            return {"outcome": "RESOLVED", "stale": True, "reason": gate["reason"],
                    "rolled_back": rolled}

        incidents.transition(conn, incident_id, "ROLLBACK", "test-gate",
                             f"Test gate failed at '{gate['stage']}': {gate['reason']}",
                             {"stage": gate["stage"], "reason": gate["reason"]})
        incidents.transition(conn, incident_id, "ESCALATED", "test-gate",
                             "Rolled back to the known-good state and escalated to a human.")
        _raise_escalation(conn, incident_id,
                          f"Test gate failed at '{gate['stage']}': {gate['reason']}")
        return {"outcome": "ESCALATED", "reason": gate["reason"], "stage": gate["stage"],
                "rolled_back": rolled}

    # The instant the patched code went live. Everything after this is the
    # repaired system; everything before it is the incident.
    deployed_at = datetime.now(timezone.utc)
    incidents.transition(conn, incident_id, "VALIDATING", "validation-agent",
                         f"Patch applied and deployed at "
                         f"{deployed_at.isoformat(timespec='seconds')}. "
                         f"Replaying the original failing request.")

    result = validate(conn, incident_id, deployed_at)

    if result["passed"]:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        conn.execute(
            "UPDATE ash_incidents SET resolved_at = now(), outcome = 'repaired' "
            "WHERE incident_id = %s", (incident_id,)
        )
        conn.execute(
            "INSERT INTO tel_deployments (service_name, version, ref, deployed_by, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (_target_service(plan), "1.0.1", incident_id, agent,
             f"autonomous repair: {patch.get('change_summary', '')[:200]}"),
        )
        ttr = (datetime.now(timezone.utc) - incident["first_seen"]).total_seconds()
        for metric, value in (("ttr_seconds", ttr), ("execution_seconds", elapsed)):
            conn.execute(
                "INSERT INTO ash_metrics (incident_id, metric, value) VALUES (%s, %s, %s)",
                (incident_id, metric, value),
            )
        incidents.transition(conn, incident_id, "RESOLVED", "validation-agent",
                             f"All validation checks passed. User journey healthy. "
                             f"Time to repair {ttr:.0f}s.",
                             {"checks": result["checks"], "ttr_seconds": round(ttr, 1)})
        return {"outcome": "RESOLVED", "validation": result, "ttr_seconds": round(ttr, 1),
                "change_summary": patch.get("change_summary")}

    # Validation failed -- restore the known-good state.
    rolled = _roll_back(snap, _target_service(plan))
    incidents.audit(conn, incident_id, "validation-agent", "tool_call",
                    "Rolled back after validation failure.",
                    {"tool": "restore_snapshot", "outcome": "ok", **rolled})
    incidents.transition(conn, incident_id, "ROLLBACK", "validation-agent",
                         "Validation failed; restored the known-good state.",
                         {"checks": result["checks"], **rolled})
    incidents.transition(conn, incident_id, "ESCALATED", "validation-agent",
                         "Repair reverted and escalated to a human.")
    _raise_escalation(conn, incident_id,
                      "Validation failed after the patch; the change was reverted.")
    return {"outcome": "ESCALATED", "reason": "validation_failed", "validation": result,
            "rolled_back": rolled}
