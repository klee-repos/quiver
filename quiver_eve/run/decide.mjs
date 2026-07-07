// Standalone Quiver decision brain — REAL multi-agent deep-research pipeline.
//
// WHY multi-turn (not the prior single generateText): instructions.md describes
// a genuine gather -> bull/bear debate -> research plan -> trade proposal ->
// risk debate -> PM decision flow. The prior decide.mjs collapsed that into one
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

const TICKER = process.argv[2] || "AAPL";
const DATE = process.argv[3] || "";
const PAST_CONTEXT = await readStdin();
const ROUNDS = Math.max(1, parseInt(process.env.QUIVER_RESEARCH_ROUNDS || "1", 10) || 1);

const or = createOpenAI({
  baseURL: process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1",
  apiKey: process.env.OPENROUTER_API_KEY,
  name: "openrouter",
  compatibility: "compatible",
});
const DEEP = or.chat(process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2");
const QUICK = or.chat(process.env.QUIVER_CHAT_MODEL || "z-ai/glm-4.7-flash");

// Reasoning-token accumulator (Gate-B): emitted per deep turn in a finally so a
// later-turn crash still surfaces the count; analyze.py SUMS the lines.
let reasoningTokens = 0;

async function deepTurn(system, prompt) {
  try {
    const r = await generateText({
      model: DEEP,
      maxOutputTokens: 16000,
      providerOptions: { openrouter: { max_tokens: 16000, reasoning: { effort: "high" } } },
      system, prompt,
      stopWhen: isStepCount(1),
    });
    const t = r.usage?.outputTokenDetails?.reasoningTokens ?? r.usage?.reasoningTokens ?? 0;
    reasoningTokens += t;
    process.stderr.write(`REASONING_TOKENS: ${t}\n`);
    return r.text;
  } catch (e) {
    process.stderr.write(`[decide] deep turn error: ${e?.message || e}\n`);
    throw e;  // the caller's top-level catch maps to a non-zero exit -> analyze.py ERROR
  }
}

async function quickTurn(system, prompt) {
  try {
    const r = await generateText({
      model: QUICK,
      maxOutputTokens: 6000,
      providerOptions: { openrouter: { max_tokens: 6000 } },
      system, prompt,
      stopWhen: isStepCount(1),
    });
    return r.text;
  } catch (e) {
    process.stderr.write(`[decide] quick turn error: ${e?.message || e}\n`);
    throw e;
  }
}

const HORIZON = `You are the Quiver decision brain: a LONG-HORIZON TREND FOLLOWER. You ride
multi-quarter to multi-year trends, NOT daily pops. Treat short-term noise (RSI overbought,
Bollinger pops, single-day moves) as CONTEXT, never a thesis. Favor names with:
a persistent trend (high ADX), constructive risk-adjusted return (Sharpe/Sortino/Calmar),
tolerable drawdowns (depth AND duration), and positive multi-horizon momentum. A name with
no clear trend edge is a HOLD — do not manufacture a trade. You NEVER see trading caps, the
broker, or buying power; risk management is Python's job, downstream.`;

const PAST = `Your prior calls on ${TICKER} (read-only memory scorecard — your own past
decisions + outcomes, NOT limits):
${PAST_CONTEXT || "(no prior decisions on this ticker)"}`;

// --- Step 1: gather (analysts via tools, quick model drives the tool calls) ---
const dataTool = (kind, description) => tool({
  description,
  inputSchema: z.object({ ticker: z.string().min(1) }),
  execute: async ({ ticker }) => runPythonDataTool(kind, { ticker, date: DATE }),
});

process.stderr.write(`[decide] ticker=${TICKER} date=${DATE} rounds=${ROUNDS} deep=${process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2"}\n`);

const gather = await generateText({
  model: QUICK,
  maxOutputTokens: 8000,
  providerOptions: { openrouter: { max_tokens: 8000 } },
  system: HORIZON,
  prompt: `Analyze ${TICKER} for trade date ${DATE}. Call the market_data, fundamentals,
news, sentiment, AND trend tools to gather real data. ${PAST}

Then output FIVE report blocks, each under a single `+ "`"+`### `+ "`"+` subheading (use ###, NEVER ##):
### market_report
### trend_report
### fundamentals_report
### news_report
### sentiment_report
Summarize the fetched data in each. If a tool returned UNAVAILABLE, write "UNAVAILABLE: <reason>".
Do not emit any ## section headers — only ### subheadings inside your reports.`,
  tools: {
    market_data: dataTool("market", "Fetch OHLCV + short-term technical indicators for a ticker. Read-only."),
    trend: dataTool("trend", "Fetch LONG-HORIZON trend/risk guideposts (3y: Sharpe/Sortino/Calmar, drawdown depth+duration, ADX/DI, MA structure, regime). Read-only."),
    fundamentals: dataTool("fundamentals", "Fetch fundamentals (PE, P/B, market cap, balance sheet). Read-only."),
    news: dataTool("news", "Fetch recent news flow. Read-only."),
    sentiment: dataTool("sentiment", "Fetch sentiment (StockTwits). Read-only."),
  },
  stopWhen: isStepCount(10),
}).catch(e => { process.stderr.write(`[decide] gather failed: ${e?.message||e}\n`); process.exit(1); });
const reports = gather.text;

// --- Step 2: bull vs bear debate (quick model, `rounds` each) ---
let bull = "", bear = "";
for (let i = 0; i < ROUNDS; i++) {
  bull = await quickTurn(HORIZON, `Round ${i+1} BULL case for ${TICKER} as a long-horizon trend
follow. Use the reports + your memory scorecard as evidence; ground the bull thesis in the
trend guideposts (regime, ADX, drawdown quality, multi-horizon momentum, risk-adjusted return).
Be specific and data-backed.

REPORTS:
${reports}

${PAST}`).catch(() => process.exit(1));

  bear = await quickTurn(HORIZON, `Round ${i+1} BEAR case for ${TICKER}. Steelman the bearish
view against the same reports + memory. Where could the trend break? What drawdown risk,
regime-flip risk, or fundamental deterioration is the bull case ignoring? Be data-backed.

REPORTS:
${reports}

${PAST}`).catch(() => process.exit(1));
}

// --- Step 3: research plan (deep model) ---
const plan = await deepTurn(HORIZON, `As Research Manager, synthesize the bull + bear cases into
ONE investment plan for ${TICKER}. State a recommendation, the time horizon you're betting on,
the rationale grounded in the trend guideposts, and the strategic actions. Weigh the bull vs
bear strength honestly.

BULL CASE:
${bull}

BEAR CASE:
${bear}

REPORTS:
${reports}

${PAST}`).catch(() => process.exit(1));

// --- Step 4: trade proposal (quick model) ---
const proposal = await quickTurn(HORIZON, `As Trader, turn this research plan into a concrete
proposal for ${TICKER}. Emit EXACTLY these 8 lines (use the `+ "`"+`**Label**: value`+ "`"+` form), nothing
else for this block:
**Action**: <Buy|Overweight|Hold|Underweight|Sell|skip>
**Entry Price**: <number>
**Stop Loss**: <number>
**Position Sizing**: <e.g. "~5% of capital" | "$200">
**Position Pct**: <number, % of equity>
**Strategy Basis**: <short stable thesis tag — MUST be non-empty; keep the SAME tag across runs
  for this ticker while the thesis holds; change it only on a real new catalyst>
**Catalyst**: <named new catalyst, OR "none">  (REQUIRED when reversing your prior stance)
**Target Price**: <number or "none">
Frame entry/stop as trend-ride levels, not scalp levels. Python prices the stop; you only seed it.

PLAN:
${plan}`).catch(() => process.exit(1));

// --- Step 5: risk debate (quick model, aggressive + conservative) ---
const riskAgg = await quickTurn(HORIZON, `As an AGGRESSIVE risk reviewer for ${TICKER}, argue the
risks of the proposal below. What's the worst realistic outcome, the stop that's too tight, the
drawdown that'd force an exit? Be concrete.

PROPOSAL:
${proposal}

PLAN:
${plan}`).catch(() => process.exit(1));

const riskCon = await quickTurn(HORIZON, `As a CONSERVATIVE risk reviewer for ${TICKER}, argue
the risks of the proposal below. What's the downside the bull case is underweighting, the
position size that's too large, the regime that could flip? Be concrete.

PROPOSAL:
${proposal}

PLAN:
${plan}`).catch(() => process.exit(1));

// --- Step 6: portfolio decision (deep model, reasoning ON) ---
const decision = await deepTurn(HORIZON, `As Portfolio Manager (thinking ON), make the FINAL call
for ${TICKER}. Read the whole chain. Emit EXACTLY these 4 lines (use `+ "`"+`**Label**: value`+ "`"+`),
then a one-paragraph executive summary:
**Rating**: <Buy|Overweight|Hold|Underweight|Sell>
**Next Review Hours**: <number>
**Conviction**: <0-100>
**Uncertainty**: <0-100>
The Rating is the 5-tier signal Python derives. If core market/price data was UNAVAILABLE, set
**Rating**: Hold and note the failure. A non-trending name with no edge should be Hold.

REPORTS:
${reports}

PLAN:
${plan}
PROPOSAL:
${proposal}
RISKS (aggressive): ${riskAgg}
RISKS (conservative): ${riskCon}

${PAST}`).catch(() => process.exit(1));

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

// Pull the 8 trader labels + the 4 PM labels out of the proposal/decision turns
// so the final sections carry ONLY the contract (no stray prose that could
// introduce a second `**Label**:`). Fallback: keep the whole turn if extraction fails.
const TRADER_LABELS = ["Action","Entry Price","Stop Loss","Position Sizing","Position Pct","Strategy Basis","Catalyst","Target Price"];
const PM_LABELS = ["Rating","Next Review Hours","Conviction","Uncertainty"];
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

process.stderr.write(`REASONING_TOKENS: ${reasoningTokens}\n`);
process.stderr.write(`[decide] done: deep reasoning=${reasoningTokens}tok, ${final.length} chars\n`);

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}
