"""The cross-run report: what the reviewed findings say about the app.

`pqa signals` asks whether the pipeline is working. This asks the other
question -- across every run that passed the gate, which defects recur, who hit
them, under which scenarios, since which build, and what would most improve the
app for the people who hit them.

Three rules keep it honest:

  * Only runs that pass the review gate contribute, by default. A run the gate
    refused is not evidence, and aggregating it would launder it into some.
  * Only `findings.reviewed.json` `accepted[]` is read. Candidates are proposals.
  * Absence is reported as absence. A persona x scenario cell with no runs is a
    gap, not a clean bill of health, and a defect missing from the latest build
    is "not re-tested" unless a run on that build covered a cell where it was
    seen before.

Improvement suggestions here are mechanical: one per defect, templated from its
symptom and action, quoting `user_voice` verbatim. They say where to look, not
what to build. The `feedback-synthesizer` agent writes the judgment-bearing
version, and `pqa check-improvements` holds it to the same evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import gate, personas, scenarios, validate
from .core import PqaError, Report, RunDir, iso_now, read_json, read_jsonl

__all__ = ["build", "render_markdown", "render_json", "check_improvements"]

_SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "cosmetic": 3}
NO_SCENARIO = None

_ACTION_PHRASE = {
    "navigate": "move between screens",
    "tap": "tap",
    "type": "type",
    "submit": "submit",
    "scroll": "scroll",
    "search": "search",
    "upload": "upload something",
    "wait": "wait",
    "back": "go back",
    "dismiss": "dismiss something",
    "background": "leave the app mid-task",
    "resume": "come back to the app",
    "restart": "restart the app",
    "none": "do nothing at all",
}

_SUGGESTION = {
    "crash": "Stop {surface} from closing or freezing when people {action}.",
    "data_loss": "Keep what people have entered on {surface} when they {action}: nothing they typed should depend on the rest of the flow going right.",
    "state_desync": "Make {surface} show what the app actually holds after people {action}.",
    "no_feedback": "When people {action} on {surface}, tell them plainly whether it worked -- on screen and to a screen reader.",
    "wrong_result": "Make {surface} do what people expect when they {action}; what happened instead is quoted below.",
    "blocked_flow": "Give people a way forward on {surface} when they {action}; nobody in these runs found one.",
    "slow": "Make {surface} respond sooner when people {action}, or show that it is working.",
    "confusing_affordance": "Make it obvious on {surface} what to do, and what will happen when people {action}.",
    "unreadable_content": "Make the content on {surface} readable for the people quoted below.",
    "unexpected_cost": "Tell people what it will cost before they {action} on {surface}.",
    "access_barrier": "Make {surface} usable for the people quoted below when they {action}: every control named, every change announced.",
}


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def _run_entry(run: RunDir) -> dict[str, Any]:
    manifest = read_json(run.run_json)
    scenario = manifest.get("scenario") or {}
    entry: dict[str, Any] = {
        "run_id": manifest.get("run_id", run.path.name),
        "path": str(run.path),
        "created_at": manifest.get("created_at", ""),
        "persona": manifest.get("persona", {}).get("id", ""),
        "scenario": scenario.get("id", NO_SCENARIO),
        "target": manifest.get("target", {}).get("name", ""),
        "build": manifest.get("target", {}).get("build", ""),
        "steps": 0,
        "ended": "",
        "trust": "",
        "coverage_note": "",
        "accepted": [],
    }

    gate_report = gate.run_gate(run)
    entry["gate"] = "pass" if gate_report.ok else "fail"
    entry["gate_errors"] = [f"{p.where}: {p.message}" for p in gate_report.errors[:3]]

    if run.session.exists():
        records = [r for _, r in read_jsonl(run.session)[0] if isinstance(r, dict)]
        entry["steps"] = len(scenarios.step_numbers(records))
        ends = [r.get("reason", "") for r in records if r.get("kind") == "terminated"]
        entry["ended"] = ends[-1] if ends else ""

    if run.candidates.exists():
        entry["coverage_note"] = read_json(run.candidates).get("coverage_note", "")
    if run.reviewed.exists():
        entry["accepted"] = read_json(run.reviewed).get("accepted", [])
    if run.evaluation.exists():
        entry["trust"] = read_json(run.evaluation).get("trust", {}).get("verdict", "")
    return entry


def _collect(runs_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(runs_dir)
    if not root.is_dir():
        raise PqaError(f"not a directory: {root}")
    entries = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (path / "run.json").exists():
            continue
        try:
            entries.append(_run_entry(RunDir.open(path)))
        except PqaError as exc:
            entries.append({
                "run_id": path.name, "path": str(path), "gate": "fail",
                "gate_errors": [str(exc)], "included": False, "accepted": [],
                "persona": "", "scenario": NO_SCENARIO, "target": "", "build": "",
                "created_at": "", "steps": 0, "ended": "", "trust": "",
                "coverage_note": "",
            })
    return sorted(entries, key=lambda e: (e["created_at"], e["run_id"]))


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

def build(runs_dir: str | Path, include_ungated: bool = False) -> dict[str, Any]:
    runs = _collect(runs_dir)
    for entry in runs:
        if "included" in entry:
            continue
        reasons = []
        if entry["gate"] != "pass" and not include_ungated:
            reasons.append("failed the review gate")
        if entry["trust"] == "untrustworthy":
            reasons.append("persona-evaluator rated it untrustworthy")
        entry["included"] = not reasons
        entry["excluded_because"] = reasons

    for entry in runs:
        entry["accepted_count"] = len(entry["accepted"])
    included = [e for e in runs if e["included"]]
    defects = _defects(included)
    coverage = _coverage(included)
    _latest_build_status(defects, included, coverage)

    return {
        "generated_at": iso_now(),
        "runs_dir": str(runs_dir),
        "runs": [{k: v for k, v in e.items() if k != "accepted"} for e in runs],
        "defects": defects,
        "coverage": coverage,
        "suggestions": _suggestions(defects),
    }


def _defects(included: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in included:
        for finding in entry["accepted"]:
            key = (entry["target"], finding["fingerprint"])
            defect = by_key.setdefault(key, {
                "target": entry["target"],
                "fingerprint": finding["fingerprint"],
                "title": finding["title"],
                "surface": finding["surface"],
                "action_class": finding["action_class"],
                "symptom_class": finding["symptom_class"],
                "worst_severity": finding["severity"],
                "occurrences": [],
            })
            if _SEVERITY_RANK[finding["severity"]] < _SEVERITY_RANK[defect["worst_severity"]]:
                defect["worst_severity"] = finding["severity"]
            defect["occurrences"].append({
                "run_id": entry["run_id"],
                "finding_id": finding["id"],
                "persona": entry["persona"],
                "scenario": entry["scenario"],
                "build": entry["build"],
                "created_at": entry["created_at"],
                "severity": finding["severity"],
                "confidence": finding["confidence"],
                "title": finding["title"],
                "user_voice": finding["user_voice"],
                "trust": entry["trust"],
            })

    defects = []
    for defect in by_key.values():
        occ = defect["occurrences"]  # already in run order
        defect["runs"] = len({o["run_id"] for o in occ})
        defect["personas"] = sorted({o["persona"] for o in occ})
        defect["scenarios"] = sorted({o["scenario"] or "" for o in occ})
        defect["scenarios"] = [s or None for s in defect["scenarios"]]
        defect["builds"] = list(dict.fromkeys(o["build"] for o in occ))
        defect["first_seen_build"] = occ[0]["build"]
        defect["first_seen_run"] = occ[0]["run_id"]
        defect["last_seen_build"] = occ[-1]["build"]
        defects.append(defect)

    defects.sort(key=lambda d: (
        _SEVERITY_RANK[d["worst_severity"]], -len(d["personas"]), -d["runs"],
        d["occurrences"][0]["created_at"], d["fingerprint"],
    ))
    return defects


def _coverage(included: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known_personas = personas.list_ids()
    known_scenarios: list[tuple[str, str | None]] = []
    for scenario_id in scenarios.list_ids():
        try:
            known_scenarios.append((scenario_id, scenarios.load(scenario_id).data.get("target")))
        except PqaError:
            continue

    targets = list(dict.fromkeys(e["target"] for e in included))
    tables = []
    for target in targets:
        runs = [e for e in included if e["target"] == target]
        persona_ids = sorted(set(known_personas) | {e["persona"] for e in runs})
        scenario_ids = sorted(
            {sid for sid, t in known_scenarios if t in (None, target)}
            | {e["scenario"] for e in runs if e["scenario"]}
        )
        columns: list[str | None] = list(scenario_ids)
        if any(e["scenario"] is NO_SCENARIO for e in runs):
            columns.append(NO_SCENARIO)

        cells, gaps = [], []
        for persona_id in persona_ids:
            for scenario_id in columns:
                here = [e for e in runs if e["persona"] == persona_id and e["scenario"] == scenario_id]
                cells.append({
                    "persona": persona_id,
                    "scenario": scenario_id,
                    "runs": len(here),
                    "accepted": sum(len(e["accepted"]) for e in here),
                    "ended": sorted({e["ended"] for e in here if e["ended"]}),
                    "builds": list(dict.fromkeys(e["build"] for e in here)),
                })
                # Runs without a scenario are not a column anyone planned, so
                # an empty cell there is not a gap.
                if not here and scenario_id is not NO_SCENARIO:
                    gaps.append({"persona": persona_id, "scenario": scenario_id})

        latest = max(runs, key=lambda e: e["created_at"])
        tables.append({
            "target": target,
            "latest_build": latest["build"],
            "personas": persona_ids,
            "scenarios": columns,
            "cells": cells,
            "gaps": gaps,
            "coverage_notes": [
                {"run_id": e["run_id"], "persona": e["persona"],
                 "scenario": e["scenario"], "note": e["coverage_note"]}
                for e in runs if e["coverage_note"]
            ],
        })
    return tables


def _latest_build_status(
    defects: list[dict[str, Any]], included: list[dict[str, Any]], coverage: list[dict[str, Any]]
) -> None:
    latest = {t["target"]: t["latest_build"] for t in coverage}
    for defect in defects:
        build = latest.get(defect["target"])
        cells = {(o["persona"], o["scenario"]) for o in defect["occurrences"]}
        if build in defect["builds"]:
            defect["latest_build_status"] = "present"
            continue
        retested = [
            e for e in included
            if e["target"] == defect["target"] and e["build"] == build
            and (e["persona"], e["scenario"]) in cells
        ]
        defect["latest_build_status"] = "not seen" if retested else "not re-tested"


def _suggestions(defects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions = []
    for rank, defect in enumerate(defects, start=1):
        template = _SUGGESTION.get(defect["symptom_class"], "Look at {surface} when people {action}.")
        action = _ACTION_PHRASE.get(defect["action_class"], defect["action_class"])
        quotes = []
        for occurrence in defect["occurrences"]:
            if occurrence["user_voice"] not in [q["quote"] for q in quotes]:
                quotes.append({
                    "quote": occurrence["user_voice"],
                    "persona": occurrence["persona"],
                    "scenario": occurrence["scenario"],
                    "build": occurrence["build"],
                })
        who = ", ".join(defect["personas"])
        why = (
            f"{defect['worst_severity']}; hit by {len(defect['personas'])} persona(s) "
            f"({who}) in {defect['runs']} run(s); first seen in build "
            f"{defect['first_seen_build']}; latest build: {defect['latest_build_status']}"
        )
        suggestions.append({
            "rank": rank,
            "target": defect["target"],
            "fingerprint": defect["fingerprint"],
            "headline": defect["title"],
            "suggestion": template.format(surface=defect["surface"], action=action),
            "why": why,
            "quotes": quotes,
        })
    return suggestions


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _scenario_label(scenario_id: str | None) -> str:
    return scenario_id or "(no scenario)"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(data: dict[str, Any]) -> str:
    runs = data["runs"]
    included = [r for r in runs if r["included"]]
    excluded = [r for r in runs if not r["included"]]
    defects = data["defects"]
    out: list[str] = []
    w = out.append

    w("# Persona QA report")
    w("")
    w(f"Generated {data['generated_at']} from `{data['runs_dir']}`: "
      f"{len(included)} of {len(runs)} run(s) included, "
      f"{len(defects)} distinct accepted defect(s), "
      f"{sum(1 for d in defects if d['runs'] > 1)} seen in more than one run.")
    w("")
    w("Built only from `findings.reviewed.json` of runs that pass the review gate. "
      "It says where people struggled, not that anything else works: read the "
      "coverage gaps before reading the absence of a defect as good news.")
    w("")

    w("## Improvement suggestions")
    w("")
    if not data["suggestions"]:
        w("No accepted findings in the included runs. That is a result about these "
          "runs' coverage, listed below, not a statement that the app is fine.")
        w("")
    for s in data["suggestions"]:
        w(f"### {s['rank']}. {s['headline']}")
        w("")
        w(s["suggestion"])
        w("")
        for q in s["quotes"]:
            w(f"> \"{q['quote']}\"  ")
            w(f"> -- {q['persona']}, {_scenario_label(q['scenario'])}, build {q['build']}")
            w("")
        w(f"*Why here:* {s['why']}. `{s['fingerprint']}`")
        w("")

    w("## Defects across runs")
    w("")
    if defects:
        w("| fingerprint | defect | worst | runs | personas | scenarios | first seen | latest build |")
        w("|---|---|---|---|---|---|---|---|")
        for d in defects:
            w(f"| `{d['fingerprint']}` | {_md_escape(d['title'])} "
              f"({d['surface']}, {d['action_class']}, {d['symptom_class']}) "
              f"| {d['worst_severity']} | {d['runs']} | {', '.join(d['personas'])} "
              f"| {', '.join(_scenario_label(s) for s in d['scenarios'])} "
              f"| {d['first_seen_build']} | {d['latest_build_status']} |")
    else:
        w("None.")
    w("")

    for table in data["coverage"]:
        w(f"## Coverage: {table['target']} (latest build {table['latest_build']})")
        w("")
        w("Each cell: runs / accepted findings. `--` is a cell nobody has run.")
        w("")
        header = ["persona"] + [_scenario_label(s) for s in table["scenarios"]]
        w("| " + " | ".join(header) + " |")
        w("|" + "---|" * len(header))
        cells = {(c["persona"], c["scenario"]): c for c in table["cells"]}
        for persona_id in table["personas"]:
            row = [persona_id]
            for scenario_id in table["scenarios"]:
                c = cells[(persona_id, scenario_id)]
                row.append(f"{c['runs']} / {c['accepted']}" if c["runs"] else "--")
            w("| " + " | ".join(row) + " |")
        w("")
        if table["gaps"]:
            w(f"**Gaps ({len(table['gaps'])}):** "
              + "; ".join(f"{g['persona']} x {g['scenario']}" for g in table["gaps"]))
            w("")
        if table["coverage_notes"]:
            w("**What the runs say they did not reach** (`coverage_note`):")
            w("")
            for note in table["coverage_notes"]:
                w(f"- *{note['run_id']}* ({note['persona']}, "
                  f"{_scenario_label(note['scenario'])}): {_md_escape(note['note'])}")
            w("")

    w("## Runs")
    w("")
    w("| run | persona | scenario | build | steps | ended | accepted | gate | trust |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        accepted = r["accepted_count"] if r["included"] else "--"
        w(f"| {r['run_id']} | {r['persona']} | {_scenario_label(r['scenario'])} "
          f"| {r['build']} | {r['steps']} | {r['ended']} | {accepted} "
          f"| {r['gate']} | {r['trust'] or '--'} |")
    w("")
    if excluded:
        w("**Excluded:**")
        w("")
        for r in excluded:
            why = "; ".join(r.get("excluded_because") or ["could not be read"])
            first = f" -- {_md_escape(r['gate_errors'][0])}" if r.get("gate_errors") else ""
            w(f"- {r['run_id']}: {why}{first}")
        w("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# feedback-synthesizer output
# --------------------------------------------------------------------------

def check_improvements(path: str | Path, runs_dir: str | Path) -> Report:
    """Hold feedback-synthesizer's recommendations to the reviewed evidence.

    The same principle as the gate's evidence check: a recommendation must cite
    defects that survived review in a gated run, and its quotes must be the
    personas' words verbatim. A synthesizer free to paraphrase will translate
    "my stuff is gone" into engineering language, which is where the signal is
    lost (DESIGN.md section 2).
    """
    report = Report()
    try:
        data = read_json(path)
    except PqaError as exc:
        report.error(Path(path).name, str(exc))
        return report

    report.extend(validate.check_instance(data, "improvements.schema.json", Path(path).name))
    if not report.ok:
        return report

    aggregate = build(runs_dir)
    defects = {d["fingerprint"]: d for d in aggregate["defects"]}
    run_ids = {r["run_id"] for r in aggregate["runs"] if r["included"]}

    for run_id in data.get("source_runs", []):
        report.checks_run += 1
        if run_id not in run_ids:
            report.error("source_runs", f"{run_id} is not an included run under {runs_dir}")

    addressed: set[str] = set()
    for rec in data["recommendations"]:
        where = f"recommendations[{rec['id']}]"
        cited = rec["addresses"]
        report.checks_run += len(cited) + len(rec["user_voice"]) + 1
        for fp in cited:
            if fp not in defects:
                report.error(
                    where,
                    f"addresses {fp}, which is not an accepted finding in any included run",
                    "recommendations rest on reviewed findings only. A defect "
                    "the reviewer rejected, or that only a gate-failing run "
                    "found, is not evidence",
                )
        addressed.update(cited)

        for quote in rec["user_voice"]:
            defect = defects.get(quote["fingerprint"])
            if quote["fingerprint"] not in cited:
                report.error(where, f"quotes {quote['fingerprint']}, which it does not address")
            if defect and not any(
                quote["quote"] in o["user_voice"] for o in defect["occurrences"]
            ):
                report.error(
                    where,
                    f"quote {quote['quote']!r} is not in any user_voice for {quote['fingerprint']}",
                    "quote the persona verbatim. A paraphrase is the synthesizer's "
                    "words wearing the persona's quotation marks",
                )

        hit_by = {p for fp in cited if fp in defects for p in defects[fp]["personas"]}
        stray = sorted(set(rec["personas"]) - hit_by)
        if stray:
            report.error(where, f"names persona(s) {stray} that hit none of the cited defects")

    report.checks_run += 1
    skipped = {fp for fp in defects} - addressed - {
        n["fingerprint"] for n in data.get("not_addressed", [])
    }
    serious = sorted(fp for fp in skipped if defects[fp]["worst_severity"] in ("blocker", "major"))
    if serious:
        report.error(
            "recommendations",
            f"blocker/major defect(s) {serious} are neither addressed nor listed in not_addressed",
            "leaving the worst defect out of a recommendations list is the "
            "quietest way to drop it. Address it, or say why not",
        )
    return report
