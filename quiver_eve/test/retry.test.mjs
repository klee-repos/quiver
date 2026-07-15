// F1 retry/fallback contract test (deterministic, no LLM, no network). Imports the REAL
// withRetry from ../run/retry.mjs (NOT a copy) so reverting the fallbackValue behavior turns
// this red. Pins: an ADVISORY turn (fallbackValue set) degrades to the placeholder instead of
// throwing on exhaustion — the exact DLR/BOT/VRT class-A fix — while a CRITICAL turn (no
// fallbackValue) still throws (fail-safe -> analyze.py ERROR).
import { withRetry } from "../run/retry.mjs";

let pass = 0, fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.error("FAIL:", name); } }
const FALLBACK = "(unavailable — model returned no output after retries)";

// (1) THE bug path: validate-fails every attempt (empty output, NOT a throw) + fallbackValue
//     -> returns the fallback after exhausting all tries (and it DID retry).
{
  let n = 0;
  const out = await withRetry("risk_con", () => { n++; return ""; },
    { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: FALLBACK });
  check("(1) validate-exhaustion with fallback -> returns fallback", out === FALLBACK);
  check("(1) retried all tries before falling back", n === 3);
}

// (2) throw path: fn throws every attempt + fallbackValue -> returns the fallback (tries:1 = no backoff).
{
  const out = await withRetry("risk_agg", () => { throw new Error("boom"); },
    { tries: 1, fallbackValue: FALLBACK });
  check("(2) throw-exhaustion with fallback -> returns fallback", out === FALLBACK);
}

// (3) CRITICAL turn: validate-fails every attempt, NO fallbackValue -> throws (fail-safe preserved).
{
  let threw = false;
  try {
    await withRetry("proposal", () => "", { tries: 3, validate: (t) => !!(t && t.trim()) });
  } catch { threw = true; }
  check("(3) validate-exhaustion WITHOUT fallback -> throws (critical turn)", threw);
}

// (4) CRITICAL turn: throws every attempt, NO fallbackValue -> throws (tries:1 = fast).
{
  let threw = false;
  try {
    await withRetry("gather", () => { throw new Error("outage"); }, { tries: 1 });
  } catch { threw = true; }
  check("(4) throw-exhaustion WITHOUT fallback -> throws (critical turn)", threw);
}

// (5) happy path: success on the first try -> returns the output, fallback never used.
{
  const out = await withRetry("ok", () => "real answer",
    { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: FALLBACK });
  check("(5) success first try -> returns real output", out === "real answer");
}

// (6) recovery: empty once then good -> returns the good output (fallback NOT used on recovery).
{
  let n = 0;
  const out = await withRetry("bull", () => (++n === 1 ? "" : "recovered"),
    { tries: 3, validate: (t) => !!(t && t.trim()), fallbackValue: FALLBACK });
  check("(6) recovers on retry -> real output, not fallback", out === "recovered");
  check("(6) stopped as soon as it recovered", n === 2);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
