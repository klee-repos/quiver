#!/usr/bin/env bash
# Run the full Quiver test + e2e suite. One command, clean summary, non-zero on any failure.
#
#   tests/run_e2e.sh           # unit + all DETERMINISTIC e2e (fast, free, no network/LLM)
#   tests/run_e2e.sh --live    # ALSO the REAL e2e (test_e2e_live.py) — costs tokens,
#                              #   needs OPENROUTER_API_KEY, takes a few minutes
#
# The live suite is gated behind QUIVER_LIVE_E2E=1; without --live it self-skips (PASS), so
# this script is safe to run constantly / on a schedule. Everything runs against temp ledgers —
# the live state/ledger.db is never touched.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1
[ "${QUIVER_LIVE_E2E:-}" = "1" ] && LIVE=1

# name|file pairs. The live one is appended only when requested.
SUITES=(
  # FIRST, because it is the cheapest and it gates a failure the rest of this suite cannot see:
  # a tracked file referencing a file git does not carry still passes every test here — the file
  # is on YOUR disk — and only breaks once the box pulls the commit without it.
  "tracked-refs|tests/check_tracked_refs.py"
  "guard-tracked-refs|tests/test_tracked_refs.py"
  "unit|tests/test_units.py"
  "chat-guard|tests/test_chat_guard.py"
  "chat-history|tests/test_chat_history.py"
  "brain-node|quiver_eve/test"
  "run-analyses|tests/test_run_analyses.py"
  "e2e-dryrun|tests/test_e2e.py"
  "e2e-alerts|tests/test_e2e_alerts.py"
  "e2e-consistency|tests/test_e2e_consistency.py"
  "e2e-pipeline|tests/test_e2e_pipeline.py"
  "e2e-safety|tests/test_e2e_safety.py"
  "e2e-sellmin|tests/test_e2e_sellmin.py"
  "e2e-catalyst|tests/test_e2e_catalyst.py"
  "e2e-ptj|tests/test_e2e_ptj.py"
  "e2e-wall-replay|tests/test_e2e_wall_replay.py"
  "e2e-attribution|tests/test_e2e_attribution.py"
  "e2e-benchmark|tests/test_e2e_benchmark.py"
  # bench/ — the offline benchmark harness. All deterministic: no network, no broker, no LLM.
  "bench-diagnostics|tests/test_bench_diagnostics.py"
  "bench-replay|tests/test_bench_replay.py"
  "bench-ladder|tests/test_bench_ladder.py"
  "bench-costs|tests/test_bench_costs.py"
  "bench-sweep|tests/test_bench_sweep.py"
  # bench-brain: the OFFLINE half (fixture discovery, the fail-safe contract gate, scorecard
  # wiring) is real and deterministic. Its LIVE replay self-skips unless QUIVER_LIVE_E2E=1,
  # because QUIVER_REPLAY_REPORTS stubs only the gather turn — deepTurn/quickTurn still bill
  # tokens. It prints a sanctioned "N passed, M failed, K skipped" tail, so a skip can never be
  # mistaken for a pass by the grep at the bottom of this file.
  "bench-brain|tests/test_bench_brain.py"
  # llm-judge runs offline here (its LLM `judge()` stages SELF-SKIP without QUIVER_LIVE_E2E;
  # only the offline pipeline + deterministic det() checks run). Under --live it is run WITH
  # QUIVER_LIVE_E2E=1 (see the loop below) so the real `claude` judging kicks in.
  "llm-judge|tests/test_llm_judge.py"
)
[ "$LIVE" = "1" ] && SUITES+=("e2e-live(GLM)|tests/test_e2e_live.py")

echo "================ Quiver test suite ($([ "$LIVE" = "1" ] && echo "incl. LIVE GLM" || echo "deterministic only")) ================"
FAILED=()
for entry in "${SUITES[@]}"; do
  name="${entry%%|*}"; file="${entry##*|}"
  printf '%-26s ' "$name"
  log="/tmp/quiver_${name//[^a-zA-Z0-9]/_}.log"
  if [ "$name" = "brain-node" ]; then
    # F1: the EVE brain's node contract + retry/fallback tests (deterministic, no LLM). The
    # rest of the suite is Python-only, so without this branch these never run in the gate.
    : >"$log"
    rc=0
    for t in quiver_eve/test/retry.test.mjs quiver_eve/test/contract.test.mjs quiver_eve/test/contract_helpers.test.mjs; do
      node "$t" >>"$log" 2>&1 || rc=1
    done
  elif [ "$name" = "e2e-live(GLM)" ] || { [ "$name" = "llm-judge" ] && [ "$LIVE" = "1" ]; }; then
    # Live path: run these WITH QUIVER_LIVE_E2E=1 so their real-LLM stages actually execute.
    # (Without --live, llm-judge falls through to the plain `else` and its LLM stages self-skip.)
    QUIVER_LIVE_E2E=1 "$PY" "$file" >"$log" 2>&1
    rc=$?
  else
    "$PY" "$file" >"$log" 2>&1
    rc=$?
  fi
  # NOTE: grep -oE matches SUBSTRINGS, so the ", N skipped" alternative must come
  # FIRST — otherwise the shorter "N passed, M failed" pattern wins and the skip
  # count is silently truncated out of the summary (a skipped test would then be
  # indistinguishable from a passing one).
  tail=$(grep -oE '[0-9]+ passed, [0-9]+ failed, [0-9]+ skipped|[0-9]+ passed, [0-9]+ failed|[0-9]+ checks passed, [0-9]+ failed|PASS \(no-op\)' "/tmp/quiver_${name//[^a-zA-Z0-9]/_}.log" | tail -1)
  if [ "$rc" -eq 0 ]; then echo "OK    ${tail}"; else echo "FAIL  ${tail}  (see /tmp/quiver_${name//[^a-zA-Z0-9]/_}.log)"; FAILED+=("$name"); fi
done

echo "============================================================================"
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "ALL GREEN ✓"
  exit 0
fi
echo "FAILURES: ${FAILED[*]}"
exit 1
