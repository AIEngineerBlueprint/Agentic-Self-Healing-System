-- ASHS control-plane store.
--
-- Two concerns in one database, deliberately separated by table prefix:
--   tel_*  telemetry ingested from the OTel collector (the evidence)
--   ash_*  the repair episodes themselves (the audit trail)
--
-- Phase 0 populates tel_* only. The ash_* tables are created now so the
-- evidence and the episode always share a transaction boundary later.

-- ---------------------------------------------------------------- telemetry

CREATE TABLE IF NOT EXISTS tel_spans (
    span_id          TEXT PRIMARY KEY,
    trace_id         TEXT NOT NULL,
    parent_span_id   TEXT,
    name             TEXT NOT NULL,

    -- Resource attributes. code_repository is the one that matters: it maps a
    -- failing span to an owning repository without a lookup table.
    service_name     TEXT NOT NULL,
    service_version  TEXT,
    deployment_stack TEXT,
    code_repository  TEXT,

    kind             TEXT,
    start_time       TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ NOT NULL,
    duration_ms      NUMERIC(12,3) NOT NULL,

    status_code      TEXT NOT NULL DEFAULT 'UNSET',   -- UNSET | OK | ERROR
    status_message   TEXT,

    -- Full attribute bag, including downstream.response_body -- the raw payload
    -- that contract validation needs.
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Exception events, with type and stack trace.
    events           JSONB NOT NULL DEFAULT '[]'::jsonb,

    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spans_trace     ON tel_spans (trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_service   ON tel_spans (service_name, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_spans_errors    ON tel_spans (status_code, start_time DESC)
    WHERE status_code = 'ERROR';
CREATE INDEX IF NOT EXISTS idx_spans_time      ON tel_spans (start_time DESC);
CREATE INDEX IF NOT EXISTS idx_spans_attrs     ON tel_spans USING gin (attributes);

CREATE TABLE IF NOT EXISTS tel_logs (
    id               BIGSERIAL PRIMARY KEY,
    trace_id         TEXT,
    span_id          TEXT,
    service_name     TEXT NOT NULL,
    service_version  TEXT,
    deployment_stack TEXT,
    code_repository  TEXT,
    severity         TEXT,
    body             TEXT,
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at      TIMESTAMPTZ NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_logs_trace   ON tel_logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_logs_service ON tel_logs (service_name, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_sev     ON tel_logs (severity, observed_at DESC);

-- Deployment history, so the attribution engine can correlate a change point in
-- error rate against which services shipped in the preceding window.
CREATE TABLE IF NOT EXISTS tel_deployments (
    id           BIGSERIAL PRIMARY KEY,
    service_name TEXT NOT NULL,
    version      TEXT NOT NULL,
    ref          TEXT,
    deployed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deployed_by  TEXT NOT NULL DEFAULT 'system',
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_deploy_service ON tel_deployments (service_name, deployed_at DESC);

-- ----------------------------------------------------------------- episodes

CREATE TABLE IF NOT EXISTS ash_incidents (
    incident_id      TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    state            TEXT NOT NULL,          -- see the task state model
    severity         TEXT NOT NULL DEFAULT 'S3',
    first_seen       TIMESTAMPTZ NOT NULL,
    last_seen        TIMESTAMPTZ NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,

    entry_point      TEXT,
    surfaced_in      TEXT,                   -- where the error was VISIBLE
    fault_domain     TEXT,                   -- who actually OWNS it
    confidence       NUMERIC(4,3),
    attribution_basis JSONB NOT NULL DEFAULT '[]'::jsonb,

    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnosis        JSONB,
    plan             JSONB,
    outcome          TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_open_fingerprint
    ON ash_incidents (fingerprint)
    WHERE state NOT IN ('RESOLVED', 'ESCALATED');

-- Append-only. Every state transition, every tool call, every prompt, every
-- diff -- with the evidence that justified it.
CREATE TABLE IF NOT EXISTS ash_audit (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  TEXT NOT NULL REFERENCES ash_incidents(incident_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT NOT NULL,      -- supervisor | product-agent | tool-gateway | policy
    kind         TEXT NOT NULL,      -- state_transition | tool_call | llm_call | decision
    summary      TEXT NOT NULL,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_ref JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (incident_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_audit_incident ON ash_audit (incident_id, seq);

CREATE TABLE IF NOT EXISTS ash_metrics (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  TEXT REFERENCES ash_incidents(incident_id) ON DELETE CASCADE,
    metric       TEXT NOT NULL,      -- ttd_seconds | ttdiag_seconds | ttr_seconds | ...
    value        NUMERIC(14,3) NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Escalations. Previously a state change and nothing else -- no page, no ticket,
-- so an escalation was only visible if somebody was watching the dashboard.
CREATE TABLE IF NOT EXISTS ash_escalations (
    incident_id   TEXT PRIMARY KEY REFERENCES ash_incidents(incident_id) ON DELETE CASCADE,
    raised_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged  BOOLEAN NOT NULL DEFAULT false,
    reason        TEXT NOT NULL,
    owner_team    TEXT,
    owner_contact TEXT,
    fault_domain  TEXT,
    summary       TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb
);
