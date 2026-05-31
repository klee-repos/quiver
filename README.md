```
       ___        _
      / _ \ _   _(_)_   _____ _ __     »»———————————————►
     | | | | | | | \ \ / / _ \ '__|    »»——————————►
     | |_| | |_| | |\ V /  __/ |        »»————————————————►
      \__\_\\__,_|_| \_/ \___|_|        »»—————————►
```

<div align="center">

**A robot that buys and sells stocks for you — with the safety rules baked in.**

Smart AI studies each stock and picks a side. Plain, fixed rules decide how much to spend.
A separate helper places the orders. The AI is never allowed to touch the money on its own.

`real money` · `runs on its own` · `hard safety limits` · `keeps a logbook` · `usually once a day`

</div>

---

> [!WARNING]
> **Quiver trades real money on its own.** It starts in "practice mode" (no real orders) until
> you turn that off. It can lose money. This is not financial advice. Please read the whole
> page — especially **[Safety controls](#-safety-controls)** — before you let it trade for real.

---

## What is Quiver?

Quiver is a small stock-trading robot. Once a day it looks at a short list of stocks you
choose, decides whether to buy, sell, or wait, and places the orders for you. Then it goes
back to sleep until the next day.

It is built around one simple rule:

> **The AI is allowed to have an opinion. It is never allowed to spend the money.**

That one rule is what keeps it safe. Here is why it matters.

---

## The big idea: thinking and spending are kept apart

Most of the scary stories about trading bots come from letting the "smart" part also pull the
trigger. Quiver splits that into three separate jobs, and puts a wall between them:

```
   🧠  THE THINKER — the AI
   ──────────────────────────────────────────────────────────
   A team of AI analysts reads the news, charts, and numbers.
   It only gives an opinion: buy, sell, or wait.
   It never sees your money or your limits.
          │
          ▼   "I think: BUY"
   ⚙️  THE RULE-KEEPER — plain code
   ──────────────────────────────────────────────────────────
   Takes the opinion and works out the real, safe order.
   It can never go over the limits you set. No AI guesswork.
          │
          ▼   "Buy $25 of AAPL"
   🤖  THE HANDS — the helper
   ──────────────────────────────────────────────────────────
   Places the orders with your broker, and nothing more.
   It just does exactly what the rule-keeper said.
```

- **The thinker** is a team of AI analysts (running on a model called DeepSeek). They argue
  it out — a bull case, a bear case, a final call — and produce one opinion per stock.
- **The rule-keeper** is plain code. It takes that opinion and works out the real order, while
  obeying every limit you set (how much per trade, per day, per stock). The AI never gets to
  override these. If the math says "spend nothing," it spends nothing.
- **The hands** just place the orders. This part can't reason about the market even if it
  wanted to — it only carries out what the rule-keeper handed it.

Quiver also keeps a **logbook** of every past pick and how it turned out (did the stock go the
way it guessed? did it make or lose money?). It shows that report card to the AI next time, so
the picks can get a little smarter over time.

---

## How one trading run works

A "run" is one full pass over your stock list. Quiver wakes up, checks if it should do
anything, and most of the time quietly goes back to sleep. Here is a full run, start to finish:

```
  Wake up (about once an hour)
    │
    ├─ 1. Safety check        Is the off-switch on? Did we lose too much today?
    │                         Is the market even open? Already traded this stock today?
    │                         → If anything looks wrong, stop here and sleep.
    │
    ├─ 2. Look at the account  Read your balances and prices (read-only — touches nothing).
    │
    ├─ 3. Think                The AI studies each stock and gives its opinion.
    │
    ├─ 4. Apply the rules      The rule-keeper turns each opinion into a real, safe order
    │                         (or decides to do nothing).
    │
    ├─ 5. Place the orders     The hands place exactly those orders with the broker.
    │
    └─ 6. Write it down        Save what happened in the logbook.
```

By default this happens **once a day**. There is an optional mode where it can check a few
times a day instead — but even then, the same safety limits apply, and it can only trade a
stock a set number of times per day. That mode is **off** unless you turn it on.

---

## Getting started

### 1 · Turn it on

Quiver runs inside a Claude Code session that stays open. These commands start it and tell it
to do one trading run at a time:

```bash
cd ~/dev/quiver && caffeinate -i -s screen -S quiver
#   → in the window that opens, start Claude:   claude
#   → make sure it's connected to your broker:   /mcp
#   → start the daily loop:
/loop 1h Run one trading tick for the daily stock bot. Follow ~/dev/quiver/TICK.md exactly.
#   → leave it running in the background: press Ctrl-a then d
#   → come back to it later: screen -r quiver
```

It wakes up about once an hour and does almost nothing (a quick check, no AI, no orders) until
the market is open and a stock is due for a look. The `caffeinate` part keeps your computer
from falling asleep while it waits — so keep the lid open or stay plugged in.

### 2 · Watch a practice run

Quiver starts in **practice mode**: it does everything a real run does *except* place orders.
While the market is open, you should see it check the account, think about each stock, and then
place nothing. You can confirm it placed nothing:

```bash
sqlite3 state/ledger.db "SELECT trade_date,ticker,signal,intent,status FROM ticker_action;"  # says: dry_run
sqlite3 state/ledger.db "SELECT COUNT(*) FROM orders;"                                        # should be 0
tail -n 20 logs/orchestrator.log
```

### 3 · Let it trade for real

When you're happy with how practice runs look, open `config.yaml` and change `dry_run` to
`false`. Do this at the start of a fresh trading day. After that, the next run trades for real —
still inside every limit you set.

---

## Settings (`config.yaml`)

Everything Quiver does is set here. The starting values are small and careful — made for a tiny
account. If you fund it with more, raise the dollar limits to match.

| Setting | Example | What it means (in plain words) |
|---|---|---|
| `dry_run` | `true` | **Practice mode.** `true` = no real orders. Set to `false` to trade for real. |
| `watchlist` | `AAPL, MSFT, NVDA` | The stocks to look at each day. More stocks = a bit more cost. |
| `max_dollars_per_trade` | `25` | The most it can spend on any single buy. |
| `daily_capital_deploy_cap` | `75` | The most it can spend buying in one day, total. |
| `max_open_position_per_ticker` | `50` | The most it will ever hold in one stock. |
| `daily_loss_halt_pct` | `5.0` | If you're down this much in a day, it stops everything. |
| `min_buying_power_buffer` | `5` | Always leave at least this much cash untouched. |
| `intraday_enabled` | `false` | `false` = trade once a day. `true` = let it check a few times a day. |
| `buy_type` | `market` | How it buys. The other option only buys whole shares at a set price. |
| `protective_stop` | `false` | If on, it sets an automatic "sell if it drops too far" order after a buy. |
| `retention_days` | `30` | How long to keep old logs before cleaning them up. |

> [!TIP]
> The two "advanced" features — checking several times a day, and the fancy order types — are
> **off by default**. With the defaults, Quiver does the simple, well-tested thing: one careful
> buy or sell a day. Only turn the extras on after you've watched plenty of practice runs.

---

## 🛑 Safety controls

These are the brakes. Know where they are before you start.

| Control | What it does |
|---|---|
| **Off switch** | Type `touch KILL` and Quiver stops trading on its next wake-up. Type `rm KILL` to let it start again. |
| **Daily loss limit** | If your account drops past the limit you set in a single day, Quiver halts itself *and* flips the off switch — no more trades until you check on it. |
| **Lost connection** | If the link to your broker expires, Quiver stops instead of guessing. You reconnect by running `/mcp`. It will **never** trade on stale information. |

---

## Watching it work

You can see everything Quiver does:

- **Listen in live** — `logs/reasoning/<date>_<TICKER>.log` shows the AI's thinking as it
  happens. Run `tail -f` on it during a run to watch in real time.
- **Read the full write-up** — `state/analyze_logs/<date>_<TICKER>.json` has the complete
  analysis for each stock: the research, the bull-vs-bear debate, and the final call.
- **See what it actually did** — `logs/orchestrator.log` plus its logbook:
  ```bash
  sqlite3 state/ledger.db "SELECT * FROM ticker_action ORDER BY updated_at DESC LIMIT 20;"
  sqlite3 state/ledger.db "SELECT * FROM orders        ORDER BY submitted_at DESC LIMIT 20;"
  ```
- **Get a daily email** (optional) — a short summary of each real day: what it looked at, what
  it decided, what it traded, and how the account did. See below to switch it on.

### Daily email summary (optional)

Quiver can email you a recap after each real trading day (plus an alert if it ever halts
itself). It's off until you turn it on, and it's totally separate from trading — if an email
fails to send, trading carries on as normal.

1. **Connect the email service once:**
   ```bash
   claude mcp add resend -s user \
     -e RESEND_API_KEY=re_xxxxxxxx \
     -e SENDER_EMAIL_ADDRESS=you@your-verified-domain \
     -- npx -y resend-mcp
   ```
   Grab a key at <https://resend.com/api-keys>. Then run `/mcp` and check that **resend** shows
   as connected.
2. **Switch it on:** in `config.yaml`, set `notify.enabled: true` and put your email under
   `notify.to`.
3. **Preview first:** you can build a sample email and read it without sending anything.

---

## Handy commands

```bash
.venv/bin/python tests/test_units.py        # run the safety tests (no money, no internet)
.venv/bin/python analyze.py AAPL            # ask the AI about one stock and print its opinion
.venv/bin/python tick.py preflight          # run just the safety check
.venv/bin/python -m lib.market              # is the market open right now?
```

> Always use `.venv/bin/python` — the project's tools live there.

---

## Good to know

- **What data does it use?** Stock prices, charts, company numbers, and news all come from a
  free source — **no paid data key needed**. The only key you must have is one for the AI
  (DeepSeek).
- **What does it cost to run?** The AI is cheap — a full run is a few cents, roughly
  **$5–20 a month** if you watch three stocks a day.
- **One small gap:** it can't read Reddit (the site blocks it), so it just skips that and uses
  its other sources. Nothing breaks.

---

## What's in the box

| File or folder | What it's for |
|---|---|
| `analyze.py` | The thinker. Give it a stock, it prints back one opinion. |
| `tick.py` | The rule-keeper. All the safety math and order decisions live here. |
| `lib/` | The building blocks: market hours, the logbook, the safety rules, email, memory. |
| `TICK.md` | The exact step-by-step the helper follows on every run. |
| `config.yaml` | All your settings (the table above). |
| `state/ledger.db` | The logbook — every decision, order, and result. Survives restarts. |
| `tradingagents/` | The AI analyst team that powers the thinking. |
| `.env` | Your private keys and account number. Never shared, never committed. |

---

<div align="center">

*The code decides. The AI advises. The robot only ever pulls the trigger.*

<sub>The analyst team in `tradingagents/` is an open-source framework used under the Apache-2.0 license — see `tradingagents/LICENSE` and `tradingagents/UPSTREAM.md`.</sub>

</div>
