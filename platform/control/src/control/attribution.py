"""The attribution engine.

100% DETERMINISTIC. No language model is involved in deciding who owns a bug.
This is the single most important design decision in ASHS: the model explains
and proposes how to fix, the deterministic layers decide who owns it.

Five signals, run in order, combined into a confidence score:

  1. Trace walk          -- deepest error span. Cheap, and usually right.
  2. Contract validation -- THE DECISIVE TEST. Validate the payload a capability
                            actually returned against the schema it actually
                            published.
                              invalid  -> fault is the capability
                              valid + consumer still failed -> fault is the consumer
  3. Deploy correlation  -- which services shipped before first occurrence.
  4. Blast radius        -- one consumer affected, or every consumer?
  5. (Phase 2) model narrative, last, and only to explain.

Routing is on confidence, never certainty. Below the floor we escalate rather
than guess -- a confidently wrong attribution creates cross-team friction, which
is the worst outcome politically and the fastest way to lose trust in the system.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jsonschema import Draft202012Validator

from . import catalog

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """One attribution signal, with the evidence that produced it."""

    name: str
    verdict: str                       # the conclusion, in plain language
    suspect_repository: str | None
    weight: float                      # contribution to confidence, 0..1
    conclusive: bool = False           # can this alone decide ownership?
    evidence: dict = field(default_factory=dict)


@dataclass
class Attribution:
    fault_domain: str | None           # the repository that must change
    surfaced_in: str | None            # where the error was VISIBLE
    confidence: float
    signals: list[Signal]
    exonerated: list[dict]             # capabilities ruled out, with the reason
    basis: list[str]

    def to_dict(self) -> dict:
        return {
            "fault_domain": self.fault_domain,
            "surfaced_in": self.surfaced_in,
            "confidence": round(self.confidence, 3),
            "attribution_basis": self.basis,
            "signals": [
                {
                    "name": s.name,
                    "verdict": s.verdict,
                    "suspect_repository": s.suspect_repository,
                    "weight": s.weight,
                    "conclusive": s.conclusive,
                    "evidence": s.evidence,
                }
                for s in self.signals
            ],
            "exonerated": self.exonerated,
        }


# --------------------------------------------------------------- 1. trace walk

# ASGI/framework plumbing spans. They inherit the error status of the request
# they belong to but carry no exception of their own, so treating one as "the
# deepest error span" yields a suspect with no diagnostic detail attached.
_PLUMBING_SUFFIXES = (" http send", " http receive", " http.response.start", " http.response.body")


def _is_plumbing(span: dict) -> bool:
    name = span.get("name") or ""
    return any(name.endswith(suffix) for suffix in _PLUMBING_SUFFIXES)


def _has_exception(span: dict) -> bool:
    return any(
        (event.get("attributes") or {}).get("exception.type")
        for event in span.get("events") or []
    )


def trace_walk(spans: list[dict]) -> Signal:
    """Find the deepest MEANINGFUL span with an error status.

    Depth comes from parent links rather than timestamps. Two refinements make
    the answer useful rather than merely correct:

      - Framework plumbing spans are excluded. An ASGI `http send` span inherits
        the request's error status but has no exception on it, so picking it
        gives a suspect with nothing to diagnose.
      - A span carrying an exception outranks a deeper one that does not. The
        deepest span is only interesting because it is usually where the fault
        is; a recorded exception is the stronger evidence of that.

    In Scenario 1 this lands on cfc-api's request span, because the capabilities
    returned 200 and only the product raised.
    """
    by_id = {s["span_id"]: s for s in spans}

    def depth(span: dict, seen: set[str] | None = None) -> int:
        seen = seen or set()
        parent = span.get("parent_span_id")
        if not parent or parent not in by_id or parent in seen:
            return 0
        seen.add(parent)
        return 1 + depth(by_id[parent], seen)

    errors = [s for s in spans if s.get("status_code") == "ERROR"]
    if not errors:
        return Signal(
            name="trace_walk",
            verdict="No error span found in the trace.",
            suspect_repository=None,
            weight=0.0,
            evidence={"error_span_count": 0},
        )

    candidates = [s for s in errors if not _is_plumbing(s)] or errors
    # Rank by (carries an exception, depth) -- exception detail wins ties.
    deepest = max(candidates, key=lambda s: (_has_exception(s), depth(s)))
    repo = deepest.get("code_repository") or catalog.repository_for(deepest["service_name"])

    exception = None
    for event in deepest.get("events") or []:
        attrs = event.get("attributes", {})
        if attrs.get("exception.type"):
            exception = {
                "type": attrs.get("exception.type"),
                "message": str(attrs.get("exception.message", ""))[:300],
                "stack": str(attrs.get("exception.stacktrace", ""))[:2000],
            }
            break

    return Signal(
        name="trace_walk",
        verdict=(
            f"Deepest error span is in {deepest['service_name']} "
            f"(repository {repo})."
        ),
        suspect_repository=repo,
        weight=0.40,
        evidence={
            "span_id": deepest["span_id"],
            "service": deepest["service_name"],
            "operation": deepest["name"],
            "depth": depth(deepest),
            "error_span_count": len(errors),
            "exception": exception,
            "failure_origin": (deepest.get("attributes") or {}).get("product.failure_origin"),
        },
    )


# ------------------------------------------------------ 2. contract validation

def _validate(payload: object, schema: dict) -> list[str]:
    """Validate a payload, returning human-readable violations.

    Raises RuntimeError if the SCHEMA itself cannot be evaluated (an unresolved
    $ref, say). That is a defect in our contract handling, not in the payload,
    and it must never be reported as a contract violation by the service under
    inspection -- blaming a capability for our own schema bug is exactly the
    confidently-wrong attribution this design exists to prevent.
    """
    try:
        validator = Draft202012Validator(schema)
        errors = []
        for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
            location = "/".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(f"{location}: {err.message}")
        return errors[:10]
    except Exception as exc:  # noqa: BLE001 - schema-side failure, not payload-side
        raise RuntimeError(f"schema could not be evaluated: {type(exc).__name__}: {exc}") from exc


def contract_validation(spans: list[dict]) -> tuple[list[Signal], list[dict]]:
    """THE DECISIVE TEST.

    For every captured downstream response in the trace, validate the payload
    the capability ACTUALLY returned against the schema it ACTUALLY published.

    Returns (signals, exonerated). A capability whose payload validates cleanly
    is exonerated on objective evidence -- not on the agent's opinion, and not
    on the absence of a complaint.
    """
    signals: list[Signal] = []
    exonerated: list[dict] = []

    for span in spans:
        attrs = span.get("attributes") or {}
        callee = attrs.get("downstream.service")
        body = attrs.get("downstream.response_body")
        operation = attrs.get("downstream.operation")

        if not callee or not body or not operation:
            continue

        status = int(attrs.get("downstream.status_code") or 200)
        repo = catalog.repository_for(callee)
        schema = catalog.response_schema(callee, operation, status)

        if schema is None:
            signals.append(
                Signal(
                    name="contract_validation",
                    verdict=f"No published contract resolved for {callee} {operation}.",
                    suspect_repository=None,
                    weight=0.0,
                    evidence={"service": callee, "operation": operation},
                )
            )
            continue

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            signals.append(
                Signal(
                    name="contract_validation",
                    verdict=f"{callee} returned a body that is not valid JSON.",
                    suspect_repository=repo,
                    weight=0.88,
                    conclusive=True,
                    evidence={"service": callee, "operation": operation, "error": str(exc)},
                )
            )
            continue

        try:
            violations = _validate(payload, schema)
        except RuntimeError as exc:
            # Our schema handling failed. Degrade to "no contract available",
            # which lowers confidence and weights the other signals -- never
            # blame the service for a fault in our own validator.
            log.error("contract validation aborted for %s %s: %s", callee, operation, exc)
            signals.append(
                Signal(
                    name="contract_validation",
                    verdict=(
                        f"Contract for {callee} {operation} could not be evaluated; "
                        f"treating as no-contract-available."
                    ),
                    suspect_repository=None,
                    weight=0.0,
                    evidence={"service": callee, "operation": operation, "error": str(exc)},
                )
            )
            continue

        if violations:
            # Payload invalid -> the fault is the capability. High confidence,
            # because the evidence is objective and independently checkable.
            signals.append(
                Signal(
                    name="contract_validation",
                    verdict=(
                        f"{callee} returned a payload that VIOLATES its own published "
                        f"schema for {operation}."
                    ),
                    suspect_repository=repo,
                    weight=0.88,
                    conclusive=True,
                    evidence={
                        "service": callee,
                        "repository": repo,
                        "operation": operation,
                        "status_code": status,
                        "violations": violations,
                        "payload_excerpt": body[:600],
                    },
                )
            )
        else:
            # Payload valid. The capability behaved correctly; if the caller
            # still failed, the fault is the caller's.
            exonerated.append(
                {
                    "service": callee,
                    "repository": repo,
                    "reason": (
                        f"Response to {operation} validates cleanly against "
                        f"{callee}'s published schema."
                    ),
                    "operation": operation,
                    "status_code": status,
                    "payload_excerpt": body[:400],
                    "basis": "contract_validation",
                }
            )
            signals.append(
                Signal(
                    name="contract_validation",
                    verdict=f"{callee} response is contract-compliant. Exonerated.",
                    suspect_repository=None,
                    weight=0.30,
                    conclusive=True,
                    evidence={
                        "service": callee,
                        "repository": repo,
                        "operation": operation,
                        "status_code": status,
                        "result": "valid",
                    },
                )
            )

    if not signals:
        signals.append(
            Signal(
                name="contract_validation",
                verdict="No downstream payloads were captured for this trace.",
                suspect_repository=None,
                weight=0.0,
                evidence={"captured": 0},
            )
        )

    return signals, exonerated


# ------------------------------------------------------- 3. deploy correlation

def deploy_correlation(conn, first_seen: datetime, window_minutes: int = 30) -> Signal:
    """Which services shipped in the window before first occurrence.

    A single candidate raises confidence sharply and also suggests revert as a
    valid fix rather than a patch.
    """
    since = first_seen - timedelta(minutes=window_minutes)
    rows = conn.execute(
        """
        SELECT service_name, version, ref, deployed_at
        FROM tel_deployments
        WHERE deployed_at BETWEEN %s AND %s
        ORDER BY deployed_at DESC
        """,
        (since, first_seen),
    ).fetchall()

    if not rows:
        return Signal(
            name="deploy_correlation",
            verdict=f"No deployments in the {window_minutes} minutes before first occurrence.",
            suspect_repository=None,
            weight=0.0,
            evidence={"deployments": []},
        )

    # Serialise timestamps here. These rows land in a JSONB column, and a raw
    # datetime crashes the whole attribution -- which is exactly what happened
    # once a successful repair started writing deployment records.
    serialised = [
        {**dict(r), "deployed_at": r["deployed_at"].isoformat()} for r in rows
    ]
    services_deployed = {r["service_name"] for r in rows}
    if len(services_deployed) == 1:
        name = next(iter(services_deployed))
        repo = catalog.repository_for(name)
        return Signal(
            name="deploy_correlation",
            verdict=f"Exactly one service shipped before first occurrence: {name}.",
            suspect_repository=repo,
            weight=0.25,
            evidence={"deployments": serialised, "single_candidate": name},
        )

    return Signal(
        name="deploy_correlation",
        verdict=f"{len(services_deployed)} services shipped in the window; not discriminating.",
        suspect_repository=None,
        weight=0.05,
        evidence={"deployments": serialised},
    )


# ------------------------------------------------------------ 4. blast radius

def _chain_to(failing: list[str], start: str, seen: set[str] | None = None) -> list[str]:
    """The path of failing services from `start` down to the root of the failure."""
    seen = seen or set()
    if start in seen:
        return [start]
    seen.add(start)
    for dependency in catalog.service(start).get("depends_on") or []:
        if dependency in failing:
            return [start, *_chain_to(failing, dependency, seen)]
    return [start]


def _root_of_failure(failing: list[str], start: str) -> str:
    """Deepest failing service in the dependency chain -- the one whose own
    dependencies are all healthy, so nothing downstream can explain it."""
    return _chain_to(failing, start)[-1]


def blast_radius(conn, suspect_service: str | None, since: datetime) -> Signal:
    """Is only this consumer affected, or every consumer of the capability?

    Many consumers failing points at the capability regardless of what the trace
    says. One consumer failing while the capability serves everyone else
    correctly points at that consumer.
    """
    if not suspect_service:
        return Signal(
            name="blast_radius",
            verdict="No suspect service to assess.",
            suspect_repository=None,
            weight=0.0,
        )

    rows = conn.execute(
        """
        SELECT service_name,
               COUNT(*) FILTER (WHERE status_code = 'ERROR') AS errors,
               COUNT(*)                                      AS total
        FROM tel_spans
        WHERE start_time >= %s AND kind = 'SERVER'
        GROUP BY service_name
        """,
        (since,),
    ).fetchall()

    failing = [r["service_name"] for r in rows if r["errors"] > 0]
    dependencies = catalog.service(suspect_service).get("depends_on") or []
    failing_dependencies = [d for d in dependencies if d in failing]

    if failing_dependencies:
        # Follow the chain to its ROOT, exactly as trace_walk follows a trace to
        # its deepest meaningful error. Taking failing_dependencies[0] blamed the
        # nearest failing hop, but a middle capability failing because ITS
        # dependency failed is a victim, not a cause. Getting this wrong cost the
        # signal its credit whenever the real owner was two hops down: it pointed
        # at cap-calc while attribution had concluded cap-factors, the combiner
        # saw a mismatch, and a genuine cross-boundary fault scored 0.70 instead
        # of 0.92 -- review instead of repair.
        root = _root_of_failure(failing, failing_dependencies[0])
        chain = " -> ".join([suspect_service, *_chain_to(failing, failing_dependencies[0])])
        return Signal(
            name="blast_radius",
            verdict=(
                f"{suspect_service} and its dependencies {failing_dependencies} are all "
                f"erroring; the chain bottoms out at {root}, which has no failing "
                f"dependency of its own."
            ),
            suspect_repository=catalog.repository_for(root),
            weight=0.22,
            evidence={"failing_services": failing,
                      "failing_dependencies": failing_dependencies,
                      "failure_chain": chain, "root": root},
        )

    return Signal(
        name="blast_radius",
        verdict=(
            f"Only {suspect_service} is erroring. Its dependencies {dependencies} "
            "are serving other consumers without error."
        ),
        suspect_repository=catalog.repository_for(suspect_service),
        weight=0.22,
        evidence={
            "failing_services": failing,
            "suspect_dependencies": dependencies,
            "dependencies_healthy": True,
            "per_service": [dict(r) for r in rows],
        },
    )


# ------------------------------------------------------------------- combine

def attribute(conn, spans: list[dict], first_seen: datetime) -> Attribution:
    """Run every signal and combine into a fault domain plus a confidence."""
    signals: list[Signal] = []

    walk = trace_walk(spans)
    signals.append(walk)

    contract_signals, exonerated = contract_validation(spans)
    signals.extend(contract_signals)

    signals.append(deploy_correlation(conn, first_seen))

    suspect_service = walk.evidence.get("service")
    signals.append(blast_radius(conn, suspect_service, first_seen - timedelta(minutes=10)))

    # --- decide the fault domain --------------------------------------------
    exonerated_repos = {e["repository"] for e in exonerated}
    violating = [
        s for s in signals
        if s.name == "contract_validation" and s.conclusive and s.suspect_repository
    ]

    if violating:
        # A capability violated its own published contract. That is objective
        # and decisive: the fault is the capability's, wherever the error
        # happened to surface.
        fault_domain = violating[0].suspect_repository
        basis = ["contract_violation", "trace_error_span"]
    else:
        # Every downstream payload was valid, so the fault belongs to whoever
        # actually raised -- even though the error surfaced there rather than
        # originating in a downstream service.
        fault_domain = walk.suspect_repository
        basis = ["trace_error_span"]
        if exonerated:
            basis.append("downstream_contract_compliant")

    if any(s.name == "deploy_correlation" and s.suspect_repository for s in signals):
        basis.append("deploy_correlation")
    if fault_domain and fault_domain not in exonerated_repos:
        basis.append("blast_radius")

    # --- confidence ----------------------------------------------------------
    #
    # Weights are additive and chosen so that a CLEAN attribution clears the
    # autonomous floor with headroom, while any missing signal drops it into
    # human review rather than leaving it balanced on the threshold.
    #
    #   Scenario 1 (product owns it, both capabilities exonerated):
    #       trace_walk 0.40 + exoneration 0.15 x2 + blast_radius 0.22 = 0.92
    #   Scenario 2 (capability violates its own schema):     0.88  -> autonomous
    #   One exoneration missing:                             0.77  -> review
    #   No contracts resolvable at all:                      0.62  -> review
    #   Trace walk alone:                                    0.40  -> escalate
    #
    # A CONCLUSIVE contract violation weighs 0.88 -- above the autonomous floor
    # on its own, and the only single signal that is. That is not confidence
    # inflation: it is the one piece of evidence that is objective and
    # independently checkable by anyone, the service's own published schema
    # against its own payload, with no inference in between. It previously
    # weighed 0.70, which meant "the decisive test" could never on its own clear
    # the bar to act -- and a capability that had demonstrably broken its own
    # contract still needed a human to confirm what the schema already proved.
    #
    # Note what this does NOT do: a violation the validator cannot fully resolve
    # is weak (0.30), an exoneration is still 0.15, and blast radius cannot
    # corroborate a capability that returns 200 while violating its schema --
    # so a silent contract break rests on this signal alone, as it should.
    confidence = 0.0
    for signal in signals:
        if signal.suspect_repository == fault_domain and signal.weight > 0:
            confidence += signal.weight
        elif signal.name == "contract_validation" and signal.conclusive and not signal.suspect_repository:
            # An exoneration is positive evidence for the remaining suspect.
            confidence += signal.weight * 0.5

    confidence = max(0.0, min(confidence, 0.99))

    return Attribution(
        fault_domain=fault_domain,
        surfaced_in=walk.suspect_repository,
        confidence=confidence,
        signals=signals,
        exonerated=exonerated,
        basis=sorted(set(basis)),
    )
