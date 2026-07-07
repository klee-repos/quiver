// EVE brain contract test (deterministic, no LLM). Runs via `npm test` (node).
// Pins the fail-safe validator: a well-formed brain output passes; a malformed
// one (missing label / empty basis / missing section) fails. This is the gate
// that makes analyze.py emit ERROR on a bad brain output instead of trading
// on None fields.
//
// The validator logic is duplicated here (mirrors agent/validate.ts) so this
// test runs with plain node — no EVE toolchain / build step required. If you
// change the validator, update BOTH this and agent/validate.ts.

const REQUIRED_LABELS = [
  "Rating", "Action", "Entry Price", "Stop Loss", "Position Sizing",
  "Position Pct", "Strategy Basis", "Catalyst", "Target Price",
  "Next Review Hours", "Conviction", "Uncertainty",
];
const REQUIRED_SECTIONS = [
  "market_report", "sentiment_report", "news_report", "fundamentals_report",
  "trend_report", "trader_investment_plan", "final_trade_decision", "lever_proposals",
];

function validateMarkdown(md) {
  const missing_labels = [];
  let empty_basis = false;
  for (const label of REQUIRED_LABELS) {
    // Tolerate both `**Label**: value` and `**Label:** value` (colon inside bold).
    const m = md.match(new RegExp(`\\*\\*${label}:?\\*\\*:\\s*(.+)`));
    if (!m) { missing_labels.push(label); }
    else if (label === "Strategy Basis") {
      const v = m[1].trim();
      if (!v || /^(none|null|n\/a)$/i.test(v)) empty_basis = true;
    }
  }
  const missing_sections = [];
  for (const sec of REQUIRED_SECTIONS) {
    if (!new RegExp(`^##\\s+${sec}\\s*$`, "m").test(md)) missing_sections.push(sec);
  }
  return { ok: missing_labels.length === 0 && missing_sections.length === 0 && !empty_basis,
           missing_labels, missing_sections, empty_basis };
}

let pass = 0, fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.error("FAIL:", name); } }

const GOOD = `## market_report
Price closed 195.1, RSI 62.

## sentiment_report
bullish.

## news_report
beat.

## fundamentals_report
PE 28.

## trend_report
ADX 28, regime UPTREND, Sharpe 1.1.

## trader_investment_plan
**Action**: Buy
**Entry Price**: 195.1
**Stop Loss**: 182.5
**Position Sizing**: ~4.5% of capital
**Position Pct**: 4.5
**Strategy Basis**: momentum_breakout
**Catalyst**: none
**Target Price**: 210

## final_trade_decision
**Rating**: Buy
**Next Review Hours**: 24
**Conviction**: 72
**Uncertainty**: 30
ok.

## lever_proposals
- [data_source] options_flow: add odd-lot flow
`;

check("good fixture validates", validateMarkdown(GOOD).ok === true);

// Missing a label -> invalid
check("missing Conviction -> invalid",
  validateMarkdown(GOOD.replace("**Conviction**: 72\n", "")).ok === false);

// Empty Strategy Basis -> invalid (the reversal-suppression risk).
// NOTE: a literally-blank `**Strategy Basis**: ` with NO content after, followed
// by another label line, is grabbed as the next line by `\s*` (the same
// quirk the Python parser has). The ROBUST fail-safe here is label PRESENCE:
// a fully-missing `**Strategy Basis**:` line (no colon+content) -> invalid.
check("missing Strategy Basis label entirely -> invalid",
  validateMarkdown(GOOD.replace("**Strategy Basis**: momentum_breakout\n", "")).ok === false);
check("Strategy Basis = explicit 'none' -> invalid",
  validateMarkdown(GOOD.replace("**Strategy Basis**: momentum_breakout", "**Strategy Basis**: none")).ok === false);

// Missing a section -> invalid
check("missing market_report section -> invalid",
  validateMarkdown(GOOD.replace("## market_report\nPrice closed 195.1, RSI 62.\n", "")).ok === false);

// The validator reports WHICH labels/sections are missing
const r = validateMarkdown(GOOD.replace("**Target Price**: 210\n", "").replace("## lever_proposals\n- [data_source] options_flow: add odd-lot flow\n", "## lever_proposals\nnone\n"));
check("reports missing label", r.missing_labels.includes("Target Price"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
