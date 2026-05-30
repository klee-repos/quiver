# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Quiver is an autonomous stock bot that trades **real money** — once a day by default,
optionally several times a day at a model-paced, Python-clamped cadence. A multi-agent LLM
framework (`tradingagents/`, owned in-tree, running on DeepSeek) produces a Buy/Sell/Hold signal
per ticker. Claude Code — holding the Robinhood MCP — is the *execution orchestrator*: it feeds
broker data to deterministic Python and places exactly the orders that Python returns.

The defining constraint: **all trading decisions and risk guardrails live in Python, never in
the orchestrator's judgment.** `analyze.py` decides and never touches the broker; the
orchestrator executes and never reasons about the market. When working here, preserve that
split — do not move clamping, sizing, dedup, cadence, or halt logic into prose/runbook steps, and
do not let the analysis path read trading limits or the broker (the ledger memory scorecard it
*does* read is past decisions + outcomes only — never limits).

## Current status (2026-05-30)

Built and validated end-to-end; in **dry-run**, pending one live session.

- ✅ 169/169 unit tests; `analyze.py AAPL` ran a full DeepSeek pass → valid signal;
  `plan` / kill-switch / preflight drills pass.
- ✅ Framework owned in-tree (de-vendored); decision memory grounded in the ledger; optional
  intraday multi-run + model-driven cadence; storage retention; optional limit entries +
  protective GTC stops. The risky paths (`intraday_enabled`, `buy_type: limit`,
  `protective_stop`) ship **OFF by default** — the default behavior is the validated once-a-day
  market-order path.
- ✅ Models live: `deepseek-v4-flash` (quick/analysts) + `deepseek-v4-pro` (deep/debates).
- ✅ Account wired: an `agentic_allowed=true` "Agentic" account (number set via `RH_ACCOUNT_NUMBER` in `.env`, not committed), **~$100** buying power.
  Caps in `config.yaml` are sized for $100 ($25/trade, $75/day, 5% daily-loss halt) — bump
  them proportionally if the account is funded with more.
- ✅ Live path validated off-hours: a real `analyze.py AAPL` (de-vendored stack) → `plan`
  (dry-run) → digest emailed via Resend, **0 orders**. ⏳ Next: one **market-hours**,
  preflight-gated tick, then flip `config.yaml: dry_run` to `false` on a fresh day.
- Data / keys: prices, indicators, fundamentals, and news all use **yfinance (no API key)**;
  StockTwits is keyless. `ALPHA_VANTAGE_API_KEY` is an optional alternate vendor, **unused by
  default**. The only required key is `DEEPSEEK_API_KEY`. Reddit sentiment returns `403` (unauth
  scraping) and degrades gracefully — the other analysts + StockTwits still run.

## Commands

```bash
# Run the unit tests (pure decision logic; no pytest, plain asserts, exits non-zero on failure)
.venv/bin/python tests/test_units.py

# Smoke-test the decision path for one ticker (prints exactly one JSON line to stdout)
.venv/bin/python analyze.py AAPL [--date YYYY-MM-DD]

# Exercise the deterministic brain directly
.venv/bin/python tick.py preflight
.venv/bin/python tick.py plan   --input state/tmp/plan_input.json
.venv/bin/python tick.py commit --input state/tmp/commit.json

# Build the email digest (prints subject/html/text + should_send; sends nothing).
# should_send is false until notify.enabled: true in config.yaml.
.venv/bin/python tick.py report --input state/tmp/report_input.json
.venv/bin/python tick.py report-commit --date YYYY-MM-DD --kind digest --hash <h> --recipients "you@x.com"

# Resolve past-decision outcomes into the memory scorecard (orchestrator feeds quotes/positions)
.venv/bin/python tick.py reflect  --input state/tmp/reflect_input.json
# Python-clamped protective stop after a fill (Phase 5; only if order.protective_stop.enabled)
.venv/bin/python tick.py protect  --input state/tmp/protect_input.json
# Age out bulky logs past storage.retention_days (best-effort housekeeping; never blocks a tick)
.venv/bin/python tick.py prune

# Market-hours status (XNYS calendar, holiday/half-day aware)
.venv/bin/python -m lib.market

# Inspect persistent state
sqlite3 state/ledger.db "SELECT * FROM ticker_action ORDER BY updated_at DESC LIMIT 20;"
sqlite3 state/ledger.db "SELECT * FROM orders ORDER BY submitted_at DESC LIMIT 20;"
# Decision memory + outcomes (the scorecard's ground of record)
sqlite3 state/ledger.db "SELECT d.id,d.trade_date,d.ticker,d.signal,o.directional_return,o.realized_pnl \
  FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id ORDER BY d.id DESC LIMIT 20;"
# Intraday action history + per-ticker cadence (only populated when loop.intraday_enabled)
sqlite3 state/ledger.db "SELECT * FROM actions ORDER BY id DESC LIMIT 20;"

# Clear a day's rows (needed to flip dry_run->live on a day that already has rows)
.venv/bin/python -c "from lib.ledger import Ledger; print(Ledger('state/ledger.db').clear_day('YYYY-MM-DD'))"
```

There is no separate build or lint step. Always use the venv interpreter (`.venv/bin/python`);
the framework and its deps are installed there, not globally.

## The tick lifecycle

One "tick" is one full trading pass, driven by `TICK.md` (the exact runbook the orchestrator
follows). The phases and which layer owns each:

1. **`tick.py preflight`** (Python) — gate: kill-switch file, daily-halt flag, market open,
   minutes-since-open, and dedup. Classic mode: per-day dedup (≤1 action/ticker/day). Intraday
   mode (`loop.intraday_enabled`): per-ticker cadence eligibility + daily analysis budget.
   Returns `proceed` + config + `pending` (tickers to analyze) + `pending_outcomes` (decisions
   to score) + `unfinalized` orders to reconcile.
2. **`get_portfolio` / `get_equity_positions` / `get_equity_quotes`** (MCP) — read-only broker
   snapshot. Quotes are the one price source for sizing/decision-price/outcomes. Auth error here
   aborts the whole tick — never trade on stale auth.
3. **`analyze.py <TICKER>`** (Python → DeepSeek) — slow (~minutes per ticker, 900s timeout).
   One JSON line per ticker (signal + `position_pct` + `entry_price`/`stop_loss` +
   `next_review_hours`). Injects the ledger memory scorecard as `past_context`. Errors print
   `"signal":"ERROR"` and are recorded as skips.
4. **`tick.py plan`** (Python) — the deterministic brain: daily-loss halt, signal→intent,
   clamped sizing (structured `position_pct` or prose), intraday gates (cooldown / action-cap /
   on-change), per-ticker cadence scheduling, reserves `ref_id`s. Returns `orders` (market or
   whole-share `limit`) + `next_review_minutes`.
5. **`review_equity_order` / `place_equity_order`** (MCP) — execute exactly the returned orders.
   A sell first cancels any resting protective stops (`cancel_ref_ids`). In `dry_run` the
   orchestrator reviews but never places.
6. **`tick.py commit`** (Python) — finalize each order, advance its lifecycle state, write the
   per-ticker action row + the append-only action event.
6b. **`tick.py protect`** (Python, Phase 5) — after a buy fills, returns a Python-clamped `gtc
   stop_market` to place (only if `order.protective_stop.enabled`).
6c. **`tick.py reflect`** (Python) — grounds the memory scorecard: from a passed-in quote/position
   snapshot, scores each `pending_outcomes` decision (directional always; actual P&L when held).
7. **`tick.py report` → Resend MCP → `tick.py report-commit`**, then **`tick.py prune`** (Python
   builds/dedups + retention; MCP sends) — observability + housekeeping, strictly best-effort:
   a report/prune error is logged and the tick ends normally — it never blocks trading.

In production this is run via a kept-open `/loop` Claude session, started inside a
`caffeinate`+`screen` window so it survives terminal close and machine idle-sleep:

```bash
cd ~/dev/quiver && caffeinate -i -s screen -S quiver   # then launch `claude`, then /mcp
/loop 1h Run one trading tick for the daily stock bot. Follow ~/dev/quiver/TICK.md exactly.
```

The loop wakes hourly and no-ops cheaply until the market is open and a ticker is eligible.
**Classic mode (default, `loop.intraday_enabled: false`):** the `(trade_date, ticker)` dedup
guarantees at most one action per ticker per day regardless of tick count — today's behavior,
untouched. **Intraday mode (`true`):** the model proposes a re-check cadence (`next_review_hours`),
Python clamps it (tighter ceiling while the market is open, snapped out of closed-market gaps) and
the orchestrator schedules the next wake at the returned `next_review_minutes`; repeat trades are
bounded by a per-ticker cooldown + a daily action cap + an on-change gate, and analyses by a daily
budget. When editing `TICK.md` and `tick.py` together, keep them in lockstep — the runbook
describes the exact JSON shapes `plan`/`commit`/`reflect`/`protect` consume.

## Safety architecture (the parts that matter most)

- **`lib/ledger.py`** — SQLite at `state/ledger.db`, the source of truth across restarts.
  Guarantees: (1) `ticker_action` PK `(trade_date, ticker)` is the latest-state snapshot →
  classic mode = at most one action per ticker per day (the append-only `actions` event log adds
  intraday history for the cooldown/cap/on-change gates); (2) `orders` rows are *reserved before*
  the broker call and keyed by a UUID `ref_id`, so a crash mid-place is recoverable and retries
  reuse the same `ref_id` (Robinhood dedups by it — **never mint a new `ref_id` for an order that
  might already be sent**); (3) `day_baseline.halted` + the `KILL` file make every later tick a
  no-op until a human clears it. Also holds the **decision memory** (`decisions` + `outcomes`,
  the scorecard's ground of record), `ticker_schedule` (per-ticker cadence), and the Phase 5
  order-lifecycle columns (`order_kind`/`limit_price`/`stop_price`/`parent_ref_id`/`state`).
- **`lib/signals.py`** — pure, unit-tested signal→order mapping, sizing clamps, intraday gates,
  and Phase 5 pricing. **Long-only policy**: a Sell/Underweight with no position is a no-op
  (never opens a short). Buy dollars are the min of (structured `position_pct` or prose size or
  conservative fallback, per-trade ceiling, remaining daily deploy cap, buying_power − buffer,
  room under per-ticker cap); a non-positive result means skip. Sizing never "fails open" — an
  unparseable prose size falls back to `min(ceiling, 100)`. Also: the cooldown / action-cap /
  on-change gates, the cadence clamp (`clamp_review_minutes`), and limit/stop pricing
  (`marketable_limit_price`, `whole_shares_for_dollars`, `resolve_stop_price` — Python owns every
  price; the model only seeds the stop).
- **`lib/memory.py`** — pure distilled-scorecard builder (`build_scorecard`, `directional_return`,
  `is_hit`) + a thin ledger-reading `scorecard(led, ticker)`. Read-only; it sees past decisions +
  real outcomes but **never trading limits or the broker** — the one bit of memory the analysis
  path is allowed to read. No embeddings: recall is a compact per-ticker hit-rate / avg-move /
  realized-P&L summary injected as `past_context` into the PM **and** Trader.
- **`lib/storage.py`** — best-effort retention (`prune_dir` over a pure `select_for_prune`) + a
  pluggable `Archiver` (local default; S3 backend deferred — interface only). Like `notify`, it
  never raises into a tick; the ledger holds decision state, so pruned transcripts lose nothing.
- **`lib/config.py`** — loads/validates `config.yaml`, **fails safe**: anything other than an
  explicit `dry_run: false` stays in paper mode. Placeholder account number or `"verify"`-tagged
  model IDs raise immediately rather than trading on a bad config.
- **`lib/market.py`** — all time math in `America/New_York` via zoneinfo (never the OS tz); session
  validity from the NYSE (XNYS) calendar (`pandas_market_calendars`), so holidays and half-days
  are handled — a bare weekday check is not enough.
- **`lib/ds_config.py`** — single source of truth for DeepSeek wiring. `quick_think_llm` does the
  analysts' tool-calling so it **must** support function calling (chat/flash model);
  `deep_think_llm` does debates/judgment (reasoner model). Keeps `backend_url` as `None` on purpose
  so the provider default applies (forcing `/v1` risks a doubled path). Redirects framework state
  into `state/` rather than `~/.tradingagents`.
- **`lib/notify.py`** — pure email-digest **renderer** (observability, NOT a decider). It takes an
  already-assembled model dict and returns `{subject, html, text, content_hash}`; it never reads
  config/ledger/trading limits and never hits the network. The orchestrator sends via the Resend
  MCP. Email is **best-effort, at-least-once** — the inverse of the orders rule: a digest is marked
  sent *after* delivery is confirmed, so the rare send/commit-gap crash re-sends a duplicate rather
  than silently losing it. `content_hash` dedups on the decision skeleton (excludes timestamps), so
  repeat hourly wakes don't re-send. A `report` error must never stop a tick.

When you change a guardrail, mirror it with a case in `tests/test_units.py` — that file is the
spec for the sizing/mapping logic and runs without any network or broker. The same file also
covers the digest renderer, `content_hash` dedup, `notify` config validation, and the read-only
ledger rollups — all offline.

## Controls & failure modes

- **Kill switch:** `touch KILL` halts trading next tick; `rm KILL` resumes. Path is
  `config.yaml: kill_switch_file`.
- **Daily-loss halt:** if equity drops ≥ `risk.daily_loss_halt_pct` vs the day's opening baseline,
  `plan` sets `halt`/`write_kill` and the orchestrator writes `KILL`.
- **MCP auth expiry:** a tick logging `AUTH_ERROR` means the Robinhood token expired; recovery is a
  human running `/mcp` to re-auth. The bot must stop, not trade blind.
- **dry_run → live:** flip `config.yaml: dry_run` to `false` only after a clean dry-run validation,
  on a fresh trading day (or clear that day's rows first — see Commands).
- **Intraday trading:** `loop.intraday_enabled: false` (default) = classic ≤1 action/ticker/day,
  fixed loop. Flip `true` (globally or for a volatile day) to let the model pace re-checks within
  Python-clamped cadence bounds; repeat trades are gated by a per-ticker cooldown + daily action
  cap + on-change gate, and analyses by a daily budget. The default path is unchanged code.
- **Limit entries / protective stops:** `order.buy_type: limit` and `order.protective_stop.enabled`
  are OFF by default; both are Python-priced (the model only seeds the stop). Note a limit buy uses
  whole shares, so a sub-one-share budget skips. Validate the full lifecycle live before enabling.

## Layout

- `analyze.py` — decision wrapper: ticker → one JSON line. Redirects all framework stdout to
  stderr so stdout stays JSON-only; tees that live chatter to `logs/reasoning/<date>_<TICKER>.log`
  (best-effort, never touches stdout); dumps a full audit report set to `state/analyze_logs/`.
- `tick.py` — `preflight` / `plan` / `commit` / `reflect` / `protect` / `report` / `report-commit`
  / `prune` subcommands. Every subcommand prints one JSON line and catches all exceptions into
  `{"error": ...}` with exit code 1 (the orchestrator stops on a plan/commit error — but `report`,
  `reflect`, and `prune` are observability/housekeeping and must NOT stop the tick).
- `lib/` — `config`, `market`, `ledger`, `signals`, `ds_config`, `notify`, `memory`, `storage`
  (see above).
- `config.yaml` — account, `dry_run`, watchlist, dollar caps, order defaults (incl. `buy_type` +
  `protective_stop`), model IDs, `loop` timing + `intraday_enabled`/cadence, `storage` retention,
  `notify` (email digest — off unless `enabled: true`).
- `TICK.md` — the per-tick runbook the orchestrator follows verbatim.
- `tradingagents/` — the multi-agent framework, **owned in-tree** (de-vendored from upstream;
  tracked, pruned; provenance in `tradingagents/UPSTREAM.md`, Apache-2.0 `tradingagents/LICENSE`).
  Installed editable via the root `pyproject.toml` (`pip install -e .`).
- `state/` — `ledger.db`, `tmp/` (plan_input/commit/reflect/protect/report scratch),
  `analyze_logs/`, `memory/`, framework `results`/`cache`. Gitignored.
- `logs/` — `orchestrator.log` (one line per tick) + `reasoning/` (teed live thinking). Gitignored.
- `.env` — per-user/secret values (gitignored, `chmod 600`): `DEEPSEEK_API_KEY`,
  `RH_ACCOUNT_NUMBER` (the agentic_allowed account), and `NOTIFY_TO` (digest recipients,
  comma-separated). `config.yaml` is committed and carries none of these. MCP credentials
  (Robinhood, Resend) live in the operator's Claude config (`claude mcp add`), NOT in this repo.
