"""Loading and validating persona cards."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlite
from .core import PqaError, digest_file, kit_root
from .jsonschema_lite import ValidationError, load_schema

__all__ = ["PersonaCard", "load", "list_ids", "validate_data", "summary_block"]


@dataclass(frozen=True)
class PersonaCard:
    id: str
    version: str
    path: Path
    digest: str
    data: dict[str, Any]

    @property
    def never_uses(self) -> list[str]:
        return list(self.data.get("vocabulary", {}).get("never_uses", []))

    @property
    def success_criteria(self) -> list[str]:
        return list(self.data.get("success_criteria", []))

    @property
    def out_of_scope(self) -> list[str]:
        return list(self.data.get("out_of_scope", []))


def personas_dir() -> Path:
    return kit_root() / "personas"


def list_ids() -> list[str]:
    directory = personas_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def card_path(persona: str) -> Path:
    """Accept either a persona id or a path to a card."""
    candidate = Path(persona)
    if candidate.suffix in (".yaml", ".yml") and candidate.exists():
        return candidate.resolve()
    path = personas_dir() / f"{persona}.yaml"
    if not path.exists():
        known = ", ".join(list_ids()) or "none found"
        raise PqaError(f"no persona card for {persona!r} (available: {known})")
    return path.resolve()


def validate_data(data: Any) -> list[ValidationError]:
    validator = load_schema(kit_root() / "schemas" / "persona.schema.json")
    return validator.validate(data)


def load(persona: str, strict: bool = True) -> PersonaCard:
    """Load a card, validating it against persona.schema.json."""
    path = card_path(persona)
    data = yamlite.load(path)
    if not isinstance(data, dict):
        raise PqaError(f"{path}: persona card must be a mapping")

    errors = validate_data(data)
    if errors and strict:
        detail = "\n".join(f"    - {e}" for e in errors[:10])
        raise PqaError(f"{path}: persona card is invalid:\n{detail}")

    if data.get("id") != path.stem:
        raise PqaError(
            f"{path}: card id {data.get('id')!r} does not match filename stem "
            f"{path.stem!r} -- runs reference cards by filename, so these must agree"
        )

    return PersonaCard(
        id=data["id"],
        version=data["version"],
        path=path,
        digest=digest_file(path),
        data=data,
    )


_READING = {
    "ignores-text": "You do not read on-screen text.",
    "skims": "You skim on-screen text.",
    "reads-carefully": "You read on-screen text carefully.",
}


def _retry_sentence(tolerance: int) -> str:
    if tolerance == 0:
        return "You do not retry a failed action at all."
    if tolerance == 1:
        return "You try a failed action once more, then stop."
    return f"You try a failed action up to {tolerance} more times, then stop."


def summary_block(card: PersonaCard, task: str | None = None) -> str:
    """The text re-injected into a session by the persona-drift-check hook.

    Kept short on purpose. The point of re-injection is recency, not completeness
    -- a long block competes with the transcript on the transcript's terms and
    loses. The behavioural dials are included because those are what drift first;
    the full card is not.

    `task` is the bound scenario's task. When there is one it replaces the
    card's goals: a re-injection that reminds the persona of a different errand
    than the one the run is about pulls the session off its scenario.
    """
    data = card.data
    behavior = data.get("behavior", {})
    lines = [
        f"You are {data['display_name']}.",
        "",
        data["summary"].strip(),
        "",
        f"Your patience is {behavior.get('patience', 'unspecified')}. "
        + _READING.get(behavior.get("reading", ""), "You skim on-screen text.")
        + " "
        + _retry_sentence(behavior.get("retry_tolerance", 1)),
        "",
        "You are trying to: " + (task.strip() if task else "; ".join(data.get("goals", []))),
        "",
        "You would never say: " + ", ".join(card.never_uses[:12]) + ".",
        "",
        "Record what you did, what you saw, and how you felt. "
        "You are not evaluating this software and you have no way to grade it.",
    ]
    return "\n".join(lines)
