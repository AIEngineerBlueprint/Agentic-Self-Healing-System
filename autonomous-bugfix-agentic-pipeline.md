# Autonomous Bug Fixing Across a Product and Its Capabilities

**An agentic pipeline proposal, with a demoable build plan**

Version 0.1 · Draft for review

---

## 1. What this is trying to prove

The pitch is simple to say and hard to build: when the user facing product breaks, a system of agents should work out what actually broke, fix it in the right place, prove the fix works, and leave an audit trail a human can defend in a post incident review.

The hard part is not writing the patch. Coding agents already write decent patches. The hard part is everything around the patch: knowing a real problem occurred, attributing the fault to the correct owner across a service boundary, deciding whether an autonomous change is safe here, verifying the fix did not create a worse one, and stopping cleanly when the system is out of its depth.

This document treats the routing and the guardrails as the product, and the code generation as a commodity step inside it.

---

## 2. Requirement refinement

### 2.1 Vocabulary, fixed for the rest of the document

| Term | Meaning here |
|---|---|
| **Product** | The user facing application. Frontend plus its backend for frontend. Own namespace, own repo, own deploy pipeline. |
| **Capability** | A reusable platform building block. One capability owns one or more services, each in its own namespace. Consumed by the Product over a published contract. |
| **Contract** | The versioned, machine readable interface a capability exposes. OpenAPI for sync, AsyncAPI or schema registry for events. This is the boundary object that makes attribution possible. |
| **Incident** | A deduplicated, fingerprinted cluster of related error signals, not a single log line. |
| **Fault domain** | The service whose behaviour must change for the incident to stop. Not the service where the error surfaced. |
| **Autofix run** | One bounded attempt: diagnose, patch, test, verify, merge or abandon. |

### 2.2 Scope: what "autonomous bug fixing" means for v1

**In scope**

- Runtime defects that surface as errors, exceptions, contract violations, or SLO breaches.
- Deterministic and reproducible failures, meaning the agent can trigger the failure on demand in an isolated environment.
- Single service root causes. One service changes, the incident clears.
- Changes confined to application code, application config, and tests.

**Out of scope for v1, and worth saying so out loud**

- Infrastructure, IAM, network policy, secrets, and database migrations. Too much blast radius for an early autonomy story.
- Multi service coordinated changes, where the Product and a capability must both change in lockstep. The system detects this case and escalates it.
- Performance regressions with no clear error signal.
- Anything touching money movement, authentication and authorization logic, or personal data handling. These always route to a human.
- Data corruption incidents. The fix is usually remediation of data, not code.

### 2.3 The bug classes the demo should target

Ranked by how well they suit autonomous handling:

1. **Contract violation by a capability.** A capability returns a payload that fails its own published schema. Highest confidence attribution available, because the evidence is objective.
2. **Defensive handling gap in the Product.** The capability behaves correctly, including returning empty or null legitimately, and the Product crashes on it. Also high confidence.
3. **Unhandled downstream error state.** A capability returns a documented error code the Product never handled.
4. **Regression correlated to a specific deploy.** Change point in error rate lines up with one release. Attribution is good, the fix may still be a revert rather than a patch.
5. **Config drift.** A value in one environment differs from another and produces the failure.

Anything below this line, treat as escalate only for now.

### 2.4 The autonomy ladder

Do not build a binary between manual and fully autonomous. Build a dial, and let each service sit at a different level. This is what makes the system adoptable inside a real organization, because a capability team can opt in gradually.

| Level | Agent behaviour | Human role |
|---|---|---|
| **L0 Observe** | Detects and fingerprints incidents. Nothing more. | Everything |
| **L1 Diagnose** | Produces root cause hypothesis, evidence bundle, and attribution. | Reads and acts |
| **L2 Propose** | Opens a pull request with a failing test that now passes. | Reviews and merges |
| **L3 Ship to non prod** | Merges and deploys to staging autonomously after verification passes. | Approves prod promotion |
| **L4 Ship to prod** | Deploys behind a flag with canary and automatic rollback. | Notified, can veto |

The demo should show L2, L3, and one L4 with rollback. It should also show a case that deliberately stops at L1 because policy forbids acting.

### 2.5 Assumptions I am making, flag any that are wrong

1. Every capability publishes a versioned contract, or can be made to. Without this, attribution degrades to guesswork.
2. Distributed tracing exists or can be added. Log only correlation is possible but noticeably weaker.
3. Each service maps to exactly one owning repository, and ownership is queryable from a catalog or CODEOWNERS.
4. Ephemeral environments can be spun up per pull request. Verification is meaningless without them.
5. Deployment is GitOps based, so the only path to production is a merged commit. This is what keeps agents out of production directly.
6. The Product and capabilities are owned by different teams. If they are not, federation is over engineering and a single agent will do.

### 2.6 Success criteria for the demo

The demo is a success if a skeptical engineering leader watching it can answer yes to all of these:

- Did the system correctly decide which side owned the bug, and show its evidence?
- Did it refuse to act at least once, for a good reason?
- Could I trace every action back to a human accountable owner?
- Would rolling this out cost me less than the incidents it prevents?

---

## 3. Proposed architecture

### 3.1 Five principles

1. **Agents propose, GitOps disposes.** No agent has write access to a running cluster. Every change is a commit.
2. **Ownership boundaries are hard boundaries.** The Product agent never patches capability code. It files evidence. The capability's own agent decides.
3. **Attribution is a hypothesis with confidence, never an assertion.** Every routing decision carries evidence and a score.
4. **A fix without a failing test is not a fix.** The patch must be accompanied by a test that fails before and passes after.
5. **The system must know how to stop.** Loop detection, change budgets, and policy stops are first class features, not error handling.

### 3.2 Agent topology

Federated, with a thin supervisor. One remediation agent per ownership domain, and a shared triage and routing layer.

```
                        ┌──────────────────────────────────┐
                        │        SIGNAL LAYER              │
                        │  OTel Collector, logs, traces,   │
                        │  metrics, deploy events          │
                        └───────────────┬──────────────────┘
                                        │  incident candidate
                        ┌───────────────▼──────────────────┐
                        │      SUPERVISOR (shared)         │
                        │  ┌────────────────────────────┐  │
                        │  │ Triage: real? severity?    │  │
                        │  │ dedupe, fingerprint        │  │
                        │  ├────────────────────────────┤  │
                        │  │ Attribution: fault domain  │  │
                        │  │ + confidence + evidence    │  │
                        │  ├────────────────────────────┤  │
                        │  │ Policy: may we act here?   │  │
                        │  └────────────┬───────────────┘  │
                        └───────┬───────┴──────────┬───────┘
                     fault=Product          fault=Capability
                                │                  │
                                │        Incident Package (signed)
                                │                  │
          ┌─────────────────────▼──┐   ┌───────────▼─────────────────┐
          │  PRODUCT AGENT         │   │  CAPABILITY AGENT           │
          │  repo: web-product     │   │  repo: cap-pricing          │
          │  reproduce → patch →   │   │  accept / reject / negotiate│
          │  test → PR             │   │  reproduce → patch → PR     │
          └───────────┬────────────┘   └───────────┬─────────────────┘
                      │                            │
                      └────────────┬───────────────┘
                                   │
                   ┌───────────────▼────────────────┐
                   │  VERIFIER                      │
                   │  ephemeral env, replay failing  │
                   │  request, regression suite      │
                   └───────────────┬────────────────┘
                                   │
                   ┌───────────────▼────────────────┐
                   │  GITOPS + CANARY + ROLLBACK    │
                   └────────────────────────────────┘
```

Why federated rather than one big agent: a single agent with write access to every repository is an organizational non starter and a security problem. Federation also mirrors how the teams actually work, which means each capability team can set its own autonomy level and review standards without blocking anyone else.

### 3.3 The Incident Package, the most important artifact in the system

This is the contract between agents. It is what turns "the Product agent thinks your service is broken" into something a capability agent can act on mechanically.

```json
{
  "incident_id": "inc-2026-08-12-0417",
  "fingerprint": "sha256:9f2c...",
  "first_seen": "2026-08-12T04:17:22Z",
  "occurrence_count": 143,
  "severity": "S2",
  "reported_by": "agent://product/web-product",
  "suspected_owner": "agent://capability/pricing",
  "confidence": 0.93,
  "attribution_basis": ["contract_violation", "trace_error_span", "deploy_correlation"],
  "entry_point": "GET /api/cart/summary",
  "failing_dependency_call": "GET pricing-api/v2/quote?sku=...",
  "contract_reference": "pricing-api/openapi.yaml#/components/schemas/Quote",
  "expected": "Quote.unitPrice: number (required)",
  "actual": "Quote.unitPrice: null",
  "trace_exemplars": ["4bf92f...", "a3ce92..."],
  "reproduction": {
    "type": "http_replay",
    "artifact": "s3://autofix/repro/inc-...-0417.har"
  },
  "evidence": [
    {"type": "log_sample", "ref": "loki://..."},
    {"type": "schema_validation_report", "ref": "s3://..."},
    {"type": "deploy_event", "ref": "pricing-api@v2.14.0, 04:11Z"}
  ],
  "requested_outcome": "restore_contract_compliance",
  "sla_ack_minutes": 15
}
```

Three things this buys you. The receiving agent can validate the claim independently instead of trusting it. A human can read it and immediately understand the case. And rejections become structured too, which is how you avoid two agents arguing in a loop.

### 3.4 How attribution actually works

Run these in order and combine into a confidence score. Do not ask a language model to guess the owner from a stack trace. Give it deterministic evidence first, then use the model for the judgement call.

1. **Trace walk.** Find the deepest span in the failing trace with an error status. The service owning that span is the first suspect. Deterministic, cheap, and usually right.
2. **Contract validation.** Take the actual payload the capability returned and validate it against the capability's published schema. This is the decisive test:
   - Payload invalid → fault is the capability. High confidence.
   - Payload valid and the Product still failed → fault is the Product. High confidence.
   - No contract available → drop to lower confidence, weight the other signals.
3. **Deploy correlation.** Which services shipped in the window before the first occurrence. A single candidate raises confidence sharply and also suggests revert as a valid fix.
4. **Blast radius check.** Is only this Product affected, or every consumer of the capability? Many consumers failing points at the capability regardless of what the trace says.
5. **Model judgement, last.** Feed the model the assembled evidence and ask for a root cause narrative and a fix strategy. The model explains and decides how to fix. The deterministic layers decide who owns it.

Route on confidence, not on certainty:

- Above 0.85, act at the service's configured autonomy level.
- 0.60 to 0.85, produce a diagnosis and a draft pull request, require human review regardless of level.
- Below 0.60, escalate to a human with the evidence bundle. Do not guess.

### 3.5 Guardrails, the part that makes this deployable

| Guardrail | Implementation |
|---|---|
| **Path allowlist** | Agents may only touch declared paths. No `infra/`, no `.github/workflows`, no migrations, no IAM. Enforced in CI, not by prompt. |
| **Change budget** | Maximum autonomous merges per service per day. Exceeding it trips a circuit breaker and freezes autonomy for that service. |
| **Loop breaker** | Two failed verification attempts on the same fingerprint ends the run and pages a human. |
| **Fingerprint dedupe** | One open pull request per incident fingerprint. Prevents pull request storms during an outage. |
| **Test gate** | Patch is rejected unless it adds a test that fails on the pre patch commit. |
| **No self modification** | Agents cannot modify the autofix system's own repositories. |
| **Signed audit trail** | Every decision, prompt, tool call, and diff written to an append only log with the evidence that justified it. |
| **Kill switch** | Single flag that drops the whole system to L0 observe. Test it in the demo. |
| **Sensitive zone stop** | Auth, payments, personal data paths always route to a human, whatever the confidence. |

---

## 4. Demo design

Three scenarios, roughly twelve minutes, in this order. Scenario 3 is the one that wins the room.

### Scenario 1: Product owns the bug, fixed autonomously

**Setup.** The pricing capability legitimately returns an empty promotions array for a cart with no eligible items. The Product's cart summary component assumes at least one entry and throws.

**What the audience sees.** Error rate spikes in Grafana. The supervisor fingerprints the incident. Contract validation passes, so the capability is exonerated on objective evidence. Attribution lands on the Product at 0.94. The Product agent reproduces the failure locally, writes a guard plus a unit test, opens a pull request. Ephemeral environment spins up, the original failing request is replayed and now succeeds, the regression suite passes. Merge, deploy, error rate returns to zero.

**The line to say out loud.** The capability was never touched, and the system can show you exactly why it was ruled out.

### Scenario 2: Capability owns the bug, routed across the boundary

**Setup.** A new version of the pricing service returns `unitPrice: null` for one SKU category, violating its own OpenAPI schema.

**What the audience sees.** The Product agent detects the failure but contract validation fails against the capability schema. Attribution flips to the capability at 0.93. The Product agent explicitly does not patch the Product, and says so. It emits the Incident Package. The capability agent receives it, independently re validates the claim rather than trusting it, accepts, reproduces, fixes, adds a contract test, and opens a pull request in its own repository. After that deploy, the Product's errors clear with no Product change at all.

**The line to say out loud.** This is the organizational boundary being respected by machines. No agent reached into another team's code.

### Scenario 3: The system refuses to act

**Setup.** A spike of 401s on the login path after a session handling change.

**What the audience sees.** The incident is detected and attributed correctly. Policy then blocks the run because the code path is in the sensitive zone. The system produces a full diagnosis, a suggested patch as a draft, and pages the on call engineer. It changes nothing.

**The line to say out loud.** An autonomous system you cannot trust to stop is not one you can trust to start.

### Optional closer: rollback

Force a bad autonomous fix through, let the canary detect the regression, watch it roll back automatically and freeze autonomy for that service.

### Demo mechanics that will save you

- **Inject bugs behind feature flags.** Each scenario is a flag toggle on a running deployment, not a git revert. Repeatable on cue, resets in seconds.
- **Pre warm everything.** Container images pulled, ephemeral environment templates cached. Nothing kills a demo like an image pull.
- **Put the agent's reasoning on screen.** A simple live event feed of decisions, evidence, and confidence scores is more convincing than the fixed application.
- **Record a clean run as a fallback.** Always.

---

## 5. Build recommendation

### 5.1 Stack

| Layer | Demo choice | Production choice | Why |
|---|---|---|---|
| Cluster | k3d or kind, 3 namespaces (`product`, `cap-pricing`, `agents`) | Existing Kubernetes | Runs on a laptop, no cloud cost, no approvals |
| Product | React plus a Node or FastAPI backend for frontend | Real product | Keep it small, three endpoints is enough |
| Capability | Two Python services, an API and a worker | Real capabilities | Two services proves the "one or more" requirement |
| Telemetry | OpenTelemetry SDK → OTel Collector → Loki, Tempo, Prometheus | Existing APM, Datadog or Dynatrace | Traces are what make attribution work |
| Orchestration | LangGraph, Python | Temporal, with agent steps as activities | LangGraph for speed now. Temporal when you need durable multi hour runs, retries, and human approval signals as first class |
| Coding agent | Claude Agent SDK in a sandboxed container per run | Same, plus model routing | One container per run, repo mounted, network restricted |
| Tools | MCP servers for GitHub, Kubernetes, Loki, Tempo | Same plus catalog and paging | MCP keeps tool access uniform and auditable |
| Git and CI | Gitea in cluster, or GitHub with Actions | GitHub or GitLab | Gitea makes the demo fully offline |
| Delivery | Argo CD, ephemeral env per pull request | Argo CD plus Argo Rollouts | GitOps is the guardrail, not a preference |
| Policy | OPA or Conftest in CI | Same | Allowlists enforced by machine, never by prompt |
| Ownership | A YAML service catalog file | Backstage | Start with a file, do not build a catalog |

Note on models: use a small fast model for triage, classification, and dedupe, which is most of the volume. Reserve the large model for diagnosis and code generation. This alone will move the economics by an order of magnitude. Given your Vishwakarma and DevMate work, a local Qwen3 Coder path is a plausible variant for the code generation step if the demo needs to run air gapped, though attribution quality will drop and I would keep the hosted model for the reasoning steps.

### 5.2 Phasing, four sprints to a demo

**Sprint 0, the harness.** Cluster, three namespaces, Product and two capability services, OpenTelemetry wired end to end, Argo CD deploying from Git, feature flags for injectable bugs. No agents yet. This sprint is boring and it is where the demo is won or lost.

**Sprint 1, detect and attribute.** Signal ingestion, incident fingerprinting and dedupe, the trace walk, contract validation, deploy correlation, confidence scoring. Output is a diagnosis in Slack. This is L1 and it is genuinely valuable on its own, which matters if the project gets cut early.

**Sprint 2, the fix loop, one domain only.** Product agent only. Sandboxed workspace, reproduce, patch, test, pull request. Ephemeral environment verification. Merge to staging. Scenario 1 works end to end.

**Sprint 3, the boundary and the brakes.** Incident Package schema, capability agent, accept and reject flow, independent re validation. Policy engine, change budgets, loop breaker, kill switch, audit log. Scenarios 2 and 3 work. Canary and rollback if time allows.

Two engineers, four two week sprints. One engineer can do it in roughly ten to twelve weeks. Sprint 0 is the compressible one only if you already have a service mesh and tracing.

### 5.3 Build order inside each agent

Resist starting with the language model. Build in this order:

1. Deterministic evidence collection. No model.
2. Deterministic attribution rules. No model.
3. Model for diagnosis narrative only, output read by humans.
4. Model for patch generation, output gated by tests.
5. Autonomy levels turned up one notch at a time, per service.

Every step is independently useful. If you stop at step 3 you still have a better incident triage system than most organizations run today.

### 5.4 Metrics to instrument from day one

| Metric | Why it matters |
|---|---|
| Attribution accuracy | The single number that decides whether this is trustworthy. Track it against human judgement on every incident. |
| Autonomous resolution rate | Percentage of incidents closed with no human code change. |
| Pull request acceptance rate | Do humans keep the agent's patches, or rewrite them. |
| Mean time to diagnosis | Usually the biggest win, and it arrives before autonomy does. |
| Regression escape rate | Fixes that caused a new incident. The number that kills the project if it climbs. |
| Escalation precision | Of the cases it refused, how many genuinely needed a human. |
| Cost per incident | Tokens, compute, ephemeral environments. Compare to engineer hours saved. |

---

## 6. Risks and honest weaknesses

| Risk | Mitigation |
|---|---|
| **Confident wrong attribution** creates cross team friction, the worst outcome politically | Independent re validation by the receiving agent. Rejections are structured and cheap. Attribution accuracy tracked publicly. |
| **The patch that passes tests and is still wrong** | Verification replays the real failing request in an ephemeral environment, not just unit tests. Canary in production. |
| **Pull request storms during a real outage** | Fingerprint dedupe plus change budget plus circuit breaker. During a declared incident, drop to L0 automatically. |
| **Agents fixing symptoms not causes**, for example wrapping everything in try/catch | Require the test to assert correct behaviour, not absence of an exception. Human review sampling of merged autonomous fixes. |
| **Trust collapse after one bad fix** | Start every service at L1. Earn each level with data. Never enable L4 by default. |
| **The demo works and production does not**, because real telemetry is noisier by orders of magnitude | Be explicit that Sprint 1 accuracy numbers come from injected bugs. Plan a shadow mode run against real production signals before claiming anything. |

---

## 7. Recommended next decisions

Four things to settle before Sprint 0 starts:

1. **Pick the two real services** you would model the demo on, so the vocabulary matches something your audience recognizes.
2. **Confirm the contract situation.** If capabilities do not publish schemas today, that is the first dependency and it changes the plan.
3. **Choose LangGraph or Temporal now.** Migrating orchestration later is expensive. If this is a two week demo, LangGraph. If it is a seed for a platform capability, Temporal.
4. **Decide the target audience for the demo.** A CTO wants Scenario 3 and the metrics. An engineering team wants Scenario 1 and the pull request diff. The build is the same, the framing is not.
