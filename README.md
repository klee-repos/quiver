# Quiver Desk

Quiver Desk is a chat-first investing partner on Telegram. You talk to it like you text a
person. It reads SEC filings, earnings calls, and news for the companies in your book. It
tells you what changed. When a change calls for a trade, it sends you a proposal with
buttons. **Nothing trades without your tap.**

> **Quiver trades real money in a real Robinhood account.** Each order needs your tap on an
> Approve button in Telegram. It can still lose money. This is not financial advice. Read
> [The nine walls](#the-nine-walls) before you connect a broker account.

---

## What the desk does

- **It talks with you.** It puts a reaction on each of your messages, chosen for what the
  message says. Then it answers in short messages, one idea per message.
- **It holds the premise.** The premise is the top-level strategy of the book. The desk
  seeds it from `strategy.yaml` and `STRATEGY.md` once. You change it in conversation. Each
  change is a proposal that you confirm with a tap.
- **It builds the company list with you.** It searches SEC full-text filings, the yfinance
  screener, and the top holdings of the themed ETFs in the book. It loads each company: its
  filings, its XBRL numbers, and its latest earnings call. You decide what goes in the book.
- **It keeps a thesis for each holding.** A thesis says why we own the company, what drives
  it, what makes us sell, and what it is worth. Python computes the value from the filings
  (reverse DCF and scenarios). The desk proposes a buy-below level and a trim-above level.
- **It acts only on change.** A new filing or earnings release, a price that crosses an
  agreed level, drift past a band, or material news creates an event. No event, no proposal.
- **It remembers everything.** Every filing section, transcript, article, finding, thesis
  version, proposal, and chat message is stored in the ledger with full-text search.

ETFs stay as sleeves. The desk analyzes an ETF through its top holdings (the look-through).
The cash sleeve (`SGOV`) is never bought or sold.

---

## How a day works

```
  11:40 to 23:40 UTC, hourly at :40, Monday to Friday
    │
    ├─ quiver.service (the monitor, pure Python, no LLM, no broker)
    │    queues a broker snapshot during the session
    │    sweeps SEC EDGAR for new filings, ingests news
    │    checks prices against the agreed levels (session only)
    │    after the close: drift, the close brief, outcomes, benchmark, goal, prune
    │    each change becomes an event → an analysis job for the worker
    │
    ├─ quiver-worker (the analyst, Opus 5.5)
    │    reads the new material, records findings with exact quotes
    │    states a verdict on the thesis: intact, strengthened, weakened, or broken
    │    tells you in two to four messages; proposes a trade only when the rules allow it
    │
    ├─ You, on Telegram
    │    ask questions at any time (the desk agent, Sonnet 5.5)
    │    tap Approve, Reject, or Confirm on a card
    │
    └─ quiver-broker (the only process with Robinhood access)
         takes portfolio snapshots
         on your Approve: re-quotes, places exactly the checked orders, reports the fills
```

Each trading day ends with a **close brief**: two to four short messages with the day's
P&L, what moved, what happened, what waits on you, and earnings in the next five trading
days.

---

## The three roles

| Role | Runs in | Model (env var, default) | What it does |
|---|---|---|---|
| Desk agent | `quiver-chat` | `QUIVER_DESK_MODEL=claude-sonnet-5-5` | Talks with you. Reads the research memory. Sends proposals. |
| Analyst | `quiver-worker` | `QUIVER_ANALYST_MODEL=claude-opus-5-5` | Analyzes each event and each deep-dive. Records findings and verdicts. |
| News classifier | `quiver-worker` | `QUIVER_CLASSIFIER_MODEL=claude-haiku-5-5` | Marks each headline material or not. JSON only, no tools. |

The classifier model also picks the quick reaction for a message that arrives while a turn
runs. The broker runbooks (`EXECUTE.md`, `SNAPSHOT.md`) run as `claude -p` with
`QUIVER_BROKER_MODEL=claude-sonnet-5-5`. The code holds these defaults. Set an env var only
to override one.

No model places an order. No model sizes an order. Python sizes every order, and only your
tap approves one.

---

## The four desk services

| Unit | Program | Starts | Broker | LLM |
|---|---|---|---|---|
| `quiver-chat.service` | `deploy/runner/chat_bridge.py` | always on | none | desk agent |
| `quiver-worker.path` → `quiver-worker.service` | `deploy/runner/run_worker.py` | a job is queued | none | analyst, news classifier |
| `quiver-broker.path` → `quiver-broker.service` | `deploy/runner/run_broker.py` | a snapshot or execute job is queued | yes, the only one | broker runbook |
| `quiver.service` + `quiver.timer` | `deploy/runner/run_monitor.py` | hourly at :40, 11:40 to 23:40 UTC, Monday to Friday | none | none |

Two housekeeping services also run: `quiver-backup` (the nightly ledger backup to Cloud
Storage, 22:15 UTC) and `quiver-claude-update` (the daily Claude Code update, 05:00 UTC).

A writer starts the worker or the broker with a trigger file
(`state/triggers/worker`, `state/triggers/broker`). The runner deletes the file, then drains
its queue. A trigger written during a drain starts the runner again, so no trigger is lost.

The ledger (`state/ledger.db`, SQLite) is the one source of truth. It holds the premise, the
book, the theses, the research memory, the proposals, the orders, and the chat.

---

## The tap flow

1. The desk or the analyst calls `propose_trade` or `propose_change`.
2. Python builds the proposal. For a trade, it sizes each leg toward the target weight and
   reserves one `orders` row per leg with a fixed `ref_id`. A retry can never mint a second
   order.
3. The outbox sends a card in plain prose: the exact orders, the dollars, the effect on the
   book, the reason, and the evidence. A trade card has **Approve** and **Reject**. A change
   card has **Confirm** and **Reject**.
4. You tap. `lib/desk/approvals.handle_tap` checks your user id, the chat, the message, the
   nonce, the state, and the expiry. Then it claims the proposal in one conditional `UPDATE`.
   A second tap gets "Already approved".
5. On **Approve**, the broker runner starts. Python fetches a fresh Robinhood quote. If the
   price moved more than 1.5% (`requote_tolerance_pct`), the desk sends new numbers with new
   buttons and places nothing. Otherwise the broker places the checked orders, and the desk
   tells you the fills.
6. On **Confirm**, Python applies the change (for example a new target weight). If the
   change implies a trade, Python sends a second card with its own **Approve** button. A
   Confirm never approves a trade.

Rules that apply to every proposal:

- A proposal expires at the close of the current regular session.
- A new trade proposal for the same name replaces the old one. The old buttons change to
  "Replaced by #N" and stop working.
- An Approve tap is refused when the `KILL` file exists, when today is halted, when the
  market is closed, or less than 5 minutes before the close. The proposal stays open.
- A "yes" in chat is not an approval. Only the button approves.

The seven change kinds are `add_company`, `drop_company`, `set_weights`, `set_risk`,
`set_premise`, `set_thesis`, and `resume_trading`.

---

## The nine walls

1. **Broker isolation at the kernel.** Only `quiver-broker` can read the Robinhood OAuth.
   Every unit hides `/etc/quiver`. `quiver-chat`, `quiver-worker`, and `quiver` hide
   `state/claude-config`. The desk and the analyst use their own config dir
   (`state/claude-desk`) with no Robinhood server registered.
2. **Exact, checked orders only.** `broker_guard` allows a place call only for a leg of the
   current `execute` job. `exec-check` must have passed that leg less than 120 s ago, and the
   call fields must match the leg. The guard also needs no `KILL`, no halt, and more than
   120 s to the close. It denies every other broker write. A broken guard blocks.
   `run_broker.py` self-tests the guard before each job.
3. **Approval only by a tap.** `approvals.handle_tap` is the only code that sets
   `approved`. No tool can reach it.
4. **No shell and no file writes for the desk or the analyst.** They have typed tools,
   read-only file tools confined to the repo, and web search. A `PreToolUse` hook enforces
   the allowlist. If the hook fails, it denies.
5. **Secrets stay out of model processes.** The bridge and the worker move every secret
   into a `Secrets` object and delete it from `os.environ` before the first Claude client
   starts. The broker runner passes an explicit env to `claude -p`. No tool returns a secret.
6. **Owner allowlist by user id.** A message or a tap counts only from a `from.id` in
   `TELEGRAM_ALLOWED_CHAT_IDS`, in a private chat with that same id.
7. **Change before trade.** An agent proposes a trade only from an event with new evidence,
   or from your message in the current turn. Python sizes every order. A trade only moves a
   name toward its confirmed target.
8. **Halts.** The `KILL` file stops all execution. The daily-loss halt stops execution for
   the rest of the day.
9. **SSRF-safe fetch.** `fetch_document` refuses private and metadata addresses and pins the
   checked address.

**Remaining risks:**

- A person who holds your Telegram session can chat with the desk and tap Approve. Turn on
  Telegram two-step verification (Settings → Privacy and Security → Two-Step Verification).
- Telegram bot chats are not end-to-end encrypted. Positions and P&L pass through Telegram.
- A prompt injection in a filing, an article, or a web page can make the desk propose
  something. It cannot execute anything: you see every proposal and tap it yourself.

---

## How to talk to it

Send a message to the bot. Examples:

- "What do we own, and how did today go?"
- "Find more companies in AI power."
- "What did ANET say about AI back-end networks last quarter?"
- "Why did we add GEV?"
- "Trim CEG a bit."
- "Do a deep-dive on VRT."

Commands that Python answers without a model turn:

| Command | Effect |
|---|---|
| `/status` | Snapshot age, pending proposals, the last monitor run, the halt state, the Robinhood login state. |
| `/clear` (also `/reset`, `/new`) | Starts a new conversation. The research memory and the chat log stay. |
| `/stop` | Stops the running turn. |

[docs/CHAT.md](docs/CHAT.md) gives the full conversation model, the tools of each role, the
cards, and the research memory.

---

## Operator commands

Use the venv interpreter (`.venv/bin/python`). On the box, run each command as the `quiver`
user with `/etc/quiver/quiver.env` loaded ([docs/DEPLOY.md](docs/DEPLOY.md)).

`tick.py` has 22 subcommands. Each prints one JSON line.

| Subcommand | Who calls it |
|---|---|
| `fills`, `wait` | `EXECUTE.md` (the broker runbook) |
| `exec-prepare`, `exec-check`, `exec-commit` | `run_broker.py` and `EXECUTE.md` |
| `snapshot-prepare`, `snapshot-commit` | `run_broker.py` and `SNAPSHOT.md` |
| `auth-stop` | `SNAPSHOT.md` and `EXECUTE.md` on a Robinhood 401 |
| `reflect`, `benchmark-fetch`, `benchmark-backfill`, `goal-track`, `prune` | `run_monitor.py`, after the close |
| `flow-record`, `flow-suggest` | You (record a deposit or a withdrawal) |
| `attribution`, `factors-fetch` | You (read-only factor attribution) |
| `strategy-history`, `decision-proof` | You (the change log and the proof of one decision) |
| `send-test` | You (the deploy gate: sends one real Telegram page) |
| `desk-seed` | You, once at the cutover (idempotent) |
| `desk-review` | You (queue a deep-dive review of each holding) |

```bash
.venv/bin/python tick.py desk-seed                         # seed the premise, the book, the companies
.venv/bin/python tick.py desk-review                       # review each holding (or --tickers A,B)
.venv/bin/python tick.py send-test --kind auth_error       # send one real page to Telegram
.venv/bin/python tick.py strategy-history --limit 20       # the change log
.venv/bin/python tick.py decision-proof --id <decision_id> # the proof of one placed leg
.venv/bin/python tick.py flow-suggest                      # suspected deposits, as flow-record commands
.venv/bin/python tick.py attribution --benchmark SPY       # market exposure vs selection
.venv/bin/python deploy/runner/healthcheck.py              # offline checks
.venv/bin/python deploy/runner/healthcheck.py --live       # plus the units, the walls, the logins
.venv/bin/python deploy/runner/chat_query.py "SELECT id,state FROM proposals ORDER BY id DESC LIMIT 5"
.venv/bin/python -m lib.market                             # market hours (XNYS calendar)
```

Stop all execution: `touch KILL`. Resume: `rm KILL`.

---

## Tests

```bash
tests/run_e2e.sh            # every deterministic suite: no network, no LLM, no broker
tests/run_e2e.sh --live     # also the live desk e2e (real models; costs tokens)
.venv/bin/python tests/test_units.py   # one suite: sizing, the decision wall, the renderer
```

`tests/run_e2e.sh` exits non-zero on any failure. The suites read captured fixtures under
`tests/fixtures/desk/`. The live suite runs only with `--live`.

---

## Data sources

| Source | Used for | Key |
|---|---|---|
| SEC EDGAR | filings, sections, XBRL facts, full-text search, N-PORT holdings | `SEC_USER_AGENT` (required, "Quiver" plus your contact email) |
| yfinance | prices, news, the screener, ETF top holdings, technicals | none |
| Alpha Vantage | earnings-call transcripts | `ALPHA_VANTAGE_API_KEY` (optional, 25 calls a day) |
| Robinhood MCP | snapshots, quotes at the tap, orders, fills | OAuth, `quiver-broker` only |
| Claude | every model | `CLAUDE_CODE_OAUTH_TOKEN` (subscription token) |

---

## Files

| File or folder | What it holds |
|---|---|
| `deploy/runner/` | The four runners, `broker_guard.py`, `healthcheck.py`, `backup_ledger.py`. |
| `deploy/*.service`, `*.path`, `*.timer` | The systemd units. |
| `deploy/gcp/` | Provisioning (`setup.sh`), updates (`update.sh`, `sync.sh`), secrets (`bootstrap-gcp.sh`), Robinhood login (`rh-reauth.sh`), Terraform. |
| `lib/desk/` | The desk: store, EDGAR, sections, XBRL, valuation, transcripts, news, ETF look-through, events, levels, proposals, approvals, execution, outbox, tools, agent. |
| `lib/` | Sizing, market hours, the ledger, the run locks, the renderer, Telegram. |
| `prompts/` | The prompts of the desk agent, the analyst, the news classifier, the quick reaction, and the broker. |
| `EXECUTE.md`, `SNAPSHOT.md` | The broker runbooks. |
| `tick.py` | The operator and runbook subcommands. |
| `config.yaml` | Start values for the risk and desk settings. Committed; it holds no secrets. |
| `strategy.yaml`, `STRATEGY.md` | Your book and premise, read once by `desk-seed`. Private and gitignored. Copy `strategy.yaml.example` to start. |
| `.env` | Your secrets. Gitignored. Copy `.env.example` to start. |
| `state/ledger.db` | The ledger. Gitignored. |
| `docs/DESK_PLAN.md` | The build plan and the spec. |

After `desk-seed`, the ledger holds the book and the settings. `config.yaml` gives only the
start values. A confirmed `set_risk` proposal changes a setting. The daily-loss halt starts
at 20% (`risk.daily_loss_halt_pct`).

---

## License

**Free for noncommercial use. Commercial use must be purchased.**

`quiver` is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE) (SPDX: `PolyForm-Noncommercial-1.0.0`).

| Your use | Cost |
|---|---|
| Personal study, hobby project, experiment, amateur work | **Free** |
| Charity, school, public research body, government body | **Free** |
| Work for a company or a client | **Buy a license** |
| A product or service you sell | **Buy a license** |
| Internal use at a for-profit company | **Buy a license** |

If you pass on a copy of any part of this software, you must keep the
`Required Notice:` line from [LICENSE](LICENSE) with it.

To buy a commercial license, see [COMMERCIAL.md](COMMERCIAL.md) for the terms, the credit
requirement, and the contact address.
