"""Loading scenarios, and turning sealed perturbations into injected context.

A persona card says who is using the app. A scenario says what they came to do,
where they start, what counts as done, and what will happen to the session on
the way. Keeping the two apart means a run is persona x scenario: retargeting a
persona at a new task no longer means editing the card, which would change its
digest and break comparison with every earlier run of that persona.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlite
from .core import PqaError, digest_file, kit_root
from .jsonschema_lite import ValidationError, load_schema

__all__ = [
    "Scenario", "load", "list_ids", "validate_data", "scenarios_dir",
    "in_scope", "perturbation_block", "STEP_KINDS",
    "step_numbers", "last_step",
]

# Record kinds that constitute a step: one `observation`, or the `outcome` half
# of an intent/outcome pair. Everything that counts steps -- the budget, evidence
# resolution, re-injection cadence, perturbation timing -- counts these.
STEP_KINDS = frozenset({"observation", "outcome"})


def step_numbers(records: list[dict[str, Any]]) -> list[int]:
    return [
        r["step"] for r in records
        if isinstance(r, dict) and r.get("kind") in STEP_KINDS and isinstance(r.get("step"), int)
    ]


def last_step(records: list[dict[str, Any]]) -> int:
    steps = step_numbers(records)
    return max(steps) if steps else 0


@dataclass(frozen=True)
class Scenario:
    id: str
    version: str
    path: Path
    digest: str
    data: dict[str, Any]

    @property
    def success_criteria(self) -> list[str]:
        return list(self.data.get("success_criteria", []))

    @property
    def perturbations(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self.data.get("perturbations", [])]

    @property
    def in_scope_surfaces(self) -> list[str]:
        return list(self.data.get("in_scope_surfaces", []))

    def manifest_block(self) -> dict[str, Any]:
        """The copy sealed into run.json."""
        block: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "scenario_digest": self.digest,
            "task": self.data["task"].strip(),
            "entry": dict(self.data["entry"]),
            "perturbations": self.perturbations,
        }
        if self.data.get("preconditions"):
            block["preconditions"] = list(self.data["preconditions"])
        return block


def scenarios_dir() -> Path:
    return kit_root() / "scenarios"


def list_ids() -> list[str]:
    directory = scenarios_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def scenario_path(scenario: str) -> Path:
    """Accept either a scenario id or a path to a scenario file."""
    candidate = Path(scenario)
    if candidate.suffix in (".yaml", ".yml") and candidate.exists():
        return candidate.resolve()
    path = scenarios_dir() / f"{scenario}.yaml"
    if not path.exists():
        known = ", ".join(list_ids()) or "none found"
        raise PqaError(f"no scenario for {scenario!r} (available: {known})")
    return path.resolve()


def validate_data(data: Any) -> list[ValidationError]:
    validator = load_schema(kit_root() / "schemas" / "scenario.schema.json")
    return validator.validate(data)


def semantic_problems(data: dict[str, Any]) -> list[str]:
    """Rules the schema cannot express."""
    problems = []
    steps = [p.get("at_step") for p in data.get("perturbations", []) if isinstance(p, dict)]
    if any(b <= a for a, b in zip(steps, steps[1:]) if isinstance(a, int) and isinstance(b, int)):
        problems.append(
            f"perturbations must be in strictly increasing at_step order, got {steps}"
        )
    return problems


def load(scenario: str) -> Scenario:
    """Load a scenario, validating it against scenario.schema.json."""
    path = scenario_path(scenario)
    data = yamlite.load(path)
    if not isinstance(data, dict):
        raise PqaError(f"{path}: scenario must be a mapping")

    errors = [str(e) for e in validate_data(data)] + semantic_problems(data)
    if errors:
        detail = "\n".join(f"    - {e}" for e in errors[:10])
        raise PqaError(f"{path}: scenario is invalid:\n{detail}")

    if data.get("id") != path.stem:
        raise PqaError(
            f"{path}: scenario id {data.get('id')!r} does not match filename stem "
            f"{path.stem!r} -- runs reference scenarios by filename, so these must agree"
        )

    return Scenario(
        id=data["id"], version=data["version"], path=path,
        digest=digest_file(path), data=data,
    )


def in_scope(surface: str, prefixes: list[str]) -> bool:
    return any(surface == p or surface.startswith(p + "/") for p in prefixes)


# --------------------------------------------------------------------------
# injected context
# --------------------------------------------------------------------------

_HOW = {
    "background": "Send the app to the background (home gesture, or switch to another app){dur}, then go back to it.",
    "offline": "Turn the device's connection off (airplane mode, or the simulator's or browser's offline switch){dur}, then turn it back on.",
    "slow_network": "Throttle the connection to a very slow one{dur}, then restore it.",
    "rotate": "Rotate the device to the other orientation.",
    "interrupt": "Bring up something that takes over the screen -- an incoming call or a system alert{dur} -- then dismiss it.",
    "lock_screen": "Lock the device{dur}, then unlock it.",
    "kill": "Force-quit the app, then open it again.",
}


def _duration(perturbation: dict[str, Any]) -> str:
    raw = perturbation.get("for")
    if not raw:
        return ""
    match = re.match(r"^(\d+) ?(s|m|steps?)$", raw)
    if not match:
        return f" for {raw}"
    n, unit = int(match.group(1)), match.group(2)
    if unit == "s":
        return f" for {n} seconds"
    if unit == "m":
        return f" for {n} minute{'s' if n != 1 else ''}"
    until = perturbation["at_step"] + n
    return f" until you have recorded step {until}"


def perturbation_block(manifest_scenario: dict[str, Any], index: int) -> str:
    """The text the hook injects when perturbation `index` falls due.

    Addressed to user-agent as the operator of the device, not as the persona:
    the persona does not know this is coming and must not narrate it. What the
    persona notices afterwards goes in the log the ordinary way.
    """
    p = manifest_scenario["perturbations"][index]
    total = len(manifest_scenario["perturbations"])
    how = _HOW.get(p["do"], f"Apply '{p['do']}'{{dur}}.").format(dur=_duration(p))
    lines = [
        f"Something happens now (perturbation {index + 1} of {total}, sealed in "
        f"scenario {manifest_scenario['id']}, due after step {p['at_step']}).",
    ]
    if p.get("note"):
        lines.append(f"In the world: {p['note']}.")
    lines += [
        "",
        f"Do this to the device before your next step: {how}",
        "",
        "Then carry on as yourself. Do not mention this message in the log -- "
        "you did not get a warning. Record only what you notice, the way you "
        "would have noticed it.",
    ]
    return "\n".join(lines)
