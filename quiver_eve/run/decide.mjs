// Standalone Quiver decision brain — runs the model with the data tools via the
// ai SDK directly against OpenRouter, prints the final markdown to stdout.
//
// WHY a standalone script (not the EVE `eve dev` server): EVE's HTTP stream proxy
// stalls on long generations (the /eve/v1/session/:id/stream connection drops
// mid-report). The ai SDK + OpenRouter stream fine directly (proven: 1400+ chunks,
// clean [DONE]). For a per-ticker subprocess invoked by analyze.py, a plain
// `node decide.mjs` that streams the model call in-process is simpler, has no
// proxy to stall, and prints the contract markdown to stdout.
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

const or = createOpenAI({
  baseURL: process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1",
  apiKey: process.env.OPENROUTER_API_KEY,
  name: "openrouter",
  compatibility: "compatible",
});
const MODEL = or.chat(process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2");

const dataTool = (kind, description) => tool({
  description,
  inputSchema: z.object({ ticker: z.string().min(1) }),
  execute: async ({ ticker }) => runPythonDataTool(kind, { ticker }),
});

const prompt = `You are the Quiver decision brain. Analyze ${TICKER} for trade date ${DATE}.

Your prior calls on this ticker (read-only memory scorecard):
${PAST_CONTEXT || "(no prior decisions on this ticker)"}

Call the market_data, fundamentals, news, and sentiment tools for real data. Then emit your decision as markdown with EXACTLY these sections in order:
## market_report
## sentiment_report
## news_report
## fundamentals_report
## trader_investment_plan
## final_trade_decision
## lever_proposals

The trader_investment_plan section MUST contain these \`**Label**: value\` lines:
**Action**, **Entry Price**, **Stop Loss**, **Position Sizing**, **Position Pct**, **Strategy Basis** (MUST be non-empty), **Catalyst**, **Target Price**.
The final_trade_decision section MUST contain:
**Rating** (one of Buy/Overweight/Hold/Underweight/Sell), **Next Review Hours**, **Conviction** (0-100), **Uncertainty** (0-100).
Use the fetched data for real numbers. If core market/price data is UNAVAILABLE, set **Rating**: Hold and note the failure. Never fabricate data. In ## lever_proposals, list any NEW evaluation input you'd want for future runs (one \`- [kind] name: rationale\` per line), or \`none\`.`;

// Drive the model with tools in-process (no EVE proxy -> no stream stall). ai v7
// uses stopWhen (default isStepCount(1) = stop after one step); we allow 8 steps
// so the model can call tools then synthesize the final markdown. generateText
// returns the full final text across all steps.
process.stderr.write(`[decide] ticker=${TICKER} date=${DATE} model=z-ai/glm-5.2\n`);
const result = await generateText({
  model: MODEL,
  maxOutputTokens: 16000,
  providerOptions: { openrouter: { max_tokens: 16000, reasoning: { effort: "high" } } },
  tools: {
    market_data: dataTool("market", "Fetch OHLCV + technical indicators for a ticker. Read-only."),
    fundamentals: dataTool("fundamentals", "Fetch fundamentals (PE, P/B, market cap, balance sheet) for a ticker. Read-only."),
    news: dataTool("news", "Fetch recent news flow for a ticker. Read-only."),
    sentiment: dataTool("sentiment", "Fetch sentiment (StockTwits) for a ticker. Read-only."),
  },
  stopWhen: isStepCount(8),
  prompt,
});

const full = result.text;
process.stdout.write(full);
// Surface the reasoning token count so analyze.py can gate on thinking-ON
// (survivor #4). The @ai-sdk/openai .chat() provider doesn't map OpenRouter's
// `reasoning` content field into the V4 reasoning-text channel (r.reasoning is
// []), but it DOES report the token count in usage.outputTokenDetails.reasoningTokens.
// That count is the provider-reported proof the model reasoned. 0 = no thinking
// (a regression to catch); >0 = GLM reasoned.
const reasoningTokens = (result.usage?.outputTokenDetails?.reasoningTokens)
  ?? (result.usage?.reasoningTokens) ?? 0;
process.stderr.write(`REASONING_TOKENS: ${reasoningTokens}\n`);
process.stderr.write(`[decide] done, steps=${result.steps.length}, ${full.length} chars, reasoning=${reasoningTokens}tok\n`);

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data;
}
