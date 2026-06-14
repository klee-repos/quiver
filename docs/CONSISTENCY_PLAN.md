# Temporal Consistency & Proof Layer — Implementation Plan

## Problem (from the user)

The agent's decision-making must be **consistent with itself over time** — not
conflicting or broken — across two dimensions:

1. **Portfolio construction** (the book/weights/universe derived from the strategy)
   must change **strategically, infrequently, and consistently — data-backed with proof.**
2. **Buy/sell of an asset day-to-day** may be more frequent, but must stay
   **consistent day-to-day — data-backed with proof.**

### Decisive user direction (eng-review Q&A)

> "I don't want it hardcoded. I want it based on the memory… I just want a consistent
> strategy. You're okay to buy one day and sell the other **as long as it's part of a
> consistent strategy, not just random**."

This **rejects a hardcoded N-day dwell window.** The buy/sell gate is **memory-grounded**:
a reversal is allowed when it is *explained by a consistent, recorded strategy*
(a stop/target/review trigger firing, or a genuine recorded thesis change), and is
suppressed only when it is **random** — an ungrounded reversal that silently contradicts
the recorded stance, or serial unexplained churn. Also decided: **allow loss-catalyst
sells** inside any window; **full scope, including Component E.**

## What exists today (verified by a fan-out code audit)

- **Buy/sell:** In classic mode (default) the only cross-tick guard is per-day dedup
  (`already_acted`, `ticker_action` PK) → ≤1 action/ticker/day, reset each day. The
  intraday gates (`cooldown_ok`, `within_action_cap`, `is_material_change`) are all
  `trade_date`-scoped; `is_material_change` *passes* a Buy→Sell flip by design. **Buy(Day1)
  → Sell(Day2) → Buy(Day3) is unconstrained.** No proof is recorded *of* a decision.
- **Portfolio:** Accidentally consistent because **inert**, not gated: `set_active_book`
  has zero callers; `rebalance_enabled` + both `auto_apply_*` default OFF; `learn-review`/
  `universe-apply` are absent from `TICK.md`. `regime_label` is stateless (no dead-band,
  no confirmation, no min-dwell); `thesis_state` persists a regime but `get_thesis_state`
  is dead; strategy tables are latest-snapshot UPSERTs with **no transition history**.
- **Proof primitives are excellent but observability-only:** `lib/risk.Metric` carries
  `value + N + formula + actual inputs + caveats` with an auditable `.proof()`;
  `sustained_underperformance` is real rolling-window hysteresis. But `derive_guidance` is
  explicitly "context… does NOT change position sizing." Proof is read, never produced.

## Architectural invariants any solution MUST preserve

- **Python owns every decision/guardrail; never prose.** Gates are deterministic Python in
  `lib/signals.py` (buy/sell) or `lib/strategy.py` + `tick.py` glue (portfolio). `TICK.md`/
  prompt changes may only *present* context, never *gate*. A model-declared "catalyst" never
  auto-passes; the binding part is always Python-verified or memory-rate-limited.
- **The analysis path** (`analyze.py`, `lib.memory`, `lib.reflect_memory`, `lib.risk`,
  `lib.strategy_context`) reads the **decision-memory scorecard (decisions + outcomes) only**
  — never trading limits or the broker. The wall test (`signals.py` never imports `risk`)
  stays intact → the **binding** consistency gate cannot import `lib.risk`; its proof is
  assembled by `tick.py` plan glue. (`risk.py` MAY import the pure `signals.reversal_rate`
  to avoid duplicating the counter — one implementation, two callers.)
- **Fail safe:** on missing/ambiguous data a gate takes the MORE conservative path (suppress
  the reversal / keep the current regime), never fail-open. A gate-internal error suppresses
  only the *reversal* (never a continuation/buy) and never throws into a tick.
- **Every guardrail change is mirrored by a `tests/test_units.py` case** (offline). New
  import-graph invariants get a wall test.
- **`TICK.md` ⇄ `tick.py` lockstep** for any new subcommand / JSON field.
- **Order idempotency preserved:** the gate sits BEFORE order reservation; never perturbs
  `ref_id` reservation or the `(trade_date,ticker)` dedup.

---

## Component A — Memory-grounded strategy-consistency gate  [buy-sell, HIGH]

The #1 fix. A deterministic Python gate that allows a buy↔sell reversal **only when it is
grounded in a consistent, recorded strategy**, and suppresses **random** reversals. No
hardcoded day-count.

### Grounding paths (a reversal is allowed iff one holds)

```
                         today's decision for TICKER (intent from signals.plan_action)
                                              │
                  ┌───────────────────────────┴───────────────────────────┐
            continuation/neutral                                       reversal (open<->close vs recorded stance)
            (same direction or hold)                                       │
                  │                          ┌────────────────────────────┼────────────────────────────┐
               ALLOW                    G1 plan trigger             G2 recorded basis change        (neither)
            "continuation"          (Python-VERIFIED from the      (model declares a NEW basis/      │
                                     prior decision's recorded      catalyst, logged to the          │
                                     stop/target/review horizon     change-log WITH its proof)       │
                                     vs the live quote/elapsed)            │                          │
                                          │                    ┌──────────┴──────────┐               │
                                       ALLOW              flip-count over        within budget        │
                                "executing_recorded_plan"  memory budget?             │               │
                                  (incl. Q2 loss-catalyst)      │                   ALLOW              │
                                                              SUPPRESS         "recorded_basis_change" SUPPRESS
                                                            "basis_churn"                        "ungrounded_reversal"
```

- **G1 — executing the recorded plan (Python-verified, strongest):** the prior decision for
  this ticker recorded a `stop_loss` / target / `next_review_hours`. Today's reversal is that
  plan firing — verified against the live quote / elapsed time. This is the "consistent
  strategy playing out": a swing entry that hits its target → sell is *consistent*, not a flip.
  Includes the **loss-catalyst** (live quote ≤ recorded stop, or position down ≥
  `loss_catalyst_pct`) the user approved.
- **G2 — a recorded, proof-backed basis change:** the new decision declares a `basis`
  (strategy/thesis tag) + optional `catalyst` that DIFFERS from the recorded stance's basis;
  it is logged to the `strategy_change_log` with the model's proof. Allowed **unless** the
  ticker's recent *ungrounded-flip count* over a memory window exceeds the budget — serial
  unexplained changes of mind ARE the operational definition of "random," and that rate is
  computed from memory, not a fixed calendar rule.
- **Neither → SUPPRESS** (`ungrounded_reversal`): today's sell silently contradicts the
  recorded buy (no plan trigger, no declared basis change). Fail-safe.

### Pieces

**A1 — pure helpers in `lib/signals.py`** (siblings of `is_material_change`):
```
direction_of(intent) -> "open" | "close" | "neutral"        # buy->open, sell->close, else neutral
is_reversal(prior_intent, new_intent) -> bool               # open<->close flip only
reversal_rate(intent_sequence) -> (ungrounded_flips, transitions)   # pure counter over recorded history
strategy_consistency_verdict(*, prior_intent, new_intent, plan_trigger, basis_changed,
                             recent_ungrounded_flips, max_ungrounded_flips) -> (allowed, reason)
```
`strategy_consistency_verdict` is pure and total; `max_ungrounded_flips <= 0` OR
`consistency.enabled=false` makes it a guaranteed allow (byte-identical to today).

**A2 — `plan_trigger` is Python-verified in `tick.py`** (never the model's word):
`stop_hit` (quote ≤ recorded `stop_loss`), `loss_catalyst` (down ≥ `loss_catalyst_pct` vs
recorded `decision_price`), `target_hit` (quote ≥ recorded target if present),
`review_due` (now ≥ recorded `decided_at` + `next_review_hours`). Any one → G1.

**A3 — cross-day ledger reads in `lib/ledger.py`** (no schema change for the trade read):
```
last_completed_trade(ticker) -> {intent, trade_date, ts, signal, decision_price, stop_loss,
                                 target_price, next_review_hours, decided_at, basis} | None
recent_decisions(ticker, n) -> [{trade_date, intent, signal, basis, plan_trigger, grounded}...]  # newest-first
```
`last_completed_trade`: most recent `actions` row matching the existing `_TRADE_FILTER`,
NOT date-scoped, joined to its originating decision. `recent_decisions`: from the `decisions`
table for the flip-count.

**A4 — wire into `tick.py _run_plan`**, right after `intent, frac = signals.plan_action(...)`
and BEFORE the buy/sell branches: compute `plan_trigger` (A2) + `basis_changed` (new vs
recorded basis) + `recent_ungrounded_flips`; call `strategy_consistency_verdict`; if suppressed,
record `status="skipped"`, `detail=reason`, persist the proof token (Component B), and
`continue`. Discretionary buy/sell only — never the rebalance/reconcile sell pass (separately
gated) and never after the halt return. Independent of `intraday_enabled`. Wrapped so a gate
error suppresses only the reversal and never throws.

**A5 — config (`risk.consistency`, memory-derived, NOT a calendar dwell):**
`enabled` (default **true**), `max_ungrounded_flips` (default **1** — one unexplained change of
mind per window is tolerated; serial churn is not), `flip_window` (lookback decisions, default
**6**), `loss_catalyst_pct` (default **8.0**). `enabled:false` or `max_ungrounded_flips:0`
restores byte-identical classic behavior.

---

## Component B — Decision proof token  [proof, both, HIGH]

Every binding decision carries a structured, reproducible proof so a *grounded* change is
distinguishable from a *random* one — exactly the "data-backed with proof" requirement.

**B1 —** add `proof_json TEXT` + `basis TEXT` + `plan_trigger TEXT` to `decisions`
(migration `_migrate_decisions`, PRAGMA-diff). `record_decision` gains optional kwargs
(back-compat NULL).
**B2 —** in `tick.py _run_plan`, assemble + persist per-decision proof:
`{decision_price, position_pct, sizing_source, clamps:{ceiling,daily_cap,room_ticker,room_target},
prior_intent, prior_basis, new_basis, basis_changed, plan_trigger, reversal, consistency:{allowed,
reason}, recent_ungrounded_flips, scorecard:{hit_rate,n}}`. Plain dict in tick.py glue (no
`lib.risk` import on the binding path).
**B3 —** `analyze.py` extracts `basis` (from a `**Strategy Basis**` PM field) + `catalyst`
(from `**Catalyst**`), like it already extracts Entry/Stop/Position Pct. Absent basis → a
reversal with no plan trigger is ungrounded → suppressed (fail-safe). Observability:
`tick.py decision-proof --id N` prints the stored proof; the digest gains a per-ticker "why" line.

---

## Component C — Macro regime hysteresis + confirmation + min-dwell  [portfolio, HIGH]

Stop the strategic posture (regime → recommended book → DERISK proposal) from flipping on PCE
boundary noise.

**C1 — pure helper in `lib/strategy.py`:**
```
REGIME_DEADBAND_PCE = 0.1                                  # mirrors lib/goal._REGIME_DEADBAND_PCT
regime_with_confirmation(prior_state, reading, *, confirm_n, min_dwell_days, today)
    -> {effective_regime, pending_regime, confirm_count, regime_since, changed, reason}
```
deploy/standdown triggers get a **dead-band**; a *different* regime must recur `confirm_n`
consecutive readings before it becomes effective; once effective it cannot change for
`min_dwell_days`; missing/ambiguous reading keeps the current effective regime (sticky), never
jumps to DEPLOY.
**C2 — persist confirmation state:** extend `thesis_state` with `pending_regime`,
`pending_since`, `confirm_count`, `regime_since` (migration `_migrate_thesis_state`); wire
`get_thesis_state` as the reader in `_run_learn_review` / `_run_construct`.
**C3 — effective regime drives everything:** `select_active_book`, the DERISK proposal, and
`recommended_book` consume the confirmed effective regime, not the raw per-tick reading.
**C4 — config (`strategy.consistency` block in `lib/strategy.py`):** `regime_confirm_n`
(default **2**), `regime_min_dwell_days` (default **5**), `regime_deadband_pce` (default **0.1**).

---

## Component D — Strategic change-log + proof/diff gate on the write path  [proof, portfolio, HIGH]

Give every strategic change an append-only audit trail + a diff gate, so "infrequent &
proof-backed" is enforced and after-the-fact oscillation is detectable.

**D1 — new append-only table `strategy_change_log`** (auto-creates):
`(id, goal_id, changed_at, change_type[regime|book|weight|status|basis], ticker, from_value,
to_value, trigger, reason, proof_json)`.
**D2 — emit a row only on a REAL diff** by wrapping the mutators: make `set_active_book` the
**sole** book mutator and log a `book` change; `upsert_thesis_state` logs a `regime` change vs
the prior `get_thesis_state`; `set_holding_status` logs a `status` change; `upsert_target_holding`
logs a `weight` change when the weight differs.
**D3 — `strategy-set` diff + proof:** `_run_strategy_set` diffs the new book vs the current
ledger book, logs each changed holding/weight with a reason, and surfaces a min-interval note
(`strategy.consistency.min_strategy_set_interval_days`, default **0** = informational).
**D4 — read tool** `tick.py strategy-history [--limit N]` + a digest section.

---

## Component E — Universe-change anti-oscillation + confirm-over-N + apply re-validation  [portfolio, FULL]

Active when continual learning is enabled (OFF/absent by default), but built fully per user
direction.

**E1 —** `record_universe_proposal` suppresses re-proposing a `content_hash` rejected/applied
within `proposal_cooldown_days` (config; default **5**), reading the change-log history for that
hash (not just open 'proposed' rows).
**E2 —** a REMOVE/DERISK proposal becomes **actionable** only after it has recurred on
`>= universe_confirm_days` distinct days (first-seen age + recurrence in `build_proposals`,
surfaced to `universe-apply`).
**E3 —** `universe-apply` **re-validates** `sustained_underperformance` at apply time and, on a
REMOVE, calls `lib/universe.apply_remove` → `redistribute_to_cash` → `validate_book` (these pure
helpers exist, uncalled) so the book stays summed to ~100% and a malformed book is never committed.
**E4 — config (`strategy.learning`/`consistency`):** `proposal_cooldown_days` (**5**),
`universe_confirm_days` (**2**).

---

## Component F — Advisory consistency visibility (analysis-side; read-only)  [buy-sell, FULL]

Complement to the Python gate: make stance churn *visible with proof* to the model and add a
no-reverse-without-grounding contract to the prompts. Reads decisions only; never gates.

**F1 —** `recent_signals(ticker, n)` ledger read (decisions only, newest-first).
**F2 —** `signal_stability(decisions)` proof-bearing `Metric` in `lib/risk.py` (reuses the pure
`signals.reversal_rate` — one counter): reversals / transitions over the window, with inputs + N.
**F3 —** inject a "stance history + reversal-rate (with proof)" block into
`reflect_memory.build_past_context` (own try/except; never shrinks the proven scorecard).
**F4 —** prompt clauses in `prompts/agents/*` + PM/Trader markdown: emit `**Strategy Basis**`
and (on a reversal) a named `**Catalyst**`; "a reversal of your prior stance must name a new
catalyst — absent one, hold your prior stance." Presentation only; Component A is the enforcement.

---

## Phasing

1. **Ledger** — migrations (`_migrate_decisions`, `_migrate_thesis_state`), `strategy_change_log`
   table + writer, reads (`last_completed_trade`, `recent_decisions`, `recent_signals`,
   change-log/proposal-history readers) + unit tests.
2. **Pure helpers** — `signals.{direction_of,is_reversal,reversal_rate,strategy_consistency_verdict}`,
   `strategy.regime_with_confirmation`, `risk.signal_stability` + unit tests.
3. **tick.py wiring** — plan consistency gate + proof token (A4/B2), construct/learn-review
   confirmed-regime (C3), strategy-set diff+log (D3), universe-apply re-validation + change-log
   wraps (D2/E3), new read subcommands (B3/D4) + unit tests.
4. **Analysis-side** — `basis`/`catalyst` extraction (B3), `recent_signals` block (F1/F3) +
   prompt clauses (F4) + wall tests.
5. **Config** — `lib/config.py` `ConsistencyConfig` (risk side) + `lib/strategy.py` consistency
   block, `config.yaml`(+`.example`), `strategy.yaml`(+`.example`) + validation + tests.
6. **`TICK.md`** + `CLAUDE.md` + `README` lockstep.
7. **E2E tests** (below).

## E2E test strategy — actually runs it

New `tests/test_e2e_consistency.py` drives the **real `tick.py` subprocess** subcommands against
a temp `state/` (mirrors `tests/test_e2e.py`), asserting on stdout JSON + ledger rows:
- **Random flip (suppressed):** seed Day-1 BUY (commit `placed`, basis "X"), Day-2 feed a Sell
  analysis with NO new basis + quote above the recorded stop → run real `tick.py plan` → assert
  **no sell order** + decision `ungrounded_reversal` + proof token persisted.
- **Grounded flip — plan trigger (allowed):** same setup, but Day-2 quote ≤ recorded stop → assert
  Sell order emitted, reason `executing_recorded_plan:stop_hit`.
- **Grounded flip — basis change (allowed, then churn-suppressed):** Day-2 Sell with a new
  `basis`/`catalyst` → allowed (`recorded_basis_change`) + a `strategy_change_log` row; repeat
  ungrounded flips until the budget trips → suppressed `basis_churn`.
- **Regime hysteresis:** drive `tick.py construct`/`learn-review` with an oscillating
  `macro_reading` → effective regime does NOT flip until `confirm_n` consecutive readings;
  `strategy_change_log` shows exactly one regime change.
- **Proof:** every decision row has populated `proof_json`; `tick.py decision-proof --id` renders it.
- **Strategy-set diff:** run `strategy-set` twice with one changed weight → change-log captures
  exactly the diff.
- Plus the full existing `tests/test_units.py` stays green.

## NOT in scope (deferred)

- **Rolling multi-day turnover/$ budget** (a magnitude floor on cross-day churn) — the directional
  consistency gate is the leverage; a $-turnover budget is a follow-up once it's validated live.
- **Automatic construct-time book swap** (wiring `set_active_book` into `construct`) — stays
  human/config gated by design; this plan only makes the swap *safe* (hysteresis + change-log).
- **Model-fetched macro data** — `macro_reading` stays operator-supplied (the invariant forbids
  the bot fetching macro); C only adds provenance/stickiness, not fetching.

## What already exists (reused, not rebuilt)

- `lib/risk.Metric` — the proof format (reused for F2; not reinvented).
- `lib/portfolio.needs_rebalance` / `lib/goal._REGIME_DEADBAND_PCT` — the hysteresis/dead-band
  templates (C mirrors them).
- The `actions` event log + `_TRADE_FILTER` — the cross-day trade read needs no schema change.
- `universe_change_log` content-hash dedup + `lib/universe.apply_remove/redistribute_to_cash/
  validate_book/validate_add` — E wires the existing-but-uncalled helpers.
- The `_migrate_*` PRAGMA-diff migration pattern — B/C reuse it.
- `get_thesis_state` (currently dead) — C finally gives it a reader.

## Failure modes (new codepaths)

| Codepath | Failure | Test? | Error-handled? | Visible? |
|---|---|---|---|---|
| consistency gate (plan) | ledger read throws | yes | yes — wrap; suppress only the reversal, never throw | digest skip row |
| consistency gate | ambiguous/missing prior | yes | yes — no prior ⇒ not a reversal ⇒ allow; unreadable plan ⇒ suppress (conservative) | proof token |
| `proof_json` write | malformed/oversized | yes | yes — best-effort; NULL proof never blocks trading | — |
| regime confirmation | thesis_state read throws | yes | yes — keep current effective regime (sticky) | construct output |
| change-log write | insert throws | yes | yes — best-effort; observability, never blocks the tick | — |
| universe-apply re-validate | series too thin | yes | yes — INSUFFICIENT ⇒ refuse apply (fail-safe) | apply output |

No new failure mode is both silent AND unhandled.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_RESOLVED | Scope challenge (12 files / 1 new layer) → user chose FULL scope incl. E. Architecture: reversal-gate reframed from hardcoded N-day dwell → memory-grounded strategy-consistency gate per user direction. Migration safety: reuse `_migrate_*` PRAGMA-diff (additive cols) + CREATE-IF-NOT-EXISTS (new table); no row rewrites. Wall preserved: binding gate in `signals.py` (no `risk` import); proof assembled in `tick.py` glue; `risk.py` may import pure `signals.reversal_rate`. 6 failure modes mapped, none silent+unhandled. |
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (eng-scoped change) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (backend only) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **Key decisions (user):** (1) reversal gate must be **memory-grounded, not hardcoded** — buy/sell flips OK when part of a consistent recorded strategy, suppressed only when random; (2) **allow loss-catalyst sells** inside any window; (3) **full scope including E**.
- **UNRESOLVED:** none.
- **VERDICT:** ENG CLEARED — ready to implement. Memory-grounded gate (A), proof token (B), regime hysteresis (C), strategic change-log (D), universe anti-oscillation (E), advisory+prompts (F), all with unit + real-subprocess E2E coverage.

## Post-implementation adversarial verification

A second multi-agent adversarial pass over the binding (real-money) path found **3 real,
reproduced bugs** (0 false positives), all since fixed + regression-tested + independently
re-verified:
1. **`recent_completed_trades` JOIN fan-out** (HIGH) — duplicate decision rows per
   (day,ticker,intent) filled the flip window → under-counted churn → gate failed *permissive*.
   Fixed with a correlated single-decision subquery.
2. **Reconcile/rebalance SYSTEM sells polluted the prior-stance read** (HIGH) — fixed by
   excluding `signal IN ('REBALANCE','RECONCILE')` from both cross-day reads (a system exit is
   not a discretionary stance).
3. **Regime confirmation was dormant** (HIGH, no wrong-trade impact) — `learn-review` (its only
   writer) isn't in the runbook. Moved the confirmation into `construct` (the in-runbook STEP 3b,
   the SOLE confirmer); `learn-review` now only READS the effective regime. Also: a missing
   macro_reading is now a no-op (no erosion), and the dwell-lock clamps a skewed/unparseable
   `regime_since` to a locked state.

Final: 599 unit + 17 + 38 + 12 + 10 = **676 checks, 0 failures**, including a real-`tick.py`-
subprocess E2E. Known dormant-by-default (pre-existing, OFF): the universe-change anti-oscillation
(E1/E2/E3) only runs when `learn-review`/`universe-apply` are invoked by the operator — the regime
hysteresis (C) is now live via `construct`.
