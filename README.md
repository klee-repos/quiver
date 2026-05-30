# Quiver

Autonomous stock bot — once a day by default, optionally several times a day at a
model-paced, Python-clamped cadence. **TradingAgents** (multi-agent LLM, owned
in-tree under `tradingagents/`) runs on **DeepSeek** to produce a Buy/Sell/Hold
signal per ticker; **Claude Code** (orchestrator, holding the Robinhood MCP)
executes the trades. All risk guardrails, sizing, dedup, cadence, and idempotency
are deterministic Python (`tick.py`) — never the orchestrator's judgment. It also
keeps a **decision memory** (past calls + real outcomes) grounded in the ledger and
fed back into each analysis.

> ⚠️ **Real money, fully autonomous.** Currently in `dry_run` (paper). It can
> lose money. This is not investment advice.

## Status — as of 2026-05-30

Built and validated end-to-end; running in **dry-run** pending one live session.

| | |
|---|---|
| ✅ Unit tests | 169/169 pass (`tests/test_units.py`) |
| ✅ Decision path | `analyze.py AAPL` ran a full DeepSeek analysis → valid signal |
| ✅ Plan / kill-switch / preflight drills | pass (offline) |
| ✅ Models | `deepseek-v4-flash` (quick) + `deepseek-v4-pro` (deep) confirmed live |
| ✅ Account | `••••7171` ("Agentic", `agentic_allowed=true`), **$100** buying power |
| ✅ Guardrails | sized for $100: $25/trade, $75/day cap, 5% daily-loss halt |
| ✅ Capabilities | owned framework · ledger-grounded memory · optional intraday cadence · optional limit/stop orders (risky paths OFF by default) |
| ✅ Live path | one full dry-run pass validated off-hours: live `analyze.py` → `plan` → digest emailed, **0 orders** |
| ⏳ Pending | one **market-hours**, preflight-gated tick → then flip `dry_run: false` |

> The dry-run gate needs an open market, so the first live tick happens on the
> next trading session.

## Architecture

```
/loop (kept-open Claude session)
  └─ each tick → follow TICK.md:
       ├─ tick.py preflight         # kill / halt / market-open / dedup gates   (Python)
       ├─ get_portfolio/positions   # broker snapshot                           (MCP)
       ├─ analyze.py <TICKER>       # TradingAgents on DeepSeek → signal JSON    (Python)
       ├─ tick.py plan              # clamp + size + reserve ref_ids             (Python)
       ├─ review/place_equity_order # execute EXACTLY what plan returned         (MCP)
       └─ tick.py commit            # record outcome to sqlite ledger            (Python)
```

`analyze.py` decides and never touches the broker. The orchestrator executes and
never reasons about the market. That split is the core safety property.

## How to run

### 1. Start the loop (kept-open session)
```bash
cd ~/dev/quiver && caffeinate -i -s screen -S quiver
#   → inside the screen window, launch:   claude
#   → confirm the Robinhood MCP (and Resend, if email is on) is connected:   /mcp
#   → start the once-daily loop:
/loop 1h Run one trading tick for the daily stock bot. Follow ~/dev/quiver/TICK.md exactly.
#   detach the screen: Ctrl-a d      reattach later: screen -r quiver
```
The loop wakes hourly; a tick no-ops cheaply (config read + clock check, no LLM,
no orders) until the market is open and the ticker hasn't been acted on today.
`caffeinate -i -s` blocks idle/AC sleep (not lid-close — keep the lid open / on AC).

### 2. Watch the first dry-run tick
At ~9:35 ET it should: pass preflight → snapshot the account → analyze each
watchlist ticker → call `review_equity_order` → **place nothing**. Confirm:
```bash
sqlite3 state/ledger.db "SELECT trade_date,ticker,signal,intent,status FROM ticker_action;"  # status = dry_run
sqlite3 state/ledger.db "SELECT COUNT(*) FROM orders;"                                        # 0 (nothing placed)
tail -n 20 logs/orchestrator.log
```

### 3. Go live
Set `dry_run: false` in `config.yaml`. Do it on a **fresh trading day**, or clear
that day's rows first (otherwise dedup skips tickers already "acted" in dry-run):
```bash
.venv/bin/python -c "from lib.ledger import Ledger; print(Ledger('state/ledger.db').clear_day('YYYY-MM-DD'))"
```
The next tick trades for real, sized by the model and clamped by `risk:`.

## Configuration (`config.yaml`)

Current values (sized for the $100 account — bump proportionally when funded):

| Key | Value | Meaning |
|---|---|---|
| `account_number` | `XXXXXXXX` | the agentic-allowed Robinhood account |
| `dry_run` | `true` | paper mode; review only, never place |
| `watchlist` | `AAPL, MSFT, NVDA` | tickers analyzed each day (cost scales with size) |
| `risk.max_dollars_per_trade` | `25` | hard per-trade ceiling |
| `risk.daily_capital_deploy_cap` | `75` | max total BUY $ per day |
| `risk.max_open_position_per_ticker` | `50` | max $ held in one ticker |
| `risk.daily_loss_halt_pct` | `5.0` | equity drop vs day baseline → halt + KILL |
| `risk.min_buying_power_buffer` | `5` | never spend below this much |
| `deepseek.chat_model` | `deepseek-v4-flash` | analyst tool-calls (must support function calling) |
| `deepseek.reasoner_model` | `deepseek-v4-pro` | debates / judgment |
| `loop.intraday_enabled` | `false` | **OFF = classic once-a-day.** `true` = model-paced multi-run (see below) |
| `loop.per_ticker_cooldown_min` / `review_floor_min` / `review_ceiling_open_min` / `review_ceiling_min` | `60 / 30 / 120 / 1440` | intraday cooldown + cadence clamp bounds (minutes) |
| `risk.max_actions_per_ticker_per_day` / `max_analyses_per_ticker_per_day` | `3 / 6` | intraday trade cap + LLM-cost (analysis) cap |
| `order.buy_type` | `market` | `limit` = marketable-limit, whole-share entries (Python-priced) |
| `order.protective_stop.enabled` / `stop_pct` | `false / 8.0` | post-fill GTC stop, Python-clamped (model only seeds) |
| `storage.retention_days` | `30` | age out logs/transcripts; `archive` (S3) deferred, off |

**Intraday & advanced orders are opt-in.** With everything at defaults the bot behaves exactly as the
validated once-a-day, market-order path. `loop.intraday_enabled: true` lets the model recommend when to
look next (Python clamps it and bounds repeat trades); `order.buy_type: limit` and `protective_stop`
add price-protected entries + resting stops — enable these only after a live dry-run validation.

## Controls
- **Kill switch:** `touch KILL` halts all trading next tick; `rm KILL` resumes.
- **Daily-loss halt:** auto-fires + writes `KILL` if equity drops past
  `risk.daily_loss_halt_pct` vs the day's opening baseline.
- **MCP token expiry:** if a tick logs `AUTH_ERROR`, reattach the screen and run
  `/mcp`. The bot stops rather than trading blind. Re-auth proactively (~weekly).

## Observe a run
- **Live thinking (watch it happen):** `logs/reasoning/<date>_<TICKER>.log` — the
  framework's moment-to-moment agent chatter, teed as it runs. `tail -f` it during a tick.
- **Per-ticker reasoning + reports:** `state/analyze_logs/<date>_<TICKER>.json`
  (full analyst reports, bull/bear debate, trader plan, final decision — ~48KB).
- **What the bot did:** `logs/orchestrator.log` + the sqlite ledger:
  ```bash
  sqlite3 state/ledger.db "SELECT * FROM ticker_action ORDER BY updated_at DESC LIMIT 20;"
  sqlite3 state/ledger.db "SELECT * FROM orders ORDER BY submitted_at DESC LIMIT 20;"
  ```
- **Email digest (optional):** an executive summary of each real run — per-ticker
  signal, decision + debate conclusion, what traded, and P&L vs baseline — emailed once
  per trading day (plus halt/auth-error alerts). See "Email digest" below to enable.

## Email digest (Resend)
`tick.py report` (Python) builds the email and decides whether it's new; the orchestrator
sends it via the **Resend MCP**. Strictly best-effort — a send failure just logs and the
tick continues. The Resend MCP is registered in **your own Claude config** the same way as
the Robinhood MCP — it is **not** shipped in this repo, and the API key + sender live in
that registration (not in `.env`/`config.yaml`). Off until you enable it. To turn it on:
1. **Register the Resend MCP once** (user scope, in your Claude config — not committed here):
   ```bash
   claude mcp add resend -s user \
     -e RESEND_API_KEY=re_xxxxxxxx \
     -e SENDER_EMAIL_ADDRESS=you@your-verified-domain \
     -- npx -y resend-mcp
   ```
   Get a key at https://resend.com/api-keys; the sender must be a Resend-verified domain
   (or `onboarding@resend.dev` to email only your own Resend account address). Confirm with
   `/mcp` that **resend** is connected — exactly like the Robinhood MCP.
2. **Enable it:** in `config.yaml` set `notify.enabled: true` and `notify.to: ["you@inbox"]`.
   Leave `notify.from` blank to use the MCP's `SENDER_EMAIL_ADDRESS`, or set it to override.
3. **Preview without sending:** run `tick.py report --input <a report_input.json>` and eyeball
   the `subject`/`text` it prints (`should_send` is `false` until enabled).

## Commands
```bash
.venv/bin/python tests/test_units.py        # unit tests (no network/broker)
.venv/bin/python analyze.py AAPL            # one decision → one JSON line
.venv/bin/python tick.py preflight          # gate check
.venv/bin/python -m lib.market              # market-hours status (XNYS)
```
Always use `.venv/bin/python` — deps live in the venv, not the global pyenv.

## Notes
- **Data sources / API keys:** prices, technical indicators, fundamentals, and company
  news all come from **yfinance — no API key required** (the `data_vendors` default), and
  StockTwits is keyless too. `ALPHA_VANTAGE_API_KEY` is only for the *optional alternate*
  vendor and is **not used by default** — a blank key degrades nothing. The only required
  key is `DEEPSEEK_API_KEY`.
- **Reddit sentiment** returns `403 Blocked` (unauthenticated scraping). It
  degrades gracefully — the other three analysts and StockTwits still run.
  Optional to fix later with Reddit API credentials.
- **Cost:** a full run is a few cents on DeepSeek; ~$5–20/month at 3 tickers/day.
  The analysis uses **zero** Claude Code quota (only the thin orchestration does).

## Layout
- `analyze.py` — decision wrapper (ticker → one JSON line; injects the memory scorecard; full audit dump; tees live thinking to `logs/reasoning/`).
- `tick.py` — `preflight` / `plan` / `commit` (the deterministic brain) + `reflect` (memory outcomes) / `protect` (post-fill stop) / `report` / `report-commit` / `prune`.
- `lib/` — `config`, `market` (XNYS hours), `ledger` (sqlite), `signals` (pure), `ds_config`, `notify` (pure digest renderer), `memory` (pure scorecard), `storage` (retention/archiver).
- `TICK.md` — the exact per-tick runbook the orchestrator follows.
- `config.yaml` — account, mode, watchlist, caps, order types, models, loop timing + intraday/cadence, storage, `notify` (email).
- `state/ledger.db` — dedup + idempotency + daily P&L baseline + **decision memory** (`decisions`/`outcomes`) + intraday action history + digest markers (survives restart).
- `tradingagents/` — the multi-agent framework, **owned in-tree** (de-vendored; tracked, pruned; `UPSTREAM.md` provenance + Apache-2.0 `LICENSE`). Editable install via root `pyproject.toml`.
- `.env` — `DEEPSEEK_API_KEY` only (gitignored, `chmod 600`). MCP keys (Robinhood, Resend) live in your Claude config, not here.
