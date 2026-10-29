# Chat with the desk over Telegram

You talk to Quiver Desk in a private Telegram chat. The desk answers questions about the
book, the companies, the filings, and the trades. It sends proposals as cards with buttons.
Nothing trades unless you tap **Approve**.

`deploy/runner/chat_bridge.py` runs the chat. The unit is `quiver-chat.service`. It is
always on, and it is the only process that polls the bot token.

## Why Telegram

The box has no public inbound port (an IAP-only firewall). The Telegram Bot API supports a
long poll: the box connects out to Telegram over HTTPS. So the chat needs no public
endpoint, no phone number, and no SMS registration.

## The conversation

### Intake

1. The bridge long-polls `getUpdates` (50 s) for messages and button taps.
2. It accepts an update only from a private chat, from a `from.id` in
   `TELEGRAM_ALLOWED_CHAT_IDS`, with `chat.id == from.id`. Any other sender gets "Not
   authorized. Your chat id is NNN." once. Then the bridge ignores that sender.
3. The bridge stores each owner message in `chat_messages` before it moves the update
   offset. A crash re-delivers the update, and a unique index drops the duplicate. No
   message is lost.
4. On start, the bridge processes the backlog in order. Telegram holds updates for 24 hours
   while the bridge is down.

### The reaction

The desk puts one reaction on each of your messages. It picks the emoji for what the
message says (for example 🤔 on a hard question, 🔥 on good news, 🫡 on an instruction). It
never uses a fixed emoji.

- The `react` tool accepts only the 73 emoji that Telegram allows as reactions
  (`lib/desk/reactions.py`). For any other emoji, the tool returns the allowed set, and the
  model picks again.
- The `say` tool refuses to send until `react` has run for each owner message in the turn.
  A Telegram error on the reaction still counts, so a failed reaction never blocks a reply.
- A message that arrives while a turn runs gets a quick reaction from the classifier model
  (`QUIVER_CLASSIFIER_MODEL`, no tools, 10 s limit). If the quick pick fails, the next turn
  reacts as usual.
- The bridge records each choice in `chat_messages.reaction`.

### Short messages

- The desk sends each message with the `say` tool. One call is one Telegram message.
- `say` refuses text over 1,200 characters and tells the model to split it.
- The outbox waits at least 0.8 s between messages to one chat.
- The bridge sends the typing action every 4 seconds while a turn runs.
- The first message of a turn replies to your newest message.
- Text the model writes after its last `say` is not sent: it is a recap. A turn with no `say`
  sends its text as the answer.
- The bridge converts markdown to Telegram HTML. The analyst's messages use the same
  converter: HTML from a model reaches you as text, and a link always shows its URL. If
  Telegram rejects the HTML, the bridge sends the message again as plain text.

### Plain English

The desk and the analyst write for a reader with no finance training. Their prompts
(`prompts/desk.md`, `prompts/analyst.md`) hold the ASD-STE100 rules: short sentences, the
active voice, and common words. A finance term gets a plain explanation the first time it
appears in a message, and the prompts name one word for each idea (for example "what one
share is worth", not "fair value"). The cards (`lib/notify.py`) use the same words.

### Research on request

Ask about a ticker ("look into NVDA"). The desk gives the facts it has, then calls
`load_company` (when the name is not loaded) and `start_deep_dive` with your question as the
focus. The analyst reads the annual and quarterly reports, the latest results release and the
results call, records the facts with exact quotes, values the company, and reports back in
the chat with a thesis card. A name with no SEC filings cannot be loaded. The desk runs at most
6 deep-dives a day.

To review each holding at once, run `tick.py desk-review` on the box. It queues one deep-dive
for each position in the newest snapshot, largest first, with the 6-a-day limit off. Each
review ends with keep, buy more, sell some, or sell all, and a card when a change is due.

### Turns

One turn runs at a time. Messages that arrive during a turn wait. The next turn takes all of
them together. A turn has 600 seconds. On a timeout the bridge stops the turn and sends:
"That took too long, so I stopped. Ask again, or ask me for a deep-dive." Long work goes to
`start_deep_dive`, which runs in the worker and reports back.

The turn input starts with a `<desk_updates>` block. It lists what the desk already sent
or did since the last turn: monitor events, analyst messages, proposals, and fills. Your
messages follow it:

```
<desk_updates since="2026-10-27T18:40:00Z">
[14:40 ET · monitor] ANET filed an 8-K, item 2.02 (results). Analysis queued.
[15:02 ET · proposal #41] Buy $412 of CEG. Waiting on your tap until 4:00 PM ET.
</desk_updates>
<owner message_id="5512" sent="15:10 ET">what do you think about the ANET numbers?</owner>
```

The bridge escapes `<` and `>` in all embedded text, so quoted text cannot forge a tag. The
desk does not repeat an update unless you ask.

### Sessions

The bridge keeps one Claude Agent SDK session connected while you chat, so a turn starts
without a process start. After 30 minutes with no message, it disconnects. Your next
message resumes the same session. `chat_sessions` stores the session id. The SDK stores the
transcript under `/opt/quiver/state/claude-desk/projects/`. Claude Code compacts a long
session. Nothing is lost: the desk retrieves each fact from the research memory with tools.

### Errors

- **Auth** (the token is invalid or expired): "My Claude login stopped working. Rotate
  CLAUDE_CODE_OAUTH_TOKEN (see docs/DEPLOY.md)." The bridge also sends one `auth_error`
  page (stage `desk`).
- **Usage or spend limit:** the desk quotes the limit text from the error.
- **Any other error:** "I hit an error: \<first line>". The full error goes to
  `logs/desk/bridge.log`.

## Commands

Python answers these without a model turn:

| Command | Effect |
|---|---|
| `/status` | Snapshot age, pending proposals, the last monitor run, the halt state, and the Robinhood login state. |
| `/clear` (also `/reset`, `/new`) | Starts a new SDK session. The research memory and `chat_messages` stay. |
| `/stop` | Stops the running turn. |

`/help` and `/start` give a one-line summary of the desk and these commands.

## The tools by role

D = the desk agent (`quiver-chat`, Sonnet 5.5). A = the analyst (`quiver-worker`, Opus
5.5). `lib/desk/tools.py` holds every tool. Each tool takes typed input and returns compact
JSON.

Both roles also have four built-in tools: `Read`, `Grep`, `Glob`, and `WebSearch`. `Read`,
`Grep`, and `Glob` work only inside `lib/`, `deploy/`, `docs/`, `prompts/`, `tests/`, and
on `tick.py`, `config.yaml`, `EXECUTE.md`, `SNAPSHOT.md`, and `README.md`. A `Grep` or `Glob`
on the repo root is denied, because it would reach `state/` and `.env`.

**Read tools (D, A):** `portfolio_view`, `refresh_portfolio`, `get_quotes`, `get_book`,
`list_companies`, `get_company`, `search_research`, `list_filings`, `get_filing_sections`,
`get_document`, `get_financials`, `get_metrics`, `run_valuation`, `get_technicals`,
`get_transcript`, `get_etf_lookthrough`, `get_events`, `get_proposals`, `get_track_record`,
`get_attribution`, `get_settings`, `ledger_query` (one read-only `SELECT`, at most 200
rows), `screen_companies`.

**Write tools that need no tap:**

| Tool | Role | Effect |
|---|---|---|
| `react` | D | Sets the reaction on an owner message. |
| `say` | D | Sends one message. |
| `notify_owner` | A | Sends one message through the outbox. |
| `load_company` | D, A | Loads a company or an ETF: filings, sections, XBRL, metrics, transcript. A new name gets `status='candidate'`. |
| `fetch_document` | D, A | Fetches a web page and stores its text, so the desk can quote it later. |
| `record_finding` | D, A | Stores a fact only if its quote is in a stored document. |
| `add_note` | D, A | Stores a note, for example an owner preference. |
| `set_research_status` | D | Sets `candidate`, `watch`, or `rejected` with a reason. Never `holding` or `dropped`. |
| `log_news_event` | D, A | Creates a `news` event from a stored news document with a feed date in the last 7 days. |
| `start_deep_dive` | D | Queues a deep-dive for the analyst, with an optional `focus` (the owner's question). One at a time per ticker, at most 6 a day. |
| `record_verdict` | A | Closes an event's analysis: `intact`, `strengthened`, `weakened`, or `broken`. |
| `withdraw_proposal` | D, A | Withdraws a pending proposal that the desk made. |

**Proposal tools (you tap):**

| Tool | Role | Input |
|---|---|---|
| `propose_trade` | D, A | `tickers`, `origin` (an event or your message), `fraction` (0.25, 0.5, or 1.0, for a trade you ask for), `rationale`, `evidence` |
| `propose_change` | D, A | `change` (one of the seven kinds), `rationale`, `evidence` |

No tool approves, executes, places, or cancels an order. No tool edits a file, runs a shell
command, or changes code. A test (`tests/test_desk_tools_static.py`) parses
`lib/desk/tools.py` and enforces this.

The desk proposes a trade on its own only when an event shows a change. When you ask for a
trade, the desk passes your message id as the origin, and the card quotes your message word
for word.

## Taps and cards

### The cards

The outbox sends each proposal as a card in plain prose: the exact orders, the dollars, the
effect on the book, the reason, and the evidence. Example:

> Proposal #41. Buy $412 of CEG, about 1.6 shares at $257.03. That takes CEG from 7.4% of
> the book to its 9.0% target. Why: the Q3 10-Q raised data-center PPA backlog and the price
> is under the agreed buy-below of $260. Evidence: finding #212 (10-Q MD&A), reverse DCF now
> implies 8.1% FCF growth against 14% in the thesis. Open until 4:00 PM ET.

| Card | Buttons |
|---|---|
| Trade | **Approve**, **Reject** |
| Change | **Confirm**, **Reject** |

The seven change kinds:

| Kind | What it changes |
|---|---|
| `add_company` | Adds a name to the book with a target weight and an entry rule. |
| `drop_company` | Sets a name's target to 0 and its status to `exiting`. |
| `set_weights` | Changes target weights and bands. |
| `set_risk` | Changes one setting in `desk_settings` (for example `requote_tolerance_pct`). |
| `set_premise` | Writes a new premise version and the goal. |
| `set_thesis` | Writes a new thesis version and its buy-below and trim-above levels. |
| `resume_trading` | Lifts today's daily-loss halt. It never touches the `KILL` file. |

A confirmed change that implies a trade sends a second card with its own **Approve**
button. A Confirm never approves a trade.

### What happens on a tap

The bridge handles a tap at once, in its own task. It does not wait behind a running turn.
`lib/desk/approvals.handle_tap` runs these checks in order. The first failure answers the
tap and stops:

1. The button data parses. Your user id is on the owner list. The chat, the message, and
   the nonce match the proposal.
2. The proposal is `pending`. Otherwise the answer gives its current state.
3. The proposal has not expired. A proposal expires at the close of the current session.
4. For **Approve** on a trade: no `KILL` file, today not halted, the market open, and at
   least 5 minutes before the close. Otherwise the proposal stays open and the answer gives
   the reason.
5. One conditional `UPDATE` claims the proposal. A second tap gets "Already approved".

Then the bridge replaces the buttons with the result: "Approved", "Rejected", "Expired",
"Price moved", or "Replaced by #N".

On **Approve**, the desk answers "On it. Checking the price now." The broker runner
re-quotes each leg. If a price moved more than `requote_tolerance_pct` (1.5% to start), the
desk sends new numbers with new buttons and places nothing. Otherwise the broker places the
checked orders and the desk tells you the fills. If the broker has not started 3 minutes
after your tap, the desk tells you: "The broker has not started #41 yet."

**An old tap does not act.** If the bridge was offline for more than 60 s before a batch of
updates, each tap in that batch changes nothing. The bridge answers "I was offline when you
tapped. Tap again." and keeps the buttons.

**A "yes" in chat is not an approval.** Only the button approves.

## The research memory

The ledger stores everything the desk reads or says:

- every filing section, earnings release, transcript, news article, and fetched web page
  (`documents`);
- every finding, with its exact quote and the id of its source document (`findings`);
- every note, for example an owner preference (`notes`);
- every chat message, in both directions (`chat_messages`);
- every thesis version, premise version, event, verdict, and proposal.

Four SQLite FTS5 tables index the text: `documents_fts`, `findings_fts`, `notes_fts`, and
`chat_messages_fts`. The nightly ledger backup covers all of them.

**`search_research`** searches them. Its input is `query`, and optionally `ticker`, `kinds`
(`document`, `finding`, `note`, `chat`), `since`, and `limit` (at most 20). It returns BM25
hits with a snippet, the source, the date, and the id. The desk runs it before it says it
does not know. So you can ask "what did ANET say about AI back-end networks last quarter?"
or "why did we add GEV?" in any later conversation.

**Exact quotes.** `record_finding` stores a fact only if its quote appears in a stored
document. Python checks the quote. Each number the desk states names its source: a filing
and section, a finding id, or a metric.

## The walls

The chat runs a model over untrusted text (filings, news, web pages). These walls keep it
off the order path. [README.md](../README.md#the-nine-walls) lists all nine.

1. **No broker.** The unit hides `state/claude-config` (the Robinhood OAuth) and
   `/etc/quiver` at the kernel level (`InaccessiblePaths=`). The desk uses its own config
   dir, `state/claude-desk`, with no MCP server registered. The SDK options set
   `strict_mcp_config=True` and `setting_sources=[]`.
2. **A fixed tool set.** The session has only the four built-in tools and the desk's typed
   tools. `Bash`, `Write`, `Edit`, `NotebookEdit`, `WebFetch`, and `Task` do not exist in it.
   A `PreToolUse` hook allows a call only for a tool on the role's allowlist. It confines
   file reads to the repo by realpath and denies the secret paths
   (`lib/desk/guard.py`). If the hook fails, it denies.
3. **No secrets in the model process.** At start, the bridge reads its secrets into a
   `Secrets` object. Then `scrub_process_env()` deletes every `os.environ` key outside a
   short allowlist, before the first SDK client starts. No tool returns a secret.
4. **Approval only by a tap.** `approvals.handle_tap` is the only code that approves. No
   tool reaches it.
5. **Owner allowlist by user id.** A message or a tap counts only from an owner `from.id`,
   in a private chat with that id.
6. **SSRF-safe fetch.** `fetch_document` refuses private and metadata addresses and pins
   the checked address.

The chat unit writes the ledger, so `ReadWritePaths=` includes `/opt/quiver/state`. The
kernel does not make the chat process read-only. The tool set keeps the desk off the order
path. The chat unit cannot write `/var/log/quiver`, so it cannot forge the `tick.log` lines
that the alerts read.

**Remaining risks:**

- A person who holds your Telegram session can chat with the desk and tap Approve. Turn on
  Telegram two-step verification (Settings → Privacy and Security → Two-Step Verification).
- Telegram bot chats are not end-to-end encrypted. Positions and P&L pass through Telegram,
  and the ledger stores the chat.
- A prompt injection in a filing, an article, or a web page can make the desk propose
  something. It cannot execute anything: you see every proposal and tap it yourself.

## One-time setup

### 1. Create the bot and get your chat id

1. In Telegram, send `/newbot` to **@BotFather**. Pick a name and a username. BotFather
   gives you a bot token like `123456789:AA...`. Keep it secret.
2. Send any message to your new bot.
3. Get your numeric chat id. Message **@userinfobot**, which shows your id. Or, when the
   bridge runs with another id on the list, message the bot: it answers "Not authorized.
   Your chat id is NNN." The bridge does not start when the list is empty.

### 2. Put the values in `.env`

```
TELEGRAM_BOT_TOKEN="123456789:AA..."
TELEGRAM_ALLOWED_CHAT_IDS="NNNNNNNNN"     # comma-separated for more than one
```

Both are required. `setup.sh` stops without them. The same bot sends the outbox messages,
the close brief, and every page. `TELEGRAM_ALERT_CHAT_IDS` (optional) sends the pages to
other chat ids.

### 3. Push them and deploy

Follow [docs/DEPLOY.md](DEPLOY.md): `./deploy/gcp/bootstrap-gcp.sh` pushes the secrets,
Terraform grants the box read access (`locals.secrets` lists both keys), and `setup.sh`
writes `/etc/quiver/quiver.env` and starts `quiver-chat.service`. To change a value on a
running box, use the `quiver.env` refresh in docs/DEPLOY.md, then
`sudo systemctl restart quiver-chat.service`.

## Operating it

```bash
journalctl -u quiver-chat.service -f              # the bridge output
tail -f /opt/quiver/logs/desk/bridge.log          # one JSON line per event
sudo systemctl restart quiver-chat.service        # after a quiver.env change
```

Read the recent chat from the ledger:

```bash
sudo -u quiver sqlite3 -readonly /opt/quiver/state/ledger.db \
  "SELECT role,source,reaction,substr(text,1,60) FROM chat_messages ORDER BY id DESC LIMIT 6;"
```

Do not stop the chat for long. It is the only path for taps. Taps that arrive while it is
down act as old taps when it starts (see "What happens on a tap").
