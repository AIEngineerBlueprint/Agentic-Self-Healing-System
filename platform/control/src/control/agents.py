"""Diagnosis agent, router, and repair planner.

Division of labour, restated because it is the whole argument:

    DETERMINISTIC          |  MODEL
    ---------------------- | -------------------------------
    who owns the bug       |  why it happened, in prose
    which agent repairs it |  what the fix should be
    whether it may run     |  (nothing else)

The router and planner below are deterministic. The diagnosis narrative is the
only model call in Phase 2, and if it is unavailable the episode continues with
a narrative assembled from the attribution signals -- degraded in prose quality,
identical in every decision that follows.
"""

from __future__ import annotations

import logging


from . import catalog, incidents, llm, policy
from .incidents import J

log = logging.getLogger(__name__)

DIAGNOSIS_SYSTEM = """\
You are the diagnosis agent in an autonomous repair system.

You are given an evidence bundle and a COMPLETED deterministic attribution. The
fault domain has ALREADY been decided by trace walking and contract validation.

Your job is to explain, not to decide:
  - Write the root-cause narrative a senior engineer would write in a postmortem.
  - Propose the fix strategy.

Hard rules:
  - Do NOT dispute the fault domain. It was established from objective evidence.
  - Do NOT propose changes outside the fault domain's repository.
  - Do NOT propose broadening a try/except or swallowing the error. The fix must
    make the correct behaviour correct, not make the symptom invisible.
  - Prefer the smallest change that makes a legitimate input produce a
    legitimate result.
  - Be specific about the file and function. Vague guidance is not actionable.
"""

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "description": "What actually went wrong, 2-4 sentences."},
        "why_this_owner": {"type": "string", "description": "Why the fault domain is correct, citing the evidence."},
        "fix_strategy": {"type": "string", "description": "What the fix should do, specifically."},
        "target_file": {"type": "string", "description": "Repo-relative path most likely to need the change."},
        "target_function": {"type": "string"},
        "test_assertion": {"type": "string", "description": "What a regression test must assert about CORRECT behaviour."},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["root_cause", "why_this_owner", "fix_strategy", "target_file",
                 "target_function", "test_assertion", "risk"],
    # No `additionalProperties: false` -- that pairs with strict tool use, which
    # this Bedrock endpoint rejects. The required list still pins the shape.
}


# ------------------------------------------------------------ diagnosis agent

def _deterministic_narrative(incident: dict, diagnosis: dict) -> dict:
    """Narrative assembled from the attribution signals, no model involved.

    Used when the provider is unavailable. Every downstream decision is
    unchanged -- only the prose is poorer.
    """
    signals = {s["name"]: s for s in diagnosis.get("signals", [])}
    walk = signals.get("trace_walk", {})
    exception = (walk.get("evidence") or {}).get("exception") or {}
    exonerated = diagnosis.get("exonerated", [])

    return {
        "root_cause": (
            f"{exception.get('type', 'An error')} raised in "
            f"{(walk.get('evidence') or {}).get('service', 'the failing service')} "
            f"during {(walk.get('evidence') or {}).get('operation', 'the request')}. "
            f"Message: {exception.get('message', 'not captured')}"
        ),
        "why_this_owner": (
            "Deterministic attribution: " + "; ".join(
                [s["verdict"] for s in diagnosis.get("signals", []) if s.get("weight", 0) > 0]
            )
            + (
                ". Exonerated on contract validation: "
                + ", ".join(e["repository"] for e in exonerated)
                if exonerated else ""
            )
        ),
        "fix_strategy": (
            "Handle the legitimate downstream response correctly rather than "
            "treating it as an error condition."
        ),
        # No-model fallback. Resolve the fault domain's declared patch target
        # rather than naming one product's file, so the degraded path degrades
        # for every service instead of only working for Carbon Ledger.
        "target_file": catalog.source_layout(
            catalog.service_for_repository(incident.get("fault_domain") or "") or ""
        ).get("patch_target", ""),
        "target_function": (walk.get("evidence") or {}).get("failure_origin", "").split(".")[-1]
                           or "",
        "test_assertion": (
            "The failing input must produce a correct, non-error result."
        ),
        "risk": "low",
        "_meta": {"provider": "none", "degraded": True,
                  "note": "LLM unavailable; narrative assembled from deterministic signals."},
    }


def diagnose(conn, incident_id: str) -> dict:
    """Add the root-cause narrative. The ONLY model call in Phase 2."""
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    diagnosis = incident["diagnosis"] or {}
    evidence = incident["evidence"] or {}

    prompt = _render_evidence(incident, evidence, diagnosis)
    # Always attempt the call. Gating it on llm.available() locked diagnosis out
    # for good after one transient failure: that failure is sticky until a call
    # succeeds, and no call was ever made. complete_json degrades on its own.
    narrative = llm.complete_json(DIAGNOSIS_SYSTEM, prompt, DIAGNOSIS_SCHEMA)

    if narrative is None:
        _, detail = llm.available()
        narrative = _deterministic_narrative(incident, diagnosis)
        incidents.audit(conn, incident_id, "diagnosis-agent", "decision",
                        f"Model unavailable ({detail}); narrative assembled from "
                        f"deterministic signals. Attribution and policy are unaffected.",
                        {"degraded": True, "provider_detail": detail})
    else:
        incidents.audit(conn, incident_id, "diagnosis-agent", "llm_call",
                        "Root-cause narrative generated.",
                        {"model": narrative.get("_meta", {}).get("model"),
                         "latency_ms": narrative.get("_meta", {}).get("latency_ms"),
                         "input_tokens": narrative.get("_meta", {}).get("input_tokens"),
                         "output_tokens": narrative.get("_meta", {}).get("output_tokens"),
                         "root_cause": narrative.get("root_cause"),
                         "fix_strategy": narrative.get("fix_strategy")})

    merged = {**diagnosis, "narrative": narrative}
    conn.execute(
        "UPDATE ash_incidents SET diagnosis = %s, updated_at = now() WHERE incident_id = %s",
        (J(merged), incident_id),
    )
    return narrative


def _render_evidence(incident: dict, evidence: dict, diagnosis: dict) -> str:
    """Bounded prompt. An evidence bundle that dumps everything is a haystack."""
    lines = [
        f"INCIDENT {incident['incident_id']}",
        f"Entry point: {incident['entry_point']}",
        f"Occurrences: {incident['occurrence_count']}",
        "",
        "=== DETERMINISTIC ATTRIBUTION (already decided, do not dispute) ===",
        f"Fault domain : {diagnosis.get('fault_domain')}",
        f"Surfaced in  : {diagnosis.get('surfaced_in')}",
        f"Confidence   : {diagnosis.get('confidence')}",
        f"Basis        : {diagnosis.get('attribution_basis')}",
        "",
        "Signals:",
    ]
    for s in diagnosis.get("signals", []):
        lines.append(f"  - [{s['name']}] {s['verdict']}")

    if diagnosis.get("exonerated"):
        lines += ["", "Exonerated on objective contract validation:"]
        for e in diagnosis["exonerated"]:
            lines.append(f"  - {e['repository']}: {e['reason']}")

    walk = next((s for s in diagnosis.get("signals", []) if s["name"] == "trace_walk"), None)
    if walk and (walk.get("evidence") or {}).get("exception"):
        exc = walk["evidence"]["exception"]
        lines += ["", "=== EXCEPTION ===",
                  f"{exc.get('type')}: {exc.get('message')}", "",
                  "Stack trace (truncated):", (exc.get("stack") or "")[:1500]]

    lines += ["", "=== SPAN TREE ==="]
    for span in evidence.get("spans", [])[:20]:
        if span.get("kind") == "SERVER":
            lines.append(
                f"  {span['status']:<6} {span['service']:<12} "
                f"repo={span['repository']:<13} {span['operation']}"
            )

    lines += ["", "=== LOGS ==="]
    for entry in evidence.get("logs", [])[:12]:
        lines.append(f"  [{entry['severity']:<5}] {entry['service']}: {str(entry['message'])[:180]}")

    lines += ["", "=== OWNERSHIP ==="]
    for name, own in (evidence.get("ownership") or {}).items():
        lines.append(f"  {name}: repo={own['repository']} owner={own['owner'].get('name')} "
                     f"autonomy={own['autonomy_level']}")

    lines += ["", "Explain the root cause and propose the fix. The fault domain is settled."]
    return "\n".join(lines)


# -------------------------------------------------------------------- router

def route(conn, incident_id: str) -> dict:
    """Map fault domain -> repair agent. Deterministic table lookup.

    Ownership boundaries are hard boundaries: the Product agent never patches
    capability code. The routing table lives in services.yaml, not in a prompt.
    """
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    fault_domain = incident["fault_domain"]
    agent = catalog.repair_agent_for(fault_domain or "")

    if agent is None:
        incidents.transition(conn, incident_id, "ESCALATED", "router",
                             f"No repair agent owns repository '{fault_domain}'.",
                             {"fault_domain": fault_domain})
        return {"routed": False, "reason": f"no agent handles {fault_domain}"}

    scope = catalog._catalog.get("repair_agents", {}).get(agent, {})
    incidents.transition(conn, incident_id, "ROUTED", "router",
                         f"Assigned to {agent} (owns {fault_domain}).",
                         {"repair_agent": agent, "fault_domain": fault_domain,
                          "write_paths": scope.get("write_paths")})
    return {"routed": True, "agent": agent, "fault_domain": fault_domain}


# ------------------------------------------------------------------- planner

def _replay_for(service: str) -> dict:
    """The request that reproduces this service's defect, from services.yaml.

    `expect` is a human-readable restatement for the repair prompt; `expect_json`
    is what validation actually asserts against. The prompt gets prose, the gate
    gets data -- never the other way round.
    """
    spec = dict(catalog.validation_spec(service).get("replay") or {})
    if not spec:
        return {}
    expect_json = spec.get("expect_json") or {}
    return {
        "method": spec.get("method", "POST"),
        "path": spec.get("path"),
        "body": spec.get("body"),
        "expect_status": spec.get("expect_status", 200),
        "expect_json": expect_json,
        "expect": ", ".join(f"{k} == {v!r}" for k, v in expect_json.items())
                  or f"HTTP {spec.get('expect_status', 200)}",
    }


def plan(conn, incident_id: str) -> dict:
    """Produce a typed remediation plan. Deterministic assembly from the narrative.

    The plan carries everything policy needs to make a decision: exact targets,
    action type, risk, and the validation criteria that decide success.
    """
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    diagnosis = incident["diagnosis"] or {}
    narrative = diagnosis.get("narrative") or {}
    fault_domain = incident["fault_domain"]
    agent = catalog.repair_agent_for(fault_domain or "")

    target_service = catalog.service_for_repository(fault_domain or "") or ""
    layout = catalog.source_layout(target_service)

    target_file = narrative.get("target_file") or ""
    # A model-proposed path is untrusted input. Keep it inside the fault
    # domain's repository whatever the model said, falling back to the patch
    # target that service declares rather than to a Carbon-Ledger path.
    if not target_file.startswith(f"{fault_domain}/"):
        target_file = layout.get("patch_target") or ""

    tests_dir = layout.get("tests_dir")
    test_file = f"{tests_dir}/test_incident_{incident_id.rsplit('-', 1)[-1]}.py" if tests_dir else ""

    built = {
        "incident_id": incident_id,
        "fingerprint": incident["fingerprint"],
        "repair_agent": agent,
        "fault_domain": fault_domain,
        "target_service": target_service,
        "action_type": "code_patch",
        "risk": narrative.get("risk", "low"),
        "target_paths": [target_file, test_file],
        "summary": narrative.get("fix_strategy", ""),
        "root_cause": narrative.get("root_cause", ""),
        "test_spec": {
            "file": test_file,
            "assertion": narrative.get("test_assertion", ""),
            # A fix without a failing test is not a fix.
            "must_fail_pre_patch": True,
            "must_pass_post_patch": True,
        },
        # How this repair will be proved, taken from the target service's own
        # catalog entry. Copied into the plan so the audit trail records the
        # criteria that were in force at the time, not whatever the catalog
        # says later.
        "validation_criteria": {
            "replay_request": _replay_for(target_service),
            "error_rate_must_return_to": 0.0,
            "observation_window_seconds": 30,
        },
        "rollback": {"strategy": "restore_snapshot", "target": target_file},
    }

    conn.execute(
        "UPDATE ash_incidents SET plan = %s, updated_at = now() WHERE incident_id = %s",
        (J(built), incident_id),
    )
    incidents.transition(conn, incident_id, "PLAN_READY", agent or "planner",
                         f"{built['action_type']} targeting {target_file} "
                         f"(risk {built['risk']}), with a declared test and "
                         f"explicit validation criteria.",
                         built)
    return built


# ------------------------------------------------------------- policy check

def check_policy(conn, incident_id: str) -> dict:
    """Run the policy engine against the plan and transition accordingly."""
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    built = incident["plan"] or {}
    confidence = float(incident["confidence"] or 0)
    fault_domain = incident["fault_domain"] or ""

    decision = policy.evaluate(conn, built, confidence, fault_domain)
    payload = decision.to_dict()

    for gate in decision.gates:
        incidents.audit(conn, incident_id, "policy-engine", "decision",
                        f"[{gate.name}] {'PASS' if gate.passed else 'FAIL'}: {gate.detail}",
                        {"gate": gate.name, "passed": gate.passed})

    if decision.allowed:
        incidents.transition(conn, incident_id, "POLICY_APPROVED", "policy-engine",
                             decision.reason, payload)
    else:
        incidents.transition(conn, incident_id, "ESCALATED", "policy-engine",
                             f"[{decision.outcome}] {decision.reason}", payload)

    return payload


# ------------------------------------------------------------------ pipeline

def run_to_policy(conn, incident_id: str) -> dict:
    """Drive DIAGNOSIS_READY -> POLICY_APPROVED (or ESCALATED)."""
    narrative = diagnose(conn, incident_id)

    routing = route(conn, incident_id)
    if not routing.get("routed"):
        return {"stopped_at": "route", "routing": routing}

    built = plan(conn, incident_id)
    decision = check_policy(conn, incident_id)
    return {"narrative": narrative, "routing": routing, "plan": built, "policy": decision}
