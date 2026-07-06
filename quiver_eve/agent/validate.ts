// Output validator — the fail-safe gate. The EVE agent's final markdown MUST
// carry the 12 `**Label**:` lines + 7 `## section` blocks analyze.py parses,
// with `**Strategy Basis**:` non-empty. If any is missing, this exits 1 so
// analyze.py maps the run to an ERROR signal (the bot records a skip, never
// trades on a malformed brain output).
//
// Run as the final step of the agent's session (or standalone on a fixture).
import { REQUIRED_LABELS, REQUIRED_SECTIONS } from "./agent.js";

export interface ValidationResult {
  ok: boolean;
  missing_labels: string[];
  missing_sections: string[];
  empty_basis: boolean;
}

export function validateMarkdown(md: string): ValidationResult {
  const missing_labels: string[] = [];
  let empty_basis = false;
  for (const label of REQUIRED_LABELS) {
    // Tolerate both `**Label**: value` and `**Label:** value` (colon inside bold).
    const re = new RegExp(`\\*\\*${label}:?\\*\\*:\\s*(.+)`);
    const m = md.match(re);
    if (!m) {
      missing_labels.push(label);
    } else if (label === "Strategy Basis") {
      const v = m[1].trim();
      if (!v || /^(none|null|n\/a)$/i.test(v)) empty_basis = true;
    }
  }
  const missing_sections: string[] = [];
  for (const sec of REQUIRED_SECTIONS) {
    const re = new RegExp(`^##\\s+${sec}\\s*$`, "m");
    if (!re.test(md)) missing_sections.push(sec);
  }
  return {
    ok: missing_labels.length === 0 && missing_sections.length === 0 && !empty_basis,
    missing_labels,
    missing_sections,
    empty_basis,
  };
}

// CLI: validate a fixture file. Exits 0 on pass, 1 on fail.
if (import.meta.url === `file://${process.argv[1]}`) {
  const fs = await import("node:fs");
  const file = process.argv[2];
  if (!file) {
    console.error("usage: validate.ts <markdown-file>");
    process.exit(2);
  }
  const md = fs.readFileSync(file, "utf-8");
  const r = validateMarkdown(md);
  if (r.ok) {
    console.log("VALID");
    process.exit(0);
  } else {
    console.error("INVALID:", JSON.stringify(r));
    process.exit(1);
  }
}
