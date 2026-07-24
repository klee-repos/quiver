// Standalone Quiver decision brain — REAL multi-agent deep-research pipeline.
//
// WHY multi-turn (not the prior single generateText): the pipeline is a genuine
// gather -> bull/bear debate -> research plan -> trade proposal -> risk debate ->
// PM decision flow. (NOTE: quiver_eve/agent/instructions.md is DOCUMENTATION ONLY —
// it is NEVER loaded at runtime; the LIVE prompts are the inline HORIZON + per-turn
// strings in THIS file. Keep both in sync, but this file is the source of truth.)
// The prior decide.mjs collapsed that into one
// shot. This version runs each phase as its own model call, threading the
// report text + the read-only past_context forward, and assembles the FINAL
// markdown to carry EXACTLY the 8 `## section` blocks + 12 labels analyze.py
// parses (the contract is byte-identical to before; lib.rating.parse_rating +
// extract_fields + the F6 Python validator are untouched).
//
// WHY sequential generateText turns (not EVE defineAgent subagents): EVE's HTTP
// stream proxy stalls on long generations (the /eve/v1/session/:id/stream
// connection drops mid-report) — this script deliberately bypasses that proxy
// and calls the ai SDK in-process against OpenRouter, which streams cleanly.
// Subagents route through the proxy; sequential turns don't. agent.ts stays as
// the model-wiring source + the `eve dev` interactive path; THIS is the live
// brain. (See the divergence comment on agent.ts.)
//
// THE WALL: this script NEVER sees trading caps, the broker, buying power, or
// ref_ids. It receives only the read-only memory scorecard (past decisions +
// outcomes) from analyze.py via stdin, and the ticker/date via argv.

import { createOpenAI } from "@ai-sdk/openai";
import { generateText, tool, isStepCount } from "ai";
import { z } from "zod";
import { runPythonDataTool } from "./quill_data.mjs";
import { withRetry } from "./retry.mjs";
import { readFileSync } from "node:fs";

const TICKER = process.argv[2] || "AAPL";
const DATE = process.argv[3] || "";
const PAST_CONTEXT = await readStdin();
const ROUNDS = Math.max(1, parseInt(process.env.QUIVER_RESEARCH_ROUNDS || "1", 10) || 1);

// F1: top-level safety net. The pipeline is top-level awaits; if any turn
// exhausts its retries and throws (withRetry rethrows), this maps it to a clean
// exit 1 so analyze.py sees a non-zero rc -> signal:ERROR (fail-safe skip),
// rather than a raw unhandled-rejection dump. Never catches the success path.
let _pipelineFailed = false;
process.on("unhandledRejection", (e) => {
  _pipelineFailed = true;
  process.stderr.write(`[decide] PIPELINE FAILED: ${e?.message || e}\n`);
  process.exit(1);
});

const or = createOpenAI({
  baseURL: process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1",
  apiKey: process.env.OPENROUTER_API_KEY,
  name: "openrouter",
  compatibility: "compatible",
});
const DEEP = or.chat(process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2");
const QUICK = or.chat(process.env.QUIVER_CHAT_MODEL || "z-ai/glm-4.7-flash");

// Reasoning-token accounting (Gate-B): emitted per deep turn WITH the attempt
// index whose output is actually used. analyze.py tracks the SUCCESSFUL (final)
// attempt's count — not a sum — so reasoning_content_len honestly reflects the
// accepted contract (a thinking-OFF regression on the winning attempt is caught).
let reasoningTokens = 0;       // the FINAL attempt's count (the one whose output is used)
let reasoningAttempt = 0;      // which attempt produced reasoningTokens

// F1: a boolean "did the labels parse?" predicate. MUST mirror F6's Python `grab`
// regex `(?::\*\*|\*\*:)` (analyze.py:315) — NOT the TS test's `:?\*\*:` which
// rejects `**Label:** value` (would force a false-positive retry on every such
// ticker). Returns true iff EVERY required label is present (case-insensitive,
// spaces as \s+ — matching extractLabels at decide.mjs:268).
function hasRequiredLabels(text, labels) {
  const lines = (text || "").split("\n");
  for (const lab of labels) {
    const re = new RegExp("\\*\\*" + lab.replace(/ /g, "\\s+") + "(?::\\*\\*|\\*\\*:)\\s*(.+)", "i");
    if (!lines.some(l => re.test(l))) return false;
  }
  return true;
}

// F1: a boolean "did the gather turn produce all 5 sub-reports?" predicate.
function hasSubheads(text, names) {
  for (const n of names) {
    const re = new RegExp("###\\s+" + n + "\\s*\\n", "i");
    if (!re.test(text || "")) return false;
  }
  return true;
}

// withRetry (+ the fallbackValue option F1 relies on) is EXTRACTED to ./retry.mjs so it
// is unit-testable without importing this side-effectful script. See retry.mjs.

async function deepTurn(system, prompt) {
  // reasoningTokens/reasoningAttempt are set by the withRetry caller per
  // successful attempt (below). This fn just does ONE generateText call.
  const r = await generateText({
    model: DEEP,
    maxOutputTokens: 16000,
    providerOptions: { openrouter: { max_tokens: 16000, reasoning: { effort: "high" } } },
    // temperature 0 ONLY under the offline replay seam (deterministic before/after diffs);
    // undefined in production keeps the live provider default (unchanged brain behavior).
    temperature: process.env.QUIVER_REPLAY_REPORTS ? 0 : undefined,
    system, prompt,
    stopWhen: isStepCount(1),
  });
  const t = r.usage?.outputTokenDetails?.reasoningTokens ?? r.usage?.reasoningTokens ?? 0;
  // Stash this attempt's count; withRetry's caller promotes it to the global
  // only when the WHOLE turn (incl. validation) succeeds.
  return { text: r.text, reasoningTokens: t };
}

async function quickTurn(system, prompt) {
  const r = await generateText({
    model: QUICK,
    maxOutputTokens: 6000,
    providerOptions: { openrouter: { max_tokens: 6000 } },
    temperature: process.env.QUIVER_REPLAY_REPORTS ? 0 : undefined,
    system, prompt,
    stopWhen: isStepCount(1),
  });
  return r.text;
}

// Deep-turn wrapper with retry: on the SUCCESSFUL attempt, set the global
// reasoningTokens/reasoningAttempt to THAT attempt's count (Gate-B: the count
// is from the attempt whose output is used, not a sum across failed attempts).
// The single final REASONING_TOKENS line is emitted once at the end of the run.
async function deepTurnRetry(label, system, prompt, { tries = 3, validate = null } = {}) {
  return withRetry(label, async (attempt) => {
    const { text, reasoningTokens: t } = await deepTurn(system, prompt);
    if (validate && !validate(text)) throw new Error(`${label}: output failed validation`);
    reasoningTokens = t;        // promote ONLY on success (overwrites any prior failed attempt's stash)
    reasoningAttempt = attempt;
    return text;
  }, { tries, validate: null });  // validation handled inside (so the count isn't stashed on a validation fail)
}

async function quickTurnRetry(label, system, prompt, { tries = 3, validate = null, fallbackValue = undefined } = {}) {
  return withRetry(label, (attempt) => quickTurn(system, prompt), { tries, validate, fallbackValue });
}

// F1: advisory debate turns (bull/bear/risk_agg/risk_con) are NOT in the parsed output
// contract — they only feed the plan/PM turns. If glm-4.7-flash returns empty text 3x
// (the DLR/BOT/VRT class-A failure), degrade to this placeholder instead of throwing +
// sinking the whole ticker. CRITICAL turns (gather/proposal/plan/decision) pass NO
// fallbackValue, so they still hard-fail -> analyze.py fail-safe ERROR on a real outage.
const ADVISORY_UNAVAILABLE = "(unavailable — model returned no output after retries)";


const HORIZON = `You are the Quiver decision brain: a LONG-HORIZON TREND FOLLOWER. You ride
multi-quarter to multi-year trends, NOT daily pops. Treat short-term noise (RSI overbought,
Bollinger pops, single-day moves) as CONTEXT, never a thesis. "Edge" is a durable trend
CONFIRMED BY STRUCTURE — price above a RISING 200-day SMA, a golden cross, and positive 12-1
momentum, with constructive risk-adjusted return (Sharpe/Sortino/Calmar) and tolerable drawdowns
(depth AND duration). Edge is NOT a high ADX: a smooth multi-quarter grind-up runs only a MODERATE
14-period ADX (~15-25), so read the trend_report's REGIME LABEL as the trend read, not the ADX
number. Your judgement cuts BOTH ways — you are as wrong to miss a standing uptrend as to chase a
broken one:
- A name in a CONFIRMED uptrend (constructive/UPTREND regime, positive 12-1 momentum, above a
  rising 200d, tolerable drawdown) IS the edge a trend-follower exists to capture — rate it
  Overweight, and a clear leader breaking out a Buy. A pullback INSIDE an intact uptrend is a place
  to RIDE or ADD; do NOT default a healthy uptrend to Hold just because it paused for a week or two.
- A name with genuinely NO durable trend (RANGE / no structural edge either way) is a HOLD — do
  not manufacture a trade.
- A name whose long-horizon trend has BROKEN (DOWNTREND regime, negative 12-1, below a falling
  200d, a death cross) is Underweight/Sell.
You NEVER see trading caps, the broker, or buying power; risk management is Python's job, downstream.`;

const PAST = `Your prior calls on ${TICKER} (read-only memory scorecard — your own past
decisions + outcomes, NOT limits):
${PAST_CONTEXT || "(no prior decisions on this ticker)"}`;

// --- Step 1: gather (analysts via tools, quick model drives the tool calls) ---
// F1: gather retries re-run all 5 yfinance tool calls, so cap at 2 tries (cost/
// rate-limit). Validate the 5 ### subheads are present (not just non-empty text).
// The gather-validation set (hasSubheads retries until these are present). macro_report
// is deliberately NOT here: it's supplementary market-wide context, so a missing macro
// subhead must NOT fail gather -> ERROR (only market_report gates a trade). It's still
// prompted for + assembled below via grabSub (falls back to "UNAVAILABLE: not produced").
const GATHER_NAMES = ["market_report", "trend_report", "fundamentals_report", "news_report", "sentiment_report"];
const dataTool = (kind, description) => tool({
  description,
  inputSchema: z.object({ ticker: z.string().min(1) }),
  execute: async ({ ticker }) => runPythonDataTool(kind, { ticker, date: DATE }),
});

async function runGather() {
  const r = await generateText({
    model: QUICK,
    maxOutputTokens: 8000,
    providerOptions: { openrouter: { max_tokens: 8000 } },
    system: HORIZON,
    prompt: `Analyze ${TICKER} for trade date ${DATE}. Call the market_data, fundamentals,
news, sentiment, trend, AND macro tools to gather real data. ${PAST}

Then output SIX report blocks, each under a single `+ "`"+`### `+ "`"+` subheading (use ###, NEVER ##):
### market_report
### trend_report
### fundamentals_report
### news_report
### sentiment_report
### macro_report
Summarize the fetched data in each. The macro_report is MARKET-WIDE (Fed/rates, oil/energy,
index moves, geopolitics) — in it, note any macro read-through to ${TICKER} specifically (e.g. an
oil spike or rate move that helps/hurts this name). If a tool returned UNAVAILABLE, write
"UNAVAILABLE: <reason>". Do not emit any ## section headers — only ### subheadings inside your reports.`,
    tools: {
      market_data: dataTool("market", "Fetch OHLCV + short-term technical indicators for a ticker. Read-only."),
      trend: dataTool("trend", "Fetch LONG-HORIZON trend/risk guideposts (3y: Sharpe/Sortino/Calmar, drawdown depth+duration, ADX/DI, MA structure, regime). Read-only."),
      fundamentals: dataTool("fundamentals", "Fetch fundamentals (PE, P/B, market cap, balance sheet). Read-only."),
      news: dataTool("news", "Fetch recent ticker-specific news flow. Read-only."),
      sentiment: dataTool("sentiment", "Fetch sentiment (StockTwits). Read-only."),
      macro: dataTool("macro", "Fetch MARKET-WIDE macro/geopolitical news (Fed/rates, oil/energy, index moves) — NOT ticker-specific; the one feed that catches a macro shock no single holding mentions. Read-only."),
    },
    stopWhen: isStepCount(12),
  });
  return r.text;
}
// Offline eval seam (test infra; INERT in production — only fires when QUIVER_REPLAY_REPORTS is
// set). Loads the 6 cached analyst reports from a prior audit JSON and reconstructs the
// `### <name>` gather-turn text the downstream turns consume, so prompt changes can be replayed
// against FIXED inputs with NO network/tool calls. Production (env unset) still runs runGather.
function loadReplayReports(path) {
  const j = JSON.parse(readFileSync(path, "utf8"));
  const names = ["market_report", "trend_report", "fundamentals_report", "news_report", "sentiment_report", "macro_report"];
  return names.map((n) => `### ${n}\n${(j[n] || "UNAVAILABLE: not in replay fixture").trim()}`).join("\n\n");
}
const reports = process.env.QUIVER_REPLAY_REPORTS
  ? loadReplayReports(process.env.QUIVER_REPLAY_REPORTS)
  : await withRetry("gather", runGather, { tries: 2, validate: (t) => hasSubheads(t, GATHER_NAMES) });

process.stderr.write(`[decide] ticker=${TICKER} date=${DATE} rounds=${ROUNDS} deep=${process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2"}\n`);

// --- Step 2: bull vs bear debate (quick model, `rounds` each) ---
let bull = "", bear = "";
for (let i = 0; i < ROUNDS; i++) {
  bull = await quickTurnRetry(`bull_r${i+1}`, HORIZON, `Round ${i+1} BULL case for ${TICKER} as a long-horizon trend
follow. Use the reports + your memory scorecard as evidence; ground the bull thesis in the
trend guideposts (regime, ADX, drawdown quality, multi-horizon momentum, risk-adjusted return).
Be specific and data-backed.

REPORTS:
${reports}

${PAST}`, { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: ADVISORY_UNAVAILABLE });

  bear = await quickTurnRetry(`bear_r${i+1}`, HORIZON, `Round ${i+1} BEAR case for ${TICKER}. Steelman the bearish
view against the same reports + memory. Where could the trend break? What drawdown risk,
regime-flip risk, or fundamental deterioration is the bull case ignoring? Be data-backed.

REPORTS:
${reports}

${PAST}`, { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: ADVISORY_UNAVAILABLE });
}

// --- Step 3: research plan (deep model) ---
const plan = await deepTurnRetry("plan", HORIZON, `As Research Manager, synthesize the bull + bear cases into
ONE investment plan for ${TICKER}. State a recommendation, the time horizon you're betting on,
the rationale grounded in the trend guideposts, and the strategic actions. Weigh the bull vs
bear strength honestly.

BULL CASE:
${bull}

BEAR CASE:
${bear}

REPORTS:
${reports}

${PAST}`, { tries: 3, validate: (t) => !!(t && t.trim()) });

// --- Step 4: trade proposal (quick model) ---
const TRADER_LABELS = ["Action","Entry Price","Stop Loss","Position Sizing","Position Pct","Strategy Basis","Catalyst","Target Price"];
const proposal = await quickTurnRetry("proposal", HORIZON, `As Trader, turn this research plan into a concrete
proposal for ${TICKER}. Emit EXACTLY these 8 lines (use the `+ "`"+`**Label**: value`+ "`"+` form), nothing
else for this block:
**Action**: <Buy|Overweight|Hold|Underweight|Sell|skip>
**Entry Price**: <number>
**Stop Loss**: <number>
**Position Sizing**: <e.g. "~5% of capital" | "$200">
**Position Pct**: <number, % of equity>
**Strategy Basis**: <short stable thesis tag — MUST be non-empty; keep the SAME tag across runs
  for this ticker while the thesis holds; change it on a real new catalyst OR a material change in
  trend STRUCTURE (a regime flip, a new golden/death cross, a 200d-slope sign change)>
**Catalyst**: <named new catalyst OR named trend-structure change, OR "none">  (REQUIRED when reversing your prior stance)
**Target Price**: <number or "none">
Frame entry/stop as trend-ride levels, not scalp levels. Python prices the stop; you only seed it.
Consistency cuts BOTH ways: a reversal of your prior stance needs a named catalyst OR a named
trend-structure change (regime flip / new cross / 200d-slope sign change) — but a stale stance the
CURRENT trend now CONTRADICTS is itself an inconsistency. If the setup has turned constructive (or
broken), say so and name the structural change; do not cling to a prior call the trend has outrun.

PLAN:
${plan}`, { tries: 3, validate: (t) => hasRequiredLabels(t, TRADER_LABELS) });

// --- Step 5: risk debate (quick model, aggressive + conservative) ---
const riskAgg = await quickTurnRetry("risk_agg", HORIZON, `As a RISK-ON UPSIDE reviewer for ${TICKER}, argue the
proposal is TOO CAUTIOUS. Where is the position too SMALL, the upside underpriced, the durable trend
with further to run? What is the bear/conservative case UNDERWEIGHTING — a confirmed uptrend, positive
12-1 momentum, a pullback that is a buying opportunity inside an intact trend rather than a top? Make
the strongest data-backed case FOR more exposure. Be concrete.

PROPOSAL:
${proposal}

PLAN:
${plan}`, { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: ADVISORY_UNAVAILABLE });

const riskCon = await quickTurnRetry("risk_con", HORIZON, `As a CONSERVATIVE risk reviewer for ${TICKER}, argue
the risks of the proposal below. What's the downside the bull case is underweighting, the
position size that's too large, the regime that could flip? Be concrete.

PROPOSAL:
${proposal}

PLAN:
${plan}`, { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: ADVISORY_UNAVAILABLE });

// --- Step 6: portfolio decision (deep model, reasoning ON) ---
const PM_LABELS = ["Rating","Next Review Hours","Conviction","Uncertainty"];
const decision = await deepTurnRetry("decision", HORIZON, `As Portfolio Manager (thinking ON), make the FINAL call
for ${TICKER}. Read the whole chain. Emit EXACTLY these 4 lines (use `+ "`"+`**Label**: value`+ "`"+`),
then a one-paragraph executive summary:
**Rating**: <Buy|Overweight|Hold|Underweight|Sell>
**Next Review Hours**: <number>
**Conviction**: <0-100>
**Uncertainty**: <0-100>
The Rating is the 5-tier signal Python derives. If core market/price data was UNAVAILABLE, set
**Rating**: Hold and note the failure. Otherwise rate the NAME's long-horizon trend honestly and
symmetrically: a name with no durable trend (RANGE) is Hold, but a name in a CONFIRMED uptrend
(UPTREND regime, positive 12-1, above a rising 200d) is Overweight/Buy — do NOT default an intact
uptrend to Hold; a BROKEN trend (DOWNTREND, negative 12-1, below a falling 200d) is Underweight/Sell.

REPORTS:
${reports}

PLAN:
${plan}
PROPOSAL:
${proposal}
UPSIDE case (risk-on — the bull argument for MORE exposure): ${riskAgg}
DOWNSIDE risks (conservative): ${riskCon}

${PAST}`, { tries: 3, validate: (t) => hasRequiredLabels(t, PM_LABELS) });

// --- Step 7: lever proposals (folded into the PM turn output; extract here) ---
// The PM turn may include a lever line; default to "none" if absent.
const leverBlock = /(- \[(data_source|analysis_angle|sentiment_weight|other)\][^\n]+)/i.test(decision)
  ? decision.match(/- \[(data_source|analysis_angle|sentiment_weight|other)\][^\n]+/gi)?.join("\n") || "none"
  : "none";

// --- ASSEMBLE the final markdown (Gate-A + Gate-B: sanitize + prepend headers once) ---
// Each turn body may contain stray `## ` lines that would make analyze.py's
// _split_eve_markdown mis-key (it keys on `^##\s+<word>$`). Downgrade any `## `
// line in a turn body to `### ` so the splitter only sees the 8 canonical
// headers we prepend below. Then prepend exactly one canonical header per section.
function sanitize(body) {
  return (body || "").split("\n").map(l => l.startsWith("## ") ? "### " + l.slice(3) : l).join("\n").trim();
}

// Extract the 5 analyst sub-reports from the gather turn (### subheadings).
function grabSub(body, name) {
  const re = new RegExp("###\\s+" + name + "\\s*\\n([\\s\\S]*?)(?=###\\s+|$)", "i");
  const m = body.match(re);
  return m ? m[1].trim() : "UNAVAILABLE: not produced";
}
const marketBody = grabSub(reports, "market_report");
const trendBody = grabSub(reports, "trend_report");
const fundBody = grabSub(reports, "fundamentals_report");
const newsBody = grabSub(reports, "news_report");
const sentBody = grabSub(reports, "sentiment_report");
const macroBody = grabSub(reports, "macro_report");

// Pull the 8 trader labels + the 4 PM labels out of the proposal/decision turns
// so the final sections carry ONLY the contract (no stray prose that could
// introduce a second `**Label**:`). Fallback: keep the whole turn if extraction fails.
// (TRADER_LABELS/PM_LABELS declared above at the proposal/decision call sites.)
function extractLabels(text, labels) {
  const lines = (text || "").split("\n");
  const out = [];
  for (const lab of labels) {
    const re = new RegExp("\\*\\*" + lab.replace(/ /g, "\\s+") + "(?::\\*\\*|\\*\\*:)\\s*(.+)", "i");
    const hit = lines.find(l => re.test(l));
    if (hit) out.push(hit.trim());
  }
  return out.length ? out.join("\n") : text.trim();  // fallback: whole turn
}
const traderBlock = extractLabels(proposal, TRADER_LABELS);
const pmBlock = extractLabels(decision, PM_LABELS);

const final = [
  "## market_report",
  sanitize(marketBody),
  "",
  "## trend_report",
  sanitize(trendBody),
  "",
  "## sentiment_report",
  sanitize(sentBody),
  "",
  "## news_report",
  sanitize(newsBody),
  "",
  "## macro_report",
  sanitize(macroBody),
  "",
  "## fundamentals_report",
  sanitize(fundBody),
  "",
  "## trader_investment_plan",
  sanitize(traderBlock),
  "",
  "## final_trade_decision",
  sanitize(pmBlock),
  "",
  "## lever_proposals",
  sanitize(leverBlock),
  "",
].join("\n");

process.stdout.write(final);

process.stderr.write(`REASONING_TOKENS: ${reasoningTokens} attempt=${reasoningAttempt}\n`);
process.stderr.write(`[decide] done: deep reasoning=${reasoningTokens}tok (attempt=${reasoningAttempt}), ${final.length} chars\n`);

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}
