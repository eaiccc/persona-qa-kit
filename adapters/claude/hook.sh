#!/usr/bin/env bash
#
# Claude Code hook shim. Wired in .claude/settings.json:
#
#   hook.sh gate    Stop, SubagentStop   the review gate (DESIGN.md section 1)
#   hook.sh drift   PostToolUse          persona drift tripwire (section 4)
#   hook.sh guard   PreToolUse           optional; see docs/platforms.md
#
# The checks live in `pqa`, shared by every platform. All this adds is the
# Claude Code envelope and failing closed: exit 2 blocks and returns stderr to
# the agent, and if pqa itself cannot run, that blocks too. A gate that passes
# when it is broken is not a gate.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "${1:-}" in
  gate)  "$ROOT/bin/pqa" gate --stage auto --format claude ;;
  drift) "$ROOT/bin/pqa" drift-tick --format claude ;;
  guard) "$ROOT/bin/pqa" guard --format claude ;;
  *)     echo "usage: $0 gate|drift|guard" >&2; exit 2 ;;
esac
STATUS=$?

case "$STATUS" in
  0|2) exit "$STATUS" ;;
esac
printf 'persona-qa-kit %s hook could not run (pqa exited %s); blocking until it can.\n' \
  "$1" "$STATUS" >&2
exit 2
