#!/usr/bin/env bash
#
# Codex CLI hook shim. Wired in .codex/hooks.json:
#
#   hook.sh gate    Stop, SubagentStop        review gate, staged by agent_type
#   hook.sh drift   PostToolUse (Bash|apply_patch)  persona drift tripwire
#   hook.sh guard   PreToolUse  (Bash|apply_patch)  run-artifact and role guard
#
# Codex reads blocking decisions as JSON on stdout, and plain text on stdout is
# invalid for Stop and SubagentStop -- so pqa prints exactly one JSON object or
# nothing, and if pqa cannot run this prints the blocking object itself.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "${1:-}" in
  gate)  OUT="$("$ROOT/bin/pqa" gate --stage auto --format codex)" ;;
  drift) OUT="$("$ROOT/bin/pqa" drift-tick --format codex)" ;;
  guard) OUT="$("$ROOT/bin/pqa" guard --format codex)" ;;
  *)     echo "usage: $0 gate|drift|guard" >&2; exit 2 ;;
esac
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  [ -n "$OUT" ] && printf '%s\n' "$OUT"
  exit 0
fi

REASON="persona-qa-kit $1 hook could not run (pqa exited $STATUS); refusing until it can."
if [ "$1" = guard ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$REASON"
else
  printf '{"decision":"block","reason":"%s"}\n' "$REASON"
fi
exit 0
