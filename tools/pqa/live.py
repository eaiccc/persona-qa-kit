"""Checks that run while a session is still being recorded.

Two entry points, sharing one drift tick:

  tick()    what the persona-drift-check hook calls after every write. Finds
            vocabulary breaches, and on cadence appends a persona_injection
            record and returns the summary block to re-inject.

  record()  appends one session record after validating it, refusing it on a
            vocabulary breach. Platforms whose hooks cannot block after a tool
            ran (Antigravity's PostToolUse returns `{}` and nothing else) give
            user-agent this as its only way to write the log, which moves the
            tripwire from "fail the step after the write" to "refuse the write".

Neither is a model call, and neither can drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import drift, personas, validate
from .core import PqaError, RunDir, iso_now, read_json, read_jsonl

__all__ = ["TickResult", "tick", "breach_message", "record"]


@dataclass
class TickResult:
    card: personas.PersonaCard | None = None
    enforce: bool = True
    violations: list[drift.Violation] = field(default_factory=list)
    injection: str | None = None  # the summary block, when one was due

    @property
    def breached(self) -> bool:
        return bool(self.violations) and self.enforce


def _session_records(run: RunDir) -> list[dict[str, Any]]:
    if not run.session.exists():
        return []
    return [r for _, r in read_jsonl(run.session)[0] if isinstance(r, dict)]


def tick(run: RunDir) -> TickResult:
    """One live drift check. Appends a persona_injection record when one is due."""
    if not run.run_json.exists():
        return TickResult()

    manifest = read_json(run.run_json)
    control = manifest.get("drift_control", {})
    every = control.get("reinject_every_steps", 8)
    result = TickResult(
        card=personas.load(manifest["persona"]["id"]),
        enforce=control.get("enforce_never_uses", True),
    )

    records = _session_records(run)
    result.violations = drift.scan(records, result.card.never_uses)
    if result.breached:
        return result

    steps = [r["step"] for r in records if r.get("kind") == "observation"]
    last = max(steps) if steps else 0
    already = set(drift.injection_steps(records))

    if last > 0 and every > 0 and last % every == 0 and not (already & {last - 1, last, last + 1}):
        with run.session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "persona_injection",
                "step": last,
                "ts": iso_now(),
                "persona_id": result.card.id,
                "card_digest": result.card.digest,
            }) + "\n")
        result.injection = personas.summary_block(result.card)

    return result


def breach_message(result: TickResult) -> str:
    recent = result.violations[-3:]
    return (
        "Persona drift detected. "
        f"{result.card.data['display_name']} does not have these words:\n"
        + "\n".join(f"  - {v}" for v in recent)
        + "\n\nRewrite the affected session records in the persona's own "
        "vocabulary before continuing. What you are describing may well be "
        "real, but it is not what this persona would have noticed, and a "
        "drifted session's coverage claim is worthless."
    )


def record(run: RunDir, raw: str | dict[str, Any]) -> tuple[bool, str]:
    """Validate and append one session record. Returns (accepted, message).

    The message is what the recording agent reads back: either why the record
    was refused, or a confirmation -- with the persona summary appended when a
    re-injection fell due on this step, so the persona gets the recency of a
    tool result rather than a system prompt stated once at the start.
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

    if entry.get("kind") == "persona_injection":
        return False, "persona_injection records are written by the drift check, not by you"

    report = validate.check_instance(entry, "session-record.schema.json", "record")
    if not report.ok:
        return False, report.render("record refused")

    manifest = read_json(run.run_json)
    card = personas.load(manifest["persona"]["id"])
    enforce = manifest.get("drift_control", {}).get("enforce_never_uses", True)
    violations = drift.scan([entry], card.never_uses)
    if violations and enforce:
        return False, breach_message(TickResult(card=card, violations=violations))

    existing = _session_records(run)
    if any(r.get("kind") == "terminated" for r in existing):
        return False, "the session already has a 'terminated' record; nothing may follow it"
    if entry.get("kind") == "observation":
        taken = {r.get("step") for r in existing if r.get("kind") == "observation"}
        if entry.get("step") in taken:
            return False, f"step {entry.get('step')} is already recorded; steps are monotonic"

    with run.session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    try:
        result = tick(run)
    except PqaError as exc:  # pragma: no cover - the card loaded a moment ago
        return True, f"recorded step {entry.get('step')}; drift check could not run: {exc}"
    message = f"recorded {entry.get('kind')} step {entry.get('step')}"
    if result.injection:
        message += "\n\n" + result.injection
    return True, message
