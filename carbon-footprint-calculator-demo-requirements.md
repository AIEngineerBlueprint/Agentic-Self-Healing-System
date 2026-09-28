# Carbon Footprint Calculator

**Demo application requirements for the autonomous bug fixing pipeline**

Version 0.1 · Draft for review · Companion to the agentic pipeline proposal

---

## 1. Why this application

The demo application exists to break in interesting ways. That is its actual job. Everything below is chosen so the agentic pipeline has something realistic to detect, attribute, and fix, while staying small enough to run on a laptop and reset between demo runs.

A carbon footprint calculator suits this well for three reasons. The domain splits naturally into a thin product and genuinely reusable capabilities, so the ownership boundary is real and not invented. The calculation chain is two hops deep, which lets the demo show that the service where an error surfaces is not the service that owns the fault. And the domain has an obvious protected zone, the emission factors themselves, which gives the refuse to act scenario a reason a business person understands immediately.

---

## 2. Topology

Docker Compose, one stack per ownership boundary. Each stack is a separate Compose project with its own name and its own internal network. A single shared external network carries cross stack traffic.

```
                    ┌─────────────────────────────────────┐
                    │  STACK: cfc-product                 │
                    │  ┌───────────┐   ┌───────────────┐  │
   browser ────────▶│  │ cfc-web   │──▶│ cfc-api (BFF) │  │
                    │  │ React     │   │               │  │
                    │  └───────────┘   └───────┬───────┘  │
                    └──────────────────────────┼──────────┘
                                               │
                          ─────────────────────┼──────────────── agentic-mesh
                                               │
                    ┌──────────────────────────▼──────────┐
                    │  STACK: cap-calc                    │
                    │  ┌────────────┐  ┌───────────────┐  │
                    │  │ calc-api   │  │ calc-worker   │  │
                    │  └─────┬──────┘  └───────┬───────┘  │
                    │        │      ┌──────────▼───────┐  │
                    │        │      │ calc-redis       │  │
                    │        │      └──────────────────┘  │
                    └────────┼────────────────────────────┘
                             │
                    ┌────────▼────────────────────────────┐
                    │  STACK: cap-factors                 │
                    │  ┌────────────┐  ┌───────────────┐  │
                    │  │ factor-api │──│ factor-db     │  │
                    │  └────────────┘  │ postgres      │  │
                    │                  └───────────────┘  │
                    └─────────────────────────────────────┘

   supporting stacks: obs (collector, loki, tempo, prometheus, grafana)
                      agents (supervisor, product agent, capability agents)
                      scm (gitea, ci runner)
```

### 2.1 Network rules

Create the shared network once, outside any stack:

```
docker network create agentic-mesh
```

Every stack declares it as `external: true`. Only services that need cross stack traffic join it. `factor-db` and `calc-redis` stay on their stack internal network only, which means they are unreachable from the product by design. This is the Compose equivalent of the namespace isolation in the original proposal, and it matters because it makes the ownership boundary enforceable rather than decorative.

Cross stack calls use service DNS names on the shared network, for example `http://calc-api:8080`. No host ports are used for service to service traffic. Host ports exist only for the browser and the demo dashboards.

### 2.2 Stack and port map

| Stack | Service | Role | Host port | Joins mesh |
|---|---|---|---|---|
| `cfc-product` | `cfc-web` | React frontend, nginx | 3000 | no |
| `cfc-product` | `cfc-api` | Backend for frontend | 8000 | yes |
| `cap-calc` | `calc-api` | Footprint calculation, sync | 8081 (debug only) | yes |
| `cap-calc` | `calc-worker` | Batch recalculation, async | none | yes |
| `cap-calc` | `calc-redis` | Queue and cache | none | no |
| `cap-factors` | `factor-api` | Emission factor lookup | 8082 (debug only) | yes |
| `cap-factors` | `factor-db` | Postgres, factor store | none | no |
| `obs` | grafana | Demo visuals | 3001 | yes |
| `agents` | `supervisor` | Triage, attribution, routing | 8090 | yes |
| `scm` | `gitea` | Repositories and pull requests | 3002 | yes |

### 2.3 Repository layout

One repository per ownership boundary. This is what the agents branch and open pull requests against, so it has to be right from the start.

```
cfc-product/     frontend + BFF + compose file
cap-calc/        calc-api + calc-worker + compose file
cap-factors/     factor-api + seed data + compose file
platform-autofix/  agents, policy, service catalog
```

---

## 3. Product requirements

### 3.1 What the user does

A single page, no login, no saved account. The user fills in activity data for a household or a small business over a chosen period, submits, and sees their footprint.

**Input categories**

| Category | Inputs |
|---|---|
| Electricity | kWh consumed, region |
| Heating | natural gas m³, LPG kg, or heating oil litres |
| Road travel | km by petrol car, diesel car, electric car, bus, two wheeler |
| Rail travel | km |
| Air travel | number of short haul, medium haul, and long haul flights |
| Diet | one of high meat, medium meat, low meat, pescatarian, vegetarian, vegan |
| Waste | kg per month, recycling percentage |

**Output**

- Total footprint in kg CO2e for the period, and the annualised equivalent in tonnes.
- Breakdown by category with each category's share of the total.
- Comparison against a regional average and against a 1.5 degree aligned target.
- Three ranked reduction suggestions with estimated savings.
- The emission factor dataset version used, displayed on screen. This matters, because the demo needs the audience to see when factors change.

### 3.2 Product API

`cfc-api` exposes these. The frontend talks only to this service, never directly to a capability.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/footprint/calculate` | Main flow. Validates input, calls `calc-api`, shapes the response for the UI |
| GET | `/api/footprint/{calculation_id}` | Retrieve a previous result |
| GET | `/api/regions` | Populate the region dropdown, proxied from `factor-api` |
| GET | `/api/health` | Liveness plus downstream dependency status |
| GET | `/openapi.json` | Published contract |
| POST | `/_demo/bug` | Runtime bug injection, demo only |

### 3.3 Session handling without authentication

No accounts, no login, no personal data. A calculation is identified by a server generated `calculation_id` returned on submit. The browser keeps the last few ids in `localStorage` so the user can revisit results within the same browser. Nothing is attributable to a person.

Consequences to be explicit about:

- All service APIs are open on the mesh network. There is no service to service authentication.
- CORS on `cfc-api` is open to the frontend origin only, which is the one restriction worth keeping because a broken CORS config is a fun bug but a boring one.
- No input is treated as sensitive, so evidence bundles can carry full request and response payloads. This considerably simplifies the agent design and is worth noting as a thing that will change later.
- The refuse to act scenario in the pipeline proposal was built around an authentication path. That scenario has to move. Section 6.3 replaces it.

---

## 4. Capability requirements

### 4.1 cap-calc, the calculation capability

Two services, which satisfies the one or more services per capability requirement and gives the demo a service that is not directly reachable from the product.

**`calc-api`**, synchronous calculation.

```
POST /v1/calculate
{
  "period": "monthly",
  "region": "IN-KA",
  "activities": {
    "electricity_kwh": 320,
    "petrol_car_km": 800,
    "rail_km": 120,
    "flights_short_haul": 1,
    "diet": "medium_meat",
    "waste_kg": 40
  }
}

200 OK
{
  "calculation_id": "calc_01J8X...",
  "total_kgco2e": 412.7,
  "period": "monthly",
  "breakdown": [
    {"category": "electricity", "kgco2e": 262.4, "share": 0.636},
    {"category": "road_travel", "kgco2e": 148.0, "share": 0.359}
  ],
  "factor_dataset_version": "2026.1",
  "computed_at": "2026-08-12T09:14:02Z"
}
```

Contract rules that the attribution engine will lean on:

- `total_kgco2e` is a required number, never null, never a string.
- `breakdown` is an array that may be empty when all activity inputs are zero. This is legal, and the demo depends on it being legal.
- `share` is a number between 0 and 1 inclusive.
- Every response carries `factor_dataset_version`.

Other endpoints: `GET /v1/calculations/{id}`, `GET /health`, `GET /openapi.json`, `POST /_demo/bug`.

**`calc-worker`**, asynchronous recalculation. Consumes a `recalc` queue from `calc-redis`. Its job is to recompute stored calculations when the factor dataset version changes. It exists so the demo has an async failure path where the error never surfaces in a user request, which is a genuinely different detection problem and worth showing if time allows.

### 4.2 cap-factors, the emission factor capability

**`factor-api`**, the source of truth for emission coefficients.

```
GET /v1/factors?region=IN-KA&activity=electricity_grid&year=2026

200 OK
{
  "activity": "electricity_grid",
  "region": "IN-KA",
  "year": 2026,
  "factor": 0.82,
  "unit": "kgCO2e/kWh",
  "source": "CEA CO2 Baseline Database v20",
  "dataset_version": "2026.1"
}
```

Also `GET /v1/factors/bulk` for the batch lookup `calc-api` uses on every calculation, `GET /v1/regions`, `GET /health`, `GET /openapi.json`, `POST /_demo/bug`.

Contract rules:

- `factor` is a required number greater than or equal to zero. Never null, never a string.
- An unknown region returns 404 with a typed error body, it does not return a default factor silently.
- `dataset_version` is required on every response.

Seed data covers roughly six regions and fifteen activities. Postgres, seeded on first start from a SQL file in the repository so a reset is a volume delete.

### 4.3 Optional third capability

`cap-report` producing a shareable summary, historical trend, and reduction recommendations. Add it only if the first two are working. It is the natural place to demo a capability that fails partially rather than completely, where the report renders but one section is empty.

---

## 5. Cross cutting requirements

Every service in every stack must satisfy these. The pipeline does not work without them, and retrofitting them is painful.

### 5.1 Telemetry

- OpenTelemetry SDK instrumented, exporting traces, metrics, and logs to the collector in the `obs` stack over OTLP.
- Trace context propagated on every cross stack call. A single calculation must produce one trace spanning `cfc-web`, `cfc-api`, `calc-api`, and `factor-api`.
- Span attributes must include `service.name`, `service.version`, `deployment.stack`, and `code.repository`. The last one is what lets the supervisor map a failing span to a repository without a lookup table.
- Errors recorded as span events with the exception type and stack trace, not just a status code.

### 5.2 Structured logging

JSON only. Required fields on every line: `timestamp`, `level`, `service`, `version`, `trace_id`, `span_id`, `message`. Error lines add `error.type`, `error.message`, `error.stack`.

### 5.3 Contract publication

Every service serves its OpenAPI document at `/openapi.json`, and that document is generated from the code rather than maintained by hand. The attribution engine validates real responses against these documents, so a stale hand written spec would silently break the whole attribution model.

### 5.4 Evidence capture

`cfc-api` and `calc-api` must record the raw downstream response body when a call fails, attach it to the span, and keep it retrievable for a short window. Without the actual payload, contract validation has nothing to validate.

### 5.5 Bug injection

Every service exposes `POST /_demo/bug` taking `{"name": "...", "enabled": true}` and applies it at runtime with no restart. Flags default to off and reset on container restart. Available flag names are listed in each repository's README and are also readable via `GET /_demo/bug`.

This endpoint is demo only. It is behind an env var that is off by default and it never ships beyond the demo, which is worth stating in the code so nobody has to guess later.

### 5.6 Demo operability

- Full stack cold start under 90 seconds on a 16 GB laptop.
- Full reset to a clean state in under 30 seconds, via a single `make reset` that tears down volumes and reseeds.
- No external network dependency except the model API. Images pre pulled, factor data seeded from the repository.
- A `make demo-scenario-1` style target per scenario that sets the right flags and generates the traffic.
- Synthetic traffic generator producing a low baseline of successful calculations, so error rate graphs have something to spike against. A flat zero line is not convincing.

Multi host readiness. The demo targets a single Docker engine, but nothing in the design should assume it. Every cross stack dependency URL must be supplied through an environment variable with a sensible single host default, for example CALC_API_URL=http://calc-api:8080 in cfc-product and FACTOR_API_URL=http://factor-api:8080 in cap-calc. No service name is ever hardcoded in application code. Held to that one rule, moving a capability onto a second machine is a config change rather than a code change: publish the relevant service ports on that host, point the caller at the host address, and the system runs unchanged. If the split needs to preserve service DNS and network isolation rather than fall back to IP addresses and published ports, promote agentic-mesh from a bridge network to a Docker Swarm overlay network, which keeps existing service names resolving across both hosts with only minor Compose file changes. Keep the databases and the queue unpublished in either case, since exposing them on a shared network removes the isolation the stack boundaries exist to provide.

---

## 6. Bug scenarios this application must support

These are the reason the application exists. Each maps to a scenario in the pipeline proposal.

### 6.1 Product owns the fault

**Flag** `zero_total_division` on `cfc-api`.

A user submits all zeros. `calc-api` correctly returns `total_kgco2e: 0.0` and an empty `breakdown`. This is contract compliant and correct. `cfc-api` computes each category's percentage by dividing by the total, produces `NaN`, and the chart component throws.

Attribution should exonerate both capabilities on objective evidence, because the `calc-api` response validates cleanly against its published schema. The fix belongs in `cfc-api`, with a test asserting that a zero footprint renders as a zero state rather than an error.

### 6.2 Capability owns the fault, two hops down

**Flag** `null_factor_new_region` on `factor-api`.

`factor-api` starts returning `"factor": null` for the `IN-KA` region, violating its own schema. `calc-api` receives it, produces `total_kgco2e: null` or throws, and returns a 500 to `cfc-api`. The user sees a generic failure.

This is the scenario worth building the demo around. The error surfaces in `cfc-api`. The 500 comes from `calc-api`. Naive attribution blames `calc-api`, and a naive agent would wrap the null in a guard there, which hides a real data defect. Correct attribution walks the trace to the deepest error span, validates the `factor-api` response against the factor contract, finds it invalid, and routes the Incident Package to `cap-factors`.

The fix belongs in `cap-factors`, with a contract test that asserts factors are non null for every seeded region.

A useful variant, flag `factor_as_string`, returns `"factor": "0.82"`. `calc-api` string concatenates instead of adding and produces an absurd total with no error at all. This is the silent corruption case and it shows why error rate alone is not sufficient detection.

### 6.3 The system must refuse to act

**Flag** `wrong_grid_factor` on `factor-api`, which changes the electricity coefficient to a plausible but incorrect value.

Totals shift by around forty percent. No errors, no exceptions. Detection comes from a plausibility check on the calculation output distribution rather than from an error signal.

Attribution correctly identifies `cap-factors`. Policy then blocks the run, because emission factor values and the calculation coefficients are in the protected zone. The reason is one any audience will accept without explanation. These numbers end up in disclosures. A machine may flag that they look wrong. A machine does not get to quietly change what a customer reports as their carbon output.

The system produces a full diagnosis, a draft change, and a page to a human. It changes nothing.

Protected paths to declare in policy:

```
cap-factors/data/**
cap-factors/src/factors/coefficients.*
cap-calc/src/calc/formulas.*
**/migrations/**
**/docker-compose*.yml
**/.gitea/workflows/**
```

### 6.4 Optional, async failure

**Flag** `worker_stale_version` on `calc-worker`. The worker silently skips recalculation when the factor dataset version bumps, so stored calculations drift out of date. Nothing fails in a user request. Detection has to come from a metric, not a log. Build this only if the first three land early.

---

## 7. Explicitly out of scope

Worth writing down so nobody adds them by reflex:

- Authentication, authorization, accounts, sessions, roles. Not in v1.
- Persistence of user data beyond a calculation record with no identity attached.
- Kubernetes. Compose is the target runtime for this demo. The design keeps the boundaries clean enough that a later move to namespaces is mechanical rather than structural.
- Mobile responsive polish beyond making the demo look acceptable on a projector.
- Real emission factor accuracy. The seeded values should be plausible and sourced, but this is not a product.
- Multi language, accessibility compliance, and internationalised units. Metric only.

---

## 8. What to settle before building

1. **Language for each tier.** My default would be Python with FastAPI for `cfc-api`, `calc-api`, `factor-api`, and `calc-worker`, and React with Vite for `cfc-web`. One language across the backend keeps the agent tooling simple, which matters more than variety here. Say if you want a Node or Java service in the mix to prove the agents are not language locked.
2. **Whether `cap-report` is in or out.** It affects the sprint plan by roughly a week.
3. **Gitea or GitHub.** Gitea keeps the demo fully offline. GitHub gives you Actions and a pull request UI everyone recognises, which is worth something in a room.
4. **How much of the reset story matters.** If you will run this live more than once in a session, the `make reset` target is not optional and should be built in Sprint 0.
