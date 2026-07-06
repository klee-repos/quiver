// Market-data tools for the Quiver EVE brain.
//
// DESIGN (wall-preserving): the data-fetch logic (yfinance + alpha_vantage
// fallback + StockTwits + stockstats indicators) already lives in
// tradingagents/dataflows/ and is tested. Rather than re-implement it in
// TypeScript (risk: drift, a second source of truth for vendor routing), each
// EVE tool shells out to a tiny Python helper (quiver_eve/run/quill_data.py)
// that returns the SAME JSON the legacy analysts consumed. The tools are
// READ-ONLY to the market; none can place orders or read the broker.
//
// EVE's defineTool takes a Standard JSON Schema `inputSchema` (NOT zod). The
// tool's runtime name is the filename slug (get_market_data). toModelOutput
// projects the JSON down to a compact text REPORT the model reads.

import { defineTool } from "eve/tools";
import { runPythonDataTool } from "../../run/quill_data.js";

const tickerSchema = {
  type: "object",
  properties: {
    ticker: { type: "string", minLength: 1 },
    date: { type: "string" },
  },
  required: ["ticker"],
  additionalProperties: false,
} as const;

const tickerOnlySchema = {
  type: "object",
  properties: { ticker: { type: "string", minLength: 1 } },
  required: ["ticker"],
  additionalProperties: false,
} as const;

export default defineTool({
  description: "Fetch OHLCV + technical indicators (RSI, MACD, MAs) for a ticker. Returns a compact market report. Read-only.",
  inputSchema: tickerSchema,
  async execute({ ticker, date }) {
    const out = await runPythonDataTool("market", { ticker, date });
    return { ticker, report: out.report, core_available: out.core_available };
  },
  toModelOutput(o) {
    return { type: "text", value: `market_report for ${o.ticker}:\n${o.report}` };
  },
});
