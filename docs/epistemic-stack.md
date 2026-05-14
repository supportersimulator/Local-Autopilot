# Epistemic Stack

> How Local Autopilot makes good judgments under autonomy.
>
> **Audience:** future-Aaron, contributors who need to reason about *why* the
> loop behaves the way it does — not just *what* it does. If you're touching
> `archloop_runner.py`, `headless_executor.py`, or anything that escalates
> off-box, read this first.

This document is a faithful map of what exists in the repo today. Anything
that is planned-but-not-shipped is marked **(EPIS-1 ships this in parallel)**
so nobody mistakes design for code.

---

## TL;DR

Autopilot is a stack of six judgment layers, each with a strict scope. No
single layer is trusted to decide the cycle's outcome. Each layer either
**signs off**, **defers up**, or **escalates**. The user (Aaron) is the
top of the stack and the only actor who can clear `on_permanent`.

```
                     YOU (Aaron) — always-on kill switch (`autopilot off`)
                                          │
                                          ▼
                     LAUNCHD TICK every 30 min  (StartInterval=1800)
                                          │
                                          ▼
                  ┌──── archloop_runner (--cycles 8) ────┐
                  │                                       │
   ┌──────────────┴──────────────┐         ┌─────────────┴────────────┐
   │   1. SYNAPTIC REVIEW         │         │  ITERATE up to 8 times    │
   │      • Read complexity vec   │  ┌──────│  Exit early if satisfied  │
   │      • Qwen3.6-35B-A3B (MLX) │  │      └────────────────────────────┘
   │      • Pick 5 hardening prompts                                    │
   └──────────────┬──────────────┘                                      │
                  │                                                     │
                  ▼                                                     │
   ┌──────────────────────────────┐                                     │
   │   2. HEADLESS EXECUTE         │                                    │
   │      • 5x claude --print in   │                                    │
   │        parallel (threadpool)  │                                    │
   │      • Superpowers skills     │                                    │
   │        available via OAuth    │                                    │
   │      • Per-agent budget cap   │                                    │
   │      • STATUS: PASS/FAIL/SKIP │                                    │
   └──────────────┬──────────────┘                                      │
                  │                                                     │
                  ▼                                                     │
   ┌──────────────────────────────┐                                     │
   │   3. 3-SURGEONS CROSS-EXAM    │                                    │
   │      • Cardiologist: DeepSeek │                                    │
   │      • Neurologist: Qwen3.6   │                                    │
   │      • Atlas: Claude          │                                    │
   │      • Surface disagreements  │                                    │
   └──────────────┬──────────────┘                                      │
                  │                                                     │
                  ▼                                                     │
   ┌──────────────────────────────┐                                     │
   │   4. SYNAPTIC RE-EVALUATE     │                                    │
   │      • Read cross-exam output │                                    │
   │      • Decide: satisfied?     │──── SIGN-OFF ──► cycle complete    │
   │                               │──── NOT YET ───┐                   │
   └──────────────────────────────┘                │                    │
                                                   ▼                    │
                                       loop back to step 1 ◄────────────┘
                                                   │ (after 8 rounds)
                                                   ▼
   ┌──────────────────────────────┐
   │   5. FLEET ESCALATE (final)   │  (EPIS-1 ships this in parallel)
   │      • POST to mac1 chief     │
   │      • Includes: cross-exam,  │
   │        re-eval, all results   │
   │      • Chief's Atlas (Opus)   │
   │        weighs in with deeper  │
   │        context-DNA history    │
   └──────────────┬──────────────┘
                  ▼
   ┌──────────────────────────────┐
   │   6. FINAL VERDICT             │
   │      • SIGN-OFF / PARTIAL /   │
   │        UNRESOLVABLE → Aaron   │
   └──────────────────────────────┘
```

---

## 1. The six layers

### Layer 1 — Synaptic Review (priming)

* **What it does.** Reads the local complexity-vector SQLite, current
  invariant snapshot, and drift signals. Asks Synaptic (local MLX
  `Qwen3.6-35B-A3B-4bit-DWQ` on port 5044) for the next 5 highest-leverage
  hardening prompts.
* **Why it exists.** Cheap, deterministic-ish prompt selection without
  hitting paid endpoints. Local model, no rate limits, $0 marginal cost.
* **Can decide on its own:** which 5 prompts to run.
* **Cannot decide:** whether the cycle is done, whether to escalate, whether
  to spend budget.

Implementation: `local_autopilot/tools/synaptic_client.py`, called from
`archloop_runner.py` step ~600.

### Layer 2 — Headless Execute

* **What it does.** Writes each prompt to disk as `agent_N.prompt`, then
  spawns `claude --print` in a thread pool (default `parallel=5`) with a
  per-agent `--max-budget-usd` cap. Superpowers skills are available because
  the local Claude CLI is OAuth-authenticated. Each agent must emit a
  `STATUS: PASS|FAIL|SKIP` line; missing-status defaults to SKIP.
* **Why it exists.** The actual work. Five independent agents in parallel
  means we exercise the full Claude Code skill graph without a babysitter.
* **Can decide on its own:** how to satisfy a single prompt.
* **Cannot decide:** whether its own PASS is correct (Layer 3 checks).
  Cannot escalate. Cannot exceed its budget cap (claude CLI enforces).

Implementation: `local_autopilot/tools/headless_executor.py`. Opt-in via
`--headless-executor`. Counters tick on every spawn / timeout / OSError /
budget refusal — see ZSF section below.

### Layer 3 — 3-Surgeons Cross-Exam

* **What it does.** Sends the aggregated agent results to three distinct
  models and asks each whether the work is sufficient. Surfaces
  *disagreements*, not agreements.
  * **Cardiologist** — DeepSeek-chat (HTTPS, paid).
  * **Neurologist** — Qwen3.6-35B-A3B via local MLX (or DeepSeek fallback
    per superrepo config).
  * **Atlas** — Claude (the headless agent layer above doubles as Atlas's
    voice; the synthesis happens in the runner).
* **Why it exists.** Three vendors, three training cuts, three perspectives.
  Single-model self-assessment is structurally bad at catching its own blind
  spots — see distinctness invariant below.
* **Can decide on its own:** HOLD / SIGN-OFF / verdict per surgeon.
* **Cannot decide:** whether to escalate off-box.

Implementation: `run_cross_exam()` in `archloop_runner.py:410`. Counters:
`cross_exam_total`, `cross_exam_errors`, `cross_exam_holds`,
`cross_exam_signoff`.

### Layer 4 — Synaptic Re-Evaluate

* **What it does.** Hands the cross-exam verdict back to Synaptic and asks
  "satisfied?". If yes → cycle ends. If no → loop back to Layer 1 with the
  fresh signal, up to the 8-cycle cap.
* **Why it exists.** Synaptic gets to integrate the surgeons' counter-
  evidence and either accept the verdict or push for another round.
  Without this, a single shaky cross-exam could short-circuit hardening.
* **Can decide on its own:** continue or sign off, within the cycle cap.
* **Cannot decide:** override the user's `off` state, bust the cost cap,
  bypass the kill file.

### Layer 5 — Fleet Escalate (final) — **(EPIS-1 ships this in parallel)**

* **Status today.** Not yet wired in `local-autopilot`. `grep fleet|escalate|chief|mac1`
  on the source tree returns zero matches. EPIS-1 is the parallel branch
  that adds the POST to mac1's chief daemon (`http://mac1:8855/message`) and
  the result-handling loop.
* **Designed behavior.** When the cycle cap is exhausted without a re-eval
  sign-off, the runner writes `fleet_escalation_sent.json` into the cycle
  dir and POSTs the full bundle (prompts, results, cross-exam, re-eval) to
  the chief. The chief's Atlas (Opus, 1M ctx) replies with a SIGN-OFF /
  PARTIAL / UNRESOLVABLE verdict, using deeper context-DNA history that
  this box doesn't carry.
* **Why it exists.** Eight rounds of local thinking without consensus is a
  reliable signal that the problem is structurally hard. The chief box has
  more context (full superrepo memory, hermes ledger, gold passes) and a
  bigger model. Escalation is *rare by design* — if it fires often, lower
  the cycle cap or raise the local model quality.

### Layer 6 — Final Verdict

Three terminal outcomes, all written to `manifest.json` in the cycle dir:

* **SIGN-OFF** — Layer 4 (or Layer 5) is satisfied. Counters:
  `cycles_satisfied`.
* **PARTIAL** — mixed PASS/FAIL across agents; some hardening landed,
  remainder deferred to next tick.
* **UNRESOLVABLE** — chief returned UNRESOLVABLE, or Layer 5 itself failed
  (network down, chief offline). Surfaces to Aaron via the next webhook /
  inbox check; no further automated action.

---

## 2. Distinctness invariant (CP#5)

> Three vendors, three training cuts, three perspectives. **Never let two
> surgeons share a base model.**

| Role          | Model                | Vendor   | Training cut |
| ------------- | -------------------- | -------- | ------------ |
| Cardiologist  | `deepseek-chat`      | DeepSeek | distinct     |
| Neurologist   | `Qwen3.6-35B-A3B`    | Alibaba  | distinct     |
| Atlas         | Claude Opus / Sonnet | Anthropic| distinct     |

This is **constitutional**, not a tunable. The "exoskeleton" framing in the
superrepo (`docs/dao/`, 3-surgeons protocol) is the reason: a single-vendor
panel is structurally a single brain wearing three hats. You catch your own
blind spots only by importing a perspective you couldn't have generated.

If you're tempted to collapse two roles onto the same backend "to save
money" or "because they agree anyway" — that *is* the failure mode. The
value is in the disagreements, and disagreements only happen across
genuinely distinct training. Bump `LLM_EXTERNAL_PROVIDER` instead.

---

## 3. The 8-round limit

Why 8 and not infinity?

* **Cost ceiling.** `cost_cap_usd: 5.0` per `archloop_runner` invocation
  (config default). At ~$0.27 per agent + cross-exam overhead, 8 rounds
  consume the cap on bad days. Going beyond 8 would routinely blow the cap
  and force aborts mid-cycle (`cycles_aborted_cost`).
* **Diminishing returns.** Empirically (and structurally), if two distinct
  models plus Synaptic can't converge in 8 rounds, the next round is
  vanishingly unlikely to converge either. The information content of the
  Nth disagreement decays fast.
* **Escalation as design.** After 8 rounds, the *correct* action is not
  "think harder locally" — it's "ask someone with more context". That's the
  chief. So 8 is the point where local thinking becomes structurally
  inferior to escalation.

The cap is enforced two ways:

* `--cycles 8` flag (default `AUTOPILOT_MAX_CYCLES=10`, plan calls for 8).
  Hitting it bumps `cycles_aborted_cycle_cap`.
* `--cost-cap-usd 5.0`. Hitting it bumps `cycles_aborted_cost`.

Either trigger fires the escalation path (when EPIS-1 lands).

---

## 4. The chief escalation — **(EPIS-1 ships this in parallel)**

When fleet-escalate fires:

1. Runner writes `fleet_escalation_sent.json` into `cycle-<ts>/`. Contents:
   the full prompts, all agent results, cross-exam verdict, re-eval
   verdict, run-id, and the reason for escalation (`cycle_cap_hit` /
   `cost_cap` / `unresolvable_disagreement`).
2. POST to `http://mac1:8855/message` with `type=context`,
   `to=chief`, payload referencing the cycle dir.
3. Mac1's fleet daemon hands the message to its Atlas. The chief Atlas
   loads context-DNA history (gold passes, hermes ledger, recent
   criticals), reads the bundle, and produces a verdict.
4. Verdict comes back over the fleet channel and is written to
   `fleet_escalation_reply.json` in the same cycle dir.

What the chief can decide: SIGN-OFF, PARTIAL, UNRESOLVABLE. The chief
cannot turn autopilot off — only Aaron does that.

EPIS-1 also wires the fallback chain (P1 NATS → P2 HTTP → P3 chief relay
→ P7 git push) so escalation degrades gracefully when the network is
flaky. If all channels fail, the runner writes `fleet_escalation_failed.json`
and counter `fleet_escalation_send_errors` ticks. **Nothing silent.**

---

## 5. The kill switch

```bash
autopilot off      # halts at next tick boundary; only actor=user can do this
autopilot on       # sets on_permanent (actor=user only)
autopilot temp <reason> [--until <iso>]
```

Mechanics, verified in `autopilot_state.py`:

* State is a closed enum: `{off, on_permanent, on_temporary}`.
* All transitions are flock-serialized (`fcntl.flock` + atomic rename).
  Concurrent CLI calls cannot race.
* **Invariant 4:** Atlas cannot set `on_permanent`. Hard-coded refusal at
  line 359.
* **Invariant 2 + 5:** Only `actor=user` can clear `on_permanent → off`.
  Hard-coded refusal at line 369.
* **Invariant 5 extension:** Atlas cannot even transition
  `on_permanent → on_temporary`. Once Aaron locks it on, the lock is
  sticky for the user.

Why this matters: the kill switch is the *floor* of the trust ladder.
Every other layer can be wrong, captured, or hallucinating; this one is
structurally tamper-resistant by the OS file lock and the actor check.

A kill file at `/tmp/autopilot.stop` (override via
`AUTOPILOT_KILL_FILE`) is a *belt-and-suspenders* emergency stop: any
cycle observes it and aborts (`cycles_aborted_kill_file`).

---

## 6. Audit trail

Every cycle creates `~/.context-dna/autopilot-logs/cycle-<UTC-ts>/`
containing:

* `prompts.json` — the 5 prompts Synaptic chose (Layer 1).
* `agent_N.prompt` / `agent_N.result` — one per agent (Layer 2).
* `cross_exam.json` — surgeon verdicts and disagreements (Layer 3).
* `re_eval.json` — Synaptic's satisfaction verdict (Layer 4).
* `manifest.json` — final cycle state, counters delta, terminal verdict.
* `fleet_escalation_sent.json` — present only when Layer 5 fired
  **(EPIS-1)**.
* `fleet_escalation_reply.json` — chief's response **(EPIS-1)**.

**Nothing is ephemeral.** This is the auditability invariant: if a cycle
did anything, you can reconstruct it from disk weeks later. The pruner
(`scripts/prune-old-cycles.sh`) only deletes after explicit retention
windows.

---

## 7. Cost accounting

Realistic per-cycle spend:

| Component                | Cost          | Notes                              |
| ------------------------ | ------------- | ---------------------------------- |
| Synaptic (MLX local)     | $0.00         | Qwen3.6 on 5044, $0 marginal       |
| Headless agents × 5      | $0.05–$0.30   | `--max-budget-usd` per agent       |
| Cardiologist (DeepSeek)  | ~$0.27/1M tok | one cross-exam round per cycle     |
| Neurologist (Qwen local) | $0.00         | unless DeepSeek-fallback enabled   |
| Re-evaluate (MLX)        | $0.00         | local                              |
| **Per-cycle total**      | **$0.05–$0.40** | depending on agent depth        |

The cumulative `cost_cap_usd: 5.0` is the ceiling per
`archloop_runner` invocation. The runner's cost heuristic is documented
inline at `archloop_runner.py:161` ("Cost-cap accounting"). It's a
heuristic, not a meter — the floor is whatever the underlying CLIs report.

Layer 5 (fleet escalate) hits the chief's Opus, which is the most
expensive single call in the system. It is gated by the 8-cycle cap
exactly so it only fires when local thinking has provably failed.

---

## 8. Failure modes

For every failure, ZSF requires: (a) a named counter, (b) an artifact on
disk, (c) an observable channel. No `except Exception: pass`.

| Failure                        | Counter                              | Fallback / artifact                                                |
| ------------------------------ | ------------------------------------ | ------------------------------------------------------------------ |
| `claude` CLI unauthenticated   | `headless_executor_errors{auth}`     | STATUS: SKIP written to result; cycle continues with PASS=0/5      |
| `claude --print` timeout       | `headless_executor_timeouts`         | result file: "claude timed out after Ns"; STATUS: SKIP             |
| `claude` OSError on spawn      | `headless_executor_oserrors`         | result file: "OSError invoking claude: …"                          |
| Per-agent budget exceeded      | `headless_executor_budget_exceeded`  | STATUS: SKIP (claude CLI enforces, we record)                      |
| MLX server (5044) down         | LLM-priority-queue counter           | Provider chain falls back per `LLM_EXTERNAL_PROVIDER`              |
| DeepSeek API failure           | `cross_exam_errors`                  | Cross-exam marked HOLD, cycle defers                               |
| Cost cap exceeded mid-cycle    | `cycles_aborted_cost`                | Cycle aborts, manifest records `aborted=cost_cap`                  |
| Cycle cap hit                  | `cycles_aborted_cycle_cap`           | Triggers Layer 5 escalation                                        |
| Kill file present              | `cycles_aborted_kill_file`           | Cycle exits immediately                                            |
| User toggled `autopilot off`   | `cycles_aborted_user_off`            | Tick observes state; refuses to run                                |
| Synaptic JSON parse failure    | `cycles_aborted_synaptic_parse`      | Manifest records, cycle aborts before spending money               |
| Agent timeout in poll          | `cycles_aborted_agent_timeout`       | Partial results captured, cycle aborts                             |
| Fleet escalation send error    | `fleet_escalation_send_errors` *(EPIS-1)* | `fleet_escalation_failed.json` written, no automated retry    |

---

## TRUST LADDER

Explicit map of *what each layer is allowed to decide unilaterally* vs.
*what gets escalated*. Read top-down. Lower = more trusted, narrower scope.

| Layer | Decides unilaterally                                              | Escalates to                          |
| ----- | ----------------------------------------------------------------- | ------------------------------------- |
| **0. Aaron** | Everything. Source of `on_permanent`. Only actor that clears it. | nobody                                |
| **launchd tick** | Whether *now* is a tick boundary. Reads autopilot state. | refuses to run if state ≠ on          |
| **1. Synaptic Review** | Which 5 prompts to run.                              | runner (always proceeds to Layer 2)   |
| **2. Headless Agents** | How to solve their individual prompt.                | cross-exam (always)                   |
| **3. 3-Surgeons** | HOLD vs SIGN-OFF *for this round*.                        | re-eval (always)                      |
| **4. Synaptic Re-Eval** | Cycle is satisfied → exit. Else → another round. | next round, capped at 8               |
| **5. Fleet Escalate** *(EPIS-1)* | Whether to POST to chief. Cannot turn autopilot off. | mac1 chief Atlas                  |
| **5b. Chief Atlas (mac1)** | SIGN-OFF / PARTIAL / UNRESOLVABLE.            | Aaron (via inbox / webhook)           |
| **6. Final Verdict** | Writes manifest. Surfaces UNRESOLVABLE to Aaron.    | Aaron                                 |

**Hard invariants no layer can break:**

1. Atlas cannot set `on_permanent`.
2. Only `actor=user` can clear `on_permanent → off`.
3. Cost cap is a wall; no layer can extend it mid-cycle.
4. Cycle cap (8) is a wall; hitting it forces escalation, not "one more
   try".
5. Every counter increment is durable. Every artifact is written to disk
   before the cycle exits.
6. Distinctness: Cardiologist ≠ Neurologist ≠ Atlas at the vendor level.

If you're adding a feature and it requires breaking one of these, stop and
write a doc justifying the change. The whole stack's trustworthiness rests
on these six.

---

*Last updated: 2026-05-14. If Layer 5 ships before this doc is updated,
remove the "(EPIS-1 ships this in parallel)" markers and add the implementation
paths.*
