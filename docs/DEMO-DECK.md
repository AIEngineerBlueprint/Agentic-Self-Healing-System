# ASHS — Demo Deck

**Review this before I convert it to PPTX.** One `##` heading = one slide.
Bullets are slide text. `> Notes:` are speaker notes, not slide content.
`![...]` marks an image to embed at that position.

Target: **22 slides, ~30 minutes** including a 5-minute live demo.
Every measured number is sourced on slide 18. Assets: `docs/logo.svg`,
`docs/architecture.svg`.

---

## 1 · Title

![docs/logo.svg — centred, ~55% slide width]

**An autonomous repair control plane for multi-team service estates**

*Presented by — [name] · [date]*

> Notes: Hold while people settle. Do not open with architecture.

---

## 2 · Why this exists

**A 500 in production names nobody.**

Today, when a user-facing request fails across a service boundary:

| Step | Who does it | Typical cost |
|---|---|---|
| Notice it | on-call, or a customer | minutes → hours |
| Work out whose bug it is | 3 teams on a call | **the expensive part** |
| Convince the owning team | politics | hours → days |
| Write the fix | one engineer | minutes |
| Prove it worked | often skipped | — |

The fix is the cheapest step. **Everything around it is the cost.**

> Notes: Ask the room: "how long from page to the right team having the ticket?"
> Nobody answers "under an hour." That gap is what this attacks — not typing speed.

---

## 3 · Why attribution is the hard part

A user hits `POST /api/footprint/calculate`. It returns **500**.

The error surfaced in the **product**. The cause could be:

| Candidate | Distance from the symptom |
|---|---|
| the product itself | 0 hops — mishandling a valid response |
| the calculation capability | 1 hop down |
| the emission-factors capability | 2 hops down |
| none of them | a bad deploy, a schema drift |

Three teams. Three repositories. **One error message that names none of them.**

The naive move — blame whatever you called — is wrong roughly as often as it's right.

> Notes: This slide makes the rest matter. In most orgs this is a 40-minute
> conference call before anyone writes a line of code.

---

## 4 · The one idea

> **Attribution is deterministic. Only the narrative and the patch come from a model.**

| Deterministic — code | Model — Claude |
|---|---|
| who owns the bug | why it happened, in prose |
| which agent may repair it | what the fix should be |
| whether it may run at all | *(nothing else)* |

The exit tests assert **no model call appears at or before the attribution decision**.

> Notes: If you take one thing away, take this. A demo where you ask an LLM
> "who broke this?" proves nothing — you cannot audit it, and it will be
> confidently wrong at the worst possible moment.

---

## 5 · Architecture

![docs/architecture.svg — full-bleed, edge to edge]

> Notes: 60 seconds. Trace one request left to right, then point at the purple
> arrows: every span carries `code.repository`, the join key that turns a failing
> span into an owner with no lookup table and no guessing.

---

## 6 · Five signals, combined

| Signal | Weight | What it actually does |
|---|---|---|
| **Contract validation** | **0.88** | The payload a capability *returned* vs the schema it *published* |
| Trace walk | 0.40 | Deepest error span carrying an exception |
| Deploy correlation | 0.25 | What shipped before first occurrence |
| Blast radius | 0.22 | One consumer failing, or every consumer? Follows the chain to its root |
| Exoneration | 0.15 | Ruling a capability *out* is positive evidence too |

Contract validation is the only signal that can clear the bar alone — because it
is the only one that is **objective and independently checkable**: their schema,
their payload, no inference in between.

> Notes: If asked "why 0.88?" — a conclusive schema violation is proof, not
> inference. Anyone in the room can re-run that check and get the same answer.

---

## 7 · Routing on confidence, never certainty

| Confidence | Action |
|---|---|
| **≥ 0.85** | Act, at that service's own autonomy level |
| **0.60 – 0.85** | Diagnose and draft the patch — a human approves |
| **< 0.60** | Escalate with the evidence bundle. **Do not guess.** |

Configurable in three places, most specific wins:

```
services.yaml   confidence_floors:      ← per service
.env            ASHS_AUTONOMOUS_FLOOR
policy.yaml     confidence:             ← global default
```

> Notes: "Act at 0.85 on the product, but demand 0.95 before anything touches
> emission factors" is one line of YAML. That is an ownership decision, not a
> code change — and different teams will want different answers.

---

## 8 · The guardrails — in code, never by prompt

| Guardrail | Enforcement |
|---|---|
| **Test gate** | Patch rejected unless its test **fails pre-patch and passes post-patch** |
| Path allowlist | Tool gateway, per agent, from `services.yaml` |
| Protected zone | Emission factors, formulas, migrations, `platform/**` |
| Change budget | Max autonomous merges per service per day |
| Loop breaker | 2 failed attempts on a fingerprint → human |
| No self-modification | Agents cannot touch ASHS's own policy |
| Kill switch | Halts the reconciler, checked every pass |

**11 typed tools. No raw shell, no `kubectl`, no `eval`.** The exit tests assert
those do not exist.

> Notes: Five of these are *proven to refuse* in the phase-2 suite. Not
> "configured to" — proven, with the refusal written to the audit trail.

---

## 9 · The test gate is the centrepiece

```
TEST GATE pre_patch:   FAILED   (must FAIL)
TEST GATE post_patch:  passed   (must PASS)
```

**A test that passes before the fix proves nothing.**

The patch is rejected on the spot — whatever the model claimed, however good the
diff looks. If the defect stopped reproducing on its own, the incident closes as
**stale**: no patch, no page.

> Notes: The most important slide for a skeptical engineer. The model writes the
> patch *and* the test; it does not get to decide whether either is acceptable.

---

## 10 · Why you should trust it

Trust is not a claim on a slide. It is five structural properties:

| Property | Because |
|---|---|
| **Auditable** | Every decision is an append-only row with the evidence that justified it. Replayable from an incident ID. |
| **Deterministic where it matters** | Ownership, routing and policy never consult a model. Same evidence → same verdict, every time. |
| **Independently checkable** | The decisive signal is a public schema vs an observed payload. You can re-run it by hand. |
| **Reversible** | Snapshot before mutation; failed validation restores **byte-for-byte**. |
| **Bounded** | 11 typed tools, path allowlist, protected zones, change budget, loop breaker, kill switch. |

**And it stops.** Below 0.85 it drafts and asks. Below 0.60 it refuses outright.

> Notes: The honest framing — you should not trust the model. You should trust
> the *envelope* around it, and that envelope is code you can read.

---

## 11 · LIVE DEMO — Act 1: the product owns it

```
calc-api  HTTP 200  total=0        ← still contract-compliant
product   HTTP 500 × 8             ← same request, through the product
```

- Both capabilities **exonerated** on their own published schemas
- `cfc-product` at **0.92** → clears the floor → repairs autonomously
- Test gate: fails pre-patch, passes post-patch
- **RESOLVED in ~80s**

> Notes: `make demo`. Say: "the capability is behaving perfectly — that response
> validates against its own schema. Every naive system starts by blaming what it
> called."

---

## 12 · LIVE DEMO — Act 2: the capability owns it

```
surfaced in   cfc-product      where it broke
fault domain  cap-factors      ROUTED PAST the service that failed
confidence    0.88+
```

- factor-api returns **HTTP 200** that violates **its own** published schema
- It never errors — only contract validation can see this
- Routed **two repositories** past where the error appeared
- The **capability-repair-agent** drafted the patch for a different repo, owned
  by a different team — and **refused to merge it**: factor-api runs at
  autonomy L1, so a human approves

> Notes: The money beat. Also mention the second incident opened for `cap-calc` —
> the middle capability failed too, attributed to the same owner. That is blast
> radius doing real work.

---

## 13 · Everything is evidence

The dashboard shows nothing that is not backed by a row in the audit trail:

- **Incident board** — state, fault domain, confidence, occurrences
- **Decision feed** — grouped by phase, each decision with its justification
- **The patch** — the actual unified diff
- **Test-gate result** — both runs, with pytest output
- **Exonerations** — who was ruled out, and on what evidence
- **Escalation queue** — owning team, contact, reason

Append-only, sequenced, never rewritten.

> Notes: Click into the Act 2 incident live. Collapse the decision feed and say
> "thirty-three decisions, every one with the evidence that justified it."

---

## 14 · The value

**Where the time goes today, and what this removes:**

| | Today | With ASHS |
|---|---|---|
| Detect | minutes → hours | ~10s sweep, 3 occurrences |
| Attribute | **hours → days**, 3 teams | seconds, deterministic, with evidence |
| Route to owner | manual, political | automatic, from the catalog |
| Fix | minutes | ~80s, or a drafted patch awaiting approval |
| Prove it worked | often skipped | mandatory, or it does not merge |

**Second-order value, which matters more:**

- **The argument moves from opinion to evidence.** "Your service returned this
  payload; here is your published schema." That ends the call.
- **Exoneration is as valuable as blame.** Two teams get their morning back.
- **Every incident leaves a reviewable artifact**, whether it repaired or refused.

> Notes: Do not lead with "saves engineer hours" — lead with "ends the ownership
> argument." Engineering leaders feel that pain more sharply than headcount.

---

## 15 · The risks, honestly

| Risk | Mitigation in place | Residual |
|---|---|---|
| Wrong attribution → cross-team friction | Deterministic signals; escalate below 0.60 | Real if telemetry is poor |
| Bad patch merged | Test gate must fail-then-pass; validation replay | A test can be well-formed and shallow |
| Runaway repair loop | Loop breaker, change budget, kill switch | — |
| Agent touches something it shouldn't | Path allowlist, protected zones, 11 typed tools | Depends on catalog accuracy |
| Silent corruption with no error signal | **None — not detected today** | **Open gap** |
| Model unavailable mid-incident | Degrades to deterministic diagnosis and escalates | Loses the patch, not the attribution |

**The honest one:** the system is only as good as `code.repository` on your spans
and an OpenAPI generated from your code. Without both, attribution degrades to
guesswork — and it will *say* it is guessing, but that is still a worse product.

> Notes: Volunteering this is what makes slides 1–14 credible. Do not skip it.

---

## 16 · This is not production grade

**Say this out loud. It is a working prototype, not a product.**

- Runs on Docker Compose on one machine — no HA, no multi-tenancy, no RBAC
- Patches are snapshot/restore on a mounted volume — **not git, not a pull request**
- "Deploy" is a hot reload, not a build, canary or progressive rollout
- Real telemetry is noisier by orders of magnitude than this demo estate
- No authentication on the control plane or dashboard
- Demo fault-injection endpoints are compiled into the services
- Emission factors are indicative only — not for reporting or disclosure use

**What is real:** the attribution engine, the policy gates, the test gate, the
audit trail, and the rollback. Those are the parts worth arguing about.

> Notes: If someone asks "can we run this next quarter?" — the answer is no, and
> the next slide is why that is fine.

---

## 17 · Where it goes next — near term

**Close the honesty gaps first.** *(weeks)*

| # | Feature | Why it matters |
|---|---|---|
| 1 | **Git-backed patches → real PRs** | Replace snapshot/restore with a branch, a diff and a review. Makes every repair land in the workflow teams already trust. |
| 2 | **Human-in-the-loop approval UI** | The 0.60–0.85 band already drafts a patch. Today a human reads it in the terminal — give them Approve / Reject / Amend in the dashboard, and record which they chose. |
| 3 | **Escalations to Slack / PagerDuty** | The escalation record already carries owner team and contact. Wiring it is one handler, not a redesign. |
| 4 | **TTD / time-to-diagnosis metrics** | We record time-to-repair but not how fast it *noticed*. That is the number that proves the loop is alive. |
| 5 | **Rename `POLICY_APPROVED`** | It reads as if a human approved. Nine deterministic gates did. Words matter in an audit trail. |

> Notes: Items 1 and 2 are what turn this from a demo into something a team could
> actually adopt — and neither requires new intelligence, only new plumbing.

---

## 18 · Where it goes next — detection beyond errors

**The biggest functional gap: we only see failures that throw.**

| Feature | What it unlocks |
|---|---|
| **SLO / anomaly detection** | Latency regressions, error-budget burn, throughput cliffs — none raise an exception today |
| **Silent corruption detection** | A capability returning a *plausible but wrong* number is invisible to error-rate detection. Contract validation catches shape, not semantics. |
| **Continuous contract diffing** | Detect a schema change at deploy time, before a consumer breaks on it |
| **Golden-output regression** | Pin known-correct results; alert when the same input produces a different answer |

**Worked example — the gap today:** flip an emission factor from `0.71` to `0.95`.
Every request returns **HTTP 200**. Totals shift ~40%. Error rate stays flat.
**Nothing detects it.** Customers report carbon numbers that are wrong.

> Notes: This is the most intellectually interesting gap, and the one a good
> engineer in the room will find. Get there first.

---

## 19 · Where it goes next — trust and calibration

**Make the system prove its own accuracy over time.**

| Feature | Why |
|---|---|
| **Outcome feedback loop** | Did the repair hold for 7 days, or did the incident recur? Feed that back as a per-signal accuracy score. |
| **Confidence calibration** | Today the weights are hand-set. With enough episodes they should be *fitted* to observed correctness — and published, so the floors mean something empirical. |
| **Attribution disagreement log** | When a human overrides the fault domain, record it. That set is the training data for everything above. |
| **Per-signal precision / recall** | "Contract validation has been right 47/47 times" is a far stronger slide than "we weight it 0.88." |
| **Cost per incident** | Model spend, compute, and engineer-minutes saved — per repair. |

> Notes: This is what turns "trust the envelope" into "here is the measured
> track record." A year of episodes makes this deck a completely different pitch.

---

## 20 · Where it goes next — scale and estate

**From three services to a real estate.** *(quarters)*

| Feature | Why |
|---|---|
| **Multi-product, multi-team onboarding** | The catalog already drives everything; needs a self-service path and a linter for `services.yaml` |
| **Kubernetes execution backend** | Replace hot reload with a real deploy: canary, progressive rollout, automatic revert on SLO breach |
| **RBAC + authentication** | Who may change a floor, engage the kill switch, or approve a drafted patch |
| **Dependency-graph discovery** | Derive `depends_on` from observed traces instead of hand-maintaining it |
| **Suppression / maintenance windows** | Do not open incidents during a planned migration |
| **Multi-region, HA control plane** | The control plane is currently a single point of failure |

> Notes: Note that the catalog refactor already done means adding a service is a
> YAML edit — the hard part of multi-product is onboarding ergonomics and trust,
> not code.

---

## 21 · Where it goes next — the ambitious end

**Only worth attempting once the above is solid.**

- **Cross-incident learning** — "this fingerprint class is always a null-guard
  bug in a BFF." Pattern libraries per fault family.
- **Preventive repair** — the same contract validation run against a *pre-merge*
  build catches the violation before it ever reaches production.
- **Multi-service coordinated repair** — some fixes need a change on both sides
  of a boundary, with two owners approving.
- **Explain-to-the-owner** — generate the PR description *for the team that owns
  it*, in their vocabulary, with the evidence attached.
- **Chaos-driven validation** — continuously inject known faults in staging and
  assert the system still attributes them correctly. Regression testing for the
  attribution engine itself.

> Notes: Frame these as "not committed, but this is the direction." Do not let
> them overshadow slides 17–18, which are the ones that actually get funded.

---

## 22 · Verification — what is proven today

| Phase | Checks | Proves |
|---|---|---|
| 0 — harness | 13 | One calculation → one trace across three services |
| 1 — detect & attribute | 9 | `cfc-product` at 0.92, both capabilities exonerated, **no model** |
| 2 — plan & gate | 13 | Plan reaches POLICY_APPROVED; **5 guardrails proven to refuse** |
| 3 — repair & revert | 10 | Repair to RESOLVED; forced failure reverts **byte-for-byte** |

**45/45 passing.** Plus `make three-runs`: 3/3 clean runs from a full reset —
cold start 20–23s, repair loop 85–90s, reproducible.

> Closing line: *"The fix was never the hard part. Knowing whose it was, being
> allowed to make it, and proving it worked — that is the product."*

---

## Appendix A · Commands

```bash
make demo-prep     # T-2: restores source, keeps incident history
make demo          # both acts, 1m47s
ACT=1 make demo    # the repair only
ACT=2 make demo    # the attribution only
make verify        # 45 checks across 4 phases
make demo-rollback # forced validation failure → revert
```

**Never** run `make reset`, `make clear-incidents` or `make three-runs` with an
audience watching — each blanks the dashboard.

## Appendix B · Likely questions

| Question | Answer |
|---|---|
| "Did the model pick the owner?" | No. Exit tests assert zero model calls through attribution. |
| "What if it's wrong?" | Below 0.85 it drafts and asks. Below 0.60 it refuses. Bad patch → byte-for-byte revert. |
| "Can I tune the thresholds?" | Floors yes — three places. Signal weights are code, deliberately. |
| "Does it work on my services?" | If they emit OTel with `code.repository` and publish an OpenAPI generated from code, yes. |
| "What stops it going rogue?" | 11 typed tools, path allowlist, protected zones, change budget, loop breaker, kill switch. |
| "Can we run this in production?" | No — see slide 16. The parts worth arguing about are real; the deployment substrate is not. |
| "What would you build next?" | Git-backed PRs and the approval UI. Then SLO-based detection, because today we only see failures that throw. |

## Appendix C · Sources for every number

| Claim | Source |
|---|---|
| 45/45 checks | `make verify` — phases 0–3 |
| 3/3 clean runs, 20–23s boot, 85–90s loop | `make three-runs` |
| 0.92 / 0.88+ confidence, ~80s TTR (Act 1) | `make demo` — both acts |
| 5 proven refusals, 11 typed tools | phase-2 exit test |
| Demo runtime 1m47s | measured, `make demo` |
