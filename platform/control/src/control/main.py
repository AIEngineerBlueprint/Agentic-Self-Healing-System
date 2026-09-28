"""ashs-control -- the ASHS control plane.

PHASE 0 SCOPE: telemetry ingest and query only. No agents yet.

Application services remain ordinary workloads; this control plane observes them
and (from Phase 2) performs only explicitly permitted actions. Keeping ingest and
query working first is what makes the later phases boring instead of heroic.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from common.jsonlog import setup_logging

from . import agents, catalog, executor, gateway, incidents, llm, repair, sink

SERVICE_NAME = "ashs-control"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")
DSN = os.environ.get("ASHS_DB_URL", "postgresql://ashs:ashs@ashs-db:5432/ashs")

setup_logging(SERVICE_NAME, SERVICE_VERSION)
log = logging.getLogger(__name__)

app = FastAPI(
    title="ASHS Control Plane",
    version=SERVICE_VERSION,
    description="Telemetry ingest, evidence query, and (from Phase 2) the agentic repair loop.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool not initialised")
    return _pool


@app.on_event("startup")
def _startup() -> None:
    global _pool
    last: Exception | None = None
    for attempt in range(40):
        try:
            candidate = ConnectionPool(
                DSN, min_size=2, max_size=10, kwargs={"row_factory": dict_row}
            )
            with candidate.connection() as conn:
                conn.execute("SELECT 1")
            _pool = candidate
            break
        except Exception as exc:  # noqa: BLE001 - startup retry is intentional
            last = exc
            time.sleep(1.0)
    else:
        raise RuntimeError(f"ashs-db unreachable: {last}")

    schema = (pathlib.Path(__file__).parent / "schema.sql").read_text()
    with pool().connection() as conn:
        conn.execute(schema)
    catalog.load()

    # Seed a deployment baseline so deploy correlation has a real timeline to
    # reason over rather than an empty table.
    with pool().connection() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM tel_deployments").fetchone()
        if existing["n"] == 0:
            for name in catalog.services():
                conn.execute(
                    """
                    INSERT INTO tel_deployments (service_name, version, ref, deployed_at, note)
                    VALUES (%s, '1.0.0', 'baseline', now() - interval '2 hours', 'initial deploy')
                    """,
                    (name,),
                )

    asyncio.create_task(_detection_loop())
    log.info("ashs-control ready; schema applied, catalog loaded, detector running")


DETECT_INTERVAL = float(os.environ.get("DETECT_INTERVAL_SECONDS", "10"))
# Master switch for autonomous execution, separate from the policy kill
# switch: this stops the loop from acting at all, for rehearsal and tests.
AUTO_EXECUTE = os.environ.get("AUTO_EXECUTE", "true").lower() == "true"


def set_auto_execute(enabled: bool) -> None:
    """Runtime autonomy dial: observe-and-plan vs observe-plan-and-act.

    This is the L2/L3 boundary as a live switch. Distinct from the policy kill
    switch (which drops detection itself to L0) -- here the system still detects,
    attributes, plans and gates, it just stops short of mutating anything.
    Rehearsals and the earlier phase tests need exactly that.
    """
    global AUTO_EXECUTE
    AUTO_EXECUTE = bool(enabled)
    log.warning("auto_execute -> %s", AUTO_EXECUTE)


async def _detection_loop() -> None:
    """Continuous detection. An incident that needs a human to press a button
    is not autonomous detection."""
    await asyncio.sleep(15)  # let a baseline accumulate before judging anything
    while True:
        try:
            # A sweep that includes a repair can run for minutes. to_thread is
            # awaited, so passes never overlap.
            await asyncio.to_thread(_sweep)
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("detection sweep failed")
        await asyncio.sleep(DETECT_INTERVAL)


# A repair runs synchronously inside one sweep, so EXECUTING and VALIDATING are
# excluded from the pending query -- otherwise the next pass would re-enter a
# repair already in flight. Nothing then ever looks at them again, so anything
# that kills execute() mid-way strands the incident in that state forever, with
# the patch possibly applied and no one left holding the snapshot.
#
# A repair loop takes ~90s. Well past that, it is not in flight, it is dead.
STUCK_AFTER_SECONDS = float(os.environ.get("STUCK_AFTER_SECONDS", "600"))


def _reclaim_stuck(conn) -> None:
    """Unwind repairs that died mid-flight.

    This is the case the reconciler docstring claims to handle and did not:
    recovery from a partial failure is only automatic for states the sweep
    actually looks at.
    """
    rows = conn.execute(
        """
        SELECT incident_id, state, updated_at FROM ash_incidents
        WHERE state IN ('EXECUTING', 'VALIDATING')
          AND updated_at < now() - (%s * interval '1 second')
        """,
        (STUCK_AFTER_SECONDS,),
    ).fetchall()

    for row in rows:
        incident_id, state = row["incident_id"], row["state"]
        log.error("incident %s stuck in %s since %s -- reclaiming",
                  incident_id, state, row["updated_at"].isoformat(timespec="seconds"))
        try:
            rolled = executor.restore_by_incident(incident_id)
            incidents.audit(conn, incident_id, "supervisor", "tool_call",
                            f"Reclaimed a repair abandoned in {state}. "
                            + ("Source restored from the snapshot on disk."
                               if rolled.get("snapshot_found")
                               else "No snapshot on disk; source left as-is."),
                            {"tool": "restore_snapshot", "outcome": "ok",
                             "stuck_state": state, **rolled})
            incidents.transition(
                conn, incident_id, "ROLLBACK", "supervisor",
                f"Repair abandoned in {state} for more than "
                f"{STUCK_AFTER_SECONDS:.0f}s. Known-good state restored.",
                {"stuck_state": state, **rolled})
            incidents.transition(
                conn, incident_id, "ESCALATED", "supervisor",
                "A repair died mid-flight. Escalated so a human confirms the "
                "service is serving correctly.")
        except Exception:  # noqa: BLE001 - reclaiming must never kill the sweep
            log.exception("could not reclaim %s", incident_id)


def _sweep() -> None:
    """One reconciliation pass.

    RECONCILER, NOT A ONE-SHOT PIPELINE. An earlier version only advanced
    incidents that `detect()` had just created, which meant any incident that
    stalled -- a transient error, or the process restarting mid-sweep -- was
    stranded forever: dedupe stopped it being re-created, and nothing else ever
    looked at it again.

    Now every pass drives ALL non-terminal incidents forward from whatever state
    they are actually in. Recovery from a partial failure is then automatic, and
    the loop is idempotent by construction.
    """
    # EMERGENCY STOP, checked every pass. Previously the kill switch only
    # stopped NEW incidents opening, so anything already at POLICY_APPROVED
    # still executed -- a stop control that does not stop what is running is
    # the wrong thing to demonstrate confidence in.
    if catalog.policy().get("kill_switch"):
        log.warning("kill switch engaged -- reconciliation halted (L0 observe)")
        return

    with pool().connection() as conn:
        _reclaim_stuck(conn)

    with pool().connection() as conn:
        incidents.detect(conn)

    with pool().connection() as conn:
        pending = conn.execute(
            """
            SELECT incident_id, state FROM ash_incidents
            WHERE state NOT IN ('RESOLVED', 'ESCALATED', 'EXECUTING', 'VALIDATING')
            ORDER BY created_at
            """
        ).fetchall()

    for row in pending:
        incident_id, state = row["incident_id"], row["state"]
        try:
            if state in ("NEW", "TRIAGED", "EVIDENCE_READY"):
                with pool().connection() as conn:
                    incidents.triage_to_diagnosis(conn, incident_id)
                state = "DIAGNOSIS_READY"

            if state in ("DIAGNOSIS_READY", "ROUTED", "PLAN_READY"):
                with pool().connection() as conn:
                    current = conn.execute(
                        "SELECT state FROM ash_incidents WHERE incident_id = %s",
                        (incident_id,),
                    ).fetchone()["state"]
                    if current == "DIAGNOSIS_READY":
                        agents.run_to_policy(conn, incident_id)

            if not AUTO_EXECUTE:
                continue

            with pool().connection() as conn:
                current = conn.execute(
                    "SELECT state FROM ash_incidents WHERE incident_id = %s",
                    (incident_id,),
                ).fetchone()["state"]
                if current == "POLICY_APPROVED":
                    outcome = repair.execute(conn, incident_id)
                    log.info("repair for %s finished: %s", incident_id, outcome.get("outcome"))

        except Exception:  # noqa: BLE001 - one bad incident must not stall the rest
            log.exception("reconcile failed for %s (state=%s)", incident_id, state)


# ------------------------------------------------------------------ ingest

async def _otlp_payload(request: Request) -> dict:
    """Read an OTLP body, decompressing if the collector gzipped it.

    The otlphttp exporter compresses by default. Handling it here rather than
    relying on the collector's config means the sink keeps working if someone
    changes that setting later.
    """
    raw = await request.body()
    if not raw:
        return {}
    if request.headers.get("content-encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


@app.post("/otlp/v1/traces")
async def ingest_traces(request: Request):
    payload = await _otlp_payload(request)
    rows = sink.parse_traces(payload)
    if not rows:
        return {"accepted": 0}

    with pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO tel_spans (
                span_id, trace_id, parent_span_id, name,
                service_name, service_version, deployment_stack, code_repository,
                kind, start_time, end_time, duration_ms,
                status_code, status_message, attributes, events
            ) VALUES (
                %(span_id)s, %(trace_id)s, %(parent_span_id)s, %(name)s,
                %(service_name)s, %(service_version)s, %(deployment_stack)s, %(code_repository)s,
                %(kind)s, %(start_time)s, %(end_time)s, %(duration_ms)s,
                %(status_code)s, %(status_message)s, %(attributes)s, %(events)s
            )
            ON CONFLICT (span_id) DO NOTHING
            """,
            [dict(r, attributes=Json(r["attributes"]), events=Json(r["events"])) for r in rows],
        )
    return {"accepted": len(rows)}


@app.post("/otlp/v1/logs")
async def ingest_logs(request: Request):
    payload = await _otlp_payload(request)
    rows = sink.parse_logs(payload)
    if not rows:
        return {"accepted": 0}

    with pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO tel_logs (
                trace_id, span_id, service_name, service_version,
                deployment_stack, code_repository, severity, body,
                attributes, observed_at
            ) VALUES (
                %(trace_id)s, %(span_id)s, %(service_name)s, %(service_version)s,
                %(deployment_stack)s, %(code_repository)s, %(severity)s, %(body)s,
                %(attributes)s, %(observed_at)s
            )
            """,
            [dict(r, attributes=Json(r["attributes"])) for r in rows],
        )
    return {"accepted": len(rows)}


@app.post("/otlp/v1/metrics")
async def ingest_metrics(request: Request):
    # Accepted and discarded in Phase 0. Error rate is derived from spans, which
    # is more precise for attribution than a pre-aggregated counter.
    await request.body()
    return {"accepted": 0}


# ------------------------------------------------------------------- query

@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str):
    """Full span tree for one trace. The raw material for the trace walk."""
    with pool().connection() as conn:
        spans = conn.execute(
            """
            SELECT * FROM tel_spans WHERE trace_id = %s ORDER BY start_time
            """,
            (trace_id,),
        ).fetchall()
        logs = conn.execute(
            """
            SELECT * FROM tel_logs WHERE trace_id = %s ORDER BY observed_at
            """,
            (trace_id,),
        ).fetchall()

    if not spans:
        return JSONResponse(
            status_code=404,
            content={"error": "trace_not_found", "message": f"No spans for trace {trace_id}."},
        )
    return {"trace_id": trace_id, "spans": spans, "logs": logs}


@app.get("/api/errors")
def recent_errors(minutes: int = 15, limit: int = 50):
    """Recent error spans, newest first."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT span_id, trace_id, name, service_name, deployment_stack,
                   code_repository, status_code, status_message,
                   start_time, duration_ms, attributes, events
            FROM tel_spans
            WHERE status_code = 'ERROR' AND start_time >= %s
            ORDER BY start_time DESC
            LIMIT %s
            """,
            (since, limit),
        ).fetchall()
    return {"window_minutes": minutes, "count": len(rows), "errors": rows}


@app.get("/api/health-rollup")
def health_rollup(minutes: int = 5):
    """Per-service request and error rate. Drives the dashboard sparklines."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT service_name,
                   deployment_stack,
                   COUNT(*)                                            AS requests,
                   COUNT(*) FILTER (WHERE status_code = 'ERROR')       AS errors,
                   ROUND(AVG(duration_ms)::numeric, 1)                 AS avg_ms,
                   ROUND(
                     100.0 * COUNT(*) FILTER (WHERE status_code = 'ERROR')
                     / NULLIF(COUNT(*), 0), 2
                   )                                                   AS error_rate_pct
            FROM tel_spans
            WHERE start_time >= %s AND kind = 'SERVER'
            GROUP BY service_name, deployment_stack
            ORDER BY service_name
            """,
            (since,),
        ).fetchall()
    return {"window_minutes": minutes, "services": rows}


@app.get("/api/logs")
def query_logs(service: str | None = None, minutes: int = 15, limit: int = 200):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    sql = """
        SELECT trace_id, span_id, service_name, severity, body, attributes, observed_at
        FROM tel_logs WHERE observed_at >= %s
    """
    params: list = [since]
    if service:
        sql += " AND service_name = %s"
        params.append(service)
    sql += " ORDER BY observed_at DESC LIMIT %s"
    params.append(limit)

    with pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "logs": rows}


@app.get("/api/stats")
def stats():
    """Phase 0 exit check: are spans and logs actually landing?"""
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM tel_spans)                                  AS spans,
              (SELECT COUNT(DISTINCT trace_id) FROM tel_spans)                  AS traces,
              (SELECT COUNT(*) FROM tel_logs)                                   AS logs,
              (SELECT COUNT(*) FROM tel_spans WHERE status_code = 'ERROR')      AS error_spans,
              (SELECT COUNT(DISTINCT service_name) FROM tel_spans)              AS services,
              (SELECT MAX(start_time) FROM tel_spans)                           AS latest_span
            """
        ).fetchone()


@app.get("/api/traces/recent/full")
def recent_full_traces(limit: int = 5):
    """Traces that span all four services -- the Phase 0 exit test.

    One calculation must produce ONE trace crossing cfc-web, cfc-api, calc-api,
    and factor-api. If this returns nothing, context propagation is broken and
    every later phase is built on sand.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT trace_id,
                   COUNT(*)                          AS span_count,
                   COUNT(DISTINCT service_name)      AS service_count,
                   ARRAY_AGG(DISTINCT service_name)  AS services,
                   MIN(start_time)                   AS started_at,
                   MAX(CASE WHEN status_code = 'ERROR' THEN 1 ELSE 0 END)::bool AS has_error
            FROM tel_spans
            GROUP BY trace_id
            HAVING COUNT(DISTINCT service_name) >= 3
            ORDER BY MIN(start_time) DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "traces": rows}


@app.get("/health")
def health():
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "healthy", "service": SERVICE_NAME, "version": SERVICE_VERSION}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": type(exc).__name__},
        )


# ---------------------------------------------------------------- incidents

@app.get("/api/incidents")
def list_incidents(limit: int = 20, include_resolved: bool = True):
    sql = """
        SELECT incident_id, fingerprint, state, severity, first_seen, last_seen,
               occurrence_count, entry_point, surfaced_in, fault_domain,
               confidence, attribution_basis, created_at, updated_at,
               -- 'repaired' vs 'stale': both land in RESOLVED, but only one of
               -- them shipped a patch, and the list must not claim otherwise.
               outcome
        FROM ash_incidents
    """
    if not include_resolved:
        sql += " WHERE state NOT IN ('RESOLVED', 'ESCALATED')"
    sql += " ORDER BY created_at DESC LIMIT %s"

    with pool().connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return {"count": len(rows), "incidents": rows}


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    """The full repair episode, reconstructable from the incident ID alone."""
    with pool().connection() as conn:
        incident = conn.execute(
            "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
        ).fetchone()
        if incident is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "message": f"No incident {incident_id}."},
            )
        trail = conn.execute(
            "SELECT seq, at, actor, kind, summary, detail, evidence_ref "
            "FROM ash_audit WHERE incident_id = %s ORDER BY seq",
            (incident_id,),
        ).fetchall()
    return {"incident": incident, "audit": trail}


@app.get("/api/incidents/{incident_id}/audit")
def get_audit(incident_id: str):
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT seq, at, actor, kind, summary, detail, evidence_ref "
            "FROM ash_audit WHERE incident_id = %s ORDER BY seq",
            (incident_id,),
        ).fetchall()
    return {"incident_id": incident_id, "count": len(rows), "audit": rows}


@app.post("/api/detect")
def force_detect():
    """Force a detection sweep. The loop runs continuously; this is for demos
    and tests that should not wait for the next tick."""
    with pool().connection() as conn:
        created = incidents.detect(conn)
    results = []
    for incident_id in created:
        with pool().connection() as conn:
            results.append(
                {"incident_id": incident_id, "attribution": incidents.triage_to_diagnosis(conn, incident_id)}
            )
    return {"created": len(created), "incidents": results}


@app.get("/api/catalog")
def get_catalog():
    return {
        "services": catalog.services(),
        "repair_agents": catalog._catalog.get("repair_agents", {}),
        "policy": {
            "kill_switch": catalog.policy().get("kill_switch"),
            "confidence": catalog.policy().get("confidence"),
            "protected_paths": catalog.policy().get("protected_paths"),
            "detection": catalog.policy().get("detection"),
        },
    }


# ------------------------------------------------------------------ phase 2

@app.get("/api/llm/status")
def llm_status():
    """Is a model reachable, and what happens if not?"""
    return llm.status()


@app.get("/api/tools")
def list_tools():
    """The typed tool surface. This is the complete set of things any agent can
    do -- there is no raw shell, no kubectl, no unrestricted file access."""
    return {"count": len(gateway.REGISTRY), "tools": gateway.manifest()}


@app.post("/api/incidents/{incident_id}/plan")
def plan_incident(incident_id: str):
    """Drive DIAGNOSIS_READY -> POLICY_APPROVED (or ESCALATED)."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT state FROM ash_incidents WHERE incident_id = %s", (incident_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        if row["state"] != "DIAGNOSIS_READY":
            return JSONResponse(
                status_code=409,
                content={"error": "wrong_state",
                         "message": f"Incident is {row['state']}, expected DIAGNOSIS_READY."},
            )
        return agents.run_to_policy(conn, incident_id)


@app.post("/api/incidents/{incident_id}/tools/{tool_name}")
def invoke_tool(incident_id: str, tool_name: str, body: dict):
    """Invoke a typed tool through the gateway. Every call is audited."""
    actor = body.pop("actor", "operator")
    with pool().connection() as conn:
        try:
            return {"result": gateway.invoke(conn, incident_id, actor, tool_name, **body)}
        except gateway.ToolDenied as exc:
            # A denial is a successful guardrail, not a server error.
            return JSONResponse(
                status_code=403,
                content={"error": "tool_denied", "message": str(exc),
                         "tool": tool_name, "actor": actor},
            )
        except gateway.ToolFailed as exc:
            return JSONResponse(
                status_code=422,
                content={"error": "tool_failed", "message": str(exc), "tool": tool_name},
            )


# ------------------------------------------------------------------ phase 3

@app.post("/api/incidents/{incident_id}/execute")
def execute_repair(incident_id: str):
    """Drive POLICY_APPROVED -> RESOLVED (or ROLLBACK -> ESCALATED)."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT state FROM ash_incidents WHERE incident_id = %s", (incident_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        if row["state"] != "POLICY_APPROVED":
            return JSONResponse(
                status_code=409,
                content={"error": "wrong_state",
                         "message": f"Incident is {row['state']}, expected POLICY_APPROVED."},
            )
        return repair.execute(conn, incident_id)


@app.get("/api/incidents/{incident_id}/metrics")
def incident_metrics(incident_id: str):
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT metric, value, recorded_at FROM ash_metrics WHERE incident_id = %s "
            "ORDER BY recorded_at", (incident_id,)
        ).fetchall()
    return {"incident_id": incident_id, "metrics": rows}


@app.get("/api/config")
def runtime_config():
    return {
        "auto_execute": AUTO_EXECUTE,
        "detect_interval_seconds": DETECT_INTERVAL,
        "kill_switch": catalog.policy().get("kill_switch"),
        "workspace": str(executor.WORKSPACE),
    }


@app.post("/api/_demo/force_validation_failure")
def force_validation_failure(body: dict):
    """DEMO ONLY. Make the next validation fail so rollback can be shown."""
    repair.set_force_validation_failure(bool(body.get("enabled", False)))
    return {"force_validation_failure": repair.FORCE_VALIDATION_FAILURE}


@app.post("/api/config/auto_execute")
def configure_auto_execute(body: dict):
    """Toggle autonomous execution at runtime (L2 plan-only vs L3 act)."""
    set_auto_execute(bool(body.get("enabled", True)))
    return {"auto_execute": AUTO_EXECUTE}


# ------------------------------------------------------------- dashboard feed

@app.get("/api/stream")
async def stream_events(request: Request):
    """Server-sent events for the dashboard.

    Pushes new audit entries and incident state as they happen. The agent's
    reasoning on screen is more convincing than the fixed application, so this
    is the surface the demo is actually projected from -- no polling, no refresh.
    """
    import asyncio as _asyncio

    async def gen():
        last_seq: dict[str, int] = {}
        last_states: dict[str, str] = {}
        yield "retry: 3000\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                def snapshot():
                    with pool().connection() as conn:
                        incidents_rows = conn.execute(
                            """
                            SELECT incident_id, state, severity, first_seen, last_seen,
                                   occurrence_count, entry_point, surfaced_in, fault_domain,
                                   confidence, attribution_basis, created_at, resolved_at
                            FROM ash_incidents ORDER BY created_at DESC LIMIT 8
                            """
                        ).fetchall()
                        fresh = []
                        for inc in incidents_rows:
                            since = last_seq.get(inc["incident_id"], 0)
                            rows = conn.execute(
                                "SELECT seq, at, actor, kind, summary, detail "
                                "FROM ash_audit WHERE incident_id = %s AND seq > %s "
                                "ORDER BY seq",
                                (inc["incident_id"], since),
                            ).fetchall()
                            if rows:
                                last_seq[inc["incident_id"]] = rows[-1]["seq"]
                                fresh.append({"incident_id": inc["incident_id"], "entries": rows})
                        health = conn.execute(
                            """
                            SELECT service_name,
                                   COUNT(*) AS requests,
                                   COUNT(*) FILTER (WHERE status_code='ERROR') AS errors
                            FROM tel_spans
                            WHERE start_time >= now() - interval '2 minutes' AND kind='SERVER'
                            GROUP BY service_name ORDER BY service_name
                            """
                        ).fetchall()
                        specs = catalog.journeys()
                        journey = []
                        for spec in specs:
                            rows_j = conn.execute(
                                """
                                SELECT status_code, start_time, duration_ms
                                FROM tel_spans
                                WHERE service_name = %s AND kind = 'SERVER'
                                  AND name LIKE %s AND name NOT LIKE '%%http send%%'
                                  AND start_time >= now() - interval '3 minutes'
                                ORDER BY start_time DESC LIMIT 40
                                """,
                                (spec["service"], spec["entry_point"] + "%"),
                            ).fetchall()
                            journey.append({**spec, "rows": rows_j})
                        return incidents_rows, fresh, health, journey

                incidents_rows, fresh, health, journey = await _asyncio.to_thread(snapshot)

                changed = [i for i in incidents_rows
                           if last_states.get(i["incident_id"]) != i["state"]]
                for i in incidents_rows:
                    last_states[i["incident_id"]] = i["state"]

                payload = {
                    "incidents": incidents_rows,
                    "audit": fresh,
                    "health": health,
                    "journey": [
                        {
                            "service": g["service"],
                            "service_name": g["service_name"],
                            "product": g["product"],
                            "journey": g["journey"],
                            "repository": g["repository"],
                            "entry_point": g["entry_point"],
                            "ticks": [
                                {"ok": r["status_code"] != "ERROR",
                                 "ms": float(r["duration_ms"])}
                                for r in reversed(g["rows"])
                            ],
                        }
                        for g in journey
                    ],
                    "state_changes": [i["incident_id"] for i in changed],
                    "config": {"auto_execute": AUTO_EXECUTE,
                               "kill_switch": catalog.policy().get("kill_switch")},
                }
                yield f"data: {json.dumps(payload, default=str)}\n\n"
            except Exception as exc:  # noqa: BLE001 - the feed must never die
                log.warning("stream tick failed: %s", exc)
            await _asyncio.sleep(1.5)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/journey")
def user_journey(minutes: int = 10, limit: int = 60,
                 service: str | None = None, entry_point: str | None = None):
    """User-journey health, derived from real telemetry rather than a rendered page.

    An embedded iframe of the product proves nothing you can assert on, and in
    any real deployment the product is not embeddable anyway. What an operator
    actually needs is the journey treated as a synthetic probe: did the
    user-visible entry point succeed, how long did it take, and which attempt
    was the first to fail.

    Every row here is an entry-point span the product actually served.

    Service and entry point default to the first journey declared in
    services.yaml rather than to a compiled-in endpoint, so pointing this at a
    different product is a catalog edit. Both are overridable per request.
    """
    declared = catalog.journeys()
    if service is None or entry_point is None:
        if not declared:
            return {"journey": None, "attempts": [], "window_minutes": minutes,
                    "detail": "no entry_points declared in services.yaml"}
        service = service or declared[0]["service"]
        entry_point = entry_point or declared[0]["entry_point"]

    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT trace_id, start_time, duration_ms, status_code,
                   attributes -> 'http.status_code' AS http_status
            FROM tel_spans
            WHERE service_name = %s AND kind = 'SERVER'
              AND name LIKE %s
              AND name NOT LIKE '%%http send%%'
              AND start_time >= %s
            ORDER BY start_time DESC
            LIMIT %s
            """,
            (service, entry_point + "%", since, limit),
        ).fetchall()

        healthy = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status_code <> 'ERROR') AS ok,
                   COUNT(*) AS total,
                   ROUND(AVG(duration_ms)::numeric, 0) AS avg_ms
            FROM tel_spans
            WHERE service_name = %s AND kind = 'SERVER'
              AND name LIKE %s
              AND name NOT LIKE '%%http send%%'
              AND start_time >= now() - interval '60 seconds'
            """,
            (service, entry_point + "%"),
        ).fetchone()

    attempts = [
        {
            "trace_id": r["trace_id"],
            "at": r["start_time"].isoformat(),
            "ok": r["status_code"] != "ERROR",
            "duration_ms": float(r["duration_ms"]),
            "http_status": r["http_status"],
        }
        for r in rows
    ]
    ok, total = int(healthy["ok"] or 0), int(healthy["total"] or 0)
    return {
        "journey": entry_point,
        "service": service,
        "window_minutes": minutes,
        "attempts": attempts,
        "last_60s": {
            "ok": ok, "total": total,
            "success_rate": round(ok / total, 4) if total else None,
            "avg_ms": float(healthy["avg_ms"]) if healthy["avg_ms"] else None,
            "status": ("healthy" if total and ok == total
                       else "degraded" if total else "no traffic"),
        },
    }


@app.get("/api/escalations")
def list_escalations(include_acknowledged: bool = False):
    """The human queue. An escalation nobody can see is not an escalation."""
    sql = """
        SELECT e.*, i.state, i.confidence, i.occurrence_count
        FROM ash_escalations e JOIN ash_incidents i USING (incident_id)
    """
    if not include_acknowledged:
        sql += " WHERE NOT e.acknowledged"
    sql += " ORDER BY e.raised_at DESC LIMIT 25"
    with pool().connection() as conn:
        rows = conn.execute(sql).fetchall()
    return {"count": len(rows), "escalations": rows}


@app.post("/api/escalations/{incident_id}/ack")
def ack_escalation(incident_id: str):
    with pool().connection() as conn:
        conn.execute("UPDATE ash_escalations SET acknowledged = true "
                     "WHERE incident_id = %s", (incident_id,))
    return {"incident_id": incident_id, "acknowledged": True}


def _next_steps(incident: dict, stop_reason: str, has_patch: bool) -> list[str]:
    """What the person being paged should actually do, given why it stopped.

    An escalation that says "a human must approve" and stops there has handed
    over a problem, not a briefing. The stop reason is known precisely, so the
    next step can be too.
    """
    reason = (stop_reason or "").upper()
    domain = incident.get("fault_domain") or "the owning repository"

    if "REVIEW_REQUIRED" in reason:
        steps = [
            f"Read the drafted patch below. It targets {domain} and has not been applied.",
            "If it is right, apply it yourself — the system deliberately did not.",
            "If it is wrong, say why: that is the signal we use to tune the floors.",
        ]
    elif "PROTECTED" in reason or "protected zone" in (stop_reason or ""):
        steps = [
            "This touches a protected path — emission factors, a formula, or a migration.",
            "No automated change is permitted here regardless of confidence. Make it by hand.",
        ]
    elif "TEST GATE" in reason or "test gate" in (stop_reason or ""):
        steps = [
            "The generated test did not behave as required, so the patch was rejected.",
            "Read the pre-patch and post-patch output below before writing your own fix.",
            "The source was restored; nothing from this attempt is still applied.",
        ]
    elif "VALIDATION" in reason or "validation" in (stop_reason or ""):
        steps = [
            "A patch was applied, failed validation, and was reverted byte-for-byte.",
            "Check the validation output below — it says which check failed and on what.",
        ]
    elif "ABANDONED" in reason or "died mid-flight" in (stop_reason or ""):
        steps = [
            "A repair stopped part-way and was unwound from its snapshot.",
            "Confirm the service is serving correctly — this path is the least tested one.",
        ]
    elif "BELOW THE REVIEW FLOOR" in reason or "below the review floor" in (stop_reason or ""):
        steps = [
            "Confidence was too low to name an owner, so nothing was attributed.",
            "The evidence bundle below is what we have. Start from the exemplar trace.",
        ]
    elif "PATCH GENERATION UNAVAILABLE" in reason or "MODEL" in reason:
        steps = [
            "The model was unreachable, so no patch was attempted — nothing was modified.",
            "The attribution above is unaffected: it never calls a model.",
            "Check credentials (`make check-bedrock`); the incident will be "
            "re-detected once traffic keeps failing.",
        ]
    elif "LOOP BREAKER" in reason or "CHANGE BUDGET" in reason:
        steps = [
            "A safety budget stopped further attempts on this defect.",
            "Repeated failures usually mean the diagnosis is wrong, not the patch.",
        ]
    else:
        steps = [f"Review the evidence below and decide whether {domain} needs a change."]

    if has_patch:
        steps.append("A drafted patch is attached. It was NOT applied.")
    steps.append("Acknowledge the escalation once you have picked it up — that "
                 "releases the re-detection cool-off.")
    return steps


@app.get("/api/incidents/{incident_id}/report")
def incident_report(incident_id: str):
    """The handoff for whoever is being paged.

    Everything the system knows, ordered the way a person picking this up needs
    it: what broke, why it is yours, what we tried, why we stopped, and what to
    do next -- plus a markdown rendering to paste straight into a ticket.
    """
    with pool().connection() as conn:
        incident = conn.execute(
            "SELECT * FROM ash_incidents WHERE incident_id = %s", (incident_id,)
        ).fetchone()
        if incident is None:
            return JSONResponse(status_code=404,
                                content={"error": "not_found",
                                         "message": f"No incident {incident_id}."})
        trail = conn.execute(
            "SELECT seq, at, actor, kind, summary, detail FROM ash_audit "
            "WHERE incident_id = %s ORDER BY seq", (incident_id,)
        ).fetchall()
        escalation = conn.execute(
            "SELECT * FROM ash_escalations WHERE incident_id = %s", (incident_id,)
        ).fetchone()
        metrics = conn.execute(
            "SELECT metric, value FROM ash_metrics WHERE incident_id = %s",
            (incident_id,)).fetchall()

    diagnosis = incident["diagnosis"] or {}
    narrative = diagnosis.get("narrative") or {}
    plan = incident["plan"] or {}
    evidence = incident["evidence"] or {}

    stop = next((e["summary"] for e in reversed(trail)
                 if e["kind"] == "state_transition" and "ESCALATED" in e["summary"]), "")
    gates = [
        {"gate": (e["detail"] or {}).get("gate"),
         "passed": (e["detail"] or {}).get("passed"),
         "detail": e["summary"]}
        for e in trail if e["actor"] == "policy-engine" and (e["detail"] or {}).get("gate")
    ]
    gate_runs = [
        {"phase": (e["detail"] or {}).get("phase"),
         "passed": (e["detail"] or {}).get("passed"),
         "output": ((e["detail"] or {}).get("output") or "")[-1200:]}
        for e in trail if e["actor"] == "test-gate" and (e["detail"] or {}).get("phase")
    ]
    patch = next((e["detail"] for e in trail if e["kind"] == "patch"), None)
    checks = [
        {"check": (e["detail"] or {}).get("check"),
         "passed": (e["detail"] or {}).get("passed"),
         "detail": (e["detail"] or {}).get("detail")}
        for e in trail if e["actor"] == "validation-agent" and (e["detail"] or {}).get("check")
    ]

    # A command the reader can paste to see the failure for themselves.
    replay = (plan.get("validation_criteria") or {}).get("replay_request") or {}
    service = plan.get("target_service") or ""
    root = catalog.external_base_url(service) if service else None
    reproduce = None
    if root and replay.get("path"):
        method = replay.get("method", "POST")
        reproduce = f"curl -sS -X {method} '{root}{replay['path']}'"
        if replay.get("body"):
            reproduce += (" \\\n  -H 'content-type: application/json' \\\n"
                          f"  -d '{json.dumps(replay['body'])}'")

    steps = _next_steps(dict(incident), stop, patch is not None)

    report = {
        "incident_id": incident_id,
        "state": incident["state"],
        "raised_at": (escalation["raised_at"].isoformat() if escalation else None),
        "acknowledged": bool(escalation and escalation["acknowledged"]),
        "owner": {
            "team": (escalation or {}).get("owner_team"),
            "contact": (escalation or {}).get("owner_contact"),
            "repository": incident["fault_domain"],
        },
        "what_broke": {
            "entry_point": incident["entry_point"],
            "surfaced_in": incident["surfaced_in"],
            "occurrences": incident["occurrence_count"],
            "first_seen": incident["first_seen"].isoformat(),
            "severity": incident["severity"],
            "exemplar_traces": evidence.get("trace_exemplars", [])[:5],
        },
        "why_it_is_yours": {
            "fault_domain": incident["fault_domain"],
            "confidence": float(incident["confidence"] or 0),
            "routed_past": incident["surfaced_in"] != incident["fault_domain"],
            "signals": [
                {"name": s.get("name"), "verdict": s.get("verdict"),
                 "weight": s.get("weight")}
                for s in diagnosis.get("signals", []) if s.get("weight", 0) > 0
            ],
            "exonerated": diagnosis.get("exonerated", []),
            "root_cause": narrative.get("root_cause"),
        },
        "what_we_tried": {
            "plan": {"action": plan.get("action_type"),
                     "targets": plan.get("target_paths"),
                     "risk": plan.get("risk"),
                     "summary": plan.get("summary")},
            "policy_gates": gates,
            "test_gate": gate_runs,
            "validation": checks,
            "patch_drafted": patch is not None,
            "patch_applied": incident["state"] == "RESOLVED",
            "patch": patch,
        },
        "why_we_stopped": stop,
        "what_to_do_next": steps,
        "reproduce": reproduce,
        "timings": {m["metric"]: float(m["value"]) for m in metrics},
        "audit_entries": len(trail),
    }
    report["markdown"] = _report_markdown(report)
    return report


def _report_markdown(r: dict) -> str:
    """The same report as something you can paste into a ticket."""
    w, y, t = r["what_broke"], r["why_it_is_yours"], r["what_we_tried"]
    lines = [
        f"# {r['incident_id']} — escalated to {r['owner']['team'] or 'an owner'}",
        "",
        f"**{w['entry_point']}** failed {w['occurrences']}× since "
        f"{w['first_seen'][:19].replace('T', ' ')} (severity {w['severity']}).",
        "",
        "## Why this is yours",
        f"- Fault domain: **{y['fault_domain']}** at confidence **{y['confidence']:.2f}**",
    ]
    if y["routed_past"]:
        lines.append(f"- The error surfaced in `{w['surfaced_in']}`, not in your "
                     f"repository. It was attributed past that service on evidence:")
    for s in y["signals"]:
        lines.append(f"  - `{s['name']}` ({s['weight']}) — {s['verdict']}")
    for e in y["exonerated"]:
        lines.append(f"  - ruled out **{e.get('repository')}** — {e.get('reason')}")
    if y.get("root_cause"):
        lines += ["", f"**Root cause (model-written):** {y['root_cause']}"]

    lines += ["", "## What the system tried"]
    if t["plan"]["action"]:
        lines.append(f"- Planned `{t['plan']['action']}` on "
                     f"{', '.join(t['plan']['targets'] or [])} (risk {t['plan']['risk']})")
    failed_gates = [g for g in t["policy_gates"] if g["passed"] is False]
    for g in failed_gates:
        lines.append(f"- Policy gate `{g['gate']}` FAILED — {g['detail']}")
    for g in t["test_gate"]:
        lines.append(f"- Test gate `{g['phase']}`: "
                     f"{'passed' if g['passed'] else 'failed'}")
    for c in t["validation"]:
        lines.append(f"- Validation `{c['check']}`: "
                     f"{'PASS' if c['passed'] else 'FAIL'} — {c['detail']}")
    lines.append(f"- A patch was {'drafted but NOT applied' if t['patch_drafted'] and not t['patch_applied'] else ('applied' if t['patch_applied'] else 'not drafted')}.")

    lines += ["", "## Why we stopped", f"> {r['why_we_stopped']}", "",
              "## What to do next"]
    lines += [f"{i}. {s}" for i, s in enumerate(r["what_to_do_next"], 1)]

    if r.get("reproduce"):
        lines += ["", "## Reproduce it", "```bash", r["reproduce"], "```"]
    if w["exemplar_traces"]:
        lines += ["", f"Exemplar traces: {', '.join(w['exemplar_traces'][:3])}"]
    lines += ["", f"_Full audit trail: {r['audit_entries']} entries, "
                  f"replayable from {r['incident_id']}._"]
    return "\n".join(lines)


@app.get("/api/incidents/{incident_id}/artifacts")
def incident_artifacts(incident_id: str):
    """Patch diff, generated test, test-gate output, refusals and timings.

    Everything the system produced that a reviewer would actually want to read,
    in one call -- previously all of it existed only in the audit trail.
    """
    with pool().connection() as conn:
        trail = conn.execute(
            "SELECT seq, at, actor, kind, summary, detail FROM ash_audit "
            "WHERE incident_id = %s ORDER BY seq", (incident_id,)
        ).fetchall()
        metrics = conn.execute(
            "SELECT metric, value FROM ash_metrics WHERE incident_id = %s",
            (incident_id,)
        ).fetchall()

    patch = next((e["detail"] for e in trail if e["kind"] == "patch"), None)
    gate = [
        {"phase": (e["detail"] or {}).get("phase"),
         "passed": (e["detail"] or {}).get("passed"),
         "output": (e["detail"] or {}).get("output", "")}
        for e in trail if e["actor"] == "test-gate" and (e["detail"] or {}).get("phase")
    ]
    refusals = [
        {"seq": e["seq"], "tool": (e["detail"] or {}).get("tool"),
         "actor": (e["detail"] or {}).get("actor"),
         "reason": (e["detail"] or {}).get("reason"), "summary": e["summary"]}
        for e in trail
        if e["kind"] == "tool_call" and (e["detail"] or {}).get("outcome") == "denied"
    ]
    return {"incident_id": incident_id, "patch": patch, "test_gate": gate,
            "refusals": refusals,
            "metrics": {m["metric"]: float(m["value"]) for m in metrics}}
