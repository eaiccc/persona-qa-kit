"""What the hooks actually decide, in Python so it can be tested.

The platform shims in adapters/ only forward stdin here, naming their format.
The decisions are platform-neutral; hookio turns each one into the envelope
that platform reads (exit 2 for Claude Code, a JSON decision for Codex and
Antigravity). Keeping them in one importable module means every branch below
has a test that drives it with the JSON a harness would send, rather than
being exercised only when a real session happens to reach it.

Two hooks, two different jobs:

  drift   PostToolUse, after every Write/Edit/Bash. Checks only the records
          appended since it last ran, so an old breach does not keep firing at
          whichever agent happens to call a tool next. Records the breach in the
          log rather than asking anyone to remove it, re-injects the persona on
          cadence, applies the scenario's sealed perturbations as they fall
          due, and extends the append-only ledger.

  gate    Stop / SubagentStop. Runs the gate up to the stage the stopping agent
          is responsible for -- user-agent answers for the session log, not for
          a review that cannot exist yet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import drift, gate, hookio, ledger, personas, roles, scenarios
from .core import (
    PqaError, RunDir, active_run, clear_active, iso_now, read_json,
)

__all__ = ["Tick", "drift_tick", "drift_hook", "gate_hook", "stage_for"]

# A gate that can block forever turns one unfixable artifact into a session
# that never ends. After this many consecutive refusals at the same stage the
# hook lets the agent stop, tells the user so, and leaves the run failing:
# `pqa gate` is deterministic and still says no.
DEFAULT_MAX_BLOCKS = 3


@dataclass
class Tick:
    """One drift check's verdict, before any platform has formatted it."""

    blocked: bool = False
    messages: list[str] = field(default_factory=list)  # why it blocked
    context: list[str] = field(default_factory=list)   # what to put in front of the model


def _payload(stdin_text: str) -> dict[str, Any]:
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _event(payload: dict[str, Any], default: str) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or default)


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------

def drift_hook(
    stdin_text: str = "", fmt: str = "claude", run_dir: Path | None = None,
) -> hookio.HookOutcome:
    """The drift tick as a hook. Claude Code and Codex call it after a write
    (PostToolUse); Antigravity, whose PostToolUse can neither block nor inject,
    calls it before each model call (PreInvocation)."""
    payload = _payload(stdin_text)
    event = _event(payload, "PreInvocation" if fmt == "agy" else "PostToolUse")
    run_path = run_dir or active_run()
    if run_path is None:
        return hookio.allow(fmt)
    try:
        tick = drift_tick(RunDir.open(run_path))
    except PqaError as exc:
        # A drift check that cannot run must not wedge every tool call in the
        # session. The gate re-checks all of this, and fails closed, at the end.
        outcome = hookio.allow(fmt)
        outcome.stderr = f"persona-drift-check could not run: {exc}"
        return outcome
    if tick.blocked:
        # A block discards the context channel, so the context rides along.
        return hookio.block(fmt, event, "\n\n".join(tick.messages + tick.context))
    if tick.context:
        return hookio.inject(fmt, event, "\n\n---\n\n".join(tick.context))
    return hookio.allow(fmt)


def drift_tick(run: RunDir) -> Tick:
    if not run.run_json.exists() or not run.session.exists():
        return Tick()

    manifest = read_json(run.run_json)
    control = manifest.get("drift_control", {})
    every = control.get("reinject_every_steps", 8)
    enforce = control.get("enforce_never_uses", True)
    card = personas.load(manifest["persona"]["id"])

    lines = ledger.session_lines(run.session)
    entries = [e for e in ledger.read(run.ledger) if e.lines >= 0]
    seen = entries[-1].lines if entries else 0
    messages: list[str] = []
    blocked = False

    if entries and (seen > len(lines) or ledger.prefix_digest(lines, seen) != entries[-1].digest):
        messages.append(
            "session.jsonl was edited, not appended to. Records already in the "
            "log are evidence and must stay exactly as written; this run will "
            "fail the review gate. Do not try to restore the old text -- "
            "append from here on."
        )
        blocked = True
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
        blocked = True
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

    sealed = manifest.get("scenario")
    task = sealed["task"] if sealed else None
    context: list[str] = []
    last = scenarios.last_step(all_records)
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
        context.append(personas.summary_block(card, task))

    # Sealed perturbations fall due once the step they follow is on the log.
    # Read from run.json, never from the scenario file: what is injected is
    # what was sealed. A late one is still applied, and recorded at the step
    # it actually landed on, so the gate can see it was late. None are applied
    # once the session has stopped for drift -- there is no next step.
    if sealed and not (violations and enforce):
        done = {r.get("index") for r in all_records if r.get("kind") == "perturbation"}
        for index, perturbation in enumerate(sealed.get("perturbations", [])):
            if index in done or last < perturbation["at_step"]:
                continue
            record = {
                "kind": "perturbation",
                "step": last,
                "ts": iso_now(),
                "index": index,
                "at_step": perturbation["at_step"],
                "do": perturbation["do"],
                "scenario_id": sealed["id"],
                "scenario_digest": sealed["scenario_digest"],
            }
            if perturbation.get("for"):
                record["for"] = perturbation["for"]
            appended.append(record)
            context.append(scenarios.perturbation_block(sealed, index))

    if appended:
        text = run.session.read_bytes()
        with run.session.open("a", encoding="utf-8") as handle:
            if text and not text.endswith(b"\n"):
                handle.write("\n")
            for record in appended:
                handle.write(json.dumps(record) + "\n")

    if appended or not entries or len(lines) != entries[-1].lines:
        ledger.append(run.ledger, run.session)

    return Tick(blocked, messages, context)


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def stage_for(payload: dict[str, Any], run: RunDir) -> str | None:
    """The stage to gate, or None when this stop is not the pipeline's business."""
    override = os.environ.get("PQA_STAGE")
    if override:
        return override

    role_name = hookio.agent_type(payload)  # agent_type, Codex's agent_role, or $PQA_ROLE
    event = _event(payload, "Stop")

    if role_name is not None:
        stage = roles.stage_for(role_name)
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


def _give_up(fmt: str, message: str) -> hookio.HookOutcome:
    """Let the agent stop, and make sure a person hears why."""
    if fmt == "claude":
        return hookio.HookOutcome(json.dumps({"systemMessage": message}))
    outcome = hookio.allow(fmt)
    outcome.stderr = message
    return outcome


def gate_hook(
    stdin_text: str = "", fmt: str = "claude",
    run_dir: Path | None = None, stage: str | None = None,
) -> hookio.HookOutcome:
    """The review gate as a Stop / SubagentStop hook.

    Fails closed: if the gate cannot determine whether the review happened, it
    blocks. A gate that passes when it is broken is not a gate. The one case
    where passing is correct is "no run in progress", because then this session
    was not a QA run and there is nothing to gate.
    """
    payload = _payload(stdin_text)
    event = _event(payload, "Stop")
    run_path = run_dir or active_run()
    if run_path is None:
        return hookio.allow(fmt)

    try:
        run = RunDir.open(run_path)
    except PqaError as exc:
        return hookio.block(fmt, event, f"The review gate cannot find the active run: {exc}")
    if not run.run_json.exists():
        return hookio.allow(fmt)

    if stage in (None, "auto"):
        stage = stage_for(payload, run)
    if stage is None:
        return hookio.allow(fmt)

    try:
        report = gate.run_gate(run, stage)
    except Exception as exc:  # fail closed: a broken gate must not pass
        return hookio.block(fmt, event, f"The review gate crashed and so refuses this run: {exc!r}")

    title = f"review gate ({stage}): {run.path.name}"
    rendered = report.render(title, color=False)

    if report.ok:
        _bump(run, stage, reset=True)
        if stage == "final":
            clear_active(run.path)
        return hookio.allow(fmt, rendered)

    count = _bump(run, stage, reset=False)
    limit = _max_blocks()
    if count > limit:
        return _give_up(fmt, (
            f"persona-qa-kit: the review gate refused {run.path.name} at stage "
            f"'{stage}' {limit} times in a row and is now letting the agent "
            "stop so the session does not loop. The run is NOT valid: "
            f"`pqa gate {run.path}` still fails. Do not report its findings."
        ))

    who = hookio.agent_type(payload) or "this agent"
    reason = (
        f"The review gate refused this run at stage '{stage}' ({run.path}).\n\n"
        f"{rendered}\n\n"
        f"These are the artifacts {who} is responsible for. Fix them and stop "
        "again. Do not summarize the run as complete while this check fails.\n"
        "This is not an instruction the agent can decline -- see DESIGN.md "
        "section 1 on constraints versus requests."
    )
    return hookio.block(fmt, event, reason)
