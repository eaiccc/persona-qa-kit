#!/usr/bin/env bash
#
# qa-review-gate -- refuses to let a run finish without a valid review.
#
# This is the load-bearing piece of the kit. DESIGN.md section 1:
#
#   Prompt-level instructions like "do not skip the review step" are requests.
#   A hook that refuses to advance the pipeline is a constraint. Anything you
#   actually need to be true should be a constraint.
#
# Wired as a Stop / SubagentStop hook in .claude/settings.json. Exit code 2
# blocks completion and returns stderr to the agent; the agent has no way to
# override it, because the decision is not the agent's to make.
#
# Fails closed on its own errors too: if this script cannot determine whether
# the review happened, it blocks. A gate that passes when it is broken is not a
# gate.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Hook input arrives on stdin as JSON. We do not need any of it -- the run
# directory is located on disk, not taken from the agent -- but draining it
# avoids a broken pipe upstream.
cat >/dev/null 2>&1 || true

resolve_run_dir() {
  if [ -n "${PQA_RUN_DIR:-}" ]; then
    printf '%s' "$PQA_RUN_DIR"
    return 0
  fi

  local base="${PQA_RUNS_DIR:-$ROOT/runs}"
  [ -d "$base" ] || return 1

  local newest=""
  for candidate in "$base"/*/; do
    [ -f "${candidate}run.json" ] || continue
    if [ -z "$newest" ] || [ "${candidate}run.json" -nt "${newest}run.json" ]; then
      newest="$candidate"
    fi
  done

  [ -n "$newest" ] || return 1
  printf '%s' "${newest%/}"
}

RUN_DIR="$(resolve_run_dir)" || {
  # No run in progress: this session was not a QA run, so there is nothing to
  # gate. This is the one case where passing is correct.
  exit 0
}

OUTPUT="$("$ROOT/bin/pqa" gate "$RUN_DIR" 2>&1)"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  printf '%s\n' "$OUTPUT"
  exit 0
fi

{
  printf 'The review gate refused this run (%s).\n\n' "$RUN_DIR"
  printf '%s\n\n' "$OUTPUT"
  printf 'Fix the artifacts listed above and let the gate run again.\n'
  printf 'Do not summarize the run as complete while this check is failing.\n'
} >&2

exit 2
