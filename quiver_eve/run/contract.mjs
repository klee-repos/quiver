// Pure helpers that ASSEMBLE the brain's contract markdown, EXTRACTED from decide.mjs so they are
// importable + unit-testable without firing the pipeline. decide.mjs is a side-effectful
// top-level-await script with ZERO exports (it reads stdin, calls the network, process.exit()s), so
// nothing defined inside it can be driven by a test — importing it would hang. Same reasoning, and
// the same precedent, as ./retry.mjs.
//
// WHY THIS FILE EXISTS — the residual at lib/data_sentinels.py:42-47.
// analyze.py's core-data trade gate scores the MODEL'S PROSE about the data, not the fetch:
// runGather -> grabSub -> analyze.py:_split_eve_markdown -> _report_available. So when the market
// tool fails and the model writes a plausible priced report anyway, NO text or sentinel rule can
// catch it. Measured, not hypothetical: state/analyze_logs/2026-07-06_AAPL.json derives
// "Estimated current price ~$313 (Market Cap $4.592T / ~14.668B shares)" while its price window is
// 34 days stale and "The indicators block returned empty" — and it scores core_available TRUE. The
// model even wrote "the `core_available` flag returned false" in its own decision: the honest
// boolean reached the MODEL and the Python wall never saw it. Only the model's goodwill (it chose
// Hold) stopped a trade, which is prompt luck, not a safety property.
//
// The honest answer already exists one layer up: every data-tool envelope carries a deterministic
// {report, core_available} pair (quill_data.py:45, quill_data.mjs:52/59/62) computed by the FETCHER
// from the PAYLOAD. decide.mjs used to hand that envelope to the model and drop the boolean on the
// floor. Here we accumulate it and render it into the contract as a `## data_availability` section,
// so Python can gate on what the fetcher reported instead of on what the model wrote about it.
//
// THE WALL is unaffected: this sees no caps, no broker, no buying power, no ref_ids — only booleans
// about whether a read-only fetch returned data, plus the markdown that carries them.

// The tool `kind`s decide.mjs exposes, in report order. NOTE these are the dataTool kinds
// (decide.mjs:207-212), NOT analyze.py's `avail`, which is a FIVE-key set with no `trend`
// (analyze.py:209-210) because trend_report is deliberately never scored. Keep the two lists
// un-unified: adding a channel to analyze's `avail` changes `sources_unavailable`, which
// tests/test_bench_diagnostics.py:260-261 equality-asserts against the real producer.
export const AVAILABILITY_CHANNELS = ["market", "trend", "fundamentals", "news", "sentiment", "macro"];

// The `## <section>` name this renders under. analyze.py:_EVE_SECTIONS must carry the SAME string or
// the splitter drops the section and the gate silently falls back to prose.
export const AVAILABILITY_SECTION = "data_availability";

// macro is market-wide: quill_data.py:271 ignores the ticker entirely, so it is exempt from the
// ticker-match credit rule below.
export const TICKERLESS_CHANNELS = new Set(["macro"]);

// EVERY line boundary Python's str.splitlines() recognizes. This is not decoration — it is the fix
// for a MEASURED, no-adversary-required forgery. sanitize() used to split on "\n" while
// analyze.py:355 splits with str.splitlines(), which ALSO breaks on CR, VT, FF, 0x1c, 0x1d, 0x1e,
// 0x85, U+2028 and U+2029. A model-authored trader body containing
// `**Action**: Buy<CR>## market_report<CR>AAPL trades at $313.44…` is therefore ONE line to JS (so
// no downgrade happens) and TWO lines to Python — and because _split_eve_markdown lets a LATER
// duplicate header win, that forged block replaced the genuine market_report the gate reads. All 9
// separators were measured to flip a real `UNAVAILABLE:` market report to core_available TRUE.
// Splitting on the same set and rejoining with "\n" canonicalizes separators, so the two languages
// cannot disagree about where a line begins.
const LINE_BOUNDARY_RX = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

// `^##` NOT followed by `#`, deliberately BROADER than the Python splitter's `^##\s+`. Matching the
// whitespace classes by hand does not work: Python's `\s` matches five characters JS's does not
// (U+001C U+001D U+001E U+001F U+0085), so a `/^##\s/` rule still let `##\x85market_report` through.
// Anything that is not already a deeper heading gets downgraded, which cannot leave a gap.
const TOP_HEADER_RX = /^##(?!#)/;

// Downgrade any `## ` line in a MODEL-authored turn body to `### ` so analyze.py's splitter only
// ever sees the canonical headers the assembler prepends. `l.slice(2)` preserves whatever followed
// the `##`, so `## x` -> `### x` byte-identically to the pre-hardening behaviour.
export function sanitize(body) {
  return String(body ?? "")
    .split(LINE_BOUNDARY_RX)
    .map((l) => (TOP_HEADER_RX.test(l) ? "###" + l.slice(2) : l))
    .join("\n")
    .trim();
}

const normTicker = (t) => String(t ?? "").trim().toUpperCase();

// Fold ONE tool envelope into the accumulator. MUST NOT THROW for any input: this runs inside
// dataTool.execute, a path that today cannot throw, and a throw there is surfaced to the model as a
// tool error, which can cascade into a hasSubheads validation failure and burn the tries:2 gather
// budget — an ERROR caused entirely by the observability shim.
//
// Sticky-OR, not AND and not last-write-wins: the gather turn is retried (decide.mjs tries:2) and
// each retry re-runs all six tool calls, so a channel whose FIRST fetch hit a transient yfinance
// rate-limit and whose second succeeded genuinely reached the model. AND would ERROR that ticker —
// exactly the wasted skip quill_data.py:_retry (F2) was built to kill.
//
// Credit requires the call's ticker to MATCH argv's: the tool's ticker is model-supplied and
// unvalidated (decide.mjs:182-183 `z.object({ticker: z.string().min(1)})`), so a call for a
// different name must not vouch for this one. Normalized the way quill_data.py:261 does.
//
// `core_available === true` is checked STRICTLY: quill_data.mjs's three failure envelopes set it
// false, and a malformed envelope that lacks the field reads undefined. Absent means unavailable,
// never "assume fine".
export function recordAvailability(map, kind, envelope, { ticker, expectedTicker } = {}) {
  let ok = false;
  try {
    const want = normTicker(expectedTicker);
    const got = normTicker(ticker);
    const credited = TICKERLESS_CHANNELS.has(kind) || (want !== "" && got === want);
    ok = credited && envelope != null && typeof envelope === "object" && envelope.core_available === true;
  } catch {
    ok = false;                       // totality: any surprise input is "unavailable", never a throw
  }
  try {
    map.set(kind, map.get(kind) === true || ok);
  } catch {
    /* not even a broken map may break a tool call */
  }
  return map;
}

// Render the accumulator as the `## data_availability` section BODY.
//
// EVERY channel is emitted, including ones the model never called — a never-called tool renders
// `false`. That is the point: zero tool calls is a legal, validation-passing gather (no toolChoice,
// and hasSubheads is a pure regex over six `### ` lines), so "the model skipped the market tool and
// wrote a report from memory" is the fabrication path a prose rule cannot see.
//
// `measured: false` is the replay seam (QUIVER_REPLAY_REPORTS, decide.mjs:227-229): runGather never
// runs, so NO envelope exists and the accumulator is legitimately empty. Rendering `unknown` there
// keeps "not measured" distinguishable from "measured false" — analyze.py treats `unknown` exactly
// like an absent section (prose-only), so replay behaviour is unchanged.
//
// The line shape is rigid because Python parses it; tests/test_units.py derives its fixture by
// CALLING this function, so a dialect change goes red instead of silently degrading the gate.
export function renderAvailabilitySection(map, { measured = true, channels = AVAILABILITY_CHANNELS } = {}) {
  return channels
    .map((c) => {
      const v = measured === false ? "unknown" : (map && map.get(c) === true ? "true" : "false");
      return `- ${c}: ${v}`;
    })
    .join("\n");
}

// Build the ENTIRE stdout contract: the canonical `## ` headers in order, each body sanitized, and
// `## data_availability` STRICTLY LAST.
//
// Last is load-bearing, not cosmetic. analyze.py:_split_eve_markdown lets a LATER duplicate header
// overwrite an earlier one, so a model-authored `## data_availability` inside any turn body — which
// is emitted before this one — is always overwritten by the genuine section. (sanitize already
// downgrades such a line; last-ness is the second, structural layer.)
//
// The whole document is assembled HERE rather than inline in decide.mjs so the emit boundary is
// behaviourally testable. Inline, `const final = [...]` + `process.stdout.write(final)` meant a
// one-token slip could ship an all-green suite with the feature dead; with no second
// document-shaped binding in scope, that slip degenerates to writing nothing, and
// analyze.py:441 already raises "EVE brain returned no markdown on stdout".
export function assembleContract(bodies, availabilityMap, { measured = true, channels = AVAILABILITY_CHANNELS } = {}) {
  const out = [];
  for (const [header, body] of bodies) {
    out.push(`## ${header}`, sanitize(body), "");
  }
  out.push(`## ${AVAILABILITY_SECTION}`,
           renderAvailabilitySection(availabilityMap, { measured, channels }),
           "");
  return out.join("\n");
}
