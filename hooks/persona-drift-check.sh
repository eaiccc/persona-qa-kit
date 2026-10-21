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
#   2. string-match the records appended since the last check against the
#      card's `never_uses` list. On a hit, record a drift_breach in the log and
#      tell the agent to stop the session -- never to rephrase what it wrote.
#      The log is append-only, and a ledger of prefix digests makes an edit to
#      an earlier record visible to the gate
#
# The second only catches lexical drift. An agent can avoid every forbidden term
# and still troubleshoot with a patience the persona does not have; that is what
# persona-evaluator is for, and it is a softer guard.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# stdout carries the JSON envelope that re-injects the persona on cadence steps;
# stderr carries the explanation when a breach exits 2. Both pass straight
# through. The decisions live in tools/pqa/hook.py, where they are tested.
"$ROOT/bin/pqa" hook drift
STATUS=$?

[ "$STATUS" -eq 2 ] && exit 2
exit 0
