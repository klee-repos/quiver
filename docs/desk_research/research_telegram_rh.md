# Research: Telegram Bot API patterns + Robinhood trade approvals (for the chat-first Quiver redesign)

Date of research: 2026-10-27. Current Bot API version at fetch time: **Bot API 10.3 (2026-08-24)**.
Raw copies of the fetched pages are in `.../scratchpad/web/` (botapi.txt, changelog.txt, faq.txt, rh/*.txt, oc/*.md).

Legend: **[VERIFIED]** = read in an official doc I fetched. **[PRACTICE]** = seen in a credible third-party implementation or report, not in the official doc. **[UNVERIFIED]** = I could not confirm it. Test it live before you rely on it.

## Sources fetched

Official Telegram:
- Bot API reference: https://core.telegram.org/bots/api
- Bot API changelog: https://core.telegram.org/bots/api-changelog
- Bots FAQ (rate limits): https://core.telegram.org/bots/faq
- Bots intro (intermediary server, encryption): https://core.telegram.org/bots
- Bot features (inline keyboards, privacy mode): https://core.telegram.org/bots/features
- MTProto `messages.sendReaction` (error codes): https://core.telegram.org/method/messages.sendReaction
- MTProto reactions overview: https://core.telegram.org/api/reactions
- Telegram FAQ (cloud chats vs secret chats, 2-Step Verification, sessions): https://telegram.org/faq

Official Robinhood:
- Trading with your agent (trade approvals section): https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- Setting up an agent (built-in agents): https://robinhood.com/us/en/support/articles/setting-up-an-agent/
- Robinhood Agents overview: https://robinhood.com/us/en/support/articles/robinhood-agents-overview/
- Agentic trading overview (MCP, what the agent can access): https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Newsroom, launch (2026-05-27): https://robinhood.com/newsroom/robinhood-is-now-open-to-agents/
- Newsroom, HOOD Summit (2026-09-29): https://robinhood.com/newsroom/hood-summit-2026/
- The Robinhood MCP tool descriptions themselves (`get_trade_approval_setting`, `get_trade_approvals`, `decline_trade_approval`, `place_equity_order`, `review_equity_order`), read from the tool schemas in this session. I did not call any broker tool.

Third-party (prior art / reports):
- grammY reactions guide: https://grammy.dev/guide/reactions
- grammY flood-limit guide: https://grammy.dev/advanced/flood
- OpenClaw Telegram docs (an agent that ships the exact "👀 ack reaction + streamed preview + inline approval buttons" pattern): https://docs.openclaw.ai/channels/telegram/messaging.md , https://docs.openclaw.ai/channels/telegram/rich-messages.md , https://docs.openclaw.ai/channels/telegram/events.md , https://docs.openclaw.ai/tools/reactions.md
- TechCrunch launch article: https://techcrunch.com/2026/05/27/robinhood-now-lets-your-ai-agents-trade-stocks/
- Finder review: https://www.finder.com/stock-trading/robinhood-agentic-accounts
- Vorp Labs guide: https://vorplabs.com/agent-tools/robinhood-cli
- GitHub issue (approval-setting field name + an inconsistency): https://github.com/chasechipres/rh-gex-trader/issues/23
- GitHub PR (another bot gating on approvals): https://github.com/jack-h-park/stock-trading-skills/pull/65

---

## 1. setMessageReaction

**Introduced:** Bot API **7.0**, 2023-12-29. Changelog: "Added the method setMessageReaction that allows bots to react to messages." The same release added `message_reaction` / `message_reaction_count` updates ("The bot must explicitly allow the update to receive it") and `Chat.available_reactions`. [VERIFIED]

**Parameters** (official, current): [VERIFIED]

| Param | Type | Req | Notes |
|---|---|---|---|
| `chat_id` | Integer or String | Yes | target chat |
| `message_id` | Integer | Yes | "If the message belongs to a media group, the reaction is set to the first non-deleted message in the group instead." |
| `reaction` | Array of ReactionType | Optional | "Currently, as non-premium users, bots can set up to one reaction per message. A custom emoji reaction can be used if it is either already present on the message or explicitly allowed by chat administrators. Paid reactions can't be used by bots." |
| `is_big` | Boolean | Optional | "Pass True to set the reaction with a big animation" |

Returns `True`. Description: "Use this method to change the chosen reactions on a message. Service messages of some types can't be reacted to."

`ReactionTypeEmoji` = `{"type":"emoji","emoji":"<one of the allowed list>"}`. [VERIFIED]

**Private chat?** Yes in practice. The method lists no chat-type restriction. [VERIFIED: no restriction stated.] OpenClaw ships a DM ack reaction with default emoji "👀" (`ackReactionScope: direct`). [PRACTICE] `ChatFullInfo.available_reactions`: "If omitted, then all emoji reactions are allowed." A private chat has no admin to restrict this. [VERIFIED]

**Allowed emoji (the full official list, 73 entries, extracted from the HTML):** [VERIFIED]
❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡

- 👀 **allowed** (U+1F440)
- ✍ **allowed**. The doc's exact string is U+270D with **no** U+FE0F variation selector. Send `"✍"`, not `"✍️"`.
- 🤔 **allowed**
- 👍 **allowed**
- ✅ **NOT allowed**. Also not allowed: ❌ ⏳ 📈 📉 🚀 💸 💰 🆗 ✔ ☑ 🤖 🧐 👁 🙌.
- Good "done" replacements: 👌, 👍, 🫡, 💯, 🆒, 🏆, 🎉. Good "failed" replacements: 🤷, 😢, 🤯, 💔.
- The doc stores ❤, ⚡, ☃, 🕊 with no FE0F. Whether the server also accepts the FE0F form is **[UNVERIFIED]**. Copy the codepoints from the doc. The MTProto doc strips FE0F when it hashes reactions, which hints that the server normalizes it, but this does not prove that the Bot API accepts it.

**Replace a reaction (👀 → 👌):** call `setMessageReaction` again with the new single reaction. The method "change[s] the chosen reactions", and a bot holds at most one. So the new call replaces the old one. [VERIFIED by the method semantics; OpenClaw/WhatsApp-style "one slot, replace" is the same.]

**Remove a reaction:** omit `reaction` or pass `[]`. The param is Optional, and the MTProto field is an optional flag. OpenClaw: "Empty `emoji` removes the bot's reactions" on Telegram. [PRACTICE. The Bot API page does not say this in words. Test it once.] `deleteMessageReaction` / `deleteAllMessageReactions` (Bot API 10.0) are for **groups/supergroups only** and need `can_delete_messages`. Do not use them in a private chat. [VERIFIED]

**Failure modes:** MTProto error list for the underlying call: `REACTION_INVALID` (emoji not allowed, e.g. ✅), `REACTION_EMPTY`, `REACTIONS_TOO_MANY`, `MESSAGE_ID_INVALID` / `MSG_ID_INVALID`, `PEER_ID_INVALID`, `CHAT_WRITE_FORBIDDEN` (e.g. the user blocked the bot), `MESSAGE_NOT_MODIFIED` (you set the same reaction twice), `PREMIUM_ACCOUNT_REQUIRED`. [VERIFIED for MTProto. The Bot API wraps these as `400 Bad Request: <reason>`; the exact Bot API wording is UNVERIFIED.] A community report shows "message to react not found" when the wrong `message_id` was used. [PRACTICE]

**Rate limits:** none published for reactions specifically. [UNVERIFIED] grammY: "any API request you perform could error with status code 429". Handle `parameters.retry_after` on every call. [PRACTICE + VERIFIED `ResponseParameters.retry_after` exists.] Treat a reaction as best-effort: on any error, log and continue. Never let it block the reply.

**Receiving the owner's reactions (optional):** `Update.message_reaction`: "The bot must be an administrator in the chat and must explicitly specify "message_reaction" in the list of allowed_updates to receive these updates. The update isn't received for reactions set by bots." [VERIFIED] grammY says "In private chats and group chats, your bot will receive a `message_reaction` update". OpenClaw consumes them in DMs. [PRACTICE] The official "must be an administrator" wording conflicts with that for private chats. **[UNVERIFIED for private chats. Test it.]** The default `allowed_updates` excludes `message_reaction`.

## 2. sendChatAction ("typing") and the newer draft stream

**sendChatAction:** "The status is set for 5 seconds or less (when a message arrives from your bot, Telegram clients clear its typing status)." Params: `chat_id`, optional `message_thread_id`, optional `business_connection_id`, and `action` (`typing`, `upload_photo`, `upload_document`, …). "We only recommend using this method when a response from the bot will take a noticeable amount of time to arrive." [VERIFIED]

**Refresh pattern for long work:** send `typing` again about every **4 s** from a background thread/task until the first reply message goes out. After each `sendMessage` the client clears the status, so send `typing` again before the next chunk if more work follows. Stop on completion or error. Today `chat_bridge.py:_typing` sends it **once**, so the indicator dies after ≤5 s of a multi-minute `claude` call.

**sendMessageDraft (newer and better for a private chat):** added in Bot API **9.3** (2025-12-31). Bot API **9.5** (2026-03-01) "Allowed all bots to use the method sendMessageDraft". Bot API **10.0** "Allowed bots to pass an empty text". [VERIFIED]
- "stream a partial message to a user while the message is being generated … the streamed draft is ephemeral and acts as a temporary **30-second** preview - once the output is finalized, you must call sendMessage with the complete message to persist it."
- `chat_id` is "Unique identifier for the target **private** chat". `draft_id` is a non-zero integer ("Changes to drafts with the same identifier are animated"). `text` is 0-4096 chars: "Pass an empty text to show a “Thinking…” placeholder." `parse_mode`/`entities`.
- Bot API 10.3: `can_stop` ("show the user a button to stop further drafts. The bot will receive an Update “stopped_message_generation”") and `keep_on_stop`.
- Use: a status draft ("checking your positions…", "running the analysis on NVDA…") that you refresh at most every <30 s, then real `sendMessage`s. `can_stop` gives the owner a native "stop" control. [VERIFIED API; UX is my inference.] Rich variants: `sendRichMessage` / `sendRichMessageDraft` (10.1+).
- Draft and typing are complementary. Use typing for short waits and a draft for status during long tool runs. OpenClaw's default since 2026.8 is "one editable status draft during the turn … and sends the final answer as a normal message". [PRACTICE]

## 3. Inline keyboards + callback_query for Approve / Reject

**InlineKeyboardButton** [VERIFIED]:
- `text`, and exactly one action field. For us that is `callback_data`: "Data to be sent in a callback query to the bot when the button is pressed, **1-64 bytes**". This is bytes, not characters, so keep it ASCII.
- `style` (Bot API **9.4**): "Must be one of “danger” (red), “success” (green) or “primary” (blue)". Use `success` for Approve and `danger` for Reject.
- `disabled` (Bot API **10.3**): type `DisabledButton`. "If set, then the button is disabled and does nothing". You can use it to show a greyed "Approved ✓" button after the decision instead of removing the keyboard.
- `InlineKeyboardMarkup.inline_keyboard` is an Array of Array of buttons.

**CallbackQuery** fields: `id`, `from` (User = **the person who pressed**), `message` (MaybeInaccessibleMessage; it can be an `InaccessibleMessage` with `date == 0` if the message was deleted or is inaccessible), `inline_message_id`, `chat_instance`, `data`. Security note in the doc: "**Be aware that the message originated the query can contain no callback buttons with this data.**" A modified client can send any `data` for any bot message. **Never treat `callback_data` as authority. Treat it as a lookup key.** [VERIFIED]

**answerCallbackQuery:** "Telegram clients will display a progress bar until you call answerCallbackQuery. It is, therefore, necessary to react by calling answerCallbackQuery even if no notification to the user is needed." Params: `callback_query_id` (req), `text` (0-200 chars, a toast), `show_alert`, `url`, `cache_time`. [VERIFIED] Answer at once, before the slow work. A late answer gives "query is too old and response timeout expired or query ID is invalid". The exact window is **[UNVERIFIED]**. Treat that error as benign.

**Remove the buttons after a decision:** `editMessageReplyMarkup` with `chat_id` + `message_id` and either no `reply_markup` or `{"inline_keyboard":[]}`. That clears the keyboard. Or send a markup whose buttons have `disabled`. Or call `editMessageText` to rewrite the card ("✅ Approved 14:02 — order sent" — text may contain ✅, only *reactions* are restricted). [VERIFIED methods; "omit reply_markup clears it" is PRACTICE.] The 48-hour edit limit applies only to *business* messages that the bot did not send. The bot's own messages have no stated edit limit. [VERIFIED wording]

**Verify the presser:** check `callback_query.from.id == OWNER_USER_ID` **and** `callback_query.message.chat.id == OWNER_CHAT_ID` **and** `message.chat.type == "private"`. In a private chat, `chat.id` equals the user's id in practice. [PRACTICE; the Bot API does not state it in words.] Today `chat_bridge.py` checks only `chat.id` against `TELEGRAM_ALLOWED_CHAT_IDS`, plus `type == private`. For messages, also check `message.from.id`. Keep a separate `TELEGRAM_OWNER_USER_ID`.

**Replay / staleness** (design rules; the API gives no protection here):
1. `callback_data` = `"t:" + <opaque proposal nonce>` (for example 16-22 base64url chars + a verb byte → well under 64 bytes). Store `{nonce, message_id, chat_id, ticker, side, qty/$, plan_hash, created_at, expires_at, state}` server-side in the ledger.
2. On a press: look up the nonce. Require `state == pending`, `now < expires_at`, a stored `message_id` that matches `callback_query.message.message_id`, and the owner `from.id`. Then do an **atomic compare-and-set** `pending → approved` (single-use, so a double-tap and an update redelivery are both no-ops).
3. Dedup by `update_id` and by `callback_query.id`. Telegram redelivers unconfirmed updates. "An update is considered confirmed as soon as getUpdates is called with an offset higher than its update_id." Updates are kept "not … longer than 24 hours". "If there are no new updates for at least a week, then identifier of the next update will be chosen randomly instead of sequentially." So persist the offset, and dedup with a stored set, not with a `>` check alone. [VERIFIED quotes]
4. **Staleness:** an old card's buttons stay pressable forever unless you edit them. On expiry, edit the card (remove or disable the buttons). On a press after expiry, answer with "expired — ask me again" and do nothing.
5. **TOCTOU:** on approve, re-run the Python `plan` gates (halt, KILL, market open, cash, target room, consistency) with a **fresh quote**. Refuse if the price moved more than X% from the proposal's price or the plan hash changed. The human approves an *intent*. Python still decides whether it is still valid.

**allowed_updates:** "Specify an empty list to receive all update types **except chat_member, message_reaction, and message_reaction_count** (default). **If not specified, the previous setting will be used.** Please note that this parameter doesn't affect updates created before the call." [VERIFIED] So you **must** pass it explicitly: `["message","callback_query"]`, plus `"message_reaction"` if you want the owner's reactions, plus `"stopped_message_generation"` if you use `can_stop` (the `Update` field is `stopped_message_generation`, type `MessageGenerationStopped` [VERIFIED]. The doc does not say whether the default list includes it, so list it explicitly). Today the bridge passes `["message"]`, so **callback queries would never arrive**.

**Single poller:** only one process may long-poll a token. A second `getUpdates` consumer gets HTTP 409 "Conflict: terminated by other getUpdates request". [PRACTICE/UNVERIFIED in official docs; widely observed.] Sending (`sendMessage`, reactions, edits) from several processes is fine. So callbacks for proposals made by the tick must route through the **one** chat-bridge poller (for example, the tick writes a proposal row; the bridge owns the button handling). `getUpdates` "will not work if an outgoing webhook is set up". [VERIFIED]

## 4. Message limits and pacing

- `sendMessage.text`: "**1-4096 characters after entities parsing**". `editMessageText` is the same. Captions are **0-1024**. [VERIFIED] The existing `lib/telegram.py` chunks at 3500, which is fine.
- **Parse modes** [VERIFIED]:
  - **HTML**: supported tags `b/strong, i/em, u/ins, s/strike/del, span class="tg-spoiler"/tg-spoiler, a href, tg-emoji, tg-time, code, pre (+code class="language-x"), blockquote [expandable]`. "All `<`, `>` and `&` symbols that are not a part of a tag or an HTML entity must be replaced with … `&lt;` `&gt;` `&amp;`." Only the named entities `&lt; &gt; &amp; &quot;` are supported (numeric entities are fine). Unknown tags → 400. HTML is the safer choice for LLM text. Escape it with `html.escape(s, quote=False)` and then add your own tags.
  - **MarkdownV2**: outside entities you must escape every one of `_ * [ ] ( ) ~ \` > # + - = | { } . !`. Inside `pre`/`code`, escape `` ` `` and `\`. Inside a link `(...)`, escape `)` and `\`. `__` is greedily underline. **Pitfall:** prices ("$12.50"), percentages ("-3.2%"), and tickers in parentheses all need escapes. An LLM never gets this right. Do not use MarkdownV2 for model output.
  - **Markdown** (legacy) "retained for backward compatibility". Avoid it.
  - Keep the existing per-chunk plain-text fallback on a 400.
- **Pacing:** Bots FAQ: "In a single chat, **avoid sending more than one message per second**. We may allow short bursts that go over this limit, but eventually you'll begin receiving 429 errors." Groups: 20 msg/min. Bulk: ~30 msg/s. [VERIFIED] For "texting like a human", 2-4 short messages spaced ~1-2 s apart (with `typing` between) is well within this. On a 429, sleep `retry_after` and retry. grammY: "Do not ignore 429 errors. (This could lead to a ban.)" [PRACTICE]
- **Threading:** `reply_parameters` (Bot API 7.0 replaced `reply_to_message_id`): `{"message_id": <owner msg id>, "allow_sending_without_reply": true}`. Optional `quote` = an exact substring (0-1024) of the replied message. "The message will fail to send if the quote isn't found". [VERIFIED] Reply-thread only the **first** bubble of a multi-bubble answer to the owner's message. The rest go as plain follow-ups.
- **Progressive updates:** `editMessageText` (1-4096) on the bot's own message. Or use `sendMessageDraft` (§2). Community-reported edit cap ~20 edits/min per group; unofficial. [PRACTICE]
- **Link previews:** `disable_web_page_preview` was **replaced** by `link_preview_options` in Bot API 7.0. The current `sendMessage` doc no longer lists the old param. The live bridge still uses `disable_web_page_preview`, and it still works today (backward compatibility). The current form is `"link_preview_options":{"is_disabled":true}`. [VERIFIED change; the old param's continued acceptance is PRACTICE.]
- **Other useful flags:** `disable_notification` (silent, for low-priority bubbles), `protect_content` ("Protects the contents of the sent message from forwarding and saving"), `message_effect_id` (private chats only). [VERIFIED]
- **Files:** `sendPhoto`: "The photo must be at most **10 MB** in size. The photo's width and height must not exceed 10000 in total. Width and height ratio must be at most 20." `sendDocument`: "files of any type of up to **50 MB**". Via URL: 5 MB photos / 20 MB others. Multipart: 10 MB photos / 50 MB others. Downloads (getFile): 20 MB. A local Bot API server lifts uploads to 2000 MB. `sendMediaGroup` takes 2-10 items. Use `sendChatAction upload_photo` / `upload_document` while you render a chart or PDF. [VERIFIED]

## 5. Robinhood native trade approvals (agentic / MCP accounts)

**What Robinhood officially says** (support article "Trading with your agent", fetched 2026-10-27): [VERIFIED]
- "Note: Trade approvals are **still rolling out for MCP accounts. Not all customers have access.**"
- "Trade approvals is a safety control for agentic trading. If you turn the setting on, your agent can propose trades, but you must review and manually place the proposed orders in the Robinhood app. With trade approvals off, your agent can place eligible trades for you without your manual confirmation of each order. Trade approvals are turned on by default for Robinhood Agents (built-in agents), and are **turned off by default for MCP accounts (external agents)**."
- Where: "For MCP accounts: Select your MCP account tab → Go to Agent Settings by tapping on your connected agent → Under Safety Controls, you can toggle on/off Trade approvals."
- "Certain trades may still require your approval even when trade approvals are turned off." The setup article adds: "…depending on the trade and the model you're using."
- Setup article (built-in agents): "**Your agent can't approve a proposed trade for you.**"
- Newsroom 2026-09-29: "With approvals on, your agent cannot place an order until you approve it." The newsroom also says "Crypto trade approvals must be enabled in CT, NY, and CA." (The support article says CA and CT for built-in agents. The sources differ slightly.)
- Newsroom 2026-05-27: "You'll get **push notifications any time your agent makes a trade**". Also: "your agent only has access to the funds you deposit into that account". And: "you can disconnect the agent at any time with the tap of a button."
- Agentic overview: "Your agent can only place trades in your Robinhood Agentic account." The agent has *read* access to all accounts, positions, balances, and order history.

**What the MCP tool contract says** (tool descriptions, this session): [VERIFIED as the tool contract]
- `review_equity_order` can return `customer_approval_required: true`. `place_equity_order`: "When it comes back with customer_approval_required true and no order_checks alert, call this tool straight away … because this call does not place the order: it goes to the customer to approve or decline in the Robinhood app."
- `get_trade_approval_setting(account_number)`: "Read whether trades on an account currently require the customer's manual approval … **Only the customer can change this setting, in the Robinhood app; no tool here turns it on or off.** … Only agentic accounts have this setting." → It is a **per-agentic-account** setting. A third-party report shows the response field `human_must_approve_trades` with an `updated_at`. [PRACTICE]
- `get_trade_approvals(account_number, approval_ids?, asset_class?, cursor?)`: "the customer approves and declines in the Robinhood app and **pending approvals expire on their own**". With `approval_ids` it returns any state: "**pending, approved, declined, expired, or auto-approved**". At most 100 ids per call.
- `decline_trade_approval(account_number, approval_id)`: "**The agent can only decline; approving a trade is done by the customer in the Robinhood app.**"

**Could not verify:**
- **Expiry window** of a pending approval: no official or third-party source states it. [UNVERIFIED] Poll `get_trade_approvals(approval_ids=[…])` until the state is terminal.
- Whether a **pending approval** triggers its own push notification (as opposed to the documented push on every *trade*): not documented. [UNVERIFIED] Whether a deep link to the approval exists: [UNVERIFIED].
- What `auto-approved` means exactly. Maybe orders that need no approval are logged as auto-approved approvals. [UNVERIFIED]
- How `ref_id` idempotency works with approvals (does a retry with the same `ref_id` return the same approval?). [UNVERIFIED]
- What happens to a pending approval at market close, or to an approved *market* order after hours. The `place_equity_order` doc says market/stop orders "placed after hours as regular_hours … queue for the next regular open". Whether approval-time or placement-time governs this: [UNVERIFIED].
- A report shows the server refusing a multi-leg order "this account requires the customer to approve agent-placed trades" while the setting read `human_must_approve_trades: false`. The server-side state may lag behind or disagree with the setting. [PRACTICE]
- Repo note: `TICK.md`/`tick.py` today have no handling for `customer_approval_required` or approval ids (grep found none). Turning the setting on today would make the tick's "placed" orders become pending approvals that `commit` does not model.

## 6. Telegram button confirm vs Robinhood-app approval

| | Telegram inline button | Robinhood app approval |
|---|---|---|
| Who enforces it | Our code (Python ledger + bridge). A bug or a bypass in our code = no gate. | **The broker.** The order cannot execute without the customer's tap. "The agent can only decline." |
| Auth factor | Telegram session (SMS code + optional 2SV password). Same factor as the chat itself. | A separate app + Robinhood login/device. An independent factor. |
| Stolen Telegram session | Attacker can press Approve (`from.id` = owner, so it is indistinguishable) **and** type "buy X". Full trade authority. | Attacker can only *ask* the agent to propose. The order waits for the real owner's app tap. |
| Stolen bot token / compromised box | Token holder can impersonate the bot, read or steal updates, and set a webhook. A compromised box can skip the button check. | Still blocked at the broker. |
| UX | One tap, in-thread, instant. Rich context in the card. | A context switch to another app. The approval UI shows the broker's view, not our reasoning. Notification behavior is undocumented. |
| Price drift | We control it. Re-quote and re-gate at the tap. Enforce a TTL. | The order *as placed* (type/limit) waits. A market order fills at the approval-time price. Use a marketable **limit** to bound slippage. The expiry window is unknown. |
| Market hours | We can refuse taps when closed, or queue them. | Unknown for pending approvals. After-hours market orders queue to the open. |
| Availability | Always (our code). | "Still rolling out for MCP accounts. Not all customers have access." **Check with `get_trade_approval_setting` first.** |
| State visibility | Ledger row. | Must poll `get_trade_approvals`. The state changes out-of-band (approve, decline, expire). |

**Recommended layering (my synthesis):** Telegram button = *intent and UX* ("yes, do it"). Robinhood app approval = *authorization* (a broker-enforced second factor), if it is available on the account. Python still owns sizing, gates, and the re-check at execution. If the RH approval is not available, add a second factor on the Telegram path for trades (see §7).

## 7. Security notes for a write-capable Telegram bot

- **Bot chats are cloud chats, not end-to-end encrypted.** Telegram FAQ: "Server-client encryption is used in Cloud Chats (private and group chats), Secret Chats use an additional layer of client-client encryption." Secret Chats are "device-specific and are not part of the Telegram cloud" and are user-to-user only. Bots intro: "Telegram's intermediary server handles all encryption and communication with the Telegram API. Developers communicate with this server via an easy HTTPS-interface". So Telegram (the operator) can read every bot message, and the history syncs to every logged-in device. [VERIFIED] Do not send account numbers, tokens, or full positions you would not want stored on Telegram's servers. Use `protect_content` to stop forwards and saves (it does not stop screenshots).
- **An attacker with the owner's Telegram session** (stolen phone, SIM swap without 2SV, a malicious desktop session, a leaked login code) can: read all past chat history, including any PIN the owner ever typed; send commands; press inline buttons (the callback `from.id` is the real owner id, so the allowlist passes); see pending proposal cards; and react. The allowlist (by user id *or* chat id) does **not** stop this. It stops only *other* Telegram users. [Inference from VERIFIED API semantics.]
- **Mitigations:**
  1. **Robinhood app approval ON** = a broker-enforced second factor on a different app/login. This is the strongest control. Its availability must be checked.
  2. Owner hygiene (Telegram FAQ): turn on **2-Step Verification** ("you will need both an SMS code and a password to log in"), set a local **passcode**, and review **Settings > Devices / Active Sessions** (terminate unknown sessions). [VERIFIED]
  3. If RH approval is not available: a per-trade **TOTP** (better than a static PIN, because history is readable; a static PIN typed once is exposed forever). Have the bot **delete the owner's PIN message** after it reads it (deleteMessage: "Bots can delete incoming messages in private chats", <48 h). [VERIFIED] Or require the confirmation phrase to echo a random code shown on the card.
  4. Allowlist **`from.id`** (user) **and** `chat.id` **and** `chat.type=="private"` on both messages and callbacks. Today only `chat.id` is checked.
  5. Python-side blast-radius caps stay authoritative: the agentic account holds only the deposited funds; per-name target caps; the 20% daily-loss halt; KILL. Add a per-day "human-approved notional" ceiling and a cooldown after N approvals per hour (a stolen-session burst).
  6. Single-use nonces, TTL, re-quote at the tap, and an audit row per decision (who, `update_id`, `callback_query.id`, quote at the tap).
  7. **Bot token = a bearer secret.** With it, anyone can read or steal incoming updates, send as the bot (phishing "approve this"), or set a webhook (which breaks our `getUpdates`). Keep it in Secret Manager. Rotate it via BotFather if you suspect a leak. The RH app approval also covers this case.
  8. Treat owner message text as untrusted input to the LLM orchestrator (prompt injection via forwarded content). A trade must always go through the Python plan path, never "because the chat said so".

## 8. Exact call sequence for the requested UX (Bot API 10.3)

```
# poll (single process). The list is sticky, so always pass it.
POST getUpdates {"offset": N, "timeout": 50,
                 "allowed_updates": ["message","callback_query"]}   # + "message_reaction" optional

# (a) inbound owner message → ack at once (best-effort, ignore errors)
POST setMessageReaction {"chat_id": C, "message_id": M,
                         "reaction": [{"type":"emoji","emoji":"👀"}]}
# keep-alive while working (every ~4 s) — or a status draft (private chat only, refresh <30 s):
POST sendChatAction {"chat_id": C, "action": "typing"}
POST sendMessageDraft {"chat_id": C, "draft_id": 1, "text": ""}      # shows “Thinking…”

# (b) reply as 2-4 short bubbles, ≥1 s apart, typing between; thread only the first
POST sendMessage {"chat_id": C, "text": "<html-escaped>", "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": true},
                  "reply_parameters": {"message_id": M, "allow_sending_without_reply": true}}
POST sendMessage {"chat_id": C, "text": "...", "parse_mode": "HTML"}
# done → swap the ack (👀 → 👌 ; on failure → 🤷). ✅ is NOT a valid reaction.
POST setMessageReaction {"chat_id": C, "message_id": M, "reaction": [{"type":"emoji","emoji":"👌"}]}

# (c) proactive alert = a plain sendMessage to the owner chat (disable_notification for low priority)

# (d) trade proposal card
POST sendMessage {"chat_id": C, "text": "Buy $120 NVDA @ ~$x (why…). Expires 15:10 ET.",
  "parse_mode": "HTML",
  "reply_markup": {"inline_keyboard": [[
     {"text":"Approve","callback_data":"a:Q7f3kM2pXw9","style":"success"},
     {"text":"Reject","callback_data":"r:Q7f3kM2pXw9","style":"danger"}]]}}
# on callback_query:
POST answerCallbackQuery {"callback_query_id": Q, "text": "Got it — checking price…"}   # FIRST
#   verify from.id==OWNER && message.chat.id==C && nonce pending && not expired && msg id matches
#   CAS pending→approved ; re-run Python plan gates with a fresh quote ; then execute
POST editMessageReplyMarkup {"chat_id": C, "message_id": card_id,
  "reply_markup": {"inline_keyboard": [[{"text":"Approved ✓","disabled":{}}]]}}  # or {"inline_keyboard":[]}
POST editMessageText {...}  # optional: rewrite the card with the outcome
```

The `"disabled":{}` shape (an empty `DisabledButton` object) follows the type doc ("Currently holds no information"). Whether a disabled button may omit every other action field is **[UNVERIFIED]**. The safe fallback is `{"inline_keyboard":[]}` plus an `editMessageText` outcome line.

## 9. Gaps in today's code that the redesign must change

- `deploy/runner/chat_bridge.py` polls with `allowed_updates: ["message"]`. Add `callback_query` (and `message_reaction` if used).
- `_typing()` fires once. Add a refresh loop or a draft.
- The allowlist checks only `chat.id`. Add `from.id`.
- It uses the legacy `disable_web_page_preview`. Move to `link_preview_options`.
- There is no `retry_after` handling on a 429 (`_tg` returns None on error).
- `lib/telegram.py` (the alert sender) is send-only, so no callback handling is needed there. Proposals must route their buttons to the single bridge poller.
- `TICK.md` / `tick.py` do not model `customer_approval_required` / approval ids.
