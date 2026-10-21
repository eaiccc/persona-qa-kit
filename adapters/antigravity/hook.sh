#!/usr/bin/env bash
#
# Antigravity hook shim, for interactive use in the IDE / agy. Wired in
# .agents/hooks.json (hooks run with the working directory set to .agents/):
#
#   hook.sh drift   PreInvocation   persona re-injection (injectSteps)
#   hook.sh guard   PreToolUse      deny illegal run-artifact writes
#   hook.sh gate    Stop            review gate (decision: continue)
#
# Antigravity hooks print one JSON object and exit 0. PostToolUse can only
# return {}, so nothing is wired there; see docs/platforms.md. There is no
# SubagentStop and no agent identity in the payload, so role separation comes
# from adapters/antigravity/run_pipeline.py, which sets PQA_ROLE per agent.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "${1:-}" in
  gate)  OUT="$("$ROOT/bin/pqa" gate --stage auto --format agy)" ;;
  drift) OUT="$("$ROOT/bin/pqa" drift-tick --format agy)" ;;
  guard) OUT="$("$ROOT/bin/pqa" guard --format agy)" ;;
  *)     echo "usage: $0 gate|drift|guard" >&2; echo '{}'; exit 0 ;;
esac
STATUS=$?

if [ "$STATUS" -eq 0 ] && [ -n "$OUT" ]; then
  printf '%s\n' "$OUT"
  exit 0
fi

REASON="persona-qa-kit $1 hook could not run (pqa exited $STATUS); refusing until it can."
case "$1" in
  guard) printf '{"decision":"deny","reason":"%s"}\n' "$REASON" ;;
  gate)  printf '{"decision":"continue","reason":"%s"}\n' "$REASON" ;;
  *)     printf '{"injectSteps":[{"ephemeralMessage":"%s"}]}\n' "$REASON" ;;
esac
exit 0
