"""The policy engine.

DETERMINISTIC. Reads policy.yaml and answers one question: may this specific
plan execute autonomously, right now?

Every gate is a hard check in code. None of them consults a model, and none of
them can be argued with. The engine returns a decision plus the reason, because
"the system refused and can tell you exactly why" is the demo beat that matters
more than any successful repair.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import catalog

log = logging.getLogger(__name__)


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass
class Decision:
    allowed: bool
    outcome: str                      # APPROVED | REVIEW_REQUIRED | BLOCKED
    reason: str
    gates: list[Gate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "outcome": self.outcome,
            "reason": self.reason,
            "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail} for g in self.gates],
        }


def evaluate(conn, plan: dict, confidence: float, fault_domain: str) -> Decision:
    """Run every gate. Any hard failure blocks; order is deliberate.

    Kill switch and protected zone are checked FIRST, because they must hold
    regardless of how good the plan or how high the confidence.
    """
    gates: list[Gate] = []
    pol = catalog.policy()

    # --- 1. kill switch --------------------------------------------------
    if pol.get("kill_switch"):
        gates.append(Gate("kill_switch", False, "Kill switch engaged; system is at L0 observe."))
        return Decision(False, "BLOCKED", "Kill switch engaged -- no autonomous action.", gates)
    gates.append(Gate("kill_switch", True, "Disengaged."))

    # --- 2. protected zone -- always human, whatever the confidence -------
    targets = plan.get("target_paths") or []
    protected = [p for p in targets if catalog.is_protected(p)]
    if protected:
        zone = catalog.sensitive_zone_for(protected[0])
        reason = (zone["reason"].strip() if zone
                  else "declared in policy.protected_paths")
        gates.append(Gate("protected_zone", False,
                          f"{protected} in the protected zone. {reason}"))
        return Decision(
            False, "BLOCKED",
            f"Target {protected[0]} is in the protected zone. {reason}",
            gates,
        )
    gates.append(Gate("protected_zone", True, f"No protected paths among {targets}."))

    # --- 3. action type allowlist ----------------------------------------
    action = plan.get("action_type")
    allowed_actions = pol.get("allowed_actions") or []
    escalate_actions = pol.get("escalate_actions") or []
    if action in escalate_actions:
        gates.append(Gate("action_type", False,
                          f"'{action}' always routes to a human."))
        return Decision(False, "BLOCKED",
                        f"Action type '{action}' is never executed autonomously.", gates)
    if action not in allowed_actions:
        gates.append(Gate("action_type", False,
                          f"'{action}' is not on the allowlist {allowed_actions}."))
        return Decision(False, "BLOCKED",
                        f"Action type '{action}' is not an allowlisted action.", gates)
    gates.append(Gate("action_type", True, f"'{action}' is allowlisted."))

    # --- 4. write scope ---------------------------------------------------
    agent = catalog.repair_agent_for(fault_domain) or ""
    out_of_scope = [p for p in targets if not catalog.agent_may_write(agent, p)]
    if out_of_scope:
        gates.append(Gate("write_scope", False,
                          f"{agent} has no write scope for {out_of_scope}."))
        return Decision(False, "BLOCKED",
                        f"Agent '{agent}' may not write {out_of_scope}. "
                        f"Ownership boundaries are hard boundaries.", gates)
    gates.append(Gate("write_scope", True, f"All targets within {agent}'s write scope."))

    # --- 5. change budget / circuit breaker -------------------------------
    budgets = pol.get("budgets") or {}
    max_merges = int(budgets.get("max_autonomous_merges_per_service_per_day", 3))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM ash_incidents
        WHERE fault_domain = %s AND state = 'RESOLVED' AND resolved_at >= %s
          -- Merges only. An incident closed as stale shipped no code, so it
          -- must not consume the budget that exists to cap how much the system
          -- changes in a day.
          AND outcome = 'repaired'
        """,
        (fault_domain, since),
    ).fetchone()
    used = int(row["n"])
    if used >= max_merges:
        gates.append(Gate("change_budget", False,
                          f"{used}/{max_merges} autonomous merges for {fault_domain} in 24h."))
        return Decision(False, "BLOCKED",
                        f"Change budget exhausted for {fault_domain} "
                        f"({used}/{max_merges} in 24h). Circuit breaker tripped.", gates)
    gates.append(Gate("change_budget", True, f"{used}/{max_merges} merges used in 24h."))

    # --- 6. loop breaker --------------------------------------------------
    max_attempts = int(budgets.get("max_verification_attempts_per_fingerprint", 2))
    fingerprint = plan.get("fingerprint")
    if fingerprint:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM ash_incidents
            WHERE fingerprint = %s AND state IN ('ROLLBACK', 'ESCALATED')
            """,
            (fingerprint,),
        ).fetchone()
        attempts = int(row["n"])
        if attempts >= max_attempts:
            gates.append(Gate("loop_breaker", False,
                              f"{attempts} failed attempts on this fingerprint."))
            return Decision(False, "BLOCKED",
                            f"Loop breaker: {attempts} failed attempts on this "
                            f"fingerprint already. Escalating to a human.", gates)
        gates.append(Gate("loop_breaker", True, f"{attempts}/{max_attempts} prior attempts."))

    # --- 7. test gate declared -------------------------------------------
    tg = pol.get("test_gate") or {}
    if tg.get("require_failing_test_pre_patch") and not plan.get("test_spec"):
        gates.append(Gate("test_gate", False, "Plan declares no test. A fix without a failing test is not a fix."))
        return Decision(False, "BLOCKED",
                        "Plan carries no test specification. Policy requires a test "
                        "that fails before the patch and passes after.", gates)
    gates.append(Gate("test_gate", True, "Plan declares a test specification."))

    # --- 8. confidence ----------------------------------------------------
    floors = catalog.confidence_floors(fault_domain)
    autonomous_floor, review_floor = floors["autonomous"], floors["review"]

    if confidence < review_floor:
        gates.append(Gate("confidence", False,
                          f"{confidence:.2f} below review floor {review_floor:.2f}."))
        return Decision(False, "BLOCKED",
                        f"Confidence {confidence:.2f} is below the review floor. "
                        f"Escalating with the evidence bundle rather than guessing.", gates)

    if confidence < autonomous_floor:
        gates.append(Gate("confidence", True,
                          f"{confidence:.2f} between floors -- human review required."))
        return Decision(False, "REVIEW_REQUIRED",
                        f"Confidence {confidence:.2f} is below the autonomous floor "
                        f"{autonomous_floor:.2f}. Diagnosis and draft patch stand, "
                        f"but a human must approve.", gates)
    gates.append(Gate("confidence", True,
                      f"{confidence:.2f} clears the autonomous floor {autonomous_floor:.2f}."))

    # --- 9. autonomy level ------------------------------------------------
    service = plan.get("target_service") or ""
    level = catalog.autonomy_level(service)
    if level in ("L0", "L1"):
        gates.append(Gate("autonomy_level", False,
                          f"{service} is at {level}; execution requires L2 or above."))
        return Decision(False, "REVIEW_REQUIRED",
                        f"{service} runs at autonomy level {level}. Diagnosis stands; "
                        f"execution requires a human at this level.", gates)
    gates.append(Gate("autonomy_level", True, f"{service} runs at {level}."))

    return Decision(True, "APPROVED",
                    f"All {len(gates)} gates passed. Approved for autonomous execution "
                    f"at {level} with confidence {confidence:.2f}.", gates)
