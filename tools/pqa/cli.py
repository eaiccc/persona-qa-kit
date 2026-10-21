"""Command line entry point.

Exit codes are part of the contract, because hooks read them:

    0   the check passed
    1   the command could not run (bad path, unreadable file, usage error)
    2   a constraint was violated -- this is what stops a Claude Code hook

Distinguishing 1 from 2 matters. A gate that exits 1 when it cannot find the run
directory and 2 when the review is missing lets the caller tell "the check could
not run" from "the check ran and failed". Collapsing them means a broken gate
looks like a passing one, or a passing one looks broken.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import (
    drift, fingerprint, gate, guard, hook, hookio, live, personas, report, roles,
    scenarios, signals, validate,
)
from .core import (
    PqaError, Report, RunDir, active_run, colorize, digest_file, iso_now, kit_root,
    read_jsonl, set_active,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VIOLATION = 2


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_new_run(args: argparse.Namespace) -> int:
    card = personas.load(args.persona)
    scenario = scenarios.load(args.scenario) if args.scenario else None

    slug = args.run_id or _slug(args.target)
    run_id = slug if re.match(r"^\d{4}-\d{2}-\d{2}-", slug) else f"{iso_now()[:10]}-{slug}"
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$", run_id):
        raise PqaError(f"generated run_id {run_id!r} is not a valid slug; pass --run-id")

    out = Path(args.out) if args.out else kit_root() / "runs" / run_id
    if (out / "run.json").exists():
        raise PqaError(f"{out / 'run.json'} already exists; runs are not overwritten")
    out.mkdir(parents=True, exist_ok=True)

    if scenario is not None:
        _check_scenario_fit(scenario, card, args)

    manifest = {
        "run_id": run_id,
        "created_at": iso_now(),
        "created_by": "qa-orchestrator",
        "persona": {"id": card.id, "version": card.version, "card_digest": card.digest},
    }
    if scenario is not None:
        # Beside `persona`: together they say who, and doing what.
        manifest["scenario"] = scenario.manifest_block()
    manifest.update({
        "target": {"name": args.target, "build": args.build},
        "stopping_conditions": {
            "step_budget": args.budget,
            "goal_satisfaction": {
                # The scenario's completion test when there is one; the card's
                # is the fallback for runs that only say who, not what.
                "success_criteria": (
                    scenario.success_criteria if scenario else card.success_criteria
                ),
                "criteria_source": "scenario" if scenario else "persona",
                "allow_unreachable": True,
            },
            "blocked_state": {"no_state_change_steps": args.blocked_after},
            "scope_bounds": {"out_of_scope": card.out_of_scope},
        },
        "drift_control": {
            "reinject_every_steps": args.reinject_every,
            "enforce_never_uses": not args.lenient_vocabulary,
        },
    })
    if scenario is not None:
        manifest["stopping_conditions"]["scope_bounds"]["in_scope_surfaces"] = (
            scenario.in_scope_surfaces
        )
    if args.platform:
        manifest["target"]["platform"] = args.platform
    if args.entrypoint:
        manifest["target"]["entrypoint"] = args.entrypoint

    report = validate.check_instance(manifest, "run.schema.json", "run.json")
    if not report.ok:
        print(report.render("generated manifest is invalid"), file=sys.stderr)
        return EXIT_USAGE

    (out / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out / 'run.json'}")
    print(f"seal it before the session starts:\n\n    pqa seal {out}\n")
    return EXIT_OK


def _check_scenario_fit(
    scenario: scenarios.Scenario, card: personas.PersonaCard, args: argparse.Namespace
) -> None:
    """Refuse a scenario that cannot run within the budget; warn on the rest."""
    late = [p for p in scenario.perturbations if p["at_step"] >= args.budget]
    if late:
        raise PqaError(
            f"scenario {scenario.id} seals a perturbation after step "
            f"{late[0]['at_step']}, which a {args.budget}-step budget can never "
            "reach. Raise --budget or use a different scenario"
        )

    target = scenario.data.get("target")
    if target and target != args.target:
        print(
            f"pqa: warning: scenario {scenario.id} is written for {target!r}, "
            f"this run targets {args.target!r}",
            file=sys.stderr,
        )

    # The task is re-injected into the session alongside the persona summary. A
    # forbidden term in it primes exactly the drift the tripwire then catches.
    texts = [{"step": 0, "expected": scenario.data["task"]}] + [
        {"step": 0, "observed": c} for c in scenario.success_criteria
    ]
    for hit in drift.scan(texts, card.never_uses):
        print(
            f"pqa: warning: scenario {scenario.id} uses {hit.term!r}, which "
            f"{card.id} never says ({hit.quote!r})",
            file=sys.stderr,
        )


def cmd_seal(args: argparse.Namespace) -> int:
    run = RunDir.open(args.run_dir)

    if not run.run_json.exists():
        raise PqaError(f"{run.run_json} does not exist")

    if run.session.exists():
        print(
            colorize("FAIL", "red", sys.stdout.isatty())
            + f"  {run.session.name} already exists -- this run cannot be sealed.\n"
            "      Sealing after execution would let the stopping conditions be\n"
            "      chosen with knowledge of the results, which is the failure\n"
            "      mode the seal exists to prevent (DESIGN.md section 3).",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    from .core import read_json

    report = validate.check_instance(read_json(run.run_json), "run.schema.json", "run.json")
    if not report.ok:
        print(report.render("run.json is invalid"), file=sys.stderr)
        return EXIT_VIOLATION

    manifest = read_json(run.run_json)
    lock = {
        "run_id": manifest["run_id"],
        "sealed_at": iso_now(),
        "run_digest": digest_file(run.run_json),
        "persona_card_digest": manifest["persona"]["card_digest"],
    }
    if manifest.get("scenario"):
        lock["scenario_digest"] = manifest["scenario"]["scenario_digest"]
    run.lock.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    displaced = set_active(run.path)
    print(f"sealed {run.run_json.name} -> {run.lock.name}")
    print(f"  step budget: {manifest['stopping_conditions']['step_budget']}")
    print(f"  persona:     {manifest['persona']['id']} v{manifest['persona']['version']}")
    if manifest.get("scenario"):
        sc = manifest["scenario"]
        print(f"  scenario:    {sc['id']} v{sc['version']} "
              f"({len(sc['perturbations'])} perturbation(s))")
    print("  active:      the hooks now guard this run")
    if displaced:
        print(
            f"\n  note: {displaced.name} was the active run and no longer is. Its\n"
            "  hooks stop firing. To run two at once, launch each session with\n"
            "  PQA_RUN_DIR set to its own run directory."
        )
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    if args.self_check:
        report = validate.self_check()
        print(report.render("schema self-check"))
        return EXIT_OK if report.ok else EXIT_VIOLATION

    if args.run_dir is None:
        raise PqaError("pass a run directory, or --self-check")

    report = validate.check_run_dir(RunDir.open(args.run_dir))
    print(report.render(f"validate {args.run_dir}"))
    return EXIT_OK if report.ok else EXIT_VIOLATION


def cmd_gate(args: argparse.Namespace) -> int:
    if args.format != "text":
        # The gate as a Stop / SubagentStop hook; see hook.gate_hook.
        run_dir = Path(args.run_dir) if args.run_dir else None
        return hook.gate_hook(_stdin(), args.format, run_dir, args.stage).emit()

    if args.run_dir is None:
        raise PqaError("pass a run directory (only hook formats locate the run themselves)")
    run = RunDir.open(args.run_dir)
    stage = gate.DEFAULT_STAGE if args.stage == "auto" else args.stage
    report = gate.run_gate(run, stage)
    suffix = "" if stage == gate.DEFAULT_STAGE else f" (stage: {stage})"
    print(report.render(f"review gate: {run.path.name}{suffix}"))

    if not report.ok:
        # Both streams are usually the same pipe when a hook calls this, and
        # stdout is block-buffered there while stderr is not. Without the flush
        # the closing note overtakes the findings it refers to.
        sys.stdout.flush()
        print(
            "\n  The pipeline stops here. This is not an instruction the agent can\n"
            "  decline -- see DESIGN.md section 1 on constraints versus requests.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION
    return EXIT_OK


def _stdin() -> str:
    """The hook payload, optionally appended to $PQA_HOOK_LOG for inspection."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return ""
    log = os.environ.get("PQA_HOOK_LOG")
    if log and raw.strip():
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(raw.strip().replace("\n", " ") + "\n")
        except OSError:
            pass
    return raw


def cmd_drift(args: argparse.Namespace) -> int:
    from .core import read_json

    run = RunDir.open(args.run_dir)
    manifest = read_json(run.run_json)
    card = personas.load(manifest["persona"]["id"])

    records = [record for _, record in read_jsonl(run.session)[0] if isinstance(record, dict)]
    violations = drift.scan(records, card.never_uses)

    every = manifest.get("drift_control", {}).get("reinject_every_steps", 8)
    gaps = drift.cadence_gaps(records, every, scenarios.last_step(records))
    injections = drift.injection_steps(records)

    print(f"  persona:     {card.id} v{card.version}")
    print(f"  never_uses:  {len(card.never_uses)} terms")
    print(f"  injections:  {injections or 'none'}")
    if gaps:
        print(f"  missed at:   {gaps}")
    print()

    if not violations:
        print(colorize("  OK", "green", sys.stdout.isatty()) + "    no vocabulary breaches")
        print(
            "\n  A clean lexical check is not evidence the persona held. It cannot\n"
            "  see an agent that avoids every forbidden word while troubleshooting\n"
            "  with a patience the persona does not have."
        )
        return EXIT_OK

    for violation in violations:
        print(f"  {colorize('BREACH', 'red', sys.stdout.isatty())}  {violation}")
    print(f"\n  {len(violations)} breach(es) across {len({v.step for v in violations})} step(s)")
    return EXIT_VIOLATION


def cmd_drift_tick(args: argparse.Namespace) -> int:
    """One live drift check: the persona-drift-check hook, in each platform's envelope.

    Claude Code and Codex call it after a write (PostToolUse); Antigravity, whose
    PostToolUse can neither block nor inject, calls it before each model call
    (PreInvocation). See hook.drift_tick for what it checks and, as importantly,
    what it no longer does: ask anyone to rewrite the log.
    """
    if args.run_dir:
        # Named by hand (or by a runner), not by a harness: there is no hook
        # payload to read, and waiting on stdin for one would hang.
        return hook.drift_hook("", args.format, Path(args.run_dir)).emit()
    return hook.drift_hook(_stdin(), args.format).emit()


def cmd_guard(args: argparse.Namespace) -> int:
    """PreToolUse: refuse a tool call that would break the run. See guard.py."""
    payload = hookio.read_payload()
    run_dir = Path(args.run_dir) if args.run_dir else active_run()
    run = None
    if run_dir is not None and (Path(run_dir) / "run.json").exists():
        run = RunDir.open(run_dir)

    role = roles.lookup(args.role or hookio.agent_type(payload))
    intent = hookio.intent_from_payload(payload)
    try:
        reason = guard.evaluate(intent, cwd=hookio.payload_cwd(payload), run=run,
                                role=role, kit=kit_root())
    except PqaError as exc:
        reason = f"the run guard could not check this call: {exc}"
    if reason:
        return hookio.deny(args.format, reason).emit()
    return hookio.allow(args.format).emit()


def cmd_record(args: argparse.Namespace) -> int:
    """Append one validated session record, then run the drift tick on it."""
    raw = args.record if args.record != "-" else sys.stdin.read()
    ok, message = live.record(RunDir.open(args.run_dir), raw)
    print(message, file=sys.stdout if ok else sys.stderr)
    return EXIT_OK if ok else EXIT_VIOLATION


def cmd_signals(args: argparse.Namespace) -> int:
    summaries = signals.collect(args.runs_dir)
    print(signals.render_table(summaries))
    print()
    report = signals.analyze(summaries)
    print(report.render("pipeline health"))
    print(
        "\n  These describe the pipeline, not the app. Warnings here mean the\n"
        "  output is still well-formed but has stopped being informative."
    )
    return EXIT_OK


def cmd_personas(args: argparse.Namespace) -> int:
    ids = personas.list_ids()
    if not ids:
        print("  no persona cards in personas/")
        return EXIT_OK

    for persona_id in ids:
        try:
            card = personas.load(persona_id)
        except PqaError as exc:
            print(f"  {colorize('INVALID', 'red', sys.stdout.isatty())} {persona_id}: {exc}")
            continue
        behavior = card.data.get("behavior", {})
        print(
            f"  {card.id:<26} v{card.version:<8} "
            f"patience={behavior.get('patience', '?'):<9} "
            f"reading={behavior.get('reading', '?'):<15} "
            f"never_uses={len(card.never_uses)}"
        )
    return EXIT_OK


def cmd_scenarios(args: argparse.Namespace) -> int:
    ids = scenarios.list_ids()
    if not ids:
        print("  no scenarios in scenarios/")
        return EXIT_OK

    for scenario_id in ids:
        try:
            scenario = scenarios.load(scenario_id)
        except PqaError as exc:
            print(f"  {colorize('INVALID', 'red', sys.stdout.isatty())} {scenario_id}: {exc}")
            continue
        perturbs = ", ".join(
            f"{p['do']}@{p['at_step']}" for p in scenario.perturbations
        ) or "none"
        print(
            f"  {scenario.id:<26} v{scenario.version:<8} "
            f"target={scenario.data.get('target', 'any'):<12} "
            f"criteria={len(scenario.success_criteria)} perturbations={perturbs}"
        )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    data = report.build(args.runs_dir, include_ungated=args.include_ungated)
    text = report.render_json(data) if args.format == "json" else report.render_markdown(data)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text, end="")
    if args.json:
        Path(args.json).write_text(report.render_json(data), encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return EXIT_OK


def cmd_check_improvements(args: argparse.Namespace) -> int:
    result = report.check_improvements(args.file, args.runs_dir)
    print(result.render(f"improvements: {Path(args.file).name}"))
    return EXIT_OK if result.ok else EXIT_VIOLATION


def cmd_inject(args: argparse.Namespace) -> int:
    card = personas.load(args.persona)
    task = scenarios.load(args.scenario).data["task"] if args.scenario else None
    if args.record:
        print(json.dumps({
            "kind": "persona_injection",
            "step": args.step,
            "ts": iso_now(),
            "persona_id": card.id,
            "card_digest": card.digest,
        }))
        return EXIT_OK
    print(personas.summary_block(card, task))
    return EXIT_OK


def cmd_digest(args: argparse.Namespace) -> int:
    """The `sha256:<hex>` form that session_digest and candidates_digest expect."""
    print(digest_file(args.path))
    return EXIT_OK


def cmd_fingerprint(args: argparse.Namespace) -> int:
    print(fingerprint.compute(args.surface, args.action, args.symptom))
    return EXIT_OK


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pqa",
        description="persona-qa-kit: enforcement for persona-based agentic QA",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new-run", help="write a run manifest (qa-orchestrator's mechanical half)")
    p.add_argument("--persona", required=True, help="persona id, e.g. time-starved-parent")
    p.add_argument("--scenario",
                   help="scenario id, e.g. checkout-interrupted. Supplies the task, "
                        "success criteria, scope and perturbations")
    p.add_argument("--target", required=True, help="app name")
    p.add_argument("--build", required=True, help="commit sha, version, or build number")
    p.add_argument("--budget", type=int, default=40, help="step budget (default: 40)")
    p.add_argument("--blocked-after", type=int, default=3,
                   help="steps with no state change before terminating (default: 3)")
    p.add_argument("--reinject-every", type=int, default=8,
                   help="persona re-injection cadence (default: 8)")
    p.add_argument("--lenient-vocabulary", action="store_true",
                   help="warn instead of failing on never_uses hits, for card calibration")
    p.add_argument("--platform", help="e.g. 'iOS 18, iPhone 13'")
    p.add_argument("--entrypoint", help="URL or launch command")
    p.add_argument("--run-id", help="override the generated run id")
    p.add_argument("--out", help="run directory (default: runs/<run-id>)")
    p.set_defaults(func=cmd_new_run)

    p = sub.add_parser("seal", help="freeze run.json before the session starts")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("validate", help="schema-check artifacts")
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-check", action="store_true",
                   help="check the schemas and persona cards themselves")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("gate", help="the review gate; exits 2 if the run may not advance")
    p.add_argument("run_dir", nargs="?",
                   help="run directory (hook formats locate the active run when omitted)")
    p.add_argument("--stage", choices=("auto",) + roles.STAGES, default="auto",
                   help="check only what must exist once this stage's role stops. 'auto' "
                        "(default) means 'final' from the command line, and in a hook "
                        "the stopping agent's stage, from its agent_type or $PQA_ROLE")
    p.add_argument("--format", choices=hookio.FORMATS, default="text",
                   help="'text' for people; otherwise the hook envelope of that platform")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("drift", help="lexical persona drift check")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("drift-tick", help="live drift check, called by the hook mid-session")
    p.add_argument("run_dir", nargs="?", help="run directory (default: the active run)")
    p.add_argument("--format", choices=hookio.FORMATS[1:], default="claude",
                   help="hook envelope to emit (default: claude)")
    p.set_defaults(func=cmd_drift_tick)

    p = sub.add_parser("guard", help="PreToolUse guard: deny writes that would break the run")
    p.add_argument("run_dir", nargs="?", help="run directory (default: the active run)")
    p.add_argument("--format", choices=hookio.FORMATS, default="text")
    p.add_argument("--role", help="acting role, when the payload carries no agent_type")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("record", help="append one validated record to session.jsonl")
    p.add_argument("run_dir")
    p.add_argument("record", help="one JSON object, or - to read it from stdin")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("signals", help="cross-run pipeline health")
    p.add_argument("runs_dir", nargs="?", default=str(kit_root() / "runs"))
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("personas", help="list and validate persona cards")
    p.set_defaults(func=cmd_personas)

    p = sub.add_parser("scenarios", help="list and validate scenarios")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("report", help="cross-run findings, coverage and improvement report")
    p.add_argument("runs_dir", nargs="?", default=str(kit_root() / "runs"))
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("-o", "--output", help="write the report here instead of stdout")
    p.add_argument("--json", help="also write the JSON form to this path")
    p.add_argument("--include-ungated", action="store_true",
                   help="aggregate runs that fail the review gate (marked as such)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("check-improvements",
                       help="verify a feedback-synthesizer file against the reviewed findings")
    p.add_argument("file")
    p.add_argument("runs_dir", nargs="?", default=str(kit_root() / "runs"))
    p.set_defaults(func=cmd_check_improvements)

    p = sub.add_parser("inject", help="print the persona re-injection block")
    p.add_argument("--persona", required=True)
    p.add_argument("--scenario", help="replace the card's goals with this scenario's task")
    p.add_argument("--record", action="store_true",
                   help="emit a persona_injection JSONL record instead of the prose block")
    p.add_argument("--step", type=int, default=0)
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("digest", help="print a file's digest, as artifacts cite it")
    p.add_argument("path")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("fingerprint", help="compute a defect fingerprint")
    p.add_argument("--surface", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--symptom", required=True)
    p.set_defaults(func=cmd_fingerprint)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PqaError as exc:
        print(f"pqa: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
