"""The mechanical half of drift detection.

DESIGN.md section 4 argues for a deterministic tripwire alongside the model-based
evaluator: when a time-starved parent's session log contains "state
synchronization", that is drift, and catching it by string match costs nothing
and cannot itself drift.

What this module does NOT do is catch behavioural drift. An agent can avoid
every forbidden term while still retrying a failed action nine times with a
patience the persona does not have. `persona-evaluator` is the only guard
against that, and it is a soft one. Do not read a clean drift report as evidence
the persona held.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = ["Violation", "term_pattern", "scan", "injection_steps", "cadence_gaps"]

# Fields whose text is written in the persona's own voice.
_SCANNED_FIELDS = ("expected", "observed", "felt", "note")


@dataclass(frozen=True)
class Violation:
    step: int
    term: str
    field: str
    quote: str

    def __str__(self) -> str:
        return f"step {self.step}: {self.field} contains {self.term!r} -- {self.quote!r}"


def term_pattern(term: str) -> re.Pattern[str]:
    """Compile one `never_uses` entry into a matcher.

    Matching rules, chosen for predictability over recall:

      * case-insensitive
      * whole words only, so "cache" does not fire inside "cached out" -- where
        "word" means ASCII letters, digits and underscore. Chinese and Japanese
        are written without spaces, so a CJK character next to a term is not a
        longer word: "OCR" must fire in "用OCR辨識" and "模型" in "這個模型很爛".
        Python's \\w counts CJK characters as word characters, which silently
        made the tripwire inert for any log not written in English
      * multi-word terms tolerate any run of non-word characters between words,
        so "race condition" matches "race-condition"
      * an optional trailing "s" or "es" is allowed, so listing "API" catches
        "APIs"

    Deliberately no stemming. A tripwire that fires on cases the card author did
    not anticipate gets muted, and a muted check is worse than no check. List
    variants explicitly in the card when you need them.
    """
    words = [w for w in re.split(r"\s+", term.strip()) if w]
    if not words:
        raise ValueError("empty never_uses term")
    body = r"\W+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9_]){body}(?:es|s)?(?![A-Za-z0-9_])", re.IGNORECASE)


def _texts(record: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for name in _SCANNED_FIELDS:
        value = record.get(name)
        if isinstance(value, str) and value:
            yield name, value
    action = record.get("action")
    if isinstance(action, dict):
        for name in ("target", "input"):
            value = action.get(name)
            if isinstance(value, str) and value:
                yield f"action.{name}", value


def _excerpt(text: str, match: re.Match[str], width: int = 60) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def scan(records: Iterable[dict[str, Any]], never_uses: Iterable[str]) -> list[Violation]:
    """Every forbidden-term hit across a session log."""
    patterns = [(term, term_pattern(term)) for term in never_uses]
    violations: list[Violation] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        step = record.get("step", 0)
        if not isinstance(step, int):
            step = 0
        for field_name, text in _texts(record):
            for term, pattern in patterns:
                match = pattern.search(text)
                if match:
                    violations.append(
                        Violation(step, term, field_name, _excerpt(text, match))
                    )
    return violations


def injection_steps(records: Iterable[dict[str, Any]]) -> list[int]:
    return sorted(
        r["step"]
        for r in records
        if isinstance(r, dict) and r.get("kind") == "persona_injection" and isinstance(r.get("step"), int)
    )


def cadence_gaps(
    records: Iterable[dict[str, Any]], every: int, last_step: int
) -> list[int]:
    """Steps where a persona re-injection was due but is missing.

    Checked mechanically rather than trusting that the hook ran, because a hook
    that silently stopped firing looks exactly like a session with no drift.
    """
    if every < 1:
        return []
    seen = set(injection_steps(records))
    expected = range(every, last_step + 1, every)
    # Allow the injection to land within a step either side of the boundary.
    return [step for step in expected if not (seen & {step - 1, step, step + 1})]
