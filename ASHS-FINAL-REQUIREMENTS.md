# Agentic Self-Healing System (ASHS)

**Final requirements and implementation plan — v1 demo**

Consolidates `Agentic_Self-Healing_System_ASHS_Proposal.md`, `autonomous-bugfix-agentic-pipeline.md`, `carbon-footprint-calculator-demo-requirements.md`, and `DemoEnvironmentInstruction.md` into one buildable specification.

Version 1.0 · 2026-08-12 · This document supersedes the four source documents for build purposes.

---

## 1. What v1 is

A working local demo, on Docker Desktop, that closes the loop:

**User → Product → Capability → Failure → Detection → Evidence → Deterministic Attribution → Routing → Policy Gate → LLM-Generated Patch → Test Gate → Deploy → Validation → Resolved (or Rollback / Escalation) → Audit**

The demo application is a carbon footprint calculator. It exists to break in a controlled, repeatable way.

### 1.1 The thesis the demo must prove

Three claims, in priority order. Everything in this spec serves them.

1. **Attribution is deterministic, not vibes.** Trace walk and contract validation decide *who owns the bug*. The LLM never picks the owner. This is what separates ASHS from a log-monitoring chatbot.
2. **The system knows how to stop.** Policy gates, path allowlists, and a test gate are enforced in code, not in a prompt.
3. **Every action is traceable to evidence.** One incident ID walks you from the user-visible 500 back to the exact line of code and forward to the passing test.

### 1.2 Committed scope

**Scenario 1 — Product owns the bug, fixed autonomously.** Built end to end, demo-ready, repeatable.

Scenarios 2 (capability owns it), 3 (refuse to act), and 4 (rollback) are **not** v1 deliverables, but the architecture below is built so each is additive — a new bug flag, a new repair agent registration, and a policy rule. Section 9 states exactly what each would cost.

One thing worth flagging: Scenario 2 is what the source documents call "the scenario worth building the demo around," because it is the one that proves symptom ≠ root cause. Scenario 1 proves the loop works and exonerates the capabilities on objective evidence, which is a genuine and defensible claim — but a skeptical viewer will ask "what if the bug had been downstream?" Section 9.1 is the answer, and it is roughly two days of work once Scenario 1 lands.

### 1.3 Explicitly out of scope for v1

- Kubernetes, cloud, any external infrastructure. Docker Desktop only.
- Gitea, GitHub, real pull request UI. Agents commit to local git repos; the dashboard renders the diff.
- Grafana / Loki / Tempo / Prometheus. Real OpenTelemetry instrumentation and a real collector, but telemetry lands in Postgres and is rendered by our own dashboard.
- `calc-worker`, `calc-redis`, `cap-report`, the async failure scenario (6.4).
- Authentication, authorization, accounts, sessions, roles.
- Real emission factor accuracy. Values are plausible and sourced; this is not a product.
- Mobile responsiveness beyond "acceptable on a projector."

---

## 2. Scenario 1 in full

The single path the demo walks. Every component below exists to serve this trace.

| # | Step | What happens |
|---|---|---|
| 1 | **Baseline** | Traffic generator runs ~1 successful calculation/sec. Dashboard shows a flat green error rate. |
| 2 | **Inject** | `POST cfc-api/_demo/bug {"name": "zero_total_division", "enabled": true}` — runtime flag, no restart. |
| 3 | **Trigger** | A user (or the generator) submits all-zero activity data. |
| 4 | **Legal downstream behavior** | `calc-api` returns `200 {"total_kgco2e": 0.0, "breakdown": [], "factor_dataset_version": "2026.1"}`. Contract-compliant and correct. |
| 5 | **Product fails** | `cfc-api` computes each category's share as `kgco2e / total` → `ZeroDivisionError` → HTTP 500. User sees a generic failure. |
| 6 | **Detect** | Error-rate rule fires on the ingested telemetry. Incident created, fingerprinted, deduplicated. State: `NEW → TRIAGED`. |
| 7 | **Gather** | Evidence Builder pulls the failing trace, correlated logs, the captured raw downstream response body, deployment history, and recent changes. State: `EVIDENCE_READY`. |
| 8 | **Attribute (deterministic)** | Trace walk → deepest error span is `cfc-api`. Contract validation → `calc-api`'s actual response validates cleanly against `calc-api/openapi.json`; `factor-api`'s response validates against its own schema. **Both capabilities exonerated on objective evidence.** Deploy correlation and blast radius add signal. Confidence: 0.94. |
| 9 | **Diagnose (LLM)** | Claude receives the assembled evidence and writes the root-cause narrative and fix strategy. It does **not** choose the owner. State: `DIAGNOSIS_READY`. |
| 10 | **Route** | Fault domain `cfc-product` → Product Repair Agent. State: `ROUTED`. |
| 11 | **Plan** | Typed repair plan: target file, action type `code_patch`, risk `low`, explicit validation criteria. State: `PLAN_READY`. |
| 12 | **Policy gate** | Action type allowlisted; target path inside `cfc-product/app/**`; not in the protected zone; confidence ≥ 0.85; change budget not exceeded. State: `POLICY_APPROVED`. |
| 13 | **Patch** | Claude generates a unified diff plus a test asserting a zero footprint renders as a zero state, not an error. |
| 14 | **Test gate** | New test is run against the **pre-patch** commit and **must fail**. Patch applied. Test must now pass. Full regression suite must pass. A patch without a failing-then-passing test is rejected. State: `EXECUTING`. |
| 15 | **Deploy** | Commit to the `cfc-product` git repo on a branch, merge, service hot-reloads from the mounted volume. |
| 16 | **Validate** | The original failing request is replayed. Expect `200` with a zero-state response. Error rate must return to baseline over a 30s window. State: `VALIDATING`. |
| 17 | **Resolve** | Validation passes → `RESOLVED`. Fails → `ROLLBACK` (git revert, re-validate) → `ESCALATED`. |
| 18 | **Audit** | Full episode — every state transition, tool call, prompt, diff, and test result — queryable by incident ID and rendered in the dashboard. |

**The line to say out loud at step 8:** *"The capability was never touched, and the system can show you exactly why it was ruled out."*

---

## 3. Topology

One Compose project per ownership boundary. A shared external network carries cross-stack traffic; each stack's data store stays on its internal network only, which makes the ownership boundary enforceable rather than decorative.

```
                       ┌──────────────────────────────────────┐
   browser ───────────▶│  STACK: cfc-product                  │
                       │  cfc-web (nginx) ──▶ cfc-api (BFF)   │
                       └──────────────────────┬───────────────┘
                                              │
   ═══════════════════════════════════════════╪═══════════════ ashs-mesh
                                              │
                       ┌──────────────────────▼───────────────┐
                       │  STACK: cap-calc                     │
                       │  calc-api                            │
                       └──────────────────────┬───────────────┘
                                              │
                       ┌──────────────────────▼───────────────┐
                       │  STACK: cap-factors                  │
                       │  factor-api ──▶ factor-db (internal) │
                       └──────────────────────────────────────┘

   STACK: obs      otel-collector
   STACK: ashs     ashs-control (agents + tool gateway + telemetry sink)
                   ashs-ui (dashboard)
                   ashs-db (Postgres: telemetry + episodes, internal)
                   traffic-gen
```

### 3.1 Service and port map

| Stack | Service | Role | Host port | On mesh |
|---|---|---|---|---|
| `cfc-product` | `cfc-web` | React frontend, nginx | 3000 | no |
| `cfc-product` | `cfc-api` | Backend for frontend | 8000 | yes |
| `cap-calc` | `calc-api` | Footprint calculation | 8081 (debug) | yes |
| `cap-factors` | `factor-api` | Emission factor lookup | 8082 (debug) | yes |
| `cap-factors` | `factor-db` | Postgres, factor store | none | **no** |
| `obs` | `otel-collector` | OTLP ingest | none | yes |
| `ashs` | `ashs-control` | Agents, tool gateway, telemetry sink | 8090 | yes |
| `ashs` | `ashs-ui` | Demo dashboard | 3001 | no |
| `ashs` | `ashs-db` | Postgres, telemetry + episodes | none | **no** |
| `ashs` | `traffic-gen` | Synthetic baseline traffic | none | yes |

Ten containers. Cross-stack calls use service DNS on the mesh; host ports exist only for the browser and debugging.

### 3.2 The one networking rule

Every cross-stack dependency URL is supplied through an environment variable with a sensible single-host default:

```
CALC_API_URL=http://calc-api:8080      # in cfc-product
FACTOR_API_URL=http://factor-api:8080  # in cap-calc
```

**No service name is ever hardcoded in application code.** Held to that one rule, moving a capability to another machine — or to a Kubernetes namespace — is a config change, not a code change.

### 3.3 Repository layout

```
ashs/
├── Makefile                        # the entire demo operator interface
├── docker-compose.mesh.yml         # shared network
├── services/
│   ├── cfc-product/                # git repo — Product Repair Agent's write scope
│   │   ├── api/                    # FastAPI BFF
│   │   ├── web/                    # React + Vite
│   │   └── docker-compose.yml
│   ├── cap-calc/                   # git repo — Capability Repair Agent's write scope
│   ├── cap-factors/                # git repo — Capability Repair Agent's write scope
├── platform/
│   ├── control/                    # ashs-control: agents, gateway, policy, sink
│   ├── ui/                         # ashs-ui: dashboard
│   ├── catalog/services.yaml       # ownership + dependency graph
│   ├── policy/policy.yaml          # allowlists, protected zones, budgets
│   └── obs/otel-collector.yaml
└── scripts/                        # scenario drivers, reset, seed
```

Each `services/*` directory is a **real git repository**. This is what agents branch, commit to, and what the dashboard diffs. Repos are mounted into `ashs-control`; the tool gateway enforces which agent may write to which path.

---

## 4. Application requirements

### 4.1 Product — `cfc-product`

A single page, no login. The user enters activity data for a period, submits, sees their footprint.

**Inputs:** electricity (kWh + region), heating (gas m³ / LPG kg / oil litres), road travel (km by petrol, diesel, electric, bus, two-wheeler), rail (km), air (short/medium/long-haul flight counts), diet (one of six), waste (kg/month + recycling %).

**Outputs:** total kg CO2e for the period and annualised tonnes; breakdown by category with each category's share; comparison against a regional average and a 1.5°C-aligned target; three ranked reduction suggestions; **and the emission factor dataset version, displayed on screen.**

`cfc-api` endpoints:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/footprint/calculate` | Main flow. Validates, calls `calc-api`, shapes for UI. |
| GET | `/api/footprint/{calculation_id}` | Retrieve a previous result |
| GET | `/api/regions` | Region dropdown, proxied from `factor-api` |
| GET | `/api/health` | Liveness + downstream dependency status |
| GET | `/openapi.json` | Published contract, generated from code |
| GET/POST | `/_demo/bug` | Read / set runtime bug flags (demo only) |

**Session handling:** no accounts. A calculation is identified by a server-generated `calculation_id`; the browser keeps the last few in `localStorage`. Nothing is attributable to a person, so evidence bundles may carry full request and response payloads — which considerably simplifies the agent design, and is a thing that will change in any real deployment.

### 4.2 Capability — `cap-calc`

`calc-api`, synchronous calculation:

```
POST /v1/calculate
{ "period": "monthly", "region": "IN-KA",
  "activities": { "electricity_kwh": 320, "petrol_car_km": 800, "diet": "medium_meat" } }

200 OK
{ "calculation_id": "calc_01J8X...", "total_kgco2e": 412.7, "period": "monthly",
  "breakdown": [ {"category": "electricity", "kgco2e": 262.4, "share": 0.636} ],
  "factor_dataset_version": "2026.1", "computed_at": "2026-08-12T09:14:02Z" }
```

**Contract rules the attribution engine leans on:**

- `total_kgco2e` is a required number. Never null, never a string.
- `breakdown` is an array that **may be empty** when all activity inputs are zero. This is legal, and Scenario 1 depends on it being legal.
- `share` is a number in `[0, 1]`.
- `factor_dataset_version` is present on every response.

Also: `GET /v1/calculations/{id}`, `GET /health`, `GET /openapi.json`, `GET|POST /_demo/bug`.

### 4.3 Capability — `cap-factors`

`factor-api`, source of truth for emission coefficients:

```
GET /v1/factors?region=IN-KA&activity=electricity_grid&year=2026

200 OK
{ "activity": "electricity_grid", "region": "IN-KA", "year": 2026,
  "factor": 0.82, "unit": "kgCO2e/kWh",
  "source": "CEA CO2 Baseline Database v20", "dataset_version": "2026.1" }
```

**Contract rules:**

- `factor` is a required number ≥ 0. Never null, never a string.
- An unknown region returns `404` with a typed error body. It does **not** silently return a default.
- `dataset_version` is present on every response.

Also: `GET /v1/factors/bulk` (what `calc-api` uses on every calculation), `GET /v1/regions`, `GET /health`, `GET /openapi.json`, `GET|POST /_demo/bug`.

Seed data: ~6 regions × ~15 activities, Postgres, seeded on first start from a SQL file in the repo, so reset is a volume delete.

### 4.4 Cross-cutting requirements

Every service satisfies all of these. Retrofitting them is painful; the pipeline does not work without them.

**Telemetry.** OpenTelemetry SDK, exporting traces, metrics, and logs over OTLP to the collector. Trace context propagated on every cross-stack call — one calculation produces **one trace** spanning `cfc-web → cfc-api → calc-api → factor-api`. Span attributes must include `service.name`, `service.version`, `deployment.stack`, and **`code.repository`** — the last is what lets the supervisor map a failing span to a repo without a lookup table. Errors recorded as span events with exception type and stack trace, not just a status code.

**Structured logging.** JSON only. Required on every line: `timestamp`, `level`, `service`, `version`, `trace_id`, `span_id`, `message`. Error lines add `error.type`, `error.message`, `error.stack`.

**Contract publication.** Every service serves its OpenAPI document at `/openapi.json`, **generated from code**, never hand-maintained. The attribution engine validates real responses against these documents; a stale hand-written spec would silently break the entire attribution model.

**Evidence capture.** `cfc-api` and `calc-api` record the raw downstream response body when a call fails, attach it to the span, and keep it retrievable for a short window. Without the actual payload, contract validation has nothing to validate.

**Bug injection.** Every service exposes `POST /_demo/bug` taking `{"name": "...", "enabled": true}`, applied at runtime with no restart. Flags default to off and reset on container restart. Available names are listed in each repo's README and readable via `GET /_demo/bug`. The endpoint is behind an env var that is off by default, stated in the code so nobody has to guess later.

---

## 5. Platform architecture

Control plane / data plane. Application services are ordinary workloads. `ashs-control` observes them and performs only explicitly permitted actions.

```
┌──────────────────────────────────────────────────────────────┐
│                      ashs-control                            │
│                                                              │
│  Telemetry Sink  ──▶  ashs-db (spans, logs, metrics)         │
│         │                                                    │
│         ▼                                                    │
│  Detector ──▶ Incident (fingerprint, dedupe)                 │
│         │                                                    │
│         ▼                                                    │
│  Evidence Builder ──▶ bounded Diagnosis Context              │
│         │                                                    │
│         ▼                                                    │
│  ┌─────────────────────────────────────────────────┐         │
│  │  ATTRIBUTION ENGINE — 100% deterministic        │         │
│  │  1. trace walk   2. contract validation         │         │
│  │  3. deploy correlation  4. blast radius         │         │
│  │  → fault_domain + confidence + evidence refs    │         │
│  └────────────────────┬────────────────────────────┘         │
│                       ▼                                      │
│  Diagnosis Agent (Claude) — narrative + fix strategy ONLY    │
│                       ▼                                      │
│  Router ──▶ Product Repair Agent | Capability Repair Agent   │
│                       ▼                                      │
│  Repair Planner ──▶ typed plan                               │
│                       ▼                                      │
│  ┌─────────────────────────────────────────────────┐         │
│  │  POLICY ENGINE — deterministic, code-enforced   │         │
│  │  action allowlist · path allowlist · protected  │         │
│  │  zone · confidence floor · change budget        │         │
│  └────────────────────┬────────────────────────────┘         │
│                       ▼                                      │
│  ┌─────────────────────────────────────────────────┐         │
│  │  TOOL GATEWAY — the only path to any mutation   │         │
│  │  typed in/out · actor-scoped · every call       │         │
│  │  written to the audit log                       │         │
│  └────────────────────┬────────────────────────────┘         │
│                       ▼                                      │
│  Test Gate ──▶ Deploy ──▶ Validation Agent                   │
│                       ▼                                      │
│  RESOLVED  |  ROLLBACK ──▶ ESCALATED                         │
│                                                              │
│  Audit Store (every transition, prompt, tool call, diff)     │
└──────────────────────────────────────────────────────────────┘
```

### 5.1 Task state model

A repair task is a persisted state machine, not conversation history:

```
NEW → TRIAGED → EVIDENCE_READY → DIAGNOSIS_READY → ROUTED
    → PLAN_READY → POLICY_APPROVED → EXECUTING → VALIDATING → RESOLVED

VALIDATING        → ROLLBACK → ESCALATED
DIAGNOSIS_READY   → ESCALATED   (confidence below floor)
PLAN_READY        → ESCALATED   (policy blocks autonomous execution)
```

Implemented as an explicit Python state machine (~200 lines), not a framework. Every transition is persisted and streamed to the dashboard over SSE. LangGraph is a drop-in replacement later if durable multi-hour runs are needed; it buys nothing for a demo and costs clarity.

### 5.2 Attribution — how it actually works

Run in order, combine into a confidence score. **The language model is never asked to guess the owner from a stack trace.**

1. **Trace walk.** Deepest span in the failing trace with error status. Its owning service is suspect #1. Deterministic, cheap, usually right.
2. **Contract validation.** The decisive test. Validate the actual payload a capability returned against its published schema:
   - Payload invalid → fault is the capability. High confidence.
   - Payload valid and the Product still failed → **fault is the Product.** High confidence. *(This is the Scenario 1 branch.)*
   - No contract available → drop confidence, weight other signals.
3. **Deploy correlation.** Which services shipped in the window before first occurrence. A single candidate raises confidence sharply and suggests revert as a valid fix.
4. **Blast radius.** Only this consumer affected, or every consumer of the capability? Many consumers failing points at the capability regardless of what the trace says.
5. **Model judgement, last.** Claude gets the assembled evidence and produces the root-cause narrative and fix strategy. The model explains and decides *how* to fix. The deterministic layers decide *who owns it*.

**Routing on confidence, not certainty:**

| Confidence | Behavior |
|---|---|
| > 0.85 | Act at the service's configured autonomy level |
| 0.60 – 0.85 | Diagnosis + draft patch, human review required regardless of level |
| < 0.60 | Escalate with the evidence bundle. Do not guess. |

### 5.3 Autonomy ladder

A dial, not a binary. Each service sits at its own level, declared in `catalog/services.yaml`.

| Level | Agent behavior |
|---|---|
| L0 Observe | Detect and fingerprint. Nothing more. |
| L1 Diagnose | Root-cause hypothesis, evidence bundle, attribution. |
| L2 Propose | Branch + commit with a failing-then-passing test. |
| L3 Ship | Merge and deploy autonomously after verification passes. |
| L4 Prod | Deploy with canary and automatic rollback. |

**v1 runs `cfc-product` at L3.** The kill switch drops every service to L0 in one flag, and it is tested in the demo.

### 5.4 Guardrails

| Guardrail | Implementation |
|---|---|
| Path allowlist | Agents may only touch declared paths. Enforced in the tool gateway, **never by prompt**. |
| Protected zone | Emission factors, calculation formulas, migrations, compose files, CI config. Always human. |
| Test gate | Patch rejected unless it adds a test that **fails on the pre-patch commit** and passes after. |
| Change budget | Max autonomous merges per service per day. Exceeding it trips a circuit breaker. |
| Loop breaker | Two failed verification attempts on the same fingerprint ends the run and escalates. |
| Fingerprint dedupe | One open repair per incident fingerprint. Prevents storms during an outage. |
| No self-modification | Agents cannot modify `platform/**`. |
| Signed audit trail | Every decision, prompt, tool call, and diff appended with the evidence that justified it. |
| Kill switch | One flag drops the whole system to L0. |

Protected paths, declared in `policy/policy.yaml`:

```
cap-factors/data/**
cap-factors/src/factors/coefficients.*
cap-calc/src/calc/formulas.*
**/migrations/**
**/docker-compose*.yml
platform/**
```

### 5.5 Tool contract

The LLM gets **no raw shell, no kubectl, no unrestricted git.** Typed tools only, each with explicit input/output models, actor scoping, and an audit entry per call.

| Tool | Purpose |
|---|---|
| `get_service_health(service)` | Liveness + dependency status |
| `get_recent_logs(service, window)` | Correlated structured logs |
| `get_trace(trace_id)` | Full span tree |
| `get_contract(service)` | Published OpenAPI document |
| `validate_against_contract(service, payload)` | The decisive attribution test |
| `get_deployment_history(service)` | Version timeline |
| `get_recent_changes(service)` | Commits in the incident window |
| `read_source(repo, path)` | Read-only, allowlist-scoped |
| `run_diagnostic(check_id, target)` | Read-only hypothesis verification |
| `create_patch(repo, change_spec)` | Write, allowlist-scoped, returns a diff |
| `run_tests(repo, selector, at_commit)` | Test gate — supports pre-patch runs |
| `deploy_candidate(service, ref)` | Commit + hot reload |
| `run_validation(suite, target)` | Replay the failing journey |
| `rollback(service, ref)` | Restore known-good |
| `open_escalation(incident, diagnosis)` | Human-readable handoff |

### 5.6 LLM integration

Two calls per episode, both narrow:

1. **Diagnosis narrative** — evidence in, root-cause explanation and fix strategy out. Structured output.
2. **Patch generation** — evidence plus the relevant source file in, unified diff plus a test out. Streaming.

```python
# platform/control/llm/client.py — the only place a provider appears
from anthropic import AnthropicBedrockMantle

client = AnthropicBedrockMantle(aws_region=os.environ["AWS_REGION"])

resp = client.messages.create(
    model="anthropic.claude-opus-5",       # note: Bedrock IDs take the anthropic. prefix
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},
    system=DIAGNOSIS_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": evidence_bundle}],
)
```

**Provider switch.** `LLM_PROVIDER=bedrock|anthropic` selects the client; nothing else in the codebase knows which is in use.

**Auth: standard AWS credentials (SigV4). This is the chosen path.**

```yaml
# services/ashs/docker-compose.yml
ashs-control:
  environment:
    AWS_REGION:            ${AWS_REGION}
    AWS_ACCESS_KEY_ID:     ${AWS_ACCESS_KEY_ID}
    AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY}
    AWS_SESSION_TOKEN:     ${AWS_SESSION_TOKEN}   # empty for long-lived IAM keys
```

`AnthropicBedrockMantle()` resolves these itself through the standard AWS credential chain — env vars, then `~/.aws/credentials` profile, then assumed role, then instance metadata. **Zero credential-handling code in ASHS.** Temporary STS/SSO credentials work identically; the session token is just a third variable.

Why this over a Bedrock API key: the bearer-token key is newer, and whether this client honors `AWS_BEARER_TOKEN_BEDROCK` would need a live check against the account — with boto3 as the fallback if it doesn't, which means custom transport code we don't want. SigV4 is the SDK's native path and needs nothing.

⚠️ **One operational catch:** STS/SSO session credentials expire, typically in 1–12 hours. If the demo laptop uses SSO, re-export before each rehearsal or the LLM calls start returning `ExpiredToken` mid-demo. For a demo machine, a long-lived IAM user key scoped to `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the Claude model ARN removes that failure mode entirely. Either way, ASHS degrades gracefully — see *Failure behavior* below.

**Bedrock feature notes.** Adaptive thinking, `effort`, prompt caching, and streaming are all available. Do not pass `temperature`, `top_p`, `top_k`, or `budget_tokens` — all return 400 on Opus 5.

**Failure behavior.** If the LLM is unreachable, the episode escalates with the deterministic diagnosis intact rather than stalling. Attribution, policy, and the test gate are unaffected — they never call the model. This is also the demo's safety net: if the network dies mid-presentation, the loop still runs to `ESCALATED` and the attribution story, which is the real point, still lands.

---

## 6. Dashboard

The single surface projected during the demo. The agent's reasoning on screen is more convincing than the fixed application.

Four panes:

1. **User journey** — the calculator, live, in an iframe. The audience sees the failure and the recovery as a user would.
2. **Health** — request rate and error rate per service, sparkline over the last 5 minutes. The spike is the hook.
3. **Episode feed** — the live event stream. Each state transition as a card: what happened, the evidence that justified it, the confidence score, and the elapsed time. **This is the pane that wins the room.**
4. **Artifacts** — the assembled evidence bundle, the LLM's diagnosis narrative, the generated diff with syntax highlighting, and the test output showing red-then-green.

Plus a persistent header: current autonomy level, kill switch, and elapsed time since incident detection.

Everything streams over SSE from `ashs-control`. No polling, no refresh.

---

## 7. Demo operability

Non-negotiable, because a demo that cannot be reset cannot be rehearsed.

| Requirement | Target |
|---|---|
| Cold start | Under 90 seconds on a 16 GB laptop |
| Full reset to clean state | Under 30 seconds, single `make reset` |
| External dependencies | None except the model API. Images pre-pulled, factor data seeded from the repo. |
| Repeatability | `make demo-scenario-1` sets flags, generates traffic, and drives the scenario |

```
make up               # bring up all stacks
make seed             # seed factor-db, init service git repos
make demo-scenario-1  # inject bug, drive traffic, watch it heal
make reset            # tear down volumes, reseed, reset git repos to base commit
make down
```

`make reset` must restore the service git repos to their base commit, not just the databases — otherwise the second run of the demo starts from an already-patched product.

**Rehearsal rule: record a clean run as a fallback. Always.**

---

## 8. Acceptance criteria

Scenario 1 is done when all of these hold, repeatedly, from a clean reset:

- [ ] The demo detects a user-visible failure with no human inspecting logs.
- [ ] The agent identifies the defect as belonging to the Product, not a Capability.
- [ ] **Both capabilities are exonerated on objective evidence** — the dashboard shows the contract validation that ruled them out.
- [ ] An evidence-backed diagnosis is produced before any mutation.
- [ ] Only an allowlisted remediation is executed.
- [ ] The patch is accompanied by a test that fails pre-patch and passes post-patch, and the run is rejected if it is not.
- [ ] The repair is observable end to end in the dashboard, by incident ID.
- [ ] The user journey becomes healthy after remediation — verified by replaying the original failing request.
- [ ] A forced validation failure triggers rollback to the known-good state.
- [ ] The kill switch drops the system to L0 and is demonstrated.
- [ ] Every action is auditable by incident ID.
- [ ] `make reset` returns the system to a clean state in under 30 seconds, and the demo runs again identically.

### 8.1 Metrics instrumented from day one

Time to detect, time to diagnose, time to repair, root-cause identification accuracy, correct-owner routing rate, autonomous repair success rate, rollback success rate, unsafe-action rejection rate, escalation rate, evidence-to-action traceability.

Be explicit with any audience: **these numbers come from injected bugs.** Real telemetry is noisier by orders of magnitude. A shadow-mode run against real production signals is the prerequisite for claiming anything beyond "the control model works."

---

## 9. Implementation plan

Five phases. Phase 0 is where the demo is won or lost — it is boring and it is the foundation.

### Phase 0 — Harness (no agents)

Docker Compose skeleton across five stacks. All four application services running, wired end to end, with real OpenTelemetry producing a single trace across all four hops. Collector ingesting to Postgres. `/openapi.json` generated from code on every service. `/_demo/bug` runtime flags. Factor DB seeded. Service git repos initialised. Traffic generator producing a green baseline. `make up` / `make reset` working to target.

*Exit test:* one calculation from the browser produces one trace with four spans, visible in `ashs-db`, and `make reset` completes in under 30 seconds.

### Phase 1 — Detect and attribute (L1)

Telemetry sink and query layer. Detector with fingerprinting and dedupe. Evidence Builder producing a bounded Diagnosis Context. **The attribution engine: trace walk, contract validation, deploy correlation, blast radius, confidence scoring.** Service catalog and dependency graph. Incident state machine through `DIAGNOSIS_READY`.

*Exit test:* `zero_total_division` is injected; the system independently produces `fault_domain: cfc-product, confidence: 0.94` with the contract-validation evidence that exonerates both capabilities. **No LLM involved yet.** This phase is independently valuable — if the project were cut here, it is still a better triage system than most organisations run.

### Phase 2 — Reason and plan

LLM client module with the Bedrock provider switch, verified against the target AWS account. Diagnosis Agent producing the narrative from the evidence bundle. Router. Repair Planner emitting typed plans. Policy engine with allowlists, protected zones, confidence floor, and change budget. Tool gateway with the full typed tool set, actor scoping, and audit logging.

*Exit test:* a complete, human-readable repair plan reaches `POLICY_APPROVED` with a full audit trail — and a deliberately out-of-scope target is correctly blocked by policy.

### Phase 3 — Repair and verify

Product Repair Agent: patch generation, the test gate (pre-patch failure required), commit, hot-reload deploy. Validation Agent replaying the original failing request and checking error-rate recovery. Rollback path. Escalation path. Loop breaker and kill switch.

*Exit test:* Scenario 1 runs end to end from injection to `RESOLVED`, and a forced validation failure rolls back cleanly.

### Phase 4 — Dashboard and demo hardening

The four-pane dashboard with SSE streaming. Metrics collection. `make demo-scenario-1`. Reset reliability under repeated runs. Rehearsal, timing, and a recorded fallback run.

*Exit test:* three consecutive clean runs from reset, each under the target time, with no manual intervention.

### 9.1 What the deferred scenarios would cost

Stated now so the decision stays available:

- **Scenario 2 (capability owns it, two hops down).** Flag `null_factor_new_region` on `factor-api`. `factor-api` returns `"factor": null`, violating its own schema; `calc-api` 500s; `cfc-api` shows a generic failure. Naive attribution blames `calc-api`, and a naive agent would wrap the null in a guard there — hiding a real data defect. Correct attribution walks to the deepest error span, validates the `factor-api` response, finds it invalid, and routes to `cap-factors`. **Cost: ~2 days** — the attribution engine already does this; it needs the bug flag, a Capability Repair Agent registration, and a contract test asserting non-null factors for every seeded region. This is the highest-value increment available.
- **Scenario 3 (refuse to act).** Flag `wrong_grid_factor` — a plausible but incorrect electricity coefficient. Totals shift ~40%. No errors, no exceptions; detection comes from a plausibility check on the output distribution, not an error signal. Attribution correctly identifies `cap-factors`; **policy then blocks the run**, because emission factor values are in the protected zone. The reason is one any audience accepts without explanation: these numbers end up in disclosures. A machine may flag that they look wrong. A machine does not get to quietly change what a customer reports as their carbon output. The system produces a full diagnosis, a draft change, and a page to a human. It changes nothing. **Cost: ~2 days** — mostly the distribution-based detector, since the policy engine already exists.
- **Scenario 4 (rollback).** Already built as the failure path in Phase 3; making it a *staged* demo beat is a script, not a feature. **Cost: hours.**

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Confidently wrong attribution — the worst outcome politically | Deterministic layers decide ownership; the model only narrates. Confidence is surfaced, never hidden. Attribution accuracy tracked per run. |
| A patch that passes tests and is still wrong | Validation replays the real failing request, not just unit tests. The test must assert correct behavior, not absence of an exception. |
| Agents fixing symptoms rather than causes (wrapping everything in try/except) | The test gate requires an assertion of correct behavior. Human review sampling of merged autonomous fixes. |
| The demo works and production would not — real telemetry is noisier by orders of magnitude | Be explicit that these numbers come from injected bugs. Never claim more than the control model. |
| Live demo failure — model API unreachable | Deterministic attribution runs without the model; episode escalates cleanly with the diagnosis intact. Plus: a recorded clean run, always. |
| Reset drift across repeated runs | `make reset` restores git repos to base commit, not just volumes. Verified by three consecutive clean runs in Phase 4. |

---

## 11. Settled decisions

Recorded so they are not relitigated mid-build.

| Decision | Choice | Why |
|---|---|---|
| Backend language | Python + FastAPI across all four services | One language keeps agent tooling simple, which matters more than proving language-independence here |
| Frontend | React + Vite | Small; three views |
| LLM | Claude Opus 5 via AWS Bedrock (`anthropic.claude-opus-5`) | User has Bedrock access; provider is behind one module |
| Bedrock auth | Standard AWS credentials (SigV4): access key + secret + optional session token | Native SDK credential chain, zero custom code. Bedrock API keys would need a live compatibility check and possibly a boto3 fallback. |
| LLM role | Narrative and patch only. Never attribution, routing, or policy. | The whole credibility argument |
| Orchestration | Explicit Python state machine | Fewer moving parts; every transition trivially persisted and streamed. LangGraph is a later swap. |
| Fix delivery | Local git repos + dashboard diff | ~90% of Gitea's visual payoff for ~60% of the work |
| Observability | Real OTel SDK + collector → Postgres → custom dashboard | Four fewer containers, faster cold start, one UI to project |
| Knowledge store | Postgres. No pgvector. | Semantic retrieval buys nothing at this scale |
| Scenarios in v1 | Scenario 1 only, architecture ready for 2 and 3 | One scenario that works beats three that half-work |
