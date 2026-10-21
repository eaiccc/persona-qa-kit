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
# Gated by stage. SubagentStop names the agent that stopped, and each role
# answers only for what it produces: user-agent for the session log, qa-agent
# for the candidates, qa-reviewer for the review, persona-evaluator and the
# final Stop for everything. Gating user-agent on a review that cannot exist
# yet would not enforce the pipeline's order -- it would stop the pipeline.
#
# The run is the one `pqa seal` marked active (or PQA_RUN_DIR, if the harness
# was launched with it). No active run means this session is not a QA run.
#
# Fails closed on its own errors too: if this script cannot determine whether
# the review happened, it blocks. A gate that passes when it is broken is not a
# gate. It does give up after a few refusals in a row, so one unfixable
# artifact cannot loop a session forever -- `pqa gate` still fails the run.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The hook input (which agent stopped, and why) goes straight through on stdin.
# All decisions live in tools/pqa/hook.py, where they are tested.
"$ROOT/bin/pqa" hook gate
STATUS=$?

# 0 and 2 are answers. Anything else means the gate could not run, and a gate
# that cannot run must not let the run through.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 2 ]; then
  echo "The review gate could not run (exit $STATUS) and so refuses this run." >&2
  exit 2
fi
exit "$STATUS"
