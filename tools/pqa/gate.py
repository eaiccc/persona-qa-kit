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

from . import drift, fingerprint, personas, validate
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
    records = _check_session(run, manifest, report)

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
    observations = [r for r in records if r.get("kind") == "observation"]
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

    steps = [r["step"] for r in records if r.get("kind") == "observation"]
    last = max(steps) if steps else 0
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
    steps = {r["step"] for r in (records or []) if r.get("kind") == "observation"}

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
