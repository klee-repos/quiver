# Quiver Goal-Strategy Layer — Formal Plan

**A macro-driven, self-learning portfolio engine targeting +15% / 12 months, hosted continuously on AWS.**

Status: DRAFT (pending `/plan-eng-review` + the decisions in §1)
Author: Claude (design synthesized from a multi-agent workflow + the BRAX reference harness)
Date: 2026-06-13
Scope: an **evolution** of the existing Quiver bot — not a rewrite. Every new behavior ships OFF by default; the validated once-a-day market-order path is byte-identical when the new features are disabled.

---

## 0. The one invariant that governs everything

From `CLAUDE.md`, and non-negotiable here:

> **All trading / risk / portfolio / sizing / cadence / halt decisions live in deterministic Python. The LLM orchestrator only executes broker calls and never reasons about the market.**

So the 15% goal, the target book, rebalancing, add/remove-asset logic, and the learning loop are **Python (new `lib/` modules + new `tick.py` subcommands + new ledger tables)** — never orchestrator prose. The analysis path (`analyze.py`) may read decision memory but **never trading limits or the broker**. The new AWS harness is **execution-only**: it drives `TICK.md`, and a mechanical guard denies any order whose `ref_id` the Python brain did not authorize.

---

## 1. Decisions needed from you (with my recommended defaults)

Each has a safe default so the plan is buildable as-is; confirm or override.

| # | Decision | My recommended default |
|---|---|---|
| D1 | **AWS account / region / budget ceiling** | `us-east-1`, ~$20/mo AWS ceiling (excl. Anthropic/DeepSeek tokens). Use **SSM Parameter Store** (free) over Secrets Manager to land ~$10–15/mo on a `t4g.small`. |
| D2 | **Paper vs live first in prod** | **dry_run-first**: a full market day in AWS reviewing-but-never-placing, digests diffed vs a local run; flip `dry_run:false` only on a clean fresh day. |
| D3 | **Cash sleeve (SGOV 45%)** | **Leave as uninvested buying power** (residual, never churned) — matches "cash is the only ballast." Option to actually hold SGOV for the ~3.6% yield is a one-line flag if you want it. |
| D4 | **How the bot learns the macro reading** (core-PCE / Fed action) | A committed **`macro_state.yaml`** the operator edits (operator DATA — the bot can't fetch macro within the invariant), read by `construct`. Stale/absent → conservative default `core` book. |
| D5 | **Auto add/remove aggressiveness** | Risky direction (add exposure, rotate to dial-up) **always human-gated**. Only **de-risk-to-cash** may auto-apply, and even that **defaults OFF** (`auto_apply_derisk:false`). |
| D6 | **Dial-up (63/37) activation** | Python *recommends* it on a deploy reading, but **activating requires an operator flag** (`dial_up_63_37.enabled: true`) — fail-safe like `dry_run`. |
| D7 | **SOL 3% (no equities-MCP wrapper)** | **Park the 3% in cash** for now (RH crypto isn't exposed via the trading MCP today). Revisit if a crypto MCP lands. BTC→IBIT, ETH→ETHA execute normally. |
| D8 | **Rebalance cadence** | **Accept gradual**: the `(trade_date,ticker)` dedup means a full 11-name rebalance unfolds over several days — fine (lowers timing risk) for a small account. |
| D9 | **Glidepath shape & whether it drives action** | **Linear to +15%** over 12 months; **observability + advisory context only** at first (ahead/behind does NOT auto-trade). "Behind → tilt dial-up" can be added later behind a flag. |
| D10 | **Robinhood headless auth** | One-time interactive `/mcp` auth as the service user (BRAX's `gh auth login` analog), credential persisted on the box; **`AUTH_ERROR` → hard-stop + Resend alert, never trade blind**. *Requires confirming Robinhood ToS permits headless remote-MCP use before live.* |
| D11 | **Executor model** | Cheapest reliable tool-using model (**Haiku/Sonnet-class**), **low effort, ultracode OFF, small `--max-turns`** — the pass only shuttles JSON per `TICK.md`; the Python brain does the thinking. |
| D12 | **SGOV benchmark granularity** | A **linearized ~3.6%/yr daily-prorated constant** from `strategy.yaml` (no new market read) for per-decision alpha-vs-cash. |

---

## 2. What we're building (system overview)

Four additions layered onto today's Quiver (`analyze.py` decides per-ticker; `tick.py` is the deterministic brain; `lib/*` holds all clamps; the orchestrator executes MCP calls):

1. **Macro-strategy + portfolio-construction layer** (Python) — encodes the 15% goal + macro thesis + the two books (`core_55_45` default, `dial_up_63_37` OFF) as *data*; computes a glidepath, target weights, drift, and "what the book should look like today."
2. **Per-asset daily decision integration** — reuses `analyze.py`; injects each asset's *target weight + sleeve thesis + goal progress* as **read-only advisory context** so per-ticker reasoning serves the book. Sizing stays Python-clamped in `tick.py plan`.
3. **Continual learning + add/remove engine** — extends `lib/reflect_memory.py` + `lib/risk.py` + the ledger to score each holding/sleeve vs thesis against the goal gap and **propose** add/remove/derisk with proof. Risky changes are human-gated; only de-risk-to-cash may auto-apply (OFF by default).
4. **Headless AWS harness** — a thin systemd-managed daemon on a persistent box that, on a market-hours schedule, runs **one tick by driving `TICK.md` through the `claude` CLI in `-p` mode** with the same Robinhood + Resend MCPs. Execution-only.

### The wall holds from both sides
- Analysis path (`analyze.py` → `reflect_memory.build_past_context`) gains **one** try/except-isolated read-only block sourced only from `strategy.yaml` + strategy/decision ledger tables. It never reads dollar caps, buying power, positions, or any MCP.
- Target weights enter as a trading input in exactly **one** place — `tick.py cmd_plan` — where they become one *more* `min()` term in the existing clamp stack. They can only **reduce** a buy, never bypass a cap.
- `lib/signals.py` + `lib/portfolio.py` never import `risk`/`reflect_memory`; `lib/strategy_context.py` + `lib/learn.py` never read the broker/limits. The existing boundary test (`tests/test_units.py:815-819`) is extended to lock all of this.

---

## 3. The strategy, encoded as data

A new committed **`strategy.yaml`** (sibling of `config.yaml`, **zero secrets**), validated like `config.yaml`, **fail-safe to INACTIVE** if absent/garbled (never crashes a tick, never silently switches to the riskier book):

```yaml
schema: 1
goal:
  target_return_pct: 15
  horizon_months: 12
  benchmark: "SGOV ~3.6%"
  benchmark_annual_pct: 3.6
  constraint: "Robinhood-tradable only; crypto allowed; cash is the only ballast"
macro_thesis:
  version: "2026-06-13"
  summary: "CAPE ~40; hawkish Warsh Fed (0 cuts priced, 45-57% odds of a 2026 hike); 10yr ~4.5%; crypto mid-bear (BTC ~$63.8k, projected cycle low ~Q4 2026). Base case ~+6%; +15% only in a bull-tilted Fed-pivot year."
  catalysts_to_watch: ["2026-06-17 FOMC", "2026-06-25 core PCE", "2026-07-14 CPI"]
  deploy_trigger_pce_pct: 2.5     # core PCE <= this -> DEPLOY regime
  standdown_trigger_pce_pct: 3.5  # core PCE >= this (or a hike) -> STAND DOWN
  standdown_on_hike: true
  correlation_note: "semis+uranium+EM+crypto are ONE AI/rate/liquidity bet; they fall together on a hike."
rh_tradable_confirmed: [SMH, SOXX, URA, GRID, XLV, RSP, EEM, IBIT, FBTC, ETHA, FETH, COIN, MSTR, MARA, RIOT, CLSK, IREN, SGOV, AVUV, QQQ, XLE, SOL]
books:
  core_55_45:                     # DEFAULT, validated active book
    default: true
    holdings:   # sleeve, weight%, band%, quotable (ETF wrappers true; spot crypto false)
      - {sleeve: Semiconductors,  ticker: SMH,  weight: 9, band: 4}
      - {sleeve: Semiconductors,  ticker: SOXX, weight: 4, band: 3}
      - {sleeve: Uranium/Power,   ticker: URA,  weight: 7, band: 3}
      - {sleeve: Uranium/Power,   ticker: GRID, weight: 6, band: 3}
      - {sleeve: Value/Defensive, ticker: XLV,  weight: 9, band: 4}
      - {sleeve: Value/Defensive, ticker: RSP,  weight: 5, band: 3}
      - {sleeve: Emerging mkts,   ticker: EEM,  weight: 6, band: 3}
      - {sleeve: Crypto BTC,      ticker: IBIT, weight: 4, band: 2}
      - {sleeve: Crypto alt,      ticker: SOL,  weight: 3, band: 2, quotable: false}  # parked in cash (D7)
      - {sleeve: Crypto ETH,      ticker: ETHA, weight: 2, band: 2}
      - {sleeve: Cash,            ticker: SGOV, weight: 45}   # residual ballast (D3)
  dial_up_63_37:
    enabled: false                # OFF; activating dial-up requires this flag (D6)
    holdings: [SMH 17, URA 5, GRID 3, XLV 9, RSP 5, EEM 9, IBIT 6, SOL 4, ETHA 2, IREN 3, SGOV 37]
learning:
  underperf_window: 8
  underperf_hit_floor: 0.40
  underperf_mean_floor: 0.0
  min_resolved_n: 5
  sleeve_review_min_n: 6
  derisk_on_standdown: true
  auto_apply_derisk: false        # safe direction; still OFF by default (D5)
  auto_apply_universe_changes: false   # risky direction; human-gated (D5)
  goal_gap_derisk_pct: -5.0
  proposal_expiry_days: 5
```

Validation (`lib/strategy.py`, validate-or-raise): each book's weights sum ~100 (tolerance); every ticker in `rh_tradable_confirmed`; `0 < band < weight`; `deploy_pct < standdown_pct`. Absent/garbled → layer INACTIVE.

---

## 4. Components

> new = brand-new module · ext = extends an existing file (no rewrite)

| Module | new/ext | Responsibility |
|---|---|---|
| `lib/strategy.py` | new | Parse/validate `strategy.yaml` → frozen `StrategyConfig`; deterministic active-book selection from the operator macro reading; sleeve-thesis lookup. Imports stdlib + a thin `lib.ledger` reader only. |
| `lib/portfolio.py` | new | Pure drift / target-$ / band / trim-share / `construct_target_book` math. No I/O, no broker. SGOV is the residual; non-quotable tickers flagged/skipped. |
| `lib/goal.py` | new | Pure glidepath + progress-vs-SGOV math (proof-bearing, divide-by-zero-safe). |
| `lib/strategy_context.py` | new | The single analysis-side **read-only** renderer: target weight + sleeve thesis + coarse goal progress for one ticker (full/compact). Carries the "CONTEXT only — does NOT change sizing" disclaimer. Forbidden from importing caps/positions/broker. |
| `lib/learn.py` | new | Continual-learning scorer (analysis side). Reads ledger only; may import `risk`+`memory`; never `signals`/broker/caps. Emits KEEP / FLAG / PROPOSE_REMOVE / PROPOSE_DERISK / PROPOSE_ADD with proof. |
| `lib/universe.py` | new | Add/remove transition logic + RH allow-list + quotable gate; weight conservation (freed weight → SGOV); `validate_book`. Pure. |
| `lib/runlock.py` | new | Ledger-backed single-run mutex (belt-and-suspenders with serial scheduling). |
| `lib/signals.py` | ext | + `room_under_target` (one more `min()` term) and `resolve_target_sell_quantity` (trim/exit). Still never imports `risk`/`reflect_memory`. `resolve_buy_dollars` gains an optional kwarg defaulting to today's exact behavior. |
| `lib/risk.py` | ext | + `sustained_underperformance` + `contribution_vs_thesis` (proof-bearing). No change to existing fns. |
| `lib/reflect_memory.py` | ext | Append a 4th isolated read-only block (strategy/target) in `build_past_context`; capture `bundle['target']` once; thread `cfg.strategy` best-effort. Never-raise contract preserved. |
| `lib/config.py` | ext | Optional `strategy_path` + lazy `cfg.strategy` load (fail-safe None) + a few conservative `risk:` rebalance knobs (all default to today's behavior). |
| `lib/ledger.py` | ext | + six `CREATE TABLE IF NOT EXISTS` blocks + reader/writer methods (existing conventions). No change to safety tables. |
| `tick.py` | ext | + `strategy-set`, `construct`, `goal-track`, `learn-review`, `universe-apply` subcommands; target-aware `cmd_plan` gated behind `rebalance_enabled`. |
| `analyze.py` | ext | Thread `cfg.strategy` into context build. `extract_fields` UNCHANGED (no model-controlled allocation output). |
| `deploy/` | new | The headless harness + IaC (see §8). |

---

## 5. Ledger schema changes (six new tables, auto-created)

Appended to `lib/ledger.py` `_SCHEMA` (auto-create via the existing `ensure_schema`; brand-new tables need no migration helper). **No change** to `day_baseline`/`ticker_action`/`orders`/`decisions`/`outcomes`/`actions`/`ticker_schedule`. Rebalance orders **reuse `reserve_order`** with new free-text `order_kind` values (`rebalance_entry`/`rebalance_trim`/`rebalance_exit`) — `ref_id` reserve-before-place + `(trade_date,ticker)` dedup unchanged.

| Table | Shape (key cols) |
|---|---|
| `strategy_goal` | one active row: target_return_pct, horizon_months, benchmark(+annual_pct), macro_thesis_json, active_book, start_date, start_equity |
| `target_portfolio` | PK (goal_id,ticker); sleeve, target_weight, band, status (active/exiting/removed), book, quotable, proxy_ticker |
| `goal_tracking` | append-only: trade_date, portfolio_value, glidepath_target_value, cumulative_return_pct, ahead_behind_pct, alpha_vs_benchmark_pct, regime |
| `thesis_state` | PK goal_id: regime (neutral/deploy/standdown), active_book, last_trigger, last_macro_json |
| `universe_change_log` | append-only, content_hash-deduped: kind, ticker, sleeve, tier (soft/derisk/universe), reason (proof), status (proposed/approved/applied/rejected/expired) |
| `run_lock` | single-row mutex (id=1 CHECK), holder, acquired_at, TTL auto-expiry |

Also: **reuse the already-present-but-unused `outcomes.benchmark_return` + `outcomes.alpha` columns** via the existing `cmd_reflect` hook — the orchestrator passes the SGOV period return so per-decision alpha-vs-cash is graded (zero new columns).

---

## 6. The tick lifecycle, evolved

Shape unchanged. A new `tick.py construct` runs **between `preflight` and `plan`**, reads the active goal + targets + broker snapshot (passed via `--input` exactly like `plan`/`reflect`), runs deterministic book-selection + drift math, and emits a `target_weights` JSON the orchestrator drops into `plan_input.json` under a new **optional** key.

```
preflight → [construct] → broker snapshot → analyze.py (per ticker) → plan → place → commit
                                                                          ↘ goal-track · learn-review · report · prune  (best-effort)
```

**Provable no-op when off:** when `target_weights` is absent OR `rebalance_enabled` is false, `cmd_plan` is **byte-identical** to today (a CRITICAL regression test asserts this). `goal-track` + `learn-review` are best-effort (classified like `reflect`/`report`/`prune` — a failure never stops a tick). `universe-apply` is out-of-band, human/config-gated, and places no orders.

**Classification (per `CLAUDE.md`):** `strategy-set` + `universe-apply` STOP on error (setup/out-of-band); `construct` stops only if it errors before plan (absent target is the safe fallback); `goal-track` + `learn-review` NEVER stop a tick.

---

## 7. Continual learning + add/remove engine

`lib/learn.py` scores each holding/sleeve's realized contribution vs thesis against the 15% goal gap, using proof-bearing `risk.Metric`s (`sustained_underperformance`, `contribution_vs_thesis`). It emits deterministic proposals:

- **KEEP** when ahead-of-glidepath (even with mediocre hit-rate) or below `min_resolved_n` (INSUFFICIENT-DATA → KEEP).
- **FLAG_UNDERPERFORM / PROPOSE_REMOVE** only when sustained-underperf **AND** behind-glidepath **AND** enough N (one bad week can't evict a sleeve).
- **PROPOSE_DERISK** on a standdown macro reading.
- **PROPOSE_ADD** only for an allow-listed, quotable ticker.

**Gates (the safety model):**
- The **risky direction** (adding exposure, rotating to `dial_up_63_37`) is **always human/config-gated**.
- The **safe direction** (de-risk-to-cash) is auto-eligible *only* when `auto_apply_derisk:true` — and it defaults **OFF**.
- `cmd_universe_apply --id N --approve` re-validates via `lib/universe.py`, mutates `target_portfolio`, and **emits no order** — the next `construct→plan` winds an `exiting` name to zero via the existing clamped sell path.
- Proposals are content-hash-deduped and expire after `proposal_expiry_days` (no per-tick spam).

---

## 8. AWS deployment + Agent harness (BRAX-derived)

**Decision: persistent small EC2 box + systemd, driving the `claude` CLI in `-p` mode** — the pattern proven by your `claude-sdk-agents` (BRAX) reference, adapted from Azure→AWS. This beats scheduled-Fargate because (a) it's the pattern you already trust, (b) a resident box lets you re-auth the Robinhood OAuth credential in place (the token has **no refresh token** — re-auth is genuinely interactive), and (c) it's comparably cheap.

### Shape
A single hardened **systemd service** on a **t4g.small Ubuntu 24.04** box: a thin Node/TS daemon that wakes on a market-hours schedule, shells `.venv/bin/python tick.py preflight`, and on `proceed` runs **one `claude -p` pass over `TICK.md`**.

The `claude` invocation (copied from BRAX `spawnClaude`, trading-tuned):
```
claude -p \
  --output-format stream-json --include-partial-messages --verbose \
  --model <haiku/sonnet-class>            # D11: cheap executor
  --effort low                            # ultracode OFF — Python does the thinking
  --mcp-config /etc/quiver/mcp.json --strict-mcp-config \
  --add-dir /opt/quiver \
  --max-turns 80 \
  --append-system-prompt "<execution-orchestrator rules>" \
  "Follow ./TICK.md exactly for today's tick."
```

- **System prompt** = static "you are the execution orchestrator; all decisions come from Python; never place an order the plan didn't authorize." **User prompt** = "read & execute `./TICK.md`." `TICK.md` lives in `cwd` (the BRAX runbook-on-disk pattern).
- **MCP** via `/etc/quiver/mcp.json` (`--strict-mcp-config` so only these two servers load), `${ENV}` interpolated, env injected by the harness:
  ```json
  { "mcpServers": {
      "robinhood-trading": { "type": "http", "url": "https://agent.robinhood.com/mcp/trading", "headers": {"Authorization": "Bearer ${RH_OAUTH_TOKEN}"} },
      "resend": { "command": "npx", "args": ["-y", "resend-mcp"], "env": {"RESEND_API_KEY": "${RESEND_API_KEY}"} }
  } }
  ```
  *(Exact Robinhood MCP transport/auth to be confirmed during D10; if it's an OAuth stdio server, the credential is materialized under the service user's `CLAUDE_CONFIG_DIR` instead of a bearer header.)*
- **Watchdogs copied verbatim**: `streamIdleWatchdog` (abort on no-output-with-no-tool-in-flight) + `pollWedgeWatchdog` (abort on repeated `sleep N` polling). A wedged trading agent silently burning tokens is exactly what these stop.
- **Execution-only guard**: a `can_use_tool`/PreToolUse hook **DENIES** any `place/cancel_equity_order` whose `ref_id` was not produced by this tick's Python `plan`/`protect` output — mechanical second enforcement of the invariant.
- **`AUTH_ERROR` → hard stop**: never place, emit the `AUTH_ERROR` log line, send the Resend digest, exit non-zero (alarm fires). Re-auth is a human SSH-in + `/mcp` (the BRAX `gh auth login` analog).

### Run model
Collapse server+worker into **one daemon** whose loop is the schedule: `compute next wake (lib/market.py + Python next_review_minutes) → sleep → run one tick → loop`. Concurrency = 1; idempotency is already guaranteed by Python's `(trade_date,ticker)` PK + `ref_id` + `KILL` + `lib/runlock.py`. No queue needed for safety.

### State, secrets, observability, cost
- **State**: `state/ledger.db` on the EBS root volume (the box is persistent — no litestream/EFS needed; tiny DB). Nightly EBS snapshot for DR. Transcripts → `logs/`, logrotated (30d).
- **Secrets** (the one-line Azure→AWS swap, fetched at provision in `setup.sh`, written to `/etc/quiver/quiver.env` chmod 600, loaded via systemd `EnvironmentFile`): `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `RH_ACCOUNT_NUMBER`, `NOTIFY_TO`, + the Robinhood OAuth credential. Use **SSM Parameter Store** (free) authed by the EC2 instance-profile IAM role. App reads plain env; never touches the store at runtime.
- **Observability**: pino JSON → journald + **CloudWatch Agent**; **extend the redaction list** to scrub `RH_*`/`RESEND_API_KEY`/`ANTHROPIC_API_KEY`/account numbers. CloudWatch metric filters on `AUTH_ERROR`, `daily_halt`/`write_kill`, `KILL`-created, plan/commit error, task-failure → alarms → SNS. A daily heartbeat alarm (no successful tick in ~26h on a trading day) catches silent death.
- **No inbound ports / no Caddy / no webhook** — outbound-only (Anthropic + the two MCP endpoints). SSH-only ingress.
- **Cost**: ~**$15–18/mo** on `t4g.small` (EC2 ~$12 + EBS ~$1.6 + SSM $0 + CloudWatch ~$1–3); ~$10 with a Savings Plan. Anthropic/DeepSeek token spend is separate, usage-driven, and bounded by the cheap-executor + once-daily cadence. Bump to `t4g.medium` (~$25/mo) only if node+python+`claude` pressure 2 GiB.

---

## 9. Implementation stages (each independently testable; existing 169 tests stay green at every boundary)

| Stage | Goal | Key deliverables | Gate |
|---|---|---|---|
| **0** | Data foundation, zero behavior change | `strategy.yaml`; `lib/strategy.py`; 6 ledger tables + methods; `config.py` strategy hook + conservative knobs | load/validate raises on bad data; `select_active_book` truth table; ledger round-trips; `cfg.strategy=None` when absent; full suite green |
| **1** | Pure math + clamp helpers (no wiring) | `lib/portfolio.py`, `lib/goal.py`; `signals.py` target helpers | drift/target-$/glidepath cases; `resolve_buy_dollars` with kwarg=None **byte-identical**; wall test extended |
| **2** | Read-only goal context into analysis | `lib/strategy_context.py`; `reflect_memory` 4th block; `analyze.py` thread-through | disclaimer present; degrades to scorecard on throw (D3 fallback); snapshot matches injected (D5); analysis-path-reads-no-limits test |
| **3** | `construct` + goal-track + target-aware plan (behind `rebalance_enabled`) | `cmd_construct`/`cmd_goal_track`/`cmd_strategy_set`; clamped target sizing; `TICK.md` one new step | **CRITICAL**: plan byte-identical when off; cap-precedence (target never overrides a cap); halt precedence (zero orders on a halt day) |
| **4** | Continual learning + add/remove engine | `risk` helpers; `lib/learn.py`; `lib/universe.py`; `cmd_learn_review`/`cmd_universe_apply`; digest section | classifier table; allow-list/quotable gate; auto-vs-human gate; both-sides wall test |
| **5** | Headless AWS harness + deploy, dry_run-first | `lib/runlock.py`; `deploy/runner/*` (TS daemon + `claude` harness + watchdogs + guard); `deploy/setup.sh`; CloudWatch/SSM/IAM; e2e | runlock; offline healthcheck; `can_use_tool` DENY-unauthorized-ref_id; container/box parity; AUTH_ERROR drill; full dry-run market day |

---

## 10. Testing strategy

- **Offline unit** (`tests/test_units.py`, plain asserts, no network/broker): every new pure function + the extended **wall/boundary** checks. The existing 169 stay green at each stage boundary.
- **E2E** (listed per §9): strategy lifecycle; construct→plan seam (dry_run); **halt precedence**; read-only context (incl. forced-throw degradation); continual-learning propose→approve→wind-to-zero; per-decision alpha-vs-cash; box/container parity; crash-mid-place recovery; **AUTH_ERROR drill**; full dry-run market day with **zero `place_equity_order` calls** verified against the Robinhood order history.

---

## 11. Risks & mitigations (top items)

- **Robinhood OAuth has no refresh token** → genuine unattended gap. Mitigation: detect+alarm+page, never auto-recover, never trade blind; **measure the token TTL during the dry-run soak**; confirm ToS for headless use (D10).
- **Crypto-spot quote gap** (no RH crypto MCP tool today) → BTC/ETH ride IBIT/ETHA; **SOL parked in cash** (D7); `universe.validate_add` hard-blocks unquotable auto-apply.
- **Plan-complexity creep** → strictly gated behind `rebalance_enabled` + a byte-identical regression test.
- **Thin outcomes on a ~$100 account → premature remove** → `min_resolved_n`/`sleeve_review_min_n` force INSUFFICIENT-DATA→KEEP; removal needs a full window **and** behind-glidepath.
- **Auto-derisk misfire on a fat-fingered macro reading** → derisk is the SAFE direction (only trims to SGOV), defaults OFF, and the digest shows the exact reading+threshold that flipped it.
- **Single-writer SQLite under restarts** → `run_lock` + serial scheduling + concurrency=1.
- **Equity-derived goal line in the analysis path** → inject only the COARSE ahead/behind narrative (never raw caps/buying_power); equity is not a trading *limit*. Flagged for explicit reviewer sign-off.

---

## 12. Rollout

1. **Stages 0–4** land behind OFF-by-default flags; the validated once-a-day path is provably unchanged throughout.
2. **`strategy-set`** writes the active goal + `core_55_45` targets; observe goal-tracking + learning proposals for a few days with `rebalance_enabled:false` (pure observability).
3. Flip **`rebalance_enabled:true`** locally in dry_run; verify clamped rebalance orders + cap/halt precedence.
4. **Stage 5**: deploy to AWS in `dry_run:true` for a full market day; diff CloudWatch digests vs a local run; verify zero placements; measure the OAuth token TTL.
5. Flip **`dry_run:false`** on a clean fresh day (clear that day's ledger rows first) with tightened caps. Dial-up and auto-derisk remain OFF until explicitly enabled.

---

---

## 13. Review decisions (from `/plan-eng-review`, 2026-06-13)

- **Scope:** build **all 6 stages, in order** (each ships behind OFF-by-default flags). No scope cut.
- **D2 — invariant edge:** `strategy_context.py` injects only a **coarse `AHEAD / ON-TRACK / BEHIND` regime label** into the analysis path — never a number or `$` value. A wall test asserts the rendered block contains no digits/`$`. (Equity-derived numbers never cross the wall.)
- **D3 — OAuth:** **accept manual re-auth + paging** (BRAX `gh auth login` model); the dry-run-soak **token-TTL measurement is a hard go/no-go gate** — if the token dies in hours not days, escalate to a re-auth-automation path before live. Never trade on stale auth.
- **D4 — resilience:** **EC2 auto-recovery alarm** (reboots the same instance, EBS ledger intact) + the daily heartbeat alarm. No multi-AZ/ASG (overkill; risk state is broker-side).
- **D5 — order guard:** the `can_use_tool` hook validates each place/cancel `ref_id` **against the ledger's reserved-this-tick orders** (Quiver's reserve-before-place source of truth) — not a side-file, not prompt-only. Mechanical, airtight, agrees with the brain by construction.
- **Code-quality musts:** extend log redaction for `RH_*`/`RESEND_API_KEY`/`ANTHROPIC_API_KEY`/account numbers; `strategy.yaml` is the single committed source (→ ledger runtime state, no third copy).
- **Tests added:** (1) `strategy_context` block contains no digits/`$` (D2); (2) the `ref_id` guard allows a reserved ref_id and denies an unreserved one against a real ledger (D5).

### Deferred / go-live gates (not in scope of the build, tracked here)
- **Robinhood ToS check** — confirm headless remote-MCP use with a Secrets-Manager-materialized token is permitted **before flipping `dry_run:false`**. Hard gate.
- **Token-refresh automation** — only if the soak shows a short TTL (D3 escalation path).
- **SOL execution** — parked in cash (D7) until a Robinhood crypto MCP exposes quote/trade tools; revisit then.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 4 architecture findings, all resolved; 0 critical gaps; 2 tests added |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI) |
| Outside Voice | `/codex` | Independent 2nd opinion | 0 | — | offered, see next steps |

- **SCOPE:** accepted as-is (all 6 stages).
- **UNRESOLVED:** none — D1–D5 all answered.
- **VERDICT:** ENG CLEARED — ready to implement Stage 0. (Optional: an outside-voice / `/plan-ceo-review` pass before coding; not required.)

