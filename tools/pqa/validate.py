"""Schema validation for every artifact in the pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import PqaError, Report, RunDir, kit_root, read_json, read_jsonl
from .jsonschema_lite import SchemaError, Validator, load_schema

__all__ = [
    "VERDICT_KEYS", "check_instance", "check_session", "check_persona_file",
    "check_scenario_file", "check_run_dir", "self_check",
]

# Keys that would let `user-agent` grade its own session. The schema already
# rejects them via `additionalProperties: false`; this list exists so the error
# message explains the design decision instead of reporting an unknown property.
#
# DESIGN.md section 1: the agent cannot certify anything because it has nowhere
# to write a certification.
VERDICT_KEYS = frozenset(
    {
        "severity", "priority", "pass", "passed", "fail", "failed",
        "status", "verdict", "result", "outcome", "score", "rating", "grade",
        "bug", "bugs", "defect", "defects", "issue", "issues",
        "recommendation", "recommendations", "suggestion", "suggestions",
        "root_cause", "diagnosis", "conclusion", "assessment", "quality",
        "works", "broken", "ok",
    }
)

_VERDICT_HINT = (
    "user-agent records; it does not judge. Move this into the observation itself "
    "-- 'the list was empty' rather than a status field -- and let qa-agent "
    "propose a defect from it. See DESIGN.md section 1."
)


def _cache() -> dict[str, Validator]:
    if not hasattr(_cache, "store"):
        _cache.store = {}  # type: ignore[attr-defined]
    return _cache.store  # type: ignore[attr-defined]


def validator_for(schema_name: str) -> Validator:
    store = _cache()
    if schema_name not in store:
        path = kit_root() / "schemas" / schema_name
        store[schema_name] = load_schema(path)
    return store[schema_name]


# --------------------------------------------------------------------------
# generic instance checking
# --------------------------------------------------------------------------

def check_instance(data: Any, schema_name: str, label: str) -> Report:
    report = Report()
    report.checks_run += 1
    for error in validator_for(schema_name).validate(data):
        report.error(f"{label}{error.path}", error.message)
    return report


def _find_verdict_keys(node: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key.lower() in VERDICT_KEYS:
                found.append((here, key))
            found.extend(_find_verdict_keys(value, here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(_find_verdict_keys(item, f"{path}/{i}"))
    return found


# --------------------------------------------------------------------------
# session log
# --------------------------------------------------------------------------

def check_session(path: str | Path) -> tuple[Report, list[dict[str, Any]]]:
    """Validate session.jsonl line by line, then as a whole."""
    report = Report()
    records, parse_problems = read_jsonl(path)
    report.errors.extend(parse_problems)

    name = Path(path).name
    validator = validator_for("session-record.schema.json")
    parsed: list[dict[str, Any]] = []

    for line_no, record in records:
        report.checks_run += 1
        label = f"{name}:{line_no}"

        for where, key in _find_verdict_keys(record):
            report.error(
                f"{label}{where}",
                f"the session log has no field for a verdict, and {key!r} is one",
                _VERDICT_HINT,
            )

        errors = validator.validate(record)
        for error in errors:
            report.error(f"{label}{error.path}", error.message)
        if isinstance(record, dict):
            parsed.append(record)

    report.extend(_check_session_shape(parsed, name))
    return report, parsed


def _check_session_shape(records: list[dict[str, Any]], name: str) -> Report:
    report = Report()
    if not records:
        report.error(name, "session log is empty")
        return report

    report.checks_run += 3

    from .scenarios import step_numbers

    steps = step_numbers(records)
    if steps != sorted(steps):
        report.error(name, "observation steps are not monotonically increasing")
    if len(set(steps)) != len(steps):
        duplicates = sorted({s for s in steps if steps.count(s) > 1})
        report.error(
            name,
            f"duplicate observation step numbers: {duplicates}",
            "evidence_refs point at steps, so step numbers must be unique",
        )

    terminals = [r for r in records if r.get("kind") == "terminated"]
    if not terminals:
        report.error(
            name,
            "session log has no 'terminated' record",
            "a run without a recorded stopping reason cannot be compared with "
            "other runs -- see DESIGN.md section 3",
        )
    elif records[-1].get("kind") != "terminated":
        report.error(name, "the 'terminated' record is not the last line")
    elif len(terminals) > 1:
        report.error(name, f"session log has {len(terminals)} 'terminated' records, expected 1")

    report.extend(_check_two_phase(records, name))
    return report


def _check_two_phase(records: list[dict[str, Any]], name: str) -> Report:
    """Ordering for intent/outcome steps, checked only when they are used.

    The point of an `intent` record is that the expectation exists before the
    result does. Its schema cannot see the order records were written in; this
    can. An outcome without a prior intent, an intent written after its outcome,
    or an intent that two steps' records were interleaved around are all ways
    the expectation could have been written to fit the result.
    """
    report = Report()
    if not any(r.get("kind") in ("intent", "outcome") for r in records):
        return report

    report.checks_run += 4
    step_kinds = ("observation", "intent", "outcome")
    open_intent: dict[str, Any] | None = None
    intents_seen: set[int] = set()
    observation_steps = {r.get("step") for r in records if r.get("kind") == "observation"}

    for i, record in enumerate(records):
        kind = record.get("kind")
        if kind not in step_kinds:
            continue
        step = record.get("step")
        where = f"{name}[step {step}]"

        if kind == "intent":
            if open_intent is not None:
                report.error(
                    f"{name}[step {open_intent.get('step')}]",
                    f"intent has no outcome before the next step's records begin "
                    f"(step {step})",
                    "write the outcome of one step before starting the next",
                )
            if step in intents_seen:
                report.error(
                    where,
                    "more than one intent for this step",
                    "an expectation is committed once. Writing a second one is "
                    "revising it, which is what the intent record exists to stop",
                )
            if step in observation_steps:
                report.error(
                    where,
                    "step is recorded both as an observation and as intent/outcome",
                    "use one form per step",
                )
            intents_seen.add(step)
            open_intent = record
            continue

        if kind == "outcome":
            if "expected" in record:
                report.error(
                    where,
                    "outcome carries `expected`",
                    "the expectation was committed in this step's intent record "
                    "and cannot be restated once the result is known",
                )
            if open_intent is None or open_intent.get("step") != step:
                if step in intents_seen:
                    report.error(
                        where,
                        "outcome is not directly preceded by its own intent",
                        "another step's records were written between them",
                    )
                else:
                    report.error(
                        where,
                        "outcome has no intent written before it",
                        "the intent (with `expected`) must be on the log before "
                        "the outcome -- an expectation written afterwards is a "
                        "description of the result",
                    )
                open_intent = None
                continue
            if str(open_intent.get("ts", "")) > str(record.get("ts", "")):
                report.error(
                    where,
                    f"intent is timestamped after its outcome "
                    f"({open_intent.get('ts')} > {record.get('ts')})",
                )
            planned = (open_intent.get("action") or {}).get("class")
            actual = (record.get("action") or {}).get("class")
            if planned and actual and planned != actual:
                report.warn(
                    where,
                    f"intent planned a {planned!r} action but the outcome records {actual!r}",
                    "the expectation was about a different action than the one "
                    "taken, so it says nothing about this step's result",
                )
            open_intent = None
            continue

        # an observation in the middle of an open intent
        if open_intent is not None:
            report.error(
                f"{name}[step {open_intent.get('step')}]",
                f"intent has no outcome before the next step's records begin (step {step})",
            )
            open_intent = None

    if open_intent is not None and records[-1].get("kind") != "terminated":
        # A trailing intent followed by termination is allowed: the persona
        # decided what to do next and then gave up instead. Without the
        # termination it is a step that was started and never recorded.
        report.error(f"{name}[step {open_intent.get('step')}]", "intent has no outcome")
    return report


# --------------------------------------------------------------------------
# persona cards
# --------------------------------------------------------------------------

def check_persona_file(path: str | Path) -> Report:
    from . import yamlite  # local import: keeps the optional-PyYAML probe lazy

    report = Report()
    report.checks_run += 1
    label = Path(path).name
    try:
        data = yamlite.load(path)
    except yamlite.YamlError as exc:
        report.error(label, str(exc))
        return report

    report.extend(check_instance(data, "persona.schema.json", label))

    if isinstance(data, dict) and data.get("id") != Path(path).stem:
        report.error(
            label,
            f"id {data.get('id')!r} does not match the filename stem {Path(path).stem!r}",
        )
    return report


def check_scenario_file(path: str | Path) -> Report:
    from . import scenarios, yamlite

    report = Report()
    report.checks_run += 2
    label = Path(path).name
    try:
        data = yamlite.load(path)
    except yamlite.YamlError as exc:
        report.error(label, str(exc))
        return report

    report.extend(check_instance(data, "scenario.schema.json", label))
    if isinstance(data, dict):
        for problem in scenarios.semantic_problems(data):
            report.error(label, problem)
        if data.get("id") != Path(path).stem:
            report.error(
                label,
                f"id {data.get('id')!r} does not match the filename stem {Path(path).stem!r}",
            )
    return report


# --------------------------------------------------------------------------
# whole run directory
# --------------------------------------------------------------------------

_ARTIFACTS = [
    ("run.json", "run.schema.json", True),
    ("findings.candidates.json", "findings.candidates.schema.json", False),
    ("findings.reviewed.json", "findings.reviewed.schema.json", False),
    ("evaluation.json", "evaluation.schema.json", False),
]


def check_run_dir(run: RunDir) -> Report:
    """Validate whatever artifacts are present. Missing files are not errors here.

    Enforcing that a required artifact exists is the gate's job, not validation's.
    Keeping the two separate means `pqa validate` stays usable mid-run.
    """
    report = Report()

    for filename, schema_name, required in _ARTIFACTS:
        path = run.path / filename
        if not path.exists():
            if required:
                report.error(filename, "missing")
            continue
        try:
            report.extend(check_instance(read_json(path), schema_name, filename))
        except PqaError as exc:
            report.error(filename, str(exc))

    if run.session.exists():
        session_report, _ = check_session(run.session)
        report.extend(session_report)

    return report


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def self_check() -> Report:
    """Verify the schemas themselves are loadable and fully enforced.

    The second half matters more than the first. `jsonschema_lite` ignores
    keywords it does not implement, so a schema could gain a constraint that is
    never actually applied and nothing would look wrong. This turns that silent
    no-op into a failure.
    """
    report = Report()
    schema_dir = kit_root() / "schemas"
    schemas = sorted(schema_dir.glob("*.schema.json"))

    if not schemas:
        report.error("schemas/", "no schemas found")
        return report

    for path in schemas:
        report.checks_run += 1
        try:
            validator = load_schema(path)
        except SchemaError as exc:
            report.error(path.name, str(exc))
            continue

        unsupported = validator.unsupported_keywords()
        if unsupported:
            report.error(
                path.name,
                f"uses schema keywords the validator does not enforce: "
                f"{', '.join(sorted(unsupported))}",
                "add them to SUPPORTED_KEYWORDS in jsonschema_lite.py and "
                "implement them, or remove them from the schema -- an "
                "unenforced constraint reads as a constraint but is not one",
            )

    for path in sorted((kit_root() / "personas").glob("*.yaml")):
        report.extend(check_persona_file(path))

    for path in sorted((kit_root() / "scenarios").glob("*.yaml")):
        report.extend(check_scenario_file(path))

    return report
