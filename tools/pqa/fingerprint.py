"""Stable defect identity across runs.

A finding needs an identity that survives being described differently by a
different agent on a different day, or "this regressed" and "this has been open
for three weeks" cannot be answered (DESIGN.md section 2).

The inputs are deliberately coarse and all three are constrained values, not
free text:

    surface        a normalized route/screen id     (pattern-checked)
    action_class   one of 14 interaction kinds      (enum)
    symptom_class  one of 11 symptom kinds          (enum)

Titles, user_voice, and technical_hypothesis are excluded on purpose. They are
the fields most likely to be reworded between runs, and including them would
produce a new identity for every rewording -- which is exactly the fingerprint
churn the design document names as a detection signal.

The tradeoff is real and runs the other way too: two genuinely distinct defects
on the same surface, reached by the same action class, with the same symptom
class, collide into one fingerprint. That is an accepted cost. Under-splitting
produces a merged finding a human can pull apart; over-splitting produces a
regression report that silently never matches anything.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = ["VERSION", "compute", "for_finding", "verify", "normalize_surface"]

VERSION = "pqa1"

_SEPARATOR = "\x1f"
_SURFACE_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_surface(surface: str) -> str:
    """Reduce a surface to lowercase words joined by a single separator.

    Every run of non-alphanumeric characters is one separator, so all of

        "Settings / Notifications"   "settings/notifications"
        "settings-notifications"     "  settings   notifications  "

    hash identically. This normalization is the single biggest lever on churn.

    Note that it deliberately does not distinguish "/" from "-", which means
    "user/profile" and "user-profile" collide. That follows the tradeoff stated
    above: under-splitting produces a merged finding a human can pull apart,
    over-splitting produces a regression report that silently never matches.
    """
    parts = [p for p in _SURFACE_SPLIT.split(surface.strip().lower()) if p]
    return "/".join(parts)


def compute(surface: str, action_class: str, symptom_class: str) -> str:
    """The fingerprint for one (surface, action, symptom) triple."""
    payload = _SEPARATOR.join(
        [
            normalize_surface(surface),
            action_class.strip().lower(),
            symptom_class.strip().lower(),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{VERSION}:{digest}"


def for_finding(finding: dict[str, Any]) -> str:
    """Fingerprint of a finding dict, from its own fields."""
    try:
        return compute(
            finding["surface"], finding["action_class"], finding["symptom_class"]
        )
    except KeyError as exc:
        raise KeyError(f"finding is missing the fingerprint input {exc}") from exc


def verify(finding: dict[str, Any]) -> tuple[bool, str]:
    """Check a finding's stated fingerprint against the recomputed one.

    The review gate calls this on every finding. Without it, `fingerprint` is a
    field the proposing agent fills in freely, and a wrong value -- whether from
    carelessness or from wanting a finding to look new -- breaks cross-run
    matching in a way nobody notices, because a wrong fingerprint still looks
    like a fingerprint.
    """
    expected = for_finding(finding)
    return finding.get("fingerprint") == expected, expected
