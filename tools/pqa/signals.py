"""Cross-run health checks for the pipeline itself.

DESIGN.md names a detection signal for each of the four failure modes. Every one
of them is a statement about a *distribution across runs*, not about any single
run, which is why they live here rather than in the gate:

  1. self-certification  -- reviewer acceptance rate stuck at 100%
  2. unfalsifiable       -- fingerprint churn between runs of the same build
  3. no termination      -- every run ending at the step budget
  4. persona drift       -- consistency declining within runs

These report on whether the kit is working, not on whether the app is working.
A green gate with all four signals firing means the pipeline is producing
well-formed output that nobody should believe.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import PqaError, Report, RunDir, read_json, read_jsonl

__all__ = ["RunSummary", "collect", "analyze", "render_table"]

# Thresholds. Deliberately loose: these are prompts to go and look, not verdicts.
_MIN_RUNS = 3
_CHURN_LIMIT = 0.8
_BUDGET_LIMIT = 0.9


@dataclass
class RunSummary:
    run_id: str
    path: Path
    persona_id: str = ""
    target_name: str = ""
    target_build: str = ""
    step_budget: int = 0
    observations: int = 0
    terminate_reason: str = ""
    candidates: int = 0
    verdicts: Counter = field(default_factory=Counter)
    rejection_reasons: Counter = field(default_factory=Counter)
    fingerprints: set[str] = field(default_factory=set)
    consistency_first: float | None = None
    consistency_second: float | None = None
    trust: str = ""

    @property
    def reviewed(self) -> int:
        return sum(self.verdicts.values())

    @property
    def acceptance_rate(self) -> float | None:
        return self.verdicts["accepted"] / self.reviewed if self.reviewed else None

    @property
    def at_budget(self) -> bool:
        return self.step_budget > 0 and self.observations >= self.step_budget


def collect(runs_dir: str | Path) -> list[RunSummary]:
    """Summarize every run directory under `runs_dir`. Broken runs are skipped."""
    root = Path(runs_dir)
    if not root.is_dir():
        raise PqaError(f"not a directory: {root}")

    summaries: list[RunSummary] = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (path / "run.json").exists():
            continue
        try:
            summaries.append(_summarize(RunDir.open(path)))
        except PqaError:
            continue
    return summaries


def _summarize(run: RunDir) -> RunSummary:
    manifest = read_json(run.run_json)
    summary = RunSummary(
        run_id=manifest.get("run_id", run.path.name),
        path=run.path,
        persona_id=manifest.get("persona", {}).get("id", ""),
        target_name=manifest.get("target", {}).get("name", ""),
        target_build=manifest.get("target", {}).get("build", ""),
        step_budget=manifest.get("stopping_conditions", {}).get("step_budget", 0),
    )

    if run.session.exists():
        records, _ = read_jsonl(run.session)
        for _, record in records:
            if not isinstance(record, dict):
                continue
            if record.get("kind") == "observation":
                summary.observations += 1
            elif record.get("kind") == "terminated":
                summary.terminate_reason = record.get("reason", "")

    if run.candidates.exists():
        summary.candidates = len(read_json(run.candidates).get("findings", []))

    if run.reviewed.exists():
        reviewed = read_json(run.reviewed)
        for review in reviewed.get("reviews", []):
            summary.verdicts[review.get("verdict", "?")] += 1
            if review.get("verdict") == "rejected":
                summary.rejection_reasons[review.get("rejection_reason", "?")] += 1
        summary.fingerprints = {
            f["fingerprint"] for f in reviewed.get("accepted", []) if "fingerprint" in f
        }

    if run.evaluation.exists():
        evaluation = read_json(run.evaluation)
        consistency = evaluation.get("consistency", {})
        summary.consistency_first = consistency.get("first_half")
        summary.consistency_second = consistency.get("second_half")
        summary.trust = evaluation.get("trust", {}).get("verdict", "")

    return summary


# --------------------------------------------------------------------------
# the four signals
# --------------------------------------------------------------------------

def analyze(summaries: list[RunSummary]) -> Report:
    report = Report()

    if len(summaries) < _MIN_RUNS:
        report.warn(
            "signals",
            f"only {len(summaries)} run(s) available; these signals need at "
            f"least {_MIN_RUNS} to mean anything",
        )
        return report

    _signal_self_certification(summaries, report)
    _signal_fingerprint_churn(summaries, report)
    _signal_termination(summaries, report)
    _signal_drift(summaries, report)
    return report


def _signal_self_certification(summaries: list[RunSummary], report: Report) -> None:
    report.checks_run += 1
    with_reviews = [s for s in summaries if s.reviewed > 0]
    if not with_reviews:
        return

    total_reviewed = sum(s.reviewed for s in with_reviews)
    total_accepted = sum(s.verdicts["accepted"] for s in with_reviews)
    if total_reviewed >= 10 and total_accepted == total_reviewed:
        report.warn(
            "signal 1 (self-certification)",
            f"qa-reviewer accepted {total_accepted}/{total_reviewed} candidates "
            f"across {len(with_reviews)} runs -- a 100% acceptance rate",
            "either the reviewer is rubber-stamping, or qa-agent has learned to "
            "propose only safe findings. Both mean the proposer/reviewer "
            "separation has collapsed and is no longer buying anything",
        )

    reasons = Counter()
    for summary in with_reviews:
        reasons.update(summary.rejection_reasons)
    if len(reasons) == 1 and sum(reasons.values()) >= 5:
        only = next(iter(reasons))
        report.warn(
            "signal 1 (self-certification)",
            f"every rejection across these runs used the same reason ({only!r})",
            "the other rejection axes are not being exercised, which usually "
            "means the reviewer has settled into one pattern",
        )


def _signal_fingerprint_churn(summaries: list[RunSummary], report: Report) -> None:
    report.checks_run += 1
    by_target: dict[str, list[RunSummary]] = {}
    for summary in summaries:
        by_target.setdefault(summary.target_name, []).append(summary)

    for target, runs in by_target.items():
        runs_with_findings = [r for r in runs if r.fingerprints]
        if len(runs_with_findings) < _MIN_RUNS:
            continue

        counts = Counter()
        for run in runs_with_findings:
            counts.update(run.fingerprints)
        once = sum(1 for n in counts.values() if n == 1)
        churn = once / len(counts)

        if churn >= _CHURN_LIMIT:
            report.warn(
                "signal 2 (fingerprint churn)",
                f"{target}: {once} of {len(counts)} fingerprints appear in only "
                f"one of {len(runs_with_findings)} runs ({churn:.0%})",
                "if these runs hit the same defects, cross-run comparison is not "
                "actually working -- most likely the `surface` values are too "
                "specific. Regression detection silently degrades to impressions",
            )


def _signal_termination(summaries: list[RunSummary], report: Report) -> None:
    report.checks_run += 1
    budgeted = [s for s in summaries if s.step_budget > 0]
    if len(budgeted) < _MIN_RUNS:
        return

    at_budget = [s for s in budgeted if s.at_budget]
    ratio = len(at_budget) / len(budgeted)
    if ratio >= _BUDGET_LIMIT:
        report.warn(
            "signal 3 (termination)",
            f"{len(at_budget)} of {len(budgeted)} runs terminated at the step budget "
            f"({ratio:.0%})",
            "the goal-satisfaction check is not firing and the budget is doing "
            "all the work. Coverage is then a function of the budget, not of the "
            "app, so it cannot be compared between builds",
        )

    reasons = Counter(s.terminate_reason for s in budgeted if s.terminate_reason)
    if reasons and len(reasons) == 1 and sum(reasons.values()) >= _MIN_RUNS:
        only = next(iter(reasons))
        if only != "goal_met":
            report.warn(
                "signal 3 (termination)",
                f"every run ended with reason {only!r}",
                "one stopping condition is absorbing all runs; the others are "
                "decorative",
            )


def _signal_drift(summaries: list[RunSummary], report: Report) -> None:
    report.checks_run += 1
    scored = [
        s for s in summaries
        if s.consistency_first is not None and s.consistency_second is not None
    ]
    if len(scored) < _MIN_RUNS:
        return

    declining = [s for s in scored if s.consistency_second < s.consistency_first - 0.05]
    if len(declining) / len(scored) >= 0.5:
        drop = sum(s.consistency_first - s.consistency_second for s in declining) / len(declining)
        report.warn(
            "signal 4 (persona drift)",
            f"{len(declining)} of {len(scored)} runs score lower in their second "
            f"half (mean drop {drop:.2f})",
            "drift is progressive and it is happening systematically. Consider a "
            "tighter re-injection cadence, or a shorter step budget -- late-session "
            "findings are a QA engineer's findings, not the persona's",
        )

    untrusted = [s for s in summaries if s.trust in ("degraded", "untrustworthy")]
    if untrusted and len(untrusted) / len(summaries) >= 0.5:
        report.warn(
            "signal 4 (persona drift)",
            f"{len(untrusted)} of {len(summaries)} runs were rated degraded or "
            f"untrustworthy by persona-evaluator",
        )


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

def render_table(summaries: list[RunSummary]) -> str:
    if not summaries:
        return "  (no runs found)"

    header = (
        f"  {'run':<28} {'persona':<22} {'steps':>10} "
        f"{'cand':>5} {'acc':>5} {'rej':>5}  {'end':<16} trust"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]

    for s in summaries:
        steps = f"{s.observations}/{s.step_budget}" if s.step_budget else str(s.observations)
        lines.append(
            f"  {s.run_id[:28]:<28} {s.persona_id[:22]:<22} {steps:>10} "
            f"{s.candidates:>5} {s.verdicts['accepted']:>5} "
            f"{s.verdicts['rejected']:>5}  {s.terminate_reason[:16]:<16} {s.trust}"
        )

    reviewed = sum(s.reviewed for s in summaries)
    accepted = sum(s.verdicts["accepted"] for s in summaries)
    if reviewed:
        lines.append("")
        lines.append(
            f"  {len(summaries)} runs, {reviewed} candidates reviewed, "
            f"{accepted} accepted ({accepted / reviewed:.0%})"
        )
    return "\n".join(lines)
