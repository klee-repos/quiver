# OpenMuse research: is it a base for chat-first Quiver?

Date: 2026-10-27. Method: GitHub API metadata, and a shallow clone (depth 50) of
`https://github.com/CopilotKit/openmuse` at commit `a4944f4` (2026-10-26), read only. I did
not run any OpenMuse code. Paths below are relative to the repo root. Local clone:
`/private/tmp/claude-501/-Users-kvnlee-dev-quiver/b1816101-e244-433e-93d8-4e8a1539970a/scratchpad/openmuse_src/repo`.

---

## 1. What OpenMuse is

**Purpose.** OpenMuse is a single-owner, self-hosted personal-agent APPLICATION. You chat with
it on a phone or the web. It can read Gmail and Calendar, browse public pages in a persistent
Chromium, run commands in an optional Docker Linux box, fill PDF forms, and import a finance CSV.
It hands long jobs to a durable server task worker. External writes (send email, change a
calendar event) wait for a stored human review. README: "A personal agent with a browser,
terminal, files, and work that keeps going." It is a **reference app / clone-and-customize
template** ("Clone this template and customize it however you want", README). It is NOT a
general agent framework. The framework layers it sits on are CopilotKit runtime + AG-UI +
TanStack AI.

| Fact | Value | Evidence |
|---|---|---|
| License | MIT (app code). CopilotKit Intelligence is a separate, non-MIT hosted service that the app REQUIRES. | `LICENSE`; README "Intelligence is a separate service and is not included in this repository's MIT license" |
| Created | 2026-09-15 | GitHub API `created_at` |
| Last push | 2026-10-27 (commits on 10-25 and 10-26) | API `pushed_at`; `git log` |
| Stars / forks | 5,699 / 830 | API |
| Open issues | 49 | API |
| Releases / tags | None. `package.json` version `0.1.0`; `CHANGELOG.md` "0.1.0-alpha — 2026-09-15" | `gh api .../releases`, `.../tags` (both empty) |
| Maturity | Self-described "Alpha, for self-hosting and building on" | README banner; `ROADMAP.md` |
| Maintainer | CopilotKit org. Top committer `jerelvelarde` (39); many community PRs | `gh api .../contributors` |
| Size | ~11.3k lines of server TS (`apps/server/src`, `packages/*`), plus a large Expo app (`apps/mobile`), ~60 test files | `wc -l` |
| Language | TypeScript, Node | API `language`; `package.json` |

Confidence: **high** (all from the API and source).

---

## 2. Architecture

### 2.1 Runtime and stack (`package.json`, `apps/server/src/*`)

- Node + TypeScript. HTTP server: **Hono** (`apps/server/src/app.ts`, `index.ts`).
- Agent protocol: **AG-UI** (`@ag-ui/core`, `@ag-ui/client` 0.0.59). An agent is an AG-UI
  `AbstractAgent` whose `run(RunAgentInput)` returns an RxJS `Observable<BaseEvent>`
  (`RUN_STARTED`, `TEXT_MESSAGE_*`, `TOOL_CALL_START/ARGS/END/RESULT`, `RUN_FINISHED`,
  `RUN_ERROR`). See `apps/server/src/engine/conversation.ts` lines 29-44.
- Host runtime: **`@copilotkit/runtime/v2` 1.70.1** — `CopilotRuntime` + `createCopilotHonoHandler`
  at `/api/copilotkit` (`apps/server/src/agent.ts`).
- LLM loop: CopilotKit `BuiltInAgent` in **"tanstack" factory mode**, which calls
  **TanStack AI** `chat()` with provider adapters `@tanstack/ai-openai|anthropic|gemini`
  (`apps/server/src/engine/tanstack-agent.ts`). The loop stops on `maxIterations(maxSteps)` or a
  `shouldContinue()` callback.
- NOT LangGraph, NOT Mastra. An optional `AGENT_BACKEND=agui` swaps the chat agent for any
  remote AG-UI endpoint via `HttpAgent` (`agent.ts` lines 46-50). An `OpenBot` adapter exists
  but is disabled (`packages/backends/src/openbot.ts`, `docs/OPENBOT-INTEGRATION.md`).
- Model string `provider/model`. OpenAI-compatible gateways: `OPENAI_BASE_URL` plus
  `OPENAI_CHAT_COMPLETIONS=true` (comment names DeepSeek). So OpenRouter/GLM is plausible as
  `MODEL=openai/z-ai/glm-5.2`, but I did not test it (`tanstack-agent.ts` lines 25-44,
  `.env.example`).
- Store: **PGlite** (embedded Postgres compiled to WASM) by default, or real Postgres through
  `DATABASE_URL` (`apps/server/src/db.ts`).
- Web search: Parallel's keyless Search MCP via `@modelcontextprotocol/sdk` client
  (`apps/server/src/search.ts`).
- Browser: separate Playwright/Chromium service (`apps/worker`), token-protected HTTP.

### 2.2 The agent loops (two tiers, no sub-agent graph)

1. **Chat agent** — `ConversationAgent` (`engine/conversation.ts`). There is one per request.
   Its owner comes from the bearer token. Tools: `search_mail`, `read_mail_thread`,
   `read_calendar`, `search_web`, `browse_web`, computer tools, `present_choices`,
   `delegate_task`, `agent_status`, `create_goal`, `watch_page`, `remember_fact`. The limit is
   **6 steps per reply** (16 with an E2B desktop). At the limit it appends "Say 'continue'"
   (`reportStepLimit`). The system prompt says: "Turn other requested jobs into durable
   delegated work using delegate_task … Approvals happen in the native app, never through
   chat tool arguments."
2. **Durable task worker** — `TaskWorker` (`engine/worker.ts`) polls every 1 s. It claims up to
   **3 concurrent tasks** with a SQL compare-and-swap **lease** (60 s, heartbeat every 20 s).
   It recovers expired leases. Task kinds: `agent | document | monitor | finance | plan`
   (`packages/domain/src/agent.ts`). The `agent` kind runs a SECOND model loop,
   `executeModelTask` (`engine/model.ts`): 16 steps, a 5-minute timeout, and its own tool set
   (`set_plan`, `read_workspace`, `read_mail_thread`, `import_pdf`, `fill_pdf`, `search_web`,
   `read_web`, `save_artifact`, `prepare_email`, `prepare_event`, `ask_user`, `finish_task`).
   `document`, `monitor` and `finance` are deterministic code paths, not LLM paths
   (`engine/service.ts` `execute()`).

"Multi-agent" here means only chat → delegate → worker. There is no planner/critic graph and no
nested sub-agents.

### 2.3 Tool definition and calling

- Tools are `defineTool({ name, description, parameters: zodSchema, execute })` from
  `@copilotkit/runtime/v2`. `tanstackAgent` maps them to TanStack `toolDefinition(...).server(fn)`
  (`tanstack-agent.ts` lines 143-153). The registry is a plain array, built PER RUN, with tools
  enabled by config flags (for example `browse_web` only when a browser worker is set).
- Each tool returns `{ error }` instead of throwing. Most check an `AbortSignal` first.
- In the task loop, tool calls are **serialized** (`serial()` queue) because "durable task
  checkpoints must stay ordered" (`model.ts` lines 46-56). Side-effecting tools go through
  `cached(name, args)`. That function keys the result by `sha256(name+args)` into
  `task.state.operations`, so a re-run after a crash or resume replays the stored result and
  does not repeat the effect (`model.ts` lines 86-96).
- Tool results that hold images are cut to text before they enter the thread
  (`textOnlyToolResult`).

### 2.4 Human-in-the-loop: the exact mechanism

OpenMuse does **NOT** use CopilotKit's HITL hooks (`useHumanInTheLoop`, `useInterrupt`). A grep
of `apps/`, `packages/` and `tests/` finds none. Those hooks are React-side and pause a live run
(CopilotKit docs: https://docs.copilotkit.ai/built-in-agent/human-in-the-loop/headless).
OpenMuse instead uses a **server-side, durable, hash-bound "stored action review"**:

1. The model calls `prepare_email` / `prepare_event`. Both go to `AgentService.prepare` and then
   `ActionService.propose` (`apps/server/src/actions.ts`). There is **no tool that approves**
   ("there is no tool to approve them", `model.ts` system prompt).
2. `propose` validates the payload with a zod discriminated union (`proposalSchema`:
   `email.send | calendar.create | calendar.update | calendar.delete`,
   `packages/domain/src/index.ts`). It strips invisible Unicode from the text that the human
   will see. It binds the proposal to the current connection/account. It computes
   `hash = sha256({input, connection, target, targetVersion})`. It stores an
   `ActionProposal{status:"awaiting_review", expiresAt: now+30min}`. The ID is
   `sha256(idempotencyKey)`, where the key is `taskId:sha256(payload)`, so a re-run returns the
   same proposal.
3. The task sets `outcome = waiting_approval`. `shouldContinue()` returns false, so the LLM loop
   stops. The task row records `actionId` and the run ENDS. No process waits for the human.
4. The human approves in the app: `POST /api/actions/:id/decide {hash, decision}` (`app.ts`
   lines 191-198, client in `apps/mobile/src/details.tsx` `ReviewDetail`). `decide` rejects the
   call on a hash mismatch ("This proposal changed"), on expiry, on a cancelled task, or on a
   changed account. It then makes an **atomic SQL claim** (`Store.claim`:
   `UPDATE … WHERE status='awaiting_review' AND expiresAt>now AND task in (running,
   waiting_approval)`). **The API process executes the exact stored payload**, not the model.
   The result is `succeeded | failed | outcome_unknown`. "Edit" means deny, then reopen as a draft.
5. On the next tick the worker picks up the `waiting_approval` task again (`worker.ts` lines
   88-111). If the action expired, the worker marks it expired. If it succeeded, the result goes
   into `task.state.approvalResult`, and a NEW model run continues with the instruction "When an
   approved result is in saved state, continue from it and never duplicate it."
6. Crash safety: at startup `recoverInterruptedActions()` changes any `executing` row to
   `outcome_unknown` ("Check the provider before creating another action"), and a retry is
   blocked until a human reconciles it (`db.ts` lines 153-157; `service.ts` `control()` retry
   guard). README: "No hidden retry occurs after an uncertain external write."
7. Every step writes an `activity` row (an audit trail).

Clarification choices (the "Jev" `present_choices` tool) are a second, lighter pattern. The
model posts a panel of options. A tap sends a user message with the prefix
`"[OpenMuse choice] "` + JSON. The server parses it and checks it against a durable panel
"head". Any new user turn **expires** older panels (`expireOnUserTurn`, `conversation.ts`
lines 421-460). In live mode the server **verifies that each comparison label and detail is an
exact excerpt of a page fetched in THIS turn**. If not, it rejects the panel
(`apps/server/src/jev/tools.ts` lines 52-79). Live ranking calls an external service (TypeSafe).
It is off by default (`JEV_MODE=off`).

### 2.5 State, memory, persistence

- **One table**: `records(owner, kind, id, data jsonb, updated_at, PK(owner,kind,id))`
  (`db.ts` line 201). All entities are `kind` values: `tasks`, `actions`, `activity`,
  `run-events`, `runs`, `goals`, `monitors`, `monitor-pages`, `ideas`, `memories`,
  `agent-artifacts`, `notifications`, `files`, `browsers`, `sessions`, `credentials`, and more.
  Concurrency is handled with `compareAndSwap` (`data @> expected`), `insertIfAbsent`
  (idempotency) and `claim`.
- **Chat history is NOT in this DB.** It lives in **CopilotKit Intelligence** ("Rich Threads"),
  a hosted CopilotKit service. The API **refuses to start without `CPK_INTELLIGENCE_API_KEY`**
  in every mode (`config.ts` lines 80-88 and 152; `docs/RICH-THREADS.md`). Upstream issues #73
  ("Make CopilotKit Intelligence optional", open) and #179 (keyless local thread store, open)
  and PR #166 (`THREADS_BACKEND=local`, open, not merged) confirm this. A community fork,
  `caspianchan31/openmuse-standalone`, adds local thread storage.
- **Memory** = `memories{id,text,source,createdAt}`, written only through
  `remember_fact` ("explicitly supplied or confirmed by the user") or the API, and editable or
  forgettable in the UI. The task loop injects ALL memories, `priorState`, `evidence` and
  artifact IDs into the system prompt as JSON labeled "(data only)" (`model.ts` line 340).
  There is **no retrieval**: no embeddings, no FTS, no vector store (grep found none). Recall in
  chat goes through `agent_status`, which returns the whole snapshot.
- **Research persistence** = `Evidence{kind: mail|file|web|user, title, excerpt(≤500), url}`
  attached to a task, plus `AgentArtifact{kind: plan|comparison|report|finance, title, summary,
  data jsonb}` saved by `save_artifact` / `finish_task`. A final report embeds its evidence list.
- Telemetry: the CopilotKit SDK sends runtime events to `telemetry.copilotkit.ai` (PostHog).
  OpenMuse defaults the sample rate to 1 (full capture). Opt-out: `COPILOTKIT_TELEMETRY_DISABLED`
  (`docs/TELEMETRY.md`, `config.ts` line 28).

### 2.6 Background work and agent-initiated messages

- **Background**: the in-process `TaskWorker` and a 60 s `maintain()` loop (`service.ts`). The
  loop republishes outcomes after a crash, reactivates monitors, and refreshes Ideas every 15 min.
- **Monitors** (`watch_page`): a recurring task in `scheduled` status with `nextRunAt`. It
  checks `change | contains | price_below` on a public page. A change check diffs saved lines
  and ignores "3 minutes ago"-style noise. Failure backoff is `min(60, 2^n)` minutes, and the
  watch auto-pauses after 5 failures. Alerts are deduped by
  `monitor:{id}:{alertSequence}:{hash}` (`service.ts` `observe()`).
- **Ideas** = agent-initiated proposals. A RULES pass (regex over mail, goals without
  milestones) creates `Idea{title, reason, evidence[], prompt, kind, input, status:new}` with a
  content-hash ID. The owner can accept, edit or dismiss. Accept creates a task (idempotent).
  The pass retires obsolete ideas (`service.ts` `refreshIdeas`, `decideIdea`). The suggestions
  are not model-generated ("Rules-based suggestions", `docs/VERIFICATION.md`).
- **Proactive messages are NOT chat messages.** The server writes `notifications` rows
  (deduped by key: `task-done:`, `review:{actionId}`, `input:` …, `publishOutcome`). The client
  **polls `/api/agent` every 3 s** while visible (`apps/mobile/src/agent-workspace.tsx` lines
  49-60). The client shows a banner (`background-updates.tsx`). Nothing is posted into the chat
  thread. Device push is roadmap (`ROADMAP.md`).
- **Ask-user**: the task's `ask_user` tool sets `waiting_input` with a question. The answer
  arrives through `POST /api/agent/tasks/:id/input`. All Q&A pairs are kept in
  `state.answers` and replayed into the next fresh run (`service.ts` `answer()`,
  `model.ts` `taskAnswers`).

Confidence: **high**. I read the code paths directly. I did not exercise them live.

---

## 3. UI and channel assumptions; driving it from Telegram

- The shipped client is **Expo / React Native 0.81 / React 19** for iOS, Android and web
  (`apps/mobile`), using `@copilotkit/react-native/headless` hooks: `useAgent`, `useThreads`,
  `useRenderTool`, `useAgentContext` (`apps/mobile/src/chat.tsx`, `threads.tsx`). The UI is
  rendered through **"generative UI"** = `useRenderTool({name, render})`. The client maps each
  tool name (`search_mail`, `browse_web`, `delegate_task`, `present_choices`, …) to a native
  card that renders the tool's JSON result. The server stays presentation-free.
- The server also needs inbound HTTP: a REST API, `/api/copilotkit` SSE, a Google OAuth
  callback, and signed file/console URLs (`app.ts`).
- **Can the core run without the web UI?** Yes, partly. `ConversationAgent` is a plain AG-UI
  agent: build a `RunAgentInput{threadId, runId, messages, tools:[], context:[], state:{}}` and
  subscribe to its event Observable. A Telegram adapter can do this in process, with no
  CopilotKit runtime, which also bypasses the Intelligence requirement. The task worker, the
  actions and the notifications are plain server code with no UI dependency.
- **What you lose or must rebuild for Telegram:**
  - Thread persistence/replay (Intelligence). You must store the transcript yourself and pass
    `messages` on each run. Quiver's `deploy/runner/chat_history.py` already does this.
  - Generative-UI cards. Map them to Telegram text plus **inline keyboards** (choices, approve /
    deny).
  - The review screen ("One last look", exact recipient/payload). Replace it with a Telegram
    message that shows the exact order and approve/deny callback buttons carrying
    `(id, hash)`. Telegram callback data is limited to 64 bytes, so send a short ID and look
    up the hash on the server.
  - "Take control" of the browser, the PDF viewer, Files/Terminal panes. These are not needed
    for Quiver.
  - Streaming token-by-token. Telegram can edit a message, but in practice you send the final
    reply.
  - Polling notifications become PUSH. Telegram `sendMessage` from the worker is an upgrade
    over OpenMuse, which has no push.

Confidence: **high** on what the UI depends on. **Medium** on the Telegram adapter effort,
because I did not build it.

---

## 4. Deployment needs vs. the GCP e2-small (2 GiB, IAP-only, outbound-only)

| Need | Evidence | Fit on the Quiver box |
|---|---|---|
| Node 24 LTS (CI uses 24; `engines >=22`), pnpm 11.19.0 | README, `.github/workflows/ci.yml`, `package.json` | OK to install, but it is a second Node toolchain next to the EVE brain's |
| API RAM > 512 MB. "At 512 MB the process runs out of memory before it binds a port, because PGlite loads an embedded Postgres build." The Render plan used is Standard = 2 GB / 1 CPU | README "Deploy on Render", `render.yaml` comment; Render pricing https://render.com/pricing.md | **Bad fit.** The box is 2 GiB TOTAL and already runs Quiver (Python + Node EVE + `claude` CLI + chat bridge + intel). |
| Chromium browser worker, compose `mem_limit: 2g`, `shm_size 256mb` | `infra/compose.yaml` | **Does not fit**, and Quiver does not need it |
| Optional Docker Linux computer | `docs/COMPUTER.md` | Not needed |
| CopilotKit Intelligence key (hosted; outbound) | `config.ts` | Hard dependency upstream. It sends chat threads (here: portfolio talk) to a third party |
| Inbound HTTP for the client and the OAuth callback; `HOST=0.0.0.0` only in live mode | `config.ts` lines 174-182, README | **Conflicts with no-public-inbound.** It works only if Telegram (outbound long-poll) replaces the client |
| Telemetry to `telemetry.copilotkit.ai`, full capture by default | `docs/TELEMETRY.md` | Must opt out |
| Search queries to Parallel (default ON) | README "Public web search" | Must turn it off or accept the leak |

Conclusion: running OpenMuse itself on the e2-small next to Quiver is not realistic. The
Postgres-in-WASM floor plus Chromium alone exceed the headroom. A pure-Python port of the
PATTERNS into Quiver costs almost no RAM.

Confidence: **medium-high**. The memory floor comes from the repo's own notes. I did not
measure it.

---

## 5. Patterns worth copying into Quiver, and parts that do not fit

### Copy (map onto Quiver's existing Python + SQLite + Telegram + Claude Code stack)

1. **Stored, hash-bound, expiring proposal plus an atomic claim, executed by the server and
   not by the model** (`actions.ts`, `db.ts` `claim`). This is the core of "propose, human
   confirms, execute". Quiver mapping: `plan` (Python) emits a `proposal` row = exact order
   (ticker/side/qty/limit, `ref_id` RESERVED as today), `hash=sha256(order+account+quote_ts)`,
   `expires_at` (short: quotes go stale), status `awaiting_review`. Telegram inline button →
   `tick.py approve --id --hash` → atomic `UPDATE … WHERE status='awaiting_review' AND
   expires_at>now` → the orchestrator places EXACTLY that `ref_id`. Extend
   `deploy/runner/order_guard.py` (which already denies unreserved `ref_id`s) so that it also
   denies any `ref_id` whose proposal is not `approved`. This keeps the Python-wall rule: the
   approver is a human, and the executor is still the deterministic path.
2. **No approve tool exposed to the LLM.** The chat agent can only *propose*. Approval arrives
   on a separate channel (a Telegram callback query from an allowlisted chat ID, never parsed
   from free chat text).
3. **`outcome_unknown` plus no hidden retry** after a crash mid-execute. Quiver already has this
   with `ref_id` reconcile. Keep the same vocabulary in the proposal lifecycle.
4. **Re-validation at approve time**: OpenMuse re-checks the hash, expiry, task state and
   account at `decide`. Quiver should re-run the guardrails (halt flag, KILL, market open,
   buying power, a fresh quote within X% of the proposal price) at approve time, not only at
   propose time.
5. **Durable task + lease + checkpoint + `waiting_input`/`waiting_approval` + resume as a fresh
   run with saved state** (`worker.ts`, `model.ts`). Long research jobs (read a 10-K, then the
   10-Qs, then the 8-Ks) should be tasks that survive restarts. A run does not wait for the
   human; it stops and resumes.
6. **Idempotent side-effect cache** (`cached(name,args)` into `state.operations`) and
   content-hash IDs everywhere (`insertIfAbsent`). This is cheap and fits SQLite.
7. **Evidence model**: every claim carries `{kind, title, excerpt, url}`. For SEC research,
   extend it to `{form, accession_no, filed_at, section/item, excerpt, url}`.
8. **Grounding check** (`jev/tools.ts`): reject a structured output unless its quoted
   details are exact excerpts of a source fetched in THIS run. Apply it to research cards: a
   revenue, guidance or risk-factor quote must appear verbatim in the fetched filing text.
9. **Ideas = agent-initiated proposals with evidence, accept/edit/dismiss, idempotent, and
   auto-retired when obsolete** (`refreshIdeas`/`decideIdea`). This is the natural shape of
   "monitor the portfolio and PROPOSE a buy/sell", including dedup so the same proposal does
   not page twice.
10. **Monitor semantics**: interval, condition, a baseline then a change, backoff, auto-pause
    after N failures, and an alert key = `id:sequence:hash`. Quiver's tick already does most of
    this. Copy the dedup key and the backoff shape for filing or price watches.
11. **Notification dedup key per event** (`task-done:{id}`, `review:{actionId}`). Quiver's
    digest `content_hash` is the same idea. Push through Telegram.
12. **Stale-choice expiry**: a new user turn invalidates earlier inline-keyboard choices. This
    matters on Telegram, where old buttons stay tappable forever.
13. **Explicit memory with provenance** (`memories{text, source}`, written only on user
    confirmation, editable/forgettable). Good for "agreed portfolio" and preferences.
14. **Step-limit honesty** (`reportStepLimit`: say "continue" instead of a silent stop) and
    "never claim completion before server status confirms".

### Do not copy

- The **Expo/React Native client, `useRenderTool` cards, browser takeover console, Docker
  computer, Gmail/Calendar/PDF/finance-CSV features.** These have no role in a Telegram-first
  trading partner.
- **CopilotKit Intelligence** as the thread store. It is a hosted, non-MIT, required dependency
  that sends conversation content off-box. It also adds default-on telemetry and default-on
  Parallel search egress.
- **PGlite / the single JSONB `records` table.** Quiver's SQLite ledger with typed tables is
  stronger for money. Copy the CAS/claim idioms, not the store.
- **The TypeScript runtime itself.** Quiver already HAS an agent framework: headless Claude Code
  (`claude -p`) as orchestrator, MCP tools, PreToolUse guards (`order_guard.py`,
  `chat_guard.py`), a Telegram long-poll bridge (`deploy/runner/chat_bridge.py`) and a bounded
  transcript (`chat_history.py`). Adopting OpenMuse code would add a second agent runtime and a
  second LLM SDK (TanStack AI) next to the EVE brain.
- **OpenMuse's memory/recall as the research store.** It has no retrieval at all (it dumps the
  full snapshot into the prompt). "Retrievable research in later conversation" needs something
  OpenMuse lacks, for example SQLite FTS5 over saved filing excerpts/artifacts keyed by ticker
  and form.

### Side observation (not OpenMuse, but relevant)

The Robinhood MCP tool list in this session includes `get_trade_approval_setting`,
`get_trade_approvals` and `decline_trade_approval`. This suggests Robinhood offers its own
trade-approval flow, and it may complement or conflict with an in-house Telegram approval. The
same tool list also has `get_sec_filing`, `get_sec_filing_index`, `get_sec_filing_facts`,
`get_earnings_results` and `get_financials`, which are candidate primary-document sources. I did
not call or verify any of these tools.

---

## Sources read

- API: `https://api.github.com/repos/CopilotKit/openmuse` (+ `/releases`, `/tags`,
  `/contributors`, `/issues`, `/issues/73`, `/issues/179`, `/issues/166`)
- Repo files: `README.md`, `ROADMAP.md`, `CHANGELOG.md`, `.env.example`, `package.json`,
  `pnpm-workspace.yaml`, `render.yaml`, `infra/compose.yaml`, `.github/workflows/ci.yml`,
  `docs/{README,FEATURES,EXPERIENCE,RICH-THREADS,VERIFICATION,TELEMETRY,OPENBOT-INTEGRATION}.md`,
  `docs/demos/jev-generative-ui.md`, `apps/server/src/{agent,index,worker-entry,app,config,db,actions,auth,search}.ts`,
  `apps/server/src/engine/{conversation,tanstack-agent,model,worker,service,routes}.ts`,
  `apps/server/src/jev/tools.ts`, `packages/domain/src/{index,agent,jev}.ts`,
  `apps/mobile/src/{chat,agent-workspace,background-updates,details}.tsx`, `apps/worker/README.md`,
  `apps/worker/Dockerfile`
- CopilotKit HITL docs: https://docs.copilotkit.ai/built-in-agent/human-in-the-loop/headless
- AG-UI protocol repo (MIT): https://github.com/ag-ui-protocol/ag-ui
- Render plan specs: https://render.com/pricing.md
- Quiver (read-only, for mapping): `deploy/runner/chat_bridge.py`, `deploy/runner/order_guard.py`, `docs/CHAT.md` headings
