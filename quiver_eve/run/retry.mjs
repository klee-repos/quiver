// Bounded retry helper for the EVE brain, EXTRACTED from decide.mjs so it is
// importable + unit-testable in isolation (decide.mjs is a side-effectful
// top-level-await script that reads stdin / calls the network / process.exit on
// import — importing it in a test would hang or fire the pipeline). retry.mjs has
// NO top-level side effects: pure control-flow + stderr notes only.
//
// THE WALL is unaffected: this sees no caps/broker/limits — it only bounds retries.

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Retries `fn` up to `tries` times on (a) a thrown error AND (b) an empty/invalid
// output (checked via the optional `validate` predicate). Backoff: exponential+jitter
// for THROWN errors (rate-limit windows are 5-30s); NO delay for empty-output failures
// (a content issue, not a rate issue).
//
// On EXHAUSTION (both the validation-fail path AND the thrown-error path funnel here):
//   * `fallbackValue` provided (an ADVISORY turn, e.g. risk_agg/risk_con/bull/bear) ->
//     return it with a stderr note instead of throwing, so one empty debate turn never
//     sinks the whole ticker. The advisory turns are NOT in the parsed output contract.
//   * `fallbackValue` omitted (the DEFAULT — a CRITICAL turn: gather/proposal/decision) ->
//     throw the last error, so decide.mjs's top-level maps it to exit 1 -> analyze.py
//     signal:ERROR (fail-safe: never fabricate a decision on a real outage).
// `fallbackValue === undefined` reproduces the pre-F1 throw-on-exhaustion behavior exactly.
export async function withRetry(label, fn, { tries = 3, validate = null, fallbackValue = undefined } = {}) {
  const hasFallback = fallbackValue !== undefined;
  let lastErr;
  for (let attempt = 1; attempt <= tries; attempt++) {
    try {
      const out = await fn(attempt);
      if (validate && !validate(out)) {
        process.stderr.write(`[decide] ${label} attempt ${attempt}/${tries}: output failed validation (empty/missing labels); ${attempt < tries ? "retrying" : "giving up"}\n`);
        lastErr = new Error(`${label}: output failed validation`);
        if (attempt < tries) continue; // no backoff for content failures
        break; // exhausted -> fall through to the exhaustion handler below
      }
      return out;
    } catch (e) {
      lastErr = e;
      process.stderr.write(`[decide] ${label} attempt ${attempt}/${tries} error: ${e?.message || e}\n`);
      if (attempt < tries) {
        const base = 2000 * Math.pow(2, attempt - 1); // 2s, 4s
        const jitter = Math.floor(Math.random() * 1000);
        await sleep(base + jitter);
      }
    }
  }
  // Exhausted all tries (either a persistent validation-fail or a persistent throw).
  if (hasFallback) {
    process.stderr.write(`[decide] ${label}: exhausted ${tries} tries -> using fallback placeholder (advisory turn, not fatal)\n`);
    return fallbackValue;
  }
  throw lastErr;
}
