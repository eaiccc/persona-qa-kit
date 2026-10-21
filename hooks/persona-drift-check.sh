#!/usr/bin/env bash
#
# persona-drift-check -- re-injects the persona on cadence, and stops the
# session when the agent's vocabulary stops being the persona's.
#
# DESIGN.md section 4: the persona is stated once, while the transcript grows
# every turn and is full of methodical exploration. Recency and volume favour
# the transcript, so the agent regresses toward "helpful, thorough, technically
# literate" -- the opposite of most personas worth testing.
#
# Two mechanisms, both cheap and neither of them a model call:
#
#   1. every N steps, reinsert the persona summary and record that it happened,
#      so the persona gets some of the recency the transcript has been getting
#   2. string-match the session log against the card's `never_uses` list, and
#      fail the step on a hit
#
# The second only catches lexical drift. An agent can avoid every forbidden term
# and still troubleshoot with a patience the persona does not have; that is what
# persona-evaluator is for, and it is a softer guard.
#
# The same tick delivers a bound scenario's perturbations: once the step a
# perturbation was sealed to is on the log, it appends a `perturbation` record
# and hands the instruction back in the same JSON envelope. The gate checks
# every due perturbation was recorded on time (docs/writing-scenarios.md).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cat >/dev/null 2>&1 || true

RUN_DIR="${PQA_RUN_DIR:-}"
if [ -z "$RUN_DIR" ]; then
  base="${PQA_RUNS_DIR:-$ROOT/runs}"
  [ -d "$base" ] || exit 0
  for candidate in "$base"/*/; do
    [ -f "${candidate}run.json" ] || continue
    if [ -z "$RUN_DIR" ] || [ "${candidate}run.json" -nt "$RUN_DIR/run.json" ]; then
      RUN_DIR="${candidate%/}"
    fi
  done
fi

[ -n "$RUN_DIR" ] || exit 0
[ -f "$RUN_DIR/session.jsonl" ] || exit 0

# stderr is deliberately left unredirected: on a vocabulary breach drift-tick
# writes its explanation there, which is exactly where an exit-2 hook's message
# is read from. Only stdout is captured, because that carries the JSON envelope.
OUTPUT="$("$ROOT/bin/pqa" drift-tick "$RUN_DIR")"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  exit 2
fi

# On cadence steps, and on steps a scenario perturbation falls due, this is the
# JSON envelope carrying the persona summary and/or the perturbation back into
# context. On every other step it is empty and nothing happens.
[ -n "$OUTPUT" ] && printf '%s\n' "$OUTPUT"
exit 0
