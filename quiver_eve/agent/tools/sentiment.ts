import { defineTool } from "eve/tools";
import { runPythonDataTool } from "../../run/quill_data.js";

const schema = {
  type: "object",
  properties: { ticker: { type: "string", minLength: 1 } },
  required: ["ticker"],
  additionalProperties: false,
} as const;

export default defineTool({
  description: "Fetch sentiment (StockTwits; Reddit degrades to 403). Returns a compact sentiment report. Read-only.",
  inputSchema: schema,
  async execute({ ticker }) {
    const out = await runPythonDataTool("sentiment", { ticker });
    return { ticker, report: out.report };
  },
  toModelOutput(o) {
    return { type: "text", value: `sentiment_report for ${o.ticker}:\n${o.report}` };
  },
});
