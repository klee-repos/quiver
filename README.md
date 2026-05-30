```
       ___        _
      / _ \ _   _(_)_   _____ _ __     »»———————————————►
     | | | | | | | \ \ / / _ \ '__|    »»——————————►
     | |_| | |_| | |\ V /  __/ |        »»————————————————►
      \__\_\\__,_|_| \_/ \___|_|        »»—————————►
```

<div align="center">

**An autonomous stock-trading desk that splits *thinking* from *doing* — and keeps the trigger finger in Python.**

A multi-agent LLM reasons about the market. Deterministic Python decides every dollar.
Claude Code just pulls the arrows from the quiver and lets them fly.

`real money` · `multi-agent LLM` · `deterministic guardrails` · `SQLite ledger` · `runs once a day`

</div>

---

> [!WARNING]
> **Quiver trades real money, fully autonomously.** It is currently in `dry_run` (paper)
> mode. It can and will lose money. Nothing here is investment advice. Read the whole
> README — especially **[Controls](#-controls)** — before flipping the live switch.

---

## What it is

Quiver is a small, opinionated trading bot built around one idea:

> **Every trading decision and risk guardrail lives in deterministic Python — never in an LLM's judgment.**

Three layers, with a hard wall between reasoning and execution:

```
  ┌──────────────────────────────┐   thinks about the market,
  │   🧠  tradingagents/  (LLM)   │   never touches the broker
  │   multi-agent · DeepSeek      │   → emits a Buy/Sell/Hold signal
  └───────────────┬──────────────┘
                  │  signal JSON
  ┌───────────────▼──────────────┐   decides every dollar,
  │   ⚙️   tick.py  (pure Python) │   clamps, sizes, dedups, halts
  │   the deterministic "brain"   │   → emits the exact orders to place
  └───────────────┬──────────────┘
                  │  orders
  ┌───────────────▼──────────────┐   executes exactly what Python
  │   🤖  Claude Code  (MCP)      │   returned, reasons about nothing
  │   the execution orchestrator  │   → places orders via Robinhood MCP
  └──────────────────────────────┘
```

The LLM (a multi-agent framework owned in-tree under `tradingagents/`, running on
**DeepSeek**) produces a signal per ticker. **Claude Code** — holding the Robinhood MCP —
is *only* the hands: it feeds broker data to Python and places exactly the orders Python
hands back. All sizing, clamping, dedup, cadence, idempotency, and halt logic are pure
Python in `tick.py` / `lib/`. The orchestrator never reasons about the market; the
analysis path never sees a trading limit or the broker.

Quiver also keeps a **decision memory** — past calls plus their real outcomes, grounded in
the ledger — and feeds that scorecard back into each new analysis.

> [!NOTE]
> The framework under `tradingagents/` is an owned, in-tree fork of
> [TradingAgents](https://github.com/TauricResearch/TradingAgents), de-vendored and pruned
> to a DeepSeek-only signal path. Provenance in `tradingagents/UPSTREAM.md`; Apache-2.0.

---

## How it works — one "tick"

A *tick* is one full trading pass, driven by the runbook in `TICK.md`. The loop wakes
hourly and no-ops cheaply until the market is open and a ticker is due.

```
 /loop (a kept-open Claude Code session)
   └─ each tick → follow TICK.md:
        ├─ tick.py preflight          kill / halt / market-open / dedup gates   ⚙️ Python
        ├─ get_portfolio / positions  read-only broker snapshot                 🤖 MCP
        ├─ analyze.py <TICKER>        TradingAgents on DeepSeek → signal JSON    🧠 Python→LLM
        ├─ tick.py plan               clamp + size + reserve ref_ids            ⚙️ Python
        ├─ review / place_equity_order  execute EXACTLY what plan returned       🤖 MCP
        └─ tick.py commit             record the outcome to the SQLite ledger   ⚙️ Python
```

By default this happens **once a day**. Optionally (off by default) the model can pace its
own re-checks intraday — Python clamps the cadence and bounds repeat trades with a
per-ticker cooldown, a daily action cap, and an on-change gate.

---

## Status

<div align="center">

**`2026-05-30` — built and validated end-to-end · running in dry-run · pending one live session**

</div>

| | |
|---|---|
| ✅ **Unit tests** | 169/169 pass (`tests/test_units.py`, offline) |
| ✅ **Decision path** | `analyze.py AAPL` ran a full DeepSeek analysis → valid signal |
| ✅ **Drills** | plan / kill-switch / preflight all pass offline |
| ✅ **Models** | `deepseek-v4-flash` (quick) + `deepseek-v4-pro` (deep) live |
| ✅ **Account** | an `agentic_allowed` Robinhood account (set via `.env`), ~$100 buying power |
| ✅ **Guardrails** | sized for $100: $25/trade · $75/day · 5% daily-loss halt |
| ✅ **Capabilities** | owned framework · ledger-grounded memory · optional intraday cadence · optional limit/stop orders *(risky paths OFF by default)* |
| ✅ **Live path** | one full dry-run pass off-hours: live `analyze.py` → `plan` → digest emailed, **0 orders** |
| ⏳ **Pending** | one **market-hours**, preflight-gated tick → then flip `dry_run: false` |

> The dry-run gate needs an open market, so the first live tick happens on the next
> trading session.

---

## Quick start

### 1 · Start the loop (a kept-open session)

```bash
cd ~/dev/quiver && caffeinate -i -s screen -S quiver
#   → inside the screen window:   claude
#   → confirm the Robinhood MCP (and Resend, if email is on) is connected:   /mcp
#   → start the once-daily loop:
/loop 1h Run one trading tick for the daily stock bot. Follow ~/dev/quiver/TICK.md exactly.
#   detach the screen: Ctrl-a d        reattach later: screen -r quiver
```

The loop wakes hourly; a tick no-ops cheaply (config read + clock check, no LLM, no orders)
until the market is open and the ticker hasn't been acted on today. `caffeinate -i -s`
blocks idle/AC sleep (not lid-close — keep the lid open or stay on AC power).

### 2 · Watch the first dry-run tick

Around 9:35 ET it should pass preflight → snapshot the account → analyze each watchlist
ticker → call `review_equity_order` → **place nothing**. Confirm:

```bash
sqlite3 state/ledger.db "SELECT trade_date,ticker,signal,intent,status FROM ticker_action;"  # status = dry_run
sqlite3 state/ledger.db "SELECT COUNT(*) FROM orders;"                                        # 0 (nothing placed)
tail -n 20 logs/orchestrator.log
```

### 3 · Go live

Set `dry_run: false` in `config.yaml`. Do it on a **fresh trading day**, or clear that
day's rows first (otherwise dedup skips tickers already "acted" in dry-run):

```bash
.venv/bin/python -c "from lib.ledger import Ledger; print(Ledger('state/ledger.db').clear_day('YYYY-MM-DD'))"
```

The next tick trades for real — sized by the model, clamped by `risk:`.

---

## Configuration (`config.yaml`)

Values below are sized for the ~$100 account — bump them proportionally when funded.

| Key | Value | Meaning |
|---|---|---|
| `RH_ACCOUNT_NUMBER` *(in `.env`)* | — | the `agentic_allowed` Robinhood account — per-user secret, **not** in `config.yaml` |
| `dry_run` | `true` | paper mode; review only, never place |
| `watchlist` | `AAPL, MSFT, NVDA` | tickers analyzed each day (cost scales with size) |
| `risk.max_dollars_per_trade` | `25` | hard per-trade ceiling |
| `risk.daily_capital_deploy_cap` | `75` | max total BUY $ per day |
| `risk.max_open_position_per_ticker` | `50` | max $ held in one ticker |
| `risk.daily_loss_halt_pct` | `5.0` | equity drop vs day baseline → halt + `KILL` |
| `risk.min_buying_power_buffer` | `5` | never spend below this much |
| `deepseek.chat_model` | `deepseek-v4-flash` | analyst tool-calls (must support function calling) |
| `deepseek.reasoner_model` | `deepseek-v4-pro` | debates / judgment |
| `loop.intraday_enabled` | `false` | **OFF = classic once-a-day.** `true` = model-paced multi-run |
| `loop.*_min` cadence bounds | `60 / 30 / 120 / 1440` | cooldown · floor · open-ceiling · ceiling (minutes) |
| `risk.max_actions_/_analyses_per_ticker_per_day` | `3 / 6` | intraday trade cap + LLM-cost (analysis) cap |
| `order.buy_type` | `market` | `limit` = marketable-limit, whole-share entries (Python-priced) |
| `order.protective_stop.enabled` / `stop_pct` | `false / 8.0` | post-fill GTC stop, Python-clamped (model only seeds) |
| `storage.retention_days` | `30` | age out logs/transcripts; S3 archive deferred, off |

> [!TIP]
> **Intraday and advanced orders are opt-in.** At defaults, Quiver behaves exactly as the
> validated once-a-day, market-order path. `loop.intraday_enabled: true` lets the model
> recommend when to look next (Python clamps it and bounds repeat trades); `order.buy_type:
> limit` and `protective_stop` add price-protected entries and resting stops. Enable any of
> these only after a clean live dry-run validation.

---

## 🛑 Controls

| Control | What it does |
|---|---|
| **Kill switch** | `touch KILL` halts all trading next tick · `rm KILL` resumes |
| **Daily-loss halt** | auto-fires + writes `KILL` if equity drops past `risk.daily_loss_halt_pct` vs the day's opening baseline |
| **MCP token expiry** | a tick logging `AUTH_ERROR` means the Robinhood token expired — reattach the screen, run `/mcp` to re-auth. The bot stops rather than trading blind. Re-auth proactively (~weekly). |

---

## Observe a run

- **Live thinking** — `logs/reasoning/<date>_<TICKER>.log`: the framework's moment-to-moment
  agent chatter, teed as it runs. `tail -f` it during a tick.
- **Per-ticker reasoning + reports** — `state/analyze_logs/<date>_<TICKER>.json`: full analyst
  reports, bull/bear debate, trader plan, final decision (~48 KB).
- **What the bot did** — `logs/orchestrator.log` + the SQLite ledger:
  ```bash
  sqlite3 state/ledger.db "SELECT * FROM ticker_action ORDER BY updated_at DESC LIMIT 20;"
  sqlite3 state/ledger.db "SELECT * FROM orders        ORDER BY submitted_at DESC LIMIT 20;"
  ```
- **Email digest (optional)** — an executive summary of each real run (per-ticker signal,
  decision + debate conclusion, what traded, P&L vs baseline), emailed once per trading day
  plus halt/auth-error alerts. See below to enable.

### Email digest (Resend)

`tick.py report` (Python) builds the email and decides whether it's new; the orchestrator
sends it via the **Resend MCP**. Strictly best-effort — a send failure just logs and the
tick continues. The Resend MCP lives in **your own Claude config** (like the Robinhood MCP),
**not** in this repo; the API key + sender live in that registration. Off until you enable it.

1. **Register the Resend MCP once** (user scope, not committed here):
   ```bash
   claude mcp add resend -s user \
     -e RESEND_API_KEY=re_xxxxxxxx \
     -e SENDER_EMAIL_ADDRESS=you@your-verified-domain \
     -- npx -y resend-mcp
   ```
   Get a key at <https://resend.com/api-keys>; the sender must be a Resend-verified domain
   (or `onboarding@resend.dev` to email only your own Resend account address). Confirm with
   `/mcp` that **resend** is connected.
2. **Enable it** — in `config.yaml` set `notify.enabled: true` and `notify.to: ["you@inbox"]`.
   Leave `notify.from` blank to use the MCP's `SENDER_EMAIL_ADDRESS`, or set it to override.
3. **Preview without sending** — run `tick.py report --input <a report_input.json>` and eyeball
   the `subject`/`text` it prints (`should_send` stays `false` until enabled).

---

## Commands

```bash
.venv/bin/python tests/test_units.py        # unit tests (no network/broker)
.venv/bin/python analyze.py AAPL            # one decision → one JSON line
.venv/bin/python tick.py preflight          # gate check
.venv/bin/python -m lib.market              # market-hours status (XNYS calendar)
```

> Always use `.venv/bin/python` — deps live in the venv, not the global pyenv.

---

## Good to know

- **Data sources / API keys** — prices, technical indicators, fundamentals, and company news
  all come from **yfinance (no API key required)**, and StockTwits is keyless.
  `ALPHA_VANTAGE_API_KEY` is only for an *optional alternate* vendor and is unused by default.
  **The only required key is `DEEPSEEK_API_KEY`.**
- **Reddit sentiment** returns `403 Blocked` (unauthenticated scraping) and degrades
  gracefully — the other three analysts and StockTwits still run. Optional to fix later with
  Reddit API credentials.
- **Cost** — a full run is a few cents on DeepSeek; roughly **$5–20/month at 3 tickers/day**.
  The analysis uses **zero** Claude Code quota — only the thin orchestration does.

---

## Layout

| Path | What it is |
|---|---|
| `analyze.py` | decision wrapper: ticker → one JSON line (injects the memory scorecard; full audit dump; tees live thinking to `logs/reasoning/`) |
| `tick.py` | the deterministic brain: `preflight` / `plan` / `commit` + `reflect` / `protect` / `report` / `report-commit` / `prune` |
| `lib/` | `config` · `market` (XNYS hours) · `ledger` (SQLite) · `signals` (pure) · `ds_config` · `notify` (pure digest renderer) · `memory` (pure scorecard) · `storage` (retention/archiver) |
| `TICK.md` | the exact per-tick runbook the orchestrator follows |
| `config.yaml` | account · mode · watchlist · caps · order types · models · loop/cadence · storage · `notify` |
| `state/ledger.db` | dedup + idempotency + daily P&L baseline + **decision memory** + intraday history + digest markers (survives restart) |
| `tradingagents/` | the multi-agent framework, **owned in-tree** (de-vendored, pruned; `UPSTREAM.md` + Apache-2.0 `LICENSE`). Editable install via root `pyproject.toml` |
| `.env` | per-user secrets (gitignored, `chmod 600`): `DEEPSEEK_API_KEY`, `RH_ACCOUNT_NUMBER`, `NOTIFY_TO`. MCP keys live in your Claude config, not here |

---

<div align="center">

*Built with Python that decides, an LLM that reasons, and a robot that only ever pulls the trigger.*

</div>
