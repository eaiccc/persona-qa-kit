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
import re
import sys
from pathlib import Path

from . import drift, fingerprint, gate, personas, signals, validate
from .core import (
    PqaError, Report, RunDir, colorize, digest_file, iso_now, kit_root, read_jsonl,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VIOLATION = 2


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_new_run(args: argparse.Namespace) -> int:
    card = personas.load(args.persona)

    slug = args.run_id or _slug(args.target)
    run_id = slug if re.match(r"^\d{4}-\d{2}-\d{2}-", slug) else f"{iso_now()[:10]}-{slug}"
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$", run_id):
        raise PqaError(f"generated run_id {run_id!r} is not a valid slug; pass --run-id")

    out = Path(args.out) if args.out else kit_root() / "runs" / run_id
    if (out / "run.json").exists():
        raise PqaError(f"{out / 'run.json'} already exists; runs are not overwritten")
    out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": run_id,
        "created_at": iso_now(),
        "created_by": "qa-orchestrator",
        "persona": {"id": card.id, "version": card.version, "card_digest": card.digest},
        "target": {"name": args.target, "build": args.build},
        "stopping_conditions": {
            "step_budget": args.budget,
            "goal_satisfaction": {
                "success_criteria": card.success_criteria,
                "allow_unreachable": True,
            },
            "blocked_state": {"no_state_change_steps": args.blocked_after},
            "scope_bounds": {"out_of_scope": card.out_of_scope},
        },
        "drift_control": {
            "reinject_every_steps": args.reinject_every,
            "enforce_never_uses": not args.lenient_vocabulary,
        },
    }
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
    run.lock.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"sealed {run.run_json.name} -> {run.lock.name}")
    print(f"  step budget: {manifest['stopping_conditions']['step_budget']}")
    print(f"  persona:     {manifest['persona']['id']} v{manifest['persona']['version']}")
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
    run = RunDir.open(args.run_dir)
    report = gate.run_gate(run)
    print(report.render(f"review gate: {run.path.name}"))

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


def cmd_drift(args: argparse.Namespace) -> int:
    from .core import read_json

    run = RunDir.open(args.run_dir)
    manifest = read_json(run.run_json)
    card = personas.load(manifest["persona"]["id"])

    records = [record for _, record in read_jsonl(run.session)[0] if isinstance(record, dict)]
    violations = drift.scan(records, card.never_uses)

    steps = [r["step"] for r in records if r.get("kind") == "observation"]
    every = manifest.get("drift_control", {}).get("reinject_every_steps", 8)
    gaps = drift.cadence_gaps(records, every, max(steps) if steps else 0)
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
    """One live drift check, called by the persona-drift-check hook mid-session.

    Two jobs, both of which have to happen while the session is still running:

      * re-inject the persona summary on cadence, and record that it happened
      * fail the step when a forbidden term appears

    Catching a vocabulary breach at step 14 and stopping is worth far more than
    reporting it after the run: by the end of a drifted session every subsequent
    step is already contaminated.
    """
    from .core import read_json

    run = RunDir.open(args.run_dir)
    if not run.run_json.exists():
        return EXIT_OK

    manifest = read_json(run.run_json)
    control = manifest.get("drift_control", {})
    every = control.get("reinject_every_steps", 8)
    enforce = control.get("enforce_never_uses", True)

    card = personas.load(manifest["persona"]["id"])
    records: list = []
    if run.session.exists():
        records = [r for _, r in read_jsonl(run.session)[0] if isinstance(r, dict)]

    violations = drift.scan(records, card.never_uses)
    if violations and enforce:
        recent = violations[-3:]
        print(
            "Persona drift detected. "
            f"{card.data['display_name']} does not have these words:\n"
            + "\n".join(f"  - {v}" for v in recent)
            + "\n\nRewrite the affected session records in the persona's own "
            "vocabulary before continuing. What you are describing may well be "
            "real, but it is not what this persona would have noticed, and a "
            "drifted session's coverage claim is worthless.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    steps = [r["step"] for r in records if r.get("kind") == "observation"]
    last = max(steps) if steps else 0
    already = set(drift.injection_steps(records))

    if last > 0 and last % every == 0 and not (already & {last - 1, last, last + 1}):
        with run.session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "kind": "persona_injection",
                "step": last,
                "ts": iso_now(),
                "persona_id": card.id,
                "card_digest": card.digest,
            }) + "\n")
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": personas.summary_block(card),
            }
        }))

    return EXIT_OK


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


def cmd_inject(args: argparse.Namespace) -> int:
    card = personas.load(args.persona)
    if args.record:
        print(json.dumps({
            "kind": "persona_injection",
            "step": args.step,
            "ts": iso_now(),
            "persona_id": card.id,
            "card_digest": card.digest,
        }))
        return EXIT_OK
    print(personas.summary_block(card))
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
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("drift", help="lexical persona drift check")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("drift-tick", help="live drift check, called by the hook mid-session")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_drift_tick)

    p = sub.add_parser("signals", help="cross-run pipeline health")
    p.add_argument("runs_dir", nargs="?", default=str(kit_root() / "runs"))
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("personas", help="list and validate persona cards")
    p.set_defaults(func=cmd_personas)

    p = sub.add_parser("inject", help="print the persona re-injection block")
    p.add_argument("--persona", required=True)
    p.add_argument("--record", action="store_true",
                   help="emit a persona_injection JSONL record instead of the prose block")
    p.add_argument("--step", type=int, default=0)
    p.set_defaults(func=cmd_inject)

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
