import { defineTool } from "eve/tools";
import { runPythonDataTool } from "../../run/quill_data.js";

const schema = {
  type: "object",
  properties: { ticker: { type: "string", minLength: 1 } },
  required: ["ticker"],
  additionalProperties: false,
} as const;

export default defineTool({
  description: "Fetch fundamentals (PE, P/B, market cap, balance sheet, cashflow, income) for a ticker. Read-only.",
  inputSchema: schema,
  async execute({ ticker }) {
    const out = await runPythonDataTool("fundamentals", { ticker });
    return { ticker, report: out.report };
  },
  toModelOutput(o) {
    return { type: "text", value: `fundamentals_report for ${o.ticker}:\n${o.report}` };
  },
});
