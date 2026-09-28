# ASHS — 5 Minute Demo

Two acts. The script runs in **1m 47s**. The rest is you talking.

Longer narrated version: `DEMO-PLAYBOOK-full.md` / `make demo-scenario-1`.

---

## The argument

> Not *"an AI fixed a bug."* Everyone believes that now.
>
> **"It worked out WHO owned the bug from objective evidence — including when
> the owner was two repos away from where the error appeared — fixed the one it
> was allowed to fix, refused the one it wasn't, and can show you every step."**

Act 1 proves the repair is real. Act 2 proves the attribution is real.
Act 1 alone is weak: `surfaced in` and `fault domain` read identically, which
looks like the system just blames wherever the error surfaced.

---

## T-2 minutes

```bash
make demo-prep      # restores source, clears flags, KEEPS incident history
make check-bedrock  # must print BEDROCK OK
```

`demo-prep` will warn you if capability errors from a previous rehearsal are
still inside the blast-radius window. **Heed it** — see *Rehearsal cadence*.

Open **http://localhost:3001** and hard-refresh.

---

## Layout

| | |
|---|---|
| left | Dashboard — http://localhost:3001 |
| right | Terminal |

---

## The five minutes

### 0:00 — Frame it (45s, before you type anything)

> "A carbon calculator. One product, two capabilities behind it — calculation
> and emission factors. Different teams, different repos.
>
> I'm going to break it twice. Once where the product is genuinely at fault,
> and once where it isn't — and watch where the system points."

### 0:45 — `make demo`

#### ACT 1 — the product owns it (~80s)

```
calc-api  HTTP 200  total=0   ← still contract-compliant
product   HTTP 500 × 8        ← same request, through the product
```

> "The capability is behaving perfectly — that response validates against its
> own published schema. Every naive system starts by blaming what it called."

Two exonerations land, then `0.92 (autonomous)`. When the gate prints, stop:

```
TEST GATE pre_patch:  failed  (must FAIL)
TEST GATE post_patch: passed  (must PASS)
```

> "**This is the guardrail that matters.** The test has to fail before the patch
> and pass after. A test that passes before the fix proves nothing, and the
> patch is rejected on the spot — whatever the model claimed."

Closes `RESOLVED`, ~80s. `surfaced in` and `fault domain` both read
`cfc-product` — say so, and set up Act 2:

> "Same owner. Now let me break something that isn't the product's fault."

#### ACT 2 — the capability owns it, and it refuses (~40s)

**The beat the whole demo exists for:**

```
surfaced in   cfc-product   where it broke
fault domain  cap-factors   ROUTED PAST the service that failed
confidence    0.88+
```

> "The error surfaced in the product. The system routed *past* it and landed
> two repositories away — because it validated each capability's payload
> against that capability's own published schema, and emission-factors failed
> its own contract.
>
> Then it drafted the patch for `factor_api/main.py` — and **refused to merge
> it**. The confidence clears the floor, but factor-api runs at autonomy L1:
> emission factors end up in customer disclosures. Diagnosis stands, a human
> approves."

It also notes a second incident: the middle capability failed too, attributed
to the same owner. That's blast radius.

### ~2:35 — The dashboard (60s)

Click the Act 2 incident. Three things:

1. **Evidence & artifacts** — `surfaced in — ROUTED PAST`, in amber.
2. **The escalation card** — owner team, contact, reason.
3. **Decision feed** — collapse it. "Every decision, with the evidence that
   justified it. Reconstructable from the incident ID."

### ~3:35 — Close

> "Nine policy gates ran before it was allowed to touch anything. It cannot
> patch emission factors, it cannot patch its own policy, and two failed
> attempts on the same defect stops it and pages a human."

---

## Rehearsal cadence

The blast-radius signal is worth 0.22 and only pays out when the suspect's
dependencies are quiet — over a **10-minute lookback**. Act 2's errors sit
inside that window, so a second full run too soon scores Act 1 at 0.70 and it
asks for review instead of repairing.

| Situation | Do |
|---|---|
| Rehearsing back to back | `ACT=1 make demo` (repair only) |
| Need a pristine Act 1 | `make reset` (wipes everything, ~25s) |
| Between runs, unhurried | wait out the warning `demo-prep` prints |

---

## If it goes wrong

| Symptom | Do this |
|---|---|
| `Only N failure(s) — detection needs 3` | Service reloaded and dropped the flag. Re-run. |
| Act 1 escalates at 0.70 | Blast-radius window. See *Rehearsal cadence*. Not a failure — say "it asked for a human." |
| `Patch generation unavailable` | Credentials. `make check-bedrock`. Never restart containers with raw `docker compose` — it ignores the root `.env`. Use `make restart-control`. |
| Dashboard empty | Hard-refresh. Only `make reset` / `clear-incidents` wipe the board now. |
| Nothing detected | `make incidents`. Detection needs 3 occurrences in 5 min. |

Never run `make reset`, `make clear-incidents` or `make three-runs` with an
audience watching — each blanks the dashboard.

---

## Claim / don't claim

**Do:** attribution is deterministic and lands two hops from the symptom · the
gate rejects unprovable patches · it refuses when confidence is below the floor
· every action is audited and reversible · it monitors any service in
`services.yaml`.

**Don't:** claim production scale (this is Compose; real telemetry is noisier by
orders of magnitude) · claim the capability repair ran end to end — Act 2 stops
at the escalation by design, because capabilities are L1 · let anyone think the
model chose the owner. It didn't, and the exit tests assert it didn't.
