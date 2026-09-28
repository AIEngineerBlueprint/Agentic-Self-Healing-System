"""Incident detection, fingerprinting, evidence, and the task state machine.

A repair task is a persisted state machine, not conversation history. Every
transition is written to an append-only audit log with the evidence that
justified it, which is what makes the whole episode reconstructable from an
incident ID rather than from a chat transcript.

    NEW -> TRIAGED -> EVIDENCE_READY -> DIAGNOSIS_READY -> ROUTED
        -> PLAN_READY -> POLICY_APPROVED -> EXECUTING -> VALIDATING -> RESOLVED

    VALIDATING      -> ROLLBACK -> ESCALATED
    DIAGNOSIS_READY -> ESCALATED   (confidence below floor)
    PLAN_READY      -> ESCALATED   (policy blocks autonomous execution)
    EXECUTING       -> RESOLVED    (outcome 'stale': the defect stopped
                                    reproducing before the patch landed, so
                                    there was nothing to repair and nobody to
                                    page -- see repair.run_test_gate)

Phase 1 implements NEW through DIAGNOSIS_READY (and the escalation edge).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from functools import partial
from json import dumps as _json_dumps

from psycopg.types.json import Json

from . import attribution, catalog

log = logging.getLogger(__name__)

def J(obj):
    """JSONB wrapper that never crashes the pipeline on an exotic type.

    A datetime that reached a JSONB column once stalled an incident permanently
    and silently. Stringifying is always better than losing the episode: the
    audit trail is the product, and a trail that cannot be written is worse than
    one that is slightly lossy.
    """
    return Json(obj, dumps=partial(_json_dumps, default=str))


STATES = [
    "NEW", "TRIAGED", "EVIDENCE_READY", "DIAGNOSIS_READY", "ROUTED",
    "PLAN_READY", "POLICY_APPROVED", "EXECUTING", "VALIDATING",
    "RESOLVED", "ROLLBACK", "ESCALATED",
]

# Strip volatile substrings so the same defect fingerprints identically across
# occurrences. Without this, every request would look like a new incident and
# dedupe would never fire.
_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+Z?\b"), "<ts>"),
    (re.compile(r"\b\d+\.\d+\b"), "<float>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def normalise(text: str) -> str:
    out = text or ""
    for pattern, replacement in _VOLATILE:
        out = pattern.sub(replacement, out)
    return out.strip()


def fingerprint(service: str, operation: str, exc_type: str, exc_message: str) -> str:
    """Stable identity for a defect. One open incident per fingerprint."""
    raw = "|".join([service, normalise(operation), exc_type, normalise(exc_message)])
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def new_incident_id() -> str:
    return f"inc-{datetime.now(timezone.utc):%Y-%m-%d}-{secrets.token_hex(3)}"


# ------------------------------------------------------------------- audit

def audit(conn, incident_id: str, actor: str, kind: str, summary: str,
          detail: dict | None = None, evidence_ref: list | None = None) -> None:
    """Append to the audit trail. Append-only, sequenced, never rewritten."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM ash_audit WHERE incident_id = %s",
        (incident_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO ash_audit (incident_id, seq, actor, kind, summary, detail, evidence_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (incident_id, row["seq"], actor, kind, summary,
         J(detail or {}), J(evidence_ref or [])),
    )


def transition(conn, incident_id: str, to_state: str, actor: str,
               summary: str, detail: dict | None = None) -> None:
    if to_state not in STATES:
        raise ValueError(f"unknown state {to_state}")
    conn.execute(
        "UPDATE ash_incidents SET state = %s, updated_at = now() WHERE incident_id = %s",
        (to_state, incident_id),
    )
    audit(conn, incident_id, actor, "state_transition",
          f"-> {to_state}: {summary}", detail)

    # EVERY escalation raises a page. This used to be the caller's job, and
    # three paths forgot: the router with no owning agent, the policy engine
    # refusing a plan, and attribution below the review floor. Those are
    # precisely the "the system declined to act" cases -- the ones most worth
    # telling a human about -- and they reached ESCALATED with an empty
    # escalation queue behind them.
    #
    # Making it a property of the transition means a future ESCALATED path
    # cannot forget. The insert is idempotent (ON CONFLICT on incident_id), so
    # callers that already raise explicitly do not double-page.
    if to_state == "ESCALATED":
        from . import gateway  # local: gateway imports this module
        try:
            gateway.invoke(conn, incident_id, actor, "open_escalation",
                           reason=summary)
        except Exception:  # noqa: BLE001 - a failed page must not lose the state change
            log.exception("could not raise escalation for %s", incident_id)


# ---------------------------------------------------------------- detection

def detect(conn) -> list[str]:
    """One detection sweep. Returns incident ids created.

    An incident is a deduplicated, fingerprinted CLUSTER of related error
    signals -- not a single log line. One error is noise; a sustained rate is
    an incident.
    """
    cfg = catalog.policy().get("detection", {})
    window = int(cfg.get("window_minutes", 5))
    min_occurrences = int(cfg.get("min_occurrences", 3))

    if catalog.policy().get("kill_switch"):
        log.warning("kill switch engaged -- detection suppressed (L0 observe)")
        return []

    since = datetime.now(timezone.utc) - timedelta(minutes=window)
    rows = conn.execute(
        """
        SELECT span_id, trace_id, name, service_name, code_repository,
               status_message, start_time, attributes, events
        FROM tel_spans
        WHERE status_code = 'ERROR' AND start_time >= %s AND kind = 'SERVER'
        ORDER BY start_time
        """,
        (since,),
    ).fetchall()

    # A fingerprint that has already been repaired must not be re-detected from
    # error spans that predate the repair. A post-RESOLVED cool-off can only
    # approximate this, and it races the detection window: the moment the
    # cool-off expires, a backlog of trailing pre-fix spans still inside the
    # window opens a fresh incident against already-correct source. The agent
    # then patches a file that needs no patch, and the test gate (correctly)
    # rejects it because the pre-patch run passes -- an escalation that reads
    # as "patch rejected" when nothing was actually broken.
    #
    # resolved_at is written once, when validation passed, and never rewritten,
    # so it is an exact boundary: spans before it are evidence of a defect that
    # no longer exists. Anything after it is a genuine regression and still
    # detects normally.
    repaired_at = {
        r["fingerprint"]: r["repaired_at"]
        for r in conn.execute(
            """
            SELECT fingerprint, MAX(resolved_at) AS repaired_at
            FROM ash_incidents
            WHERE state = 'RESOLVED' AND resolved_at IS NOT NULL
            GROUP BY fingerprint
            """
        ).fetchall()
    }

    clusters: dict[str, list[dict]] = {}
    for span in rows:
        exc_type, exc_message = "UnknownError", span.get("status_message") or ""
        for event in span.get("events") or []:
            attrs = event.get("attributes", {})
            if attrs.get("exception.type"):
                exc_type = attrs["exception.type"]
                exc_message = str(attrs.get("exception.message", ""))
                break
        fp = fingerprint(span["service_name"], span["name"], exc_type, exc_message)
        clusters.setdefault(fp, []).append(dict(span, _exc_type=exc_type, _exc_message=exc_message))

    created: list[str] = []

    for fp, spans in clusters.items():
        cutoff = repaired_at.get(fp)
        if cutoff:
            fresh = [s for s in spans if s["start_time"] > cutoff]
            if len(fresh) < len(spans):
                log.info("%s: discarded %d/%d error spans emitted before the "
                         "repair at %s", fp[:20], len(spans) - len(fresh),
                         len(spans), cutoff.isoformat(timespec="seconds"))
            spans = fresh

        if len(spans) < min_occurrences:
            continue

        existing = conn.execute(
            """
            SELECT incident_id FROM ash_incidents
            WHERE fingerprint = %s
              AND (
                    state NOT IN ('RESOLVED', 'ESCALATED')
                    -- COOL-OFF. Once an incident escalates, the same failing
                    -- traffic would otherwise open a fresh one on the very next
                    -- sweep, cascading until the loop breaker trips and burying
                    -- the real escalation in noise. A human has been paged; give
                    -- them a window before re-opening.
                    -- ...but only while the page is still outstanding. The
                    -- window exists to protect a human who has just been
                    -- paged; once they acknowledge, it has done its job and
                    -- suppressing detection past that point just hides live
                    -- failures from the board.
                    OR (state = 'ESCALATED'
                        AND updated_at > now() - interval '10 minutes'
                        AND NOT EXISTS (
                            SELECT 1 FROM ash_escalations e
                            WHERE e.incident_id = ash_incidents.incident_id
                              AND e.acknowledged
                        ))
                    -- No equivalent cool-off after a SUCCESSFUL repair. The
                    -- resolved_at cutoff above has already discarded every span
                    -- that predates the fix, so anything still standing here is
                    -- a genuine post-repair regression and deserves its own
                    -- incident immediately. A time cool-off would swallow it
                    -- into the resolved episode's counter instead. Runaway
                    -- re-repair is bounded by the policy loop breaker
                    -- (max_verification_attempts_per_fingerprint), not by delay.
              )
            """,
            (fp,),
        ).fetchone()

        if existing:
            # Fingerprint dedupe: bump the counter, do not open a second repair.
            conn.execute(
                """
                UPDATE ash_incidents
                SET occurrence_count = %s, last_seen = %s, updated_at = now()
                WHERE incident_id = %s
                """,
                (len(spans), spans[-1]["start_time"], existing["incident_id"]),
            )
            continue

        first, last = spans[0], spans[-1]
        incident_id = new_incident_id()

        conn.execute(
            """
            INSERT INTO ash_incidents (
                incident_id, fingerprint, state, severity,
                first_seen, last_seen, occurrence_count,
                entry_point, surfaced_in
            ) VALUES (%s, %s, 'NEW', %s, %s, %s, %s, %s, %s)
            """,
            (
                incident_id, fp,
                "S2" if len(spans) >= 10 else "S3",
                first["start_time"], last["start_time"], len(spans),
                first["name"],
                first["code_repository"] or catalog.repository_for(first["service_name"]),
            ),
        )

        audit(conn, incident_id, "detector", "decision",
              f"Incident opened from {len(spans)} correlated error spans.",
              {
                  "fingerprint": fp,
                  "service": first["service_name"],
                  "operation": first["name"],
                  "exception_type": first["_exc_type"],
                  "exception_message": first["_exc_message"][:300],
                  "occurrences": len(spans),
                  "window_minutes": window,
              },
              [{"type": "trace", "trace_id": s["trace_id"]} for s in spans[:5]])

        transition(conn, incident_id, "TRIAGED", "detector",
                   f"{len(spans)} occurrences in {window}m exceeds threshold of {min_occurrences}.",
                   {"exemplar_traces": [s["trace_id"] for s in spans[:5]]})

        created.append(incident_id)
        log.info("incident %s opened (%s, %d occurrences)", incident_id, fp[:20], len(spans))

    return created


# ------------------------------------------------------------ evidence build

def build_evidence(conn, incident_id: str) -> dict:
    """Collect ONLY relevant telemetry into a bounded Diagnosis Context.

    Bounded matters: an evidence bundle that dumps every log line in the window
    is not evidence, it is a haystack. Everything here is scoped to the
    exemplar trace and the incident window.
    """
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    if not incident:
        raise KeyError(incident_id)

    exemplar = conn.execute(
        """
        SELECT detail, evidence_ref FROM ash_audit
        WHERE incident_id = %s AND kind = 'decision'
        ORDER BY seq LIMIT 1
        """,
        (incident_id,),
    ).fetchone()

    trace_ids = [r["trace_id"] for r in (exemplar["evidence_ref"] if exemplar else [])]
    trace_id = trace_ids[0] if trace_ids else None

    spans = conn.execute(
        "SELECT * FROM tel_spans WHERE trace_id = %s ORDER BY start_time", (trace_id,)
    ).fetchall() if trace_id else []

    logs = conn.execute(
        "SELECT service_name, severity, body, attributes, observed_at "
        "FROM tel_logs WHERE trace_id = %s ORDER BY observed_at",
        (trace_id,),
    ).fetchall() if trace_id else []

    involved = sorted({s["service_name"] for s in spans})
    dependency_chain = {
        name: catalog.service(name).get("depends_on") or [] for name in involved
    }

    deployments = conn.execute(
        """
        SELECT service_name, version, ref, deployed_at
        FROM tel_deployments WHERE deployed_at >= %s ORDER BY deployed_at DESC LIMIT 20
        """,
        (incident["first_seen"] - timedelta(hours=2),),
    ).fetchall()

    evidence = {
        "incident_id": incident_id,
        "fingerprint": incident["fingerprint"],
        "first_seen": incident["first_seen"].isoformat(),
        "occurrence_count": incident["occurrence_count"],
        "entry_point": incident["entry_point"],
        "exemplar_trace_id": trace_id,
        "trace_exemplars": trace_ids,
        "services_involved": involved,
        "dependency_chain": dependency_chain,
        "ownership": {
            name: {
                "repository": catalog.repository_for(name),
                "owner": catalog.owner_for(name),
                "autonomy_level": catalog.autonomy_level(name),
            }
            for name in involved
        },
        "spans": [
            {
                "span_id": s["span_id"],
                "parent_span_id": s["parent_span_id"],
                "service": s["service_name"],
                "repository": s["code_repository"],
                "operation": s["name"],
                "kind": s["kind"],
                "status": s["status_code"],
                "duration_ms": float(s["duration_ms"]),
                "attributes": s["attributes"],
                "events": s["events"],
            }
            for s in spans
        ],
        "logs": [
            {
                "service": r["service_name"],
                "severity": r["severity"],
                "message": r["body"],
                "at": r["observed_at"].isoformat(),
            }
            for r in logs
        ],
        "deployments": [
            {**dict(d), "deployed_at": d["deployed_at"].isoformat()} for d in deployments
        ],
    }

    conn.execute(
        "UPDATE ash_incidents SET evidence = %s, updated_at = now() WHERE incident_id = %s",
        (J(evidence), incident_id),
    )
    audit(conn, incident_id, "evidence-builder", "decision",
          f"Bounded evidence assembled: {len(spans)} spans, {len(logs)} logs, "
          f"{len(involved)} services.",
          {"services": involved, "trace_id": trace_id, "log_count": len(logs)})
    transition(conn, incident_id, "EVIDENCE_READY", "evidence-builder",
               "Diagnosis context bounded to the exemplar trace and incident window.")

    return evidence


# -------------------------------------------------------------- attribution

def run_attribution(conn, incident_id: str) -> dict:
    """Deterministic attribution. No LLM. Sets fault domain and confidence."""
    incident = conn.execute(
        "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
    ).fetchone()
    evidence = incident["evidence"] or {}

    trace_id = evidence.get("exemplar_trace_id")
    spans = conn.execute(
        "SELECT * FROM tel_spans WHERE trace_id = %s ORDER BY start_time", (trace_id,)
    ).fetchall() if trace_id else []

    result = attribution.attribute(conn, [dict(s) for s in spans], incident["first_seen"])
    payload = result.to_dict()

    conn.execute(
        """
        UPDATE ash_incidents
        SET fault_domain = %s, confidence = %s, attribution_basis = %s,
            diagnosis = %s, updated_at = now()
        WHERE incident_id = %s
        """,
        (result.fault_domain, result.confidence, J(result.basis),
         J(payload), incident_id),
    )

    for signal in result.signals:
        audit(conn, incident_id, "attribution-engine", "decision",
              f"[{signal.name}] {signal.verdict}",
              {"weight": signal.weight, "conclusive": signal.conclusive,
               "suspect_repository": signal.suspect_repository,
               "evidence": signal.evidence})

    for exoneration in result.exonerated:
        audit(conn, incident_id, "attribution-engine", "decision",
              f"EXONERATED {exoneration['repository']}: {exoneration['reason']}",
              exoneration)

    # Floors are resolved against the FAULT DOMAIN, so a service can demand more
    # certainty before anyone touches it than the product does.
    floors = catalog.confidence_floors(result.fault_domain)
    autonomous_floor, review_floor = floors["autonomous"], floors["review"]

    if result.confidence < review_floor:
        transition(conn, incident_id, "ESCALATED", "attribution-engine",
                   f"Confidence {result.confidence:.2f} is below the review floor "
                   f"of {review_floor:.2f}. Escalating with the evidence bundle "
                   f"rather than guessing.",
                   payload)
    else:
        agent = catalog.repair_agent_for(result.fault_domain or "")
        transition(conn, incident_id, "DIAGNOSIS_READY", "attribution-engine",
                   f"Fault domain {result.fault_domain} at confidence "
                   f"{result.confidence:.2f} "
                   f"({'autonomous' if result.confidence >= autonomous_floor else 'human review required'}).",
                   {**payload, "repair_agent": agent,
                    "requires_human_review": result.confidence < autonomous_floor})

    return payload


def triage_to_diagnosis(conn, incident_id: str) -> dict:
    """Drive one incident from TRIAGED through to DIAGNOSIS_READY."""
    build_evidence(conn, incident_id)
    return run_attribution(conn, incident_id)
