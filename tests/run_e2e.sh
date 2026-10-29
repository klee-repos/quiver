#!/usr/bin/env bash
# Run the full Quiver Desk test suite. One command, one summary, non-zero on any failure.
#
#   tests/run_e2e.sh           # every DETERMINISTIC suite (no network, no LLM, no broker)
#   tests/run_e2e.sh --live    # ALSO the live desk e2e (test_e2e_desk_live.py). It uses the
#                              #   Claude subscription: it needs CLAUDE_CODE_OAUTH_TOKEN and
#                              #   CLAUDE_BIN, and it takes a few minutes
#
# The live suite runs only with QUIVER_LIVE_E2E=1 (--live sets it); without it the suite skips.
# Every suite uses a temp ledger. The live state/ledger.db is never touched.
set -u
cd "$(dirname "$0")/.."
PY="${QUIVER_PY:-.venv/bin/python}"   # QUIVER_PY runs the suite in another venv (the clean-lock check)

LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1
[ "${QUIVER_LIVE_E2E:-}" = "1" ] && LIVE=1

# name|file pairs (plan §18.8). tracked-refs runs FIRST: it is the cheapest, and it catches a
# tracked file that names a file git does not carry, which every other suite passes because
# the file is on your disk.
SUITES=(
  "tracked-refs|tests/check_tracked_refs.py"   "guard-tracked-refs|tests/test_tracked_refs.py"
  "unit|tests/test_units.py"                   "backup|tests/test_backup.py"
  "e2e-alerts|tests/test_e2e_alerts.py"        "e2e-safety|tests/test_e2e_safety.py"
  "desk-guard|tests/test_desk_guard.py"
  "e2e-fills|tests/test_e2e_fills.py"          "e2e-attribution|tests/test_e2e_attribution.py"
  "e2e-benchmark|tests/test_e2e_benchmark.py"  "desk-store|tests/test_desk_store.py"
  "desk-ingest|tests/test_desk_ingest.py"      "desk-tiers|tests/test_desk_tiers.py"
  "desk-sections|tests/test_desk_sections.py"  "desk-xbrl|tests/test_desk_xbrl.py"
  "desk-valuation|tests/test_desk_valuation.py" "desk-transcripts|tests/test_desk_transcripts.py"
  "desk-news|tests/test_desk_news.py"          "desk-etf|tests/test_desk_etf.py"
  "desk-findings|tests/test_desk_findings.py"  "desk-fetch|tests/test_desk_fetch.py"
  "desk-levels|tests/test_desk_levels.py"      "desk-events|tests/test_desk_events.py"
  "desk-proposals|tests/test_desk_proposals.py" "desk-changes|tests/test_desk_changes.py"
  "desk-approvals|tests/test_desk_approvals.py" "desk-execution|tests/test_desk_execution.py"
  "broker-guard|tests/test_broker_guard.py"    "desk-agent-hook|tests/test_desk_agent_hook.py"
  "desk-tools-static|tests/test_desk_tools_static.py" "chat-bridge|tests/test_chat_bridge.py"
  "desk-outbox|tests/test_desk_outbox.py"      "desk-brief|tests/test_desk_brief.py"
  "desk-agenda|tests/test_desk_agenda.py"
  "run-monitor|tests/test_run_monitor.py"      "run-worker|tests/test_run_worker.py"
  "e2e-desk|tests/test_e2e_desk.py"
  "desk-mutants|tests/test_desk_mutants.py"   "desk-gateb|tests/test_desk_gateb.py"
  "deploy-units|tests/test_deploy_units.py"
)
[ "$LIVE" = "1" ] && SUITES+=("e2e-desk-live|tests/test_e2e_desk_live.py")

echo "================ Quiver test suite ($([ "$LIVE" = "1" ] && echo "incl. LIVE desk" || echo "deterministic only")) ================"
FAILED=()
for entry in "${SUITES[@]}"; do
  name="${entry%%|*}"; file="${entry##*|}"
  printf '%-26s ' "$name"
  log="/tmp/quiver_${name//[^a-zA-Z0-9]/_}.log"
  if [ ! -f "$file" ]; then
    echo "missing: $file" >"$log"
    rc=1
  elif [ "$name" = "e2e-desk-live" ]; then
    # The live path: QUIVER_LIVE_E2E=1 makes its real Claude cases run.
    QUIVER_LIVE_E2E=1 "$PY" "$file" >"$log" 2>&1
    rc=$?
  else
    "$PY" "$file" >"$log" 2>&1
    rc=$?
  fi
  # grep -oE matches SUBSTRINGS, so the ", N skipped" form must come FIRST. Otherwise the
  # shorter "N passed, M failed" form wins and a skipped case reads like a passed one.
  tail=$(grep -oE '[0-9]+ passed, [0-9]+ failed, [0-9]+ skipped|[0-9]+ passed, [0-9]+ failed|[0-9]+ checks passed, [0-9]+ failed|PASS \(no-op\)|missing: .*' "$log" | tail -1)
  if [ "$rc" -eq 0 ]; then echo "OK    ${tail}"; else echo "FAIL  ${tail}  (see $log)"; FAILED+=("$name"); fi
done

echo "============================================================================"
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "ALL GREEN ✓"
  exit 0
fi
echo "FAILURES: ${FAILED[*]}"
exit 1
