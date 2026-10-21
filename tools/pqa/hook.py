"""What the two hooks actually decide, in Python so it can be tested.

The shell scripts in hooks/ only forward stdin here. Keeping the decisions in
one importable module means every branch below has a test that drives it with
the same JSON Claude Code would send, rather than being exercised only when a
real session happens to reach it.

Two hooks, two different jobs:

  drift   PostToolUse, after every Write/Edit/Bash. Checks only the records
          appended since it last ran, so an old breach does not keep firing at
          whichever agent happens to call a tool next. Records the breach in the
          log rather than asking anyone to remove it, re-injects the persona on
          cadence, and extends the append-only ledger.

  gate    Stop / SubagentStop. Runs the gate up to the stage the stopping agent
          is responsible for -- user-agent answers for the session log, not for
          a review that cannot exist yet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from . import drift, gate, ledger, personas
from .core import (
    PqaError, RunDir, active_run, clear_active, iso_now, read_json,
)

__all__ = ["Result", "drift_hook", "gate_hook", "stage_for", "ROLE_STAGES"]

EXIT_OK = 0
EXIT_BLOCK = 2

# Which stage each pipeline role is answerable for when it stops.
ROLE_STAGES = {
    "user-agent": "session",
    "qa-agent": "candidates",
    "qa-reviewer": "review",
    "persona-evaluator": "final",
    "qa-orchestrator": "final",
}

# A gate that can block forever turns one unfixable artifact into a session
# that never ends. After this many consecutive refusals at the same stage the
# hook lets the agent stop, tells the user so, and leaves the run failing:
# `pqa gate` is deterministic and still says no.
DEFAULT_MAX_BLOCKS = 3


@dataclass
class Result:
    code: int = EXIT_OK
    stdout: str = ""
    stderr: str = ""


def _payload(stdin_text: str) -> dict[str, Any]:
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _role(payload: dict[str, Any]) -> str | None:
    agent = payload.get("agent_type")
    if not isinstance(agent, str) or not agent:
        return None
    # Plugin-scoped agents arrive as "plugin:agent-name".
    return agent.rsplit(":", 1)[-1]


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------

def drift_hook(stdin_text: str = "") -> Result:
    run_path = active_run()
    if run_path is None:
        return Result()
    try:
        return drift_tick(RunDir.open(run_path))
    except PqaError as exc:
        # A drift check that cannot run must not wedge every tool call in the
        # session. The gate re-checks all of this, and fails closed, at the end.
        return Result(EXIT_OK, "", f"persona-drift-check could not run: {exc}")


def drift_tick(run: RunDir) -> Result:
    if not run.run_json.exists() or not run.session.exists():
        return Result()

    manifest = read_json(run.run_json)
    control = manifest.get("drift_control", {})
    every = control.get("reinject_every_steps", 8)
    enforce = control.get("enforce_never_uses", True)
    card = personas.load(manifest["persona"]["id"])

    lines = ledger.session_lines(run.session)
    entries = [e for e in ledger.read(run.ledger) if e.lines >= 0]
    seen = entries[-1].lines if entries else 0
    messages: list[str] = []
    code = EXIT_OK

    if entries and (seen > len(lines) or ledger.prefix_digest(lines, seen) != entries[-1].digest):
        messages.append(
            "session.jsonl was edited, not appended to. Records already in the "
            "log are evidence and must stay exactly as written; this run will "
            "fail the review gate. Do not try to restore the old text -- "
            "append from here on."
        )
        code = EXIT_BLOCK
        # Which records are new is unknowable once old ones changed. Scan only
        # past the old high-water mark so earlier breaches are not re-reported.
        seen = min(seen, len(lines))

    fresh = []
    for raw in lines[seen:]:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            fresh.append(record)

    violations = drift.scan(fresh, card.never_uses)
    appended: list[dict[str, Any]] = []
    for violation in violations:
        appended.append({
            "kind": "drift_breach",
            "step": violation.step,
            "ts": iso_now(),
            "term": violation.term[:60],
            "field": violation.field[:40],
            "quote": violation.quote[:300],
        })
    if violations and enforce:
        code = EXIT_BLOCK
        messages.append(
            f"Persona drift at step {min(v.step for v in violations)}. "
            f"{card.data['display_name']} does not have these words:\n"
            + "\n".join(f"  - {v}" for v in violations[:3])
            + "\n\nThe breach has been recorded in the session log. Do not edit "
            "or rephrase the record that contains it -- the log is append-only "
            "and changing it fails the run. Stop the session now: append one "
            "'terminated' record with reason \"persona_drift\" and write nothing "
            "else. Everything before this step is still usable evidence; "
            "nothing after it would be this persona's."
        )

    all_records = []
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            all_records.append(record)
    all_records.extend(appended)

    stdout = ""
    steps = [r["step"] for r in all_records
             if r.get("kind") == "observation" and isinstance(r.get("step"), int)]
    last = max(steps) if steps else 0
    due = (last // every) * every if every >= 1 else 0
    already = set(drift.injection_steps(all_records))
    # Due at a multiple of the cadence, injected as soon as the log reaches it.
    # Matching only `last % every == 0` missed the boundary entirely whenever a
    # single write crossed it, and then waited a whole further interval.
    if due > 0 and not any(due - 1 <= s <= last + 1 for s in already):
        appended.append({
            "kind": "persona_injection",
            "step": last,
            "ts": iso_now(),
            "persona_id": card.id,
            "card_digest": card.digest,
        })
        if code == EXIT_OK:
            stdout = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": personas.summary_block(card),
                }
            })
        else:
            messages.append(personas.summary_block(card))

    if appended:
        text = run.session.read_bytes()
        with run.session.open("a", encoding="utf-8") as handle:
            if text and not text.endswith(b"\n"):
                handle.write("\n")
            for record in appended:
                handle.write(json.dumps(record) + "\n")

    if appended or not entries or len(lines) != entries[-1].lines:
        ledger.append(run.ledger, run.session)

    return Result(code, stdout, "\n\n".join(messages))


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def stage_for(payload: dict[str, Any], run: RunDir) -> str | None:
    """The stage to gate, or None when this stop is not the pipeline's business."""
    role = _role(payload)
    event = payload.get("hook_event_name")

    if role is not None:
        stage = ROLE_STAGES.get(role)
        if stage is None:
            # Some other subagent finished while a run was active -- a search
            # helper, say. It produced none of the run's artifacts.
            return None
    elif event == "SubagentStop":
        # Older harnesses do not say which agent stopped. Gate whatever has been
        # produced so far, which is weaker -- it cannot tell that an agent
        # failed to write its file -- and the final Stop still checks it all.
        present = [
            (run.evaluation, "final"), (run.reviewed, "review"),
            (run.candidates, "candidates"), (run.session, "session"),
        ]
        stage = next((s for p, s in present if p.exists()), None)
    else:
        stage = "final"

    if stage == "final" and not run.session.exists():
        # The run is sealed but has not started. Nothing has been claimed yet,
        # so there is nothing to gate -- and blocking here would wedge the very
        # session that is about to dispatch the pipeline.
        return None
    return stage


def _max_blocks() -> int:
    try:
        return max(1, int(os.environ.get("PQA_GATE_MAX_BLOCKS", DEFAULT_MAX_BLOCKS)))
    except ValueError:
        return DEFAULT_MAX_BLOCKS


def _bump(run: RunDir, stage: str, reset: bool) -> int:
    try:
        counts = read_json(run.gate_blocks) if run.gate_blocks.exists() else {}
    except PqaError:
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    counts[stage] = 0 if reset else int(counts.get(stage, 0)) + 1
    run.gate_blocks.write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
    return counts[stage]


def gate_hook(stdin_text: str = "") -> Result:
    payload = _payload(stdin_text)
    run_path = active_run()
    if run_path is None:
        # No run in progress: this session was not a QA run, so there is
        # nothing to gate. This is the one case where passing is correct.
        return Result()

    try:
        run = RunDir.open(run_path)
    except PqaError as exc:
        return Result(EXIT_BLOCK, "", f"The review gate cannot find the active run: {exc}")
    if not run.run_json.exists():
        return Result()

    stage = stage_for(payload, run)
    if stage is None:
        return Result()

    try:
        report = gate.run_gate(run, stage)
    except Exception as exc:  # fail closed: a broken gate must not pass
        return Result(EXIT_BLOCK, "", f"The review gate crashed and so refuses this run: {exc!r}")

    title = f"review gate ({stage}): {run.path.name}"
    rendered = report.render(title, color=False)

    if report.ok:
        _bump(run, stage, reset=True)
        if stage == "final":
            clear_active(run.path)
        return Result(EXIT_OK, rendered, "")

    count = _bump(run, stage, reset=False)
    limit = _max_blocks()
    if count > limit:
        return Result(EXIT_OK, json.dumps({
            "systemMessage": (
                f"persona-qa-kit: the review gate refused {run.path.name} at stage "
                f"'{stage}' {limit} times in a row and is now letting the agent "
                "stop so the session does not loop. The run is NOT valid: "
                f"`pqa gate {run.path}` still fails. Do not report its findings."
            )
        }), "")

    who = _role(payload) or "this agent"
    stderr = (
        f"The review gate refused this run at stage '{stage}' ({run.path}).\n\n"
        f"{rendered}\n\n"
        f"These are the artifacts {who} is responsible for. Fix them and stop "
        "again; do not summarize the run as complete while this check fails."
    )
    return Result(EXIT_BLOCK, "", stderr)
