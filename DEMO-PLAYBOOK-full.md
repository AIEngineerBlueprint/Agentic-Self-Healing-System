# ASHS — Demo Playbook

How to run this in front of people. Twelve minutes, one scenario, one argument.

---

## The argument you are making

Not *"an AI fixed a bug."* Everyone believes coding agents can write patches.

> **"The system worked out WHO owned the bug from objective evidence, proved the
> capabilities were innocent, fixed it in the right place, verified the fix, and
> can show you every step. And when the fix was bad, it put everything back."**

Four questions a skeptical engineering leader should be able to answer *yes* to
by the end:

1. Did it correctly decide which side owned the bug, and show its evidence?
2. Did it refuse to act at least once, for a good reason?
3. Can I trace every action back to an accountable owner?
4. Would this cost me less than the incidents it prevents?

---

## T-30 minutes: pre-flight

```bash
make reset && make verify        # 45 checks, ~8 minutes
make check-bedrock               # must print BEDROCK OK
```

- [ ] `make verify` — all four phases green
- [ ] `make check-bedrock` — **BEDROCK OK**
- [ ] Dashboard loads at http://localhost:3001, hard-refresh (`Cmd+Shift+R`)
- [ ] Product loads at http://localhost:3000
- [ ] `make demo-scenario-1` once as a rehearsal, then `make reset`
- [ ] Screen at ~1400px wide or more; the dashboard is three columns
- [ ] **Recording of a clean run saved as fallback**

### The one thing that will ruin the demo

**Expired AWS credentials.** STS/SSO tokens last 1–12 hours. When they lapse you
do not get a clean error — you get escalations that *look like the system failing
to repair*, live, with no obvious cause.

- Run `make check-bedrock` **immediately before** you present.
- Better: use a long-lived IAM key scoped to `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream`.
- If it fails mid-demo: the system still detects, attributes and gates
  correctly. Say so — *"the model is unreachable; notice attribution and policy
  are unaffected, because they never call one."* That is a strength, not a save.

---

## Layout

Two windows side by side, or one screen and a second display:

| | |
|---|---|
| **left** | Dashboard — http://localhost:3001 |
| **right** | Terminal running `make demo-scenario-1` |

The terminal narrates the argument. The dashboard shows the system doing it.
**Look at the dashboard when the pipeline moves; look at the terminal for the
exonerations.**

---

## The 12 minutes

### 1 · Frame it (60s) — before touching anything

> "This is a carbon footprint calculator. A product, and two capabilities behind
> it — calculation, and emission factors. Different teams, different repos.
> The product calls the capabilities over published contracts.
>
> I'm going to break it. Not by breaking a capability — by making the product
> mishandle a **correct** response. That's the interesting case, because the
> error will surface in one place and originate in another."

Show the product briefly. Enter numbers, calculate, point at the dataset version
badge and the disclaimer.

### 2 · Establish the baseline (60s)

```bash
make demo-scenario-1
```

Step 1 prints an all-zero calculation returning `HTTP 200, total=0, breakdown=[]`.

> "Zero is a legitimate answer. Somebody who logged no activity has a zero
> footprint. Remember that — it matters in ninety seconds."

Dashboard: journeys green, error rate flat.

### 3 · Inject, and show the capability is innocent (90s)

Steps 2–4 run. Flag enabled at runtime, no restart.

**This is the beat that carries the whole demo.** Step 3 calls `calc-api`
directly:

```
HTTP 200   total=0   breakdown=[]     ← contract-compliant BY DESIGN
```

> "The capability is behaving perfectly. That response validates against its own
> published schema. Now the same request through the product:"

```
HTTP 500 × 8    the user sees a generic failure
```

> "The error is in the product. The *cause* is the product. But every naive
> system I've seen would start by blaming whatever it called."

Dashboard: journeys go red, error rate spikes.

### 4 · Let it work (3–4 min) — mostly stop talking

Step 5 streams the pipeline. Watch the dashboard advance:
`detect → evidence → attribute → route → plan → policy → patch → validate`.

Two things to point at as they appear:

**The exonerations** —

> "It didn't decide the capabilities were fine. It **validated the payload each
> one actually returned against the schema each one actually published**. That's
> objective. A different engineer could check it independently."

**The model badge on Diagnose and Repair** —

> "That violet marker is the only place a language model was involved — the
> narrative and the patch. It was never asked who owned the bug. Attribution ran
> before it and would have reached the same answer with the model switched off."

Pause on `POLICY_APPROVED`:

> "Nine gates: kill switch, protected zone, action type, write scope, change
> budget, loop breaker, test declared, confidence floor, autonomy level. All
> deterministic. **No human approved this** — the name is misleading and we're
> renaming it. Policy cleared it."

### 5 · The test gate (60s) — the credibility moment

```
TEST GATE pre_patch:  failed   (must FAIL)
TEST GATE post_patch: passed   (must PASS)
```

> "The patch came with a test. The system ran that test **against the broken
> code first** and required it to fail. A test that passes before the fix proves
> nothing, and the patch is rejected on the spot — regardless of how good it
> looks or what the model claimed.
>
> That is the difference between a fix and a plausible-looking edit."

Dashboard: open **the patch** panel. Real diff, `+13 / −6`.

> "That's the actual change. Note what it *didn't* do — it didn't wrap the
> symptom in a try/except. It removed the unguarded division and made the zero
> case correct."

### 6 · Resolution (45s)

```
[replay_original_failing_request]  PASS
[no_regression_on_normal_input]    PASS
[error_rate_returned_to_baseline]  PASS
→ RESOLVED   time to repair 91s
```

> "It replayed the exact request that opened the incident. It checked a normal
> calculation still works — a fix that makes the zero case pass by breaking
> everything else is not a fix. And it measured the error rate **from the moment
> the patch deployed**, not a trailing window."

Dashboard: journeys back to green.

**The line to say out loud:**

> "The capability was never touched, and the system can show you exactly why it
> was ruled out."

### 7 · Now break it deliberately (2 min) — the part that wins the room

```bash
make demo-rollback
```

Same run, validation forced to fail.

```
→ ROLLBACK: Validation failed; restored the known-good state.
→ ESCALATED: Repair reverted and escalated to a human.
```

> "Same patch, same test gate — but validation fails. It restores the file
> **byte-for-byte**, confirms the service is healthy, and escalates with the
> owning team attached."

Dashboard: **escalation queue** appears — incident, `Product Engineering`,
`product-oncall@…`, and the reason.

> "An autonomous system you cannot trust to stop is not one you can trust to
> start."

### 8 · Close on the guardrails (60s)

Open the **refusals** panel, or run live:

```bash
curl -X POST localhost:8090/api/incidents/<id>/tools/create_patch \
  -H 'Content-Type: application/json' \
  -d '{"actor":"capability-repair-agent","path":"cap-factors/data/seed.sql","new_content":"x"}'
```

```
403  'cap-factors/data/seed.sql' is in the PROTECTED ZONE
```

> "Emission factors are protected. Not because we don't trust the model — because
> these numbers end up in a customer's public disclosure. A machine may flag that
> they look wrong. A machine does not get to quietly change what a company
> reports as its carbon output.
>
> That rule is in a policy file and enforced in the tool gateway. Not in a prompt."

Then the kill switch, if you have time:

```bash
sed -i '' 's/^kill_switch: false/kill_switch: true/' platform/policy/policy.yaml
make restart-control
```

> "One flag. The whole system drops to observe-only — nothing new is detected,
> nothing in flight executes."

---

## Questions you will get

**"Who approves `POLICY_APPROVED`?"**
Nobody. Nine deterministic gates. The name is misleading and is being changed to
`GATES_PASSED`. There *is* a human-review path — confidence between 0.60 and 0.85
requires it — but this run cleared the autonomous floor at 0.92.

**"What if attribution is wrong?"**
Below 0.60 it escalates rather than guessing. Between 0.60 and 0.85 a human
reviews. Confidently wrong attribution creating cross-team friction is the worst
political outcome, so the system is built to under-claim.

**"Why did confidence change between runs?"** (0.92 vs 0.99)
A previous repair writes a deployment record, so deploy correlation fires and
legitimately adds weight. Recent deploy on the suspect service *is* corroborating
evidence.

**"Could the agent modify its own guardrails?"**
No — `platform/**` is in the protected zone, enforced in the gateway. There's a
test for it.

**"Does this work on real production telemetry?"**
Unknown, and I won't claim otherwise. **These numbers come from injected bugs.**
Real telemetry is noisier by orders of magnitude. The honest next step is a
shadow-mode run against production signals before claiming anything.

**"What about the bug being downstream?"**
That's Scenario 2 — the capability owns it, two hops down, and naive attribution
blames the middle service. The attribution engine already does the work; it needs
about two days. It's the highest-value increment available.

**"How do I point this at my services?"**
Two YAML files — ownership, contracts, protected paths, autonomy levels. Each
service needs OTel with `code.repository` on spans, `/openapi.json` generated
from code, evidence capture, and structured logs. About eight service URLs are
still hardcoded; making it genuinely multi-product is roughly a day.

---

## If something goes wrong

| Symptom | Do this |
|---|---|
| Escalations instead of repairs | `make check-bedrock` — credentials expired |
| Nothing detected after injection | Source may already be patched: `make reset-source` then restart `cfc-api` |
| Dashboard shows stale layout | Hard refresh (`Cmd+Shift+R`) |
| Incident stuck | The reconciler retries every 10s; give it two passes |
| Anything else | `make reset` — 20 seconds, clean state |

**Do not debug live.** Switch to the recording, keep talking, fix it afterwards.

---

## Reset between runs

```bash
make reset      # ~20s: volumes destroyed, data reseeded, source restored
```

Verified repeatable: `make three-runs` → 3/3, cold start ~20s, repair loop
85–95s. Run it the morning of the demo.

---

## What to claim, and what not to

**Claim:**
- Deterministic attribution across an ownership boundary, with objective evidence
- A test gate that rejects a patch not demonstrating a defect
- Guardrails enforced in code, demonstrably refusing
- Byte-for-byte rollback on failed validation
- Every action auditable by incident ID

**Do not claim:**
- That this is production-ready
- That the accuracy numbers generalise — they come from injected bugs
- That it handles multi-service coordinated changes — it detects and escalates them
- That escalation pages anyone yet — it's a durable queue; Slack/PagerDuty is one handler away
