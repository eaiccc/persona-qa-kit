"""The review gate: cross-artifact checks that must pass before a run counts.

This is the module the `qa-review-gate` hook calls. Everything here is a check
that an agent cannot satisfy by asserting it has been satisfied.

The distinction the design document draws is between requests and constraints.
"Do not skip the review step" is a request. Refusing to advance when
findings.reviewed.json is absent or malformed is a constraint. The checks below
are all of the second kind, which is why they live in a shell-invoked script
rather than in a prompt.
"""

from __future__ import annotations

from typing import Any

from . import drift, fingerprint, personas, scenarios, validate
from .core import PqaError, Report, RunDir, digest_file, read_json

__all__ = ["run_gate"]

_SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "cosmetic": 3}
_CONFIDENCE_RANK = {"confirmed": 0, "probable": 1, "possible": 2, "speculative": 3}


def run_gate(run: RunDir) -> Report:
    """Every gate check, in dependency order. Later stages skip if inputs failed."""
    report = Report()

    manifest = _check_manifest(run, report)
    if manifest is None:
        return report

    card = _check_persona(run, manifest, report)
    _check_scenario(manifest, report)
    records = _check_session(run, manifest, report)

    if records is not None:
        _check_perturbations(manifest, records, report)
        _check_scope(manifest, records, report)
    if records is not None and card is not None:
        _check_drift(manifest, card, records, report)

    candidates = _check_candidates(run, manifest, records, report)
    if candidates is not None:
        _check_review(run, manifest, candidates, records, report)

    return report


# --------------------------------------------------------------------------
# stage 1: the sealed manifest
# --------------------------------------------------------------------------

def _check_manifest(run: RunDir, report: Report) -> dict[str, Any] | None:
    report.checks_run += 2

    if not run.run_json.exists():
        report.error("run.json", "missing", "create it with `pqa new-run`")
        return None

    try:
        manifest = read_json(run.run_json)
    except PqaError as exc:
        report.error("run.json", str(exc))
        return None

    schema_report = validate.check_instance(manifest, "run.schema.json", "run.json")
    report.extend(schema_report)
    if not schema_report.ok:
        return None

    if not run.lock.exists():
        report.error(
            "run.lock",
            "missing -- the run manifest was never sealed",
            "run `pqa seal <run-dir>` before the session starts. Stopping "
            "conditions chosen after seeing the results are not stopping "
            "conditions (DESIGN.md section 3)",
        )
        return manifest

    try:
        lock = read_json(run.lock)
    except PqaError as exc:
        report.error("run.lock", str(exc))
        return manifest

    actual = digest_file(run.run_json)
    if lock.get("run_digest") != actual:
        report.error(
            "run.lock",
            "run.json changed after it was sealed",
            "the step budget and success criteria are fixed before execution. "
            "If they genuinely need to change, start a new run rather than "
            "editing this one",
        )

    return manifest


# --------------------------------------------------------------------------
# stage 2: persona binding
# --------------------------------------------------------------------------

def _check_persona(
    run: RunDir, manifest: dict[str, Any], report: Report
) -> personas.PersonaCard | None:
    report.checks_run += 2
    declared = manifest.get("persona", {})

    try:
        card = personas.load(declared.get("id", ""))
    except PqaError as exc:
        report.error("persona", str(exc))
        return None

    if card.digest != declared.get("card_digest"):
        report.error(
            "persona",
            f"card {card.id} has changed since the run was sealed",
            "the session was run against different persona text than the card "
            "now contains, so its drift and consistency results describe a "
            "persona that no longer exists",
        )
    if card.version != declared.get("version"):
        report.error(
            "persona",
            f"card version is {card.version}, run.json declares "
            f"{declared.get('version')}",
        )
    return card


def _check_scenario(manifest: dict[str, Any], report: Report) -> scenarios.Scenario | None:
    """The scenario is bound the same way the persona card is.

    A scenario edited after sealing changes the task, the completion test, or
    the interruptions -- the run then describes a scenario that no longer
    exists. The sealed copy in run.json must also agree with the file, so a
    hand-written manifest cannot carry the right digest with easier criteria.
    """
    declared = manifest.get("scenario")
    goal = manifest["stopping_conditions"]["goal_satisfaction"]
    if declared is None:
        report.checks_run += 1
        if goal.get("criteria_source") == "scenario":
            report.error(
                "scenario",
                "success criteria claim to come from a scenario, but run.json binds none",
            )
        return None

    report.checks_run += 4
    try:
        scenario = scenarios.load(declared.get("id", ""))
    except PqaError as exc:
        report.error("scenario", str(exc))
        return None

    if scenario.digest != declared.get("scenario_digest"):
        report.error(
            "scenario",
            f"scenario {scenario.id} has changed since the run was sealed",
            "the session was run against a different task, completion test or "
            "set of perturbations than the file now contains. Bump the "
            "scenario's version and start a new run rather than editing this one",
        )
    if scenario.version != declared.get("version"):
        report.error(
            "scenario",
            f"scenario version is {scenario.version}, run.json declares "
            f"{declared.get('version')}",
        )

    sealed = scenario.manifest_block()
    for key in ("task", "entry", "perturbations"):
        if declared.get(key) != sealed[key]:
            report.error(
                "scenario",
                f"run.json scenario.{key} does not match scenarios/{scenario.id}.yaml",
                "the manifest's copy is what the hooks act on, so it must be the "
                "scenario's text exactly. Regenerate it with `pqa new-run`",
            )
    if goal.get("success_criteria") != scenario.success_criteria:
        report.error(
            "scenario",
            "success_criteria in run.json are not the scenario's",
            "with a scenario bound, the completion test is the scenario's. The "
            "persona card's criteria are only the fallback for runs without one",
        )
    return scenario


# --------------------------------------------------------------------------
# stage 3: the session log
# --------------------------------------------------------------------------

def _check_session(
    run: RunDir, manifest: dict[str, Any], report: Report
) -> list[dict[str, Any]] | None:
    report.checks_run += 1

    if not run.session.exists():
        report.error("session.jsonl", "missing -- no session was recorded")
        return None

    session_report, records = validate.check_session(run.session)
    report.extend(session_report)
    if not session_report.ok:
        return None

    stopping = manifest["stopping_conditions"]
    budget = stopping["step_budget"]
    observations = scenarios.step_numbers(records)
    report.checks_run += 2

    if len(observations) > budget:
        report.error(
            "session.jsonl",
            f"{len(observations)} observations exceed the sealed step budget of {budget}",
            "the budget is a hard ceiling; a run that ran past it is not "
            "comparable with runs that respected it",
        )

    terminal = records[-1] if records and records[-1].get("kind") == "terminated" else None
    if terminal is not None:
        reason = terminal.get("reason")
        if reason == "step_budget" and len(observations) < budget:
            report.error(
                "session.jsonl",
                f"terminated with reason 'step_budget' after only "
                f"{len(observations)} of {budget} steps",
            )
        if reason == "blocked" and not any(r.get("kind") == "blocked" for r in records):
            report.error(
                "session.jsonl",
                "terminated with reason 'blocked' but no 'blocked' record was written",
                "a blocked run is itself a finding, so the blocking loop must be "
                "on the record for qa-agent to see it",
            )

    return records


def _check_perturbations(
    manifest: dict[str, Any], records: list[dict[str, Any]], report: Report
) -> None:
    """Every sealed perturbation that fell due was applied, on time, once.

    "Due" means the session went on past its step: if the persona recorded step
    at_step + 1, they did so under the perturbation or not at all, and only a
    record written by the hook says which. A perturbation silently skipped turns
    an interrupted-flow run into an uninterrupted one that still claims the
    scenario's name.
    """
    declared = manifest.get("scenario")
    applied = [(i, r) for i, r in enumerate(records) if r.get("kind") == "perturbation"]
    report.checks_run += 1

    if declared is None:
        if applied:
            report.error(
                "session.jsonl",
                f"{len(applied)} perturbation record(s) in a run with no scenario",
                "perturbations come from a sealed scenario; there is nothing here "
                "they could have been injected from",
            )
        return

    sealed = declared.get("perturbations", [])
    digest = declared.get("scenario_digest")
    steps = scenarios.step_numbers(records)
    last = max(steps) if steps else 0
    report.checks_run += 2 * len(sealed) + len(applied)

    by_index: dict[int, tuple[int, dict[str, Any]]] = {}
    for position, record in applied:
        index = record.get("index")
        where = f"session.jsonl[perturbation {index}]"
        if not isinstance(index, int) or not 0 <= index < len(sealed):
            report.error(
                where,
                f"names perturbation {index}, but the scenario seals {len(sealed)}",
                "a perturbation record that matches nothing sealed was not "
                "written by the hook",
            )
            continue
        if index in by_index:
            report.error(where, "recorded more than once")
            continue
        by_index[index] = (position, record)

        want = sealed[index]
        if record.get("do") != want["do"] or record.get("at_step") != want["at_step"]:
            report.error(
                where,
                f"records {record.get('do')!r} at step {record.get('at_step')}, but "
                f"the sealed perturbation is {want['do']!r} at step {want['at_step']}",
            )
        if record.get("scenario_digest") != digest or record.get("scenario_id") != declared.get("id"):
            report.error(where, "scenario id or digest does not match run.json")

    for index, want in enumerate(sealed):
        at = want["at_step"]
        label = f"perturbation {index} ({want['do']} after step {at})"
        if index not in by_index:
            if last > at:
                report.error(
                    "session.jsonl",
                    f"{label} was due but never applied",
                    "the session continued past the step it was sealed for. "
                    "Check that persona-drift-check is wired up -- a hook that "
                    "stopped firing looks exactly like a scenario that went smoothly",
                )
            continue

        position, record = by_index[index]
        before = [
            r for r in records[:position]
            if r.get("kind") in scenarios.STEP_KINDS and r.get("step") == at
        ]
        later = [
            r for r in records[:position]
            if r.get("kind") in ("observation", "intent", "outcome")
            and isinstance(r.get("step"), int) and r["step"] > at
        ]
        if record.get("step") != at or later:
            report.error(
                "session.jsonl",
                f"{label} was applied late (after step "
                f"{max([r['step'] for r in later] + [record.get('step') or 0])})",
                "the steps in between ran without it, so the session did not "
                "test what the scenario sealed",
            )
        elif not before:
            report.error(
                "session.jsonl",
                f"{label} was applied before step {at} was recorded",
            )


def _check_scope(
    manifest: dict[str, Any], records: list[dict[str, Any]], report: Report
) -> None:
    prefixes = manifest["stopping_conditions"]["scope_bounds"].get("in_scope_surfaces")
    if not prefixes:
        return
    report.checks_run += 1
    outside = sorted({
        (r["step"], r["action"]["surface"])
        for r in records
        if r.get("kind") in scenarios.STEP_KINDS
        and isinstance(r.get("action"), dict)
        and r["action"].get("surface")
        and not scenarios.in_scope(r["action"]["surface"], prefixes)
    })
    if outside:
        shown = ", ".join(f"step {s} on {surface}" for s, surface in outside[:5])
        report.warn(
            "scope",
            f"{len(outside)} step(s) outside the scenario's in_scope_surfaces: {shown}",
            "findings there are not what this scenario covers. Either the "
            "persona wandered, or the scenario's surfaces are too narrow",
        )


# --------------------------------------------------------------------------
# stage 4: drift
# --------------------------------------------------------------------------

def _check_drift(
    manifest: dict[str, Any],
    card: personas.PersonaCard,
    records: list[dict[str, Any]],
    report: Report,
) -> None:
    control = manifest.get("drift_control", {})
    enforce = control.get("enforce_never_uses", True)
    every = control.get("reinject_every_steps", 8)
    report.checks_run += 2

    violations = drift.scan(records, card.never_uses)
    for violation in violations[:20]:
        message = f"persona vocabulary breach -- {violation}"
        hint = (
            "this persona has no such word. Either the agent drifted, or the "
            "card's never_uses list is wrong; both are worth knowing"
        )
        if enforce:
            report.error("drift", message, hint)
        else:
            report.warn("drift", message, hint)
    if len(violations) > 20:
        report.warn("drift", f"... and {len(violations) - 20} further vocabulary breaches")

    last = scenarios.last_step(records)
    gaps = drift.cadence_gaps(records, every, last)
    if gaps:
        report.warn(
            "drift",
            f"persona re-injection missing at step(s) {gaps[:10]}"
            f"{' ...' if len(gaps) > 10 else ''}",
            "the drift hook may not be wired up; a hook that stopped firing "
            "looks exactly like a session with no drift",
        )


# --------------------------------------------------------------------------
# stage 5: candidates
# --------------------------------------------------------------------------

def _check_candidates(
    run: RunDir,
    manifest: dict[str, Any],
    records: list[dict[str, Any]] | None,
    report: Report,
) -> dict[str, Any] | None:
    report.checks_run += 1

    if not run.candidates.exists():
        report.error(
            "findings.candidates.json",
            "missing -- qa-agent has not run",
            "an empty findings array is a valid result; an absent file is not",
        )
        return None

    try:
        candidates = read_json(run.candidates)
    except PqaError as exc:
        report.error("findings.candidates.json", str(exc))
        return None

    schema_report = validate.check_instance(
        candidates, "findings.candidates.schema.json", "findings.candidates.json"
    )
    report.extend(schema_report)
    if not schema_report.ok:
        return None

    report.checks_run += 2
    if candidates.get("run_id") != manifest.get("run_id"):
        report.error(
            "findings.candidates.json",
            f"run_id {candidates.get('run_id')!r} does not match run.json",
        )

    if run.session.exists():
        actual = digest_file(run.session)
        if candidates.get("session_digest") != actual:
            report.error(
                "findings.candidates.json",
                "session_digest does not match session.jsonl",
                "these candidates were derived from a different session log than "
                "the one in this directory",
            )

    findings = candidates.get("findings", [])
    _check_findings(findings, run, records, "findings.candidates.json", report)
    return candidates


def _check_findings(
    findings: list[dict[str, Any]],
    run: RunDir,
    records: list[dict[str, Any]] | None,
    label: str,
    report: Report,
) -> None:
    seen_ids: set[str] = set()
    steps = set(scenarios.step_numbers(records or []))

    for finding in findings:
        report.checks_run += 3
        fid = finding.get("id", "?")

        if fid in seen_ids:
            report.error(f"{label}[{fid}]", "duplicate finding id")
        seen_ids.add(fid)

        matches, expected = fingerprint.verify(finding)
        if not matches:
            report.error(
                f"{label}[{fid}]",
                f"fingerprint {finding.get('fingerprint')!r} does not match the "
                f"value computed from its own fields ({expected})",
                "fingerprints are derived, not authored -- a wrong one still "
                "looks like a fingerprint and silently breaks regression matching",
            )

        for i, ref in enumerate(finding.get("evidence_refs", [])):
            _check_evidence(ref, f"{label}[{fid}].evidence_refs[{i}]", run, steps, records, report)


def _check_evidence(
    ref: dict[str, Any],
    label: str,
    run: RunDir,
    steps: set[int],
    records: list[dict[str, Any]] | None,
    report: Report,
) -> None:
    kind = ref.get("kind")

    if kind == "session_log":
        if records is None:
            return
        wanted = ref.get("steps") or []
        if not wanted:
            report.error(label, "cites no steps")
            return
        start = wanted[0]
        end = wanted[-1]
        if end < start:
            report.error(label, f"step range [{start}, {end}] runs backwards")
            return
        missing = [s for s in (start, end) if s not in steps]
        if missing:
            report.error(
                label,
                f"cites step(s) {missing} that do not exist in the session log",
                "evidence must point at something that happened. This check is "
                "the cheapest guard available against a fabricated defect",
            )

    elif kind == "artifact":
        try:
            target = run.resolve(ref.get("path", ""))
        except PqaError as exc:
            report.error(label, str(exc))
            return
        if not target.exists():
            report.error(label, f"cites a file that does not exist: {ref.get('path')}")


# --------------------------------------------------------------------------
# stage 6: the review itself
# --------------------------------------------------------------------------

def _check_review(
    run: RunDir,
    manifest: dict[str, Any],
    candidates: dict[str, Any],
    records: list[dict[str, Any]] | None,
    report: Report,
) -> None:
    report.checks_run += 1

    if not run.reviewed.exists():
        report.error(
            "findings.reviewed.json",
            "missing -- the pipeline stops here",
            "qa-reviewer has not accepted or rejected these candidates. This is "
            "the one check the kit exists for: a run cannot report findings it "
            "declined to have reviewed",
        )
        return

    try:
        reviewed = read_json(run.reviewed)
    except PqaError as exc:
        report.error("findings.reviewed.json", str(exc))
        return

    schema_report = validate.check_instance(
        reviewed, "findings.reviewed.schema.json", "findings.reviewed.json"
    )
    report.extend(schema_report)
    if not schema_report.ok:
        return

    report.checks_run += 3

    if reviewed.get("run_id") != manifest.get("run_id"):
        report.error(
            "findings.reviewed.json",
            f"run_id {reviewed.get('run_id')!r} does not match run.json",
        )

    if reviewed.get("candidates_digest") != digest_file(run.candidates):
        report.error(
            "findings.reviewed.json",
            "candidates_digest does not match findings.candidates.json",
            "this review was written against a different candidate set -- "
            "either the candidates changed after review, or a stale review file "
            "was carried over",
        )

    proposed = {f["id"]: f for f in candidates.get("findings", [])}
    reviews = reviewed.get("reviews", [])
    by_id: dict[str, dict[str, Any]] = {}

    for review in reviews:
        fid = review["finding_id"]
        if fid in by_id:
            report.error("findings.reviewed.json", f"{fid} is reviewed more than once")
        by_id[fid] = review

    unreviewed = sorted(set(proposed) - set(by_id))
    if unreviewed:
        report.error(
            "findings.reviewed.json",
            f"candidate(s) {unreviewed} have no review",
            "every candidate needs a verdict. Dropping an inconvenient one "
            "silently is otherwise the path of least resistance",
        )

    phantom = sorted(set(by_id) - set(proposed))
    if phantom:
        report.error(
            "findings.reviewed.json",
            f"review(s) reference candidate(s) {phantom} that were never proposed",
        )

    for review in reviews:
        target = review.get("merged_into")
        if target and target not in proposed:
            report.error(
                "findings.reviewed.json",
                f"{review['finding_id']} is merged into {target}, which does not exist",
            )

    _check_accepted(reviewed, proposed, by_id, run, records, report)
    _check_review_signals(reviews, reviewed.get("accepted", []), report)


def _check_accepted(
    reviewed: dict[str, Any],
    proposed: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    run: RunDir,
    records: list[dict[str, Any]] | None,
    report: Report,
) -> None:
    accepted = reviewed.get("accepted", [])
    report.checks_run += 2

    expected_ids = {fid for fid, r in by_id.items() if r.get("verdict") == "accepted"}
    actual_ids = {f["id"] for f in accepted}

    if expected_ids != actual_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        if missing:
            report.error(
                "findings.reviewed.json",
                f"accepted[] omits {missing}, which the reviews mark as accepted",
            )
        if extra:
            report.error(
                "findings.reviewed.json",
                f"accepted[] includes {extra}, which the reviews do not mark as accepted",
            )

    for finding in accepted:
        fid = finding["id"]
        original = proposed.get(fid)
        if original is None:
            continue
        report.checks_run += 1

        if finding.get("fingerprint") != original.get("fingerprint"):
            report.error(
                f"findings.reviewed.json[{fid}]",
                "fingerprint differs from the candidate it came from",
                "the reviewer adjusts severity and confidence; it does not "
                "redefine what the defect is",
            )

        review = by_id.get(fid, {})
        for axis in ("severity", "confidence"):
            adjusted = review.get(f"adjusted_{axis}")
            want = adjusted if adjusted is not None else original.get(axis)
            if finding.get(axis) != want:
                source = "the review's adjustment" if adjusted else "the candidate"
                report.error(
                    f"findings.reviewed.json[{fid}]",
                    f"{axis} is {finding.get(axis)!r} but {source} says {want!r}",
                )

    _check_findings(accepted, run, records, "findings.reviewed.json", report)


# --------------------------------------------------------------------------
# detection signals (warnings, never errors)
# --------------------------------------------------------------------------

def _check_review_signals(
    reviews: list[dict[str, Any]], accepted: list[dict[str, Any]], report: Report
) -> None:
    """The design document's own health checks, applied to a single run.

    These are warnings by construction. A single run that accepts everything may
    simply have had good candidates; the signal only means something across
    several runs, which is what `pqa signals` is for.
    """
    report.checks_run += 2

    if reviews:
        verdicts = [r.get("verdict") for r in reviews]
        if all(v == "accepted" for v in verdicts) and len(verdicts) >= 3:
            report.warn(
                "signal",
                f"qa-reviewer accepted all {len(verdicts)} candidates",
                "a healthy rejection rate is nonzero. If this holds across runs, "
                "the separation between proposer and reviewer has collapsed "
                "(DESIGN.md section 1)",
            )

    ranked = [
        (_SEVERITY_RANK[f["severity"]], _CONFIDENCE_RANK[f["confidence"]])
        for f in accepted
        if f.get("severity") in _SEVERITY_RANK and f.get("confidence") in _CONFIDENCE_RANK
    ]
    if len(ranked) >= 5:
        rho = _spearman([a for a, _ in ranked], [b for _, b in ranked])
        if rho is not None and rho >= 0.9:
            report.warn(
                "signal",
                f"severity and confidence move together almost perfectly "
                f"(rho={rho:.2f}) across {len(ranked)} accepted findings",
                "these are separate scales. Perfect correlation usually means "
                "'I am not sure this is real' is being written as 'this is not "
                "serious', which silently downgrades genuine high-severity "
                "findings (DESIGN.md section 2)",
            )


def _spearman(xs: list[int], ys: list[int]) -> float | None:
    """Rank correlation. Returns None when either series has no variance."""
    n = len(xs)
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy) ** 0.5


def _ranks(values: list[int]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks
