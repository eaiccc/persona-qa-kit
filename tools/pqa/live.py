"""Writing the session log through a tool instead of a file.

Platforms whose hooks cannot react after a tool ran (Antigravity's PostToolUse
returns `{}` and nothing else) give user-agent `record()` as its only way to
write the log. It validates each record before appending it, then runs the same
drift tick the hooks run, so a breach is handled exactly as it is everywhere
else: the record is written, the breach is recorded next to it, and the agent
is told to stop.

What it deliberately does not do is refuse a record for its vocabulary. Refusing
the write invites the agent to try again with a synonym, and the drift it just
showed would then leave no trace in the log -- the evaluator would grade a clean
session that was not clean. Structural problems (schema, ordering, writing after
termination) are still refused, because those records were never evidence.
"""

from __future__ import annotations

import json
from typing import Any

from . import hook, scenarios, validate
from .core import RunDir, iso_now, read_jsonl

__all__ = ["record"]

# Written by the drift tick, never by user-agent.
_HOOK_ONLY = ("persona_injection", "drift_breach", "perturbation")


def _session_records(run: RunDir) -> list[dict[str, Any]]:
    if not run.session.exists():
        return []
    return [r for _, r in read_jsonl(run.session)[0] if isinstance(r, dict)]


def record(run: RunDir, raw: str | dict[str, Any]) -> tuple[bool, str]:
    """Validate and append one session record. Returns (accepted, message).

    The message is what the recording agent reads back: why the record was
    refused, the drift notice when it breached, or a confirmation -- with the
    persona summary or a due perturbation appended, so they arrive with the
    recency of a tool result rather than a system prompt stated once at the start.
    """
    if not run.run_json.exists():
        return False, f"{run.run_json} does not exist; no run is in progress"
    if not run.lock.exists():
        return False, (
            "run.json is not sealed yet. The session may not start before the "
            "stopping conditions are fixed (DESIGN.md section 3)."
        )
    if run.candidates.exists():
        return False, (
            "findings.candidates.json already exists, so the session log is "
            "frozen: qa-agent has read it, and changing it now would detach "
            "the candidates from their evidence."
        )

    try:
        entry = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except json.JSONDecodeError as exc:
        return False, f"not valid JSON: {exc.msg} at column {exc.colno}"
    if not isinstance(entry, dict):
        return False, "a session record is one JSON object"
    entry.setdefault("ts", iso_now())

    if entry.get("kind") in _HOOK_ONLY:
        return False, f"{entry.get('kind')} records are written by the drift check, not by you"

    report = validate.check_instance(entry, "session-record.schema.json", "record")
    if not report.ok:
        return False, report.render("record refused")

    existing = _session_records(run)
    if any(r.get("kind") == "terminated" for r in existing):
        return False, "the session already has a 'terminated' record; nothing may follow it"
    if entry.get("kind") in scenarios.STEP_KINDS:
        taken = set(scenarios.step_numbers(existing))
        if entry.get("step") in taken:
            return False, f"step {entry.get('step')} is already recorded; steps are monotonic"

    with run.session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    tick = hook.drift_tick(run)
    label = f"{entry.get('kind')} step {entry.get('step')}"
    if tick.blocked:
        return False, "\n\n".join(
            [f"Recorded {label} -- and it breached the persona's vocabulary."]
            + tick.messages + tick.context
        )
    message = f"recorded {label}"
    if tick.context:
        message += "\n\n" + "\n\n---\n\n".join(tick.context)
    return True, message
