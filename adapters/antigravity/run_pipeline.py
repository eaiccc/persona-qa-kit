#!/usr/bin/env python3
"""Run the persona QA pipeline on Antigravity, one SDK agent per role.

Why a runner rather than hooks alone: Antigravity hooks have no SubagentStop
and carry no agent identity, so "user-agent may stop once the session log is
valid, qa-reviewer once the review is" cannot be expressed in .agents/hooks.json.
Role separation therefore lives here. Each role is its own `Agent` with:

  * system instructions = the role's .claude/agents/<role>.md body (the single
    source of truth, read at runtime) plus a short Antigravity preamble
  * capabilities trimmed to what the role needs, no subagents
  * a PreToolCall decide hook running pqa's guard with the role known, so the
    judging roles are read-only except for their own output file
  * a Stop hook running `pqa gate --stage <role's stage>`, which sends the
    agent back with the gate's findings instead of letting it stop
  * for user-agent, a `record_step` tool as the only way to write
    session.jsonl: it validates each record, refuses never_uses vocabulary,
    and carries the persona re-injection back in its result

Between roles the runner itself calls `pqa gate --stage`, so a role that
exhausted its continuations still cannot hand a broken artifact downstream.
qa-orchestrator's mechanical half (new-run, seal) is done by the runner from its
arguments; nothing about the stopping conditions is left to a model.

    pip install google-antigravity        # in a venv; see adapters/antigravity/README.md
    python adapters/antigravity/run_pipeline.py \\
        --persona time-starved-parent --target smoke-shopping-list --build $(git rev-parse --short HEAD) \\
        --entrypoint http://127.0.0.1:8765/ --budget 8 --reinject-every 4

Exit codes follow pqa: 0 the final gate passed, 1 the runner could not run,
2 a gate refused the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))

from pqa import gate, guard, hookio, live, roles  # noqa: E402
from pqa.core import PqaError, RunDir, iso_now  # noqa: E402

PQA = str(KIT / "bin" / "pqa")

try:
    from google.antigravity import Agent, BuiltinTools, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity import types as agy_types
    from google.antigravity.hooks import hooks as agy_hooks
    from google.antigravity.hooks import policy as agy_policy
except ImportError:  # pragma: no cover - depends on the environment
    Agent = None


# --------------------------------------------------------------------------
# per-role configuration
# --------------------------------------------------------------------------

# Tools no pipeline role needs. Subagents are off as well: a role that could
# delegate could hand its judgment to an agent outside the role table.
_NEVER = ["INVOKE_SUBAGENT", "RUN_WORKFLOW", "GENERATE_IMAGE", "SEARCH_WEB", "SCHEDULE"]


def disabled_tools(role: roles.Role) -> list:
    names = list(_NEVER)
    if role.name == "user-agent":
        # session.jsonl is written through record_step only, so the vocabulary
        # check refuses the write instead of reporting it afterwards.
        names += ["CREATE_FILE", "EDIT_FILE"]
    else:
        names += ["READ_URL_CONTENT"]  # the judging roles never see the app
    return [getattr(BuiltinTools, n) for n in names if hasattr(BuiltinTools, n)]


def preamble(role: roles.Role, run: RunDir, manifest: dict) -> str:
    lines = [
        f"You are the `{role.name}` role of persona-qa-kit, running as an Antigravity agent.",
        "",
        f"The run directory is {run.path}. The kit root, and your working directory, is {KIT}.",
        "The definition below was written for Claude Code: Read means view_file, Write and Edit",
        "mean create_file and edit_file, and Bash means run_command.",
        "",
    ]
    if role.name == "user-agent":
        target = manifest.get("target", {})
        lines += [
            f"The app is at {target.get('entrypoint', '(see run.json)')}. Use it with the tools",
            "you have (read_url_content, or curl through run_command). Describe what a person",
            "would see on the screen, not the markup.",
            "",
            "Write every session record with the record_step tool, one JSON object per call.",
            "It is the only way to write session.jsonl. It refuses a record that fails the",
            "schema or uses words your persona does not have, and when a persona re-injection",
            "is due its result carries your persona summary: read it when it appears.",
        ]
    else:
        out = ", ".join(f"{run.path}/{w}" for w in role.writes)
        lines += [
            f"You may write {out} and nothing else. Your run_command is read-only: reading files",
            "and `./bin/pqa fingerprint|digest|drift|validate|gate|personas|inject|signals`.",
            "Digests cited in your output come from `./bin/pqa digest <file>`.",
        ]
    lines += [
        "",
        "These are enforced, not requested: a hook denies tool calls that would break the run,",
        f"and when you stop, `pqa gate --stage {role.stage}` runs and sends you back with its",
        "findings until it passes.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def task_prompt(role: roles.Role, run: RunDir) -> str:
    rel = os.path.relpath(run.path, KIT)
    return {
        "user-agent": (
            f"Run {rel}: read run.json and your persona card, then use the app as the persona "
            "and record the session with record_step until a stopping condition is met. End "
            "with a 'terminated' record."
        ),
        "qa-agent": f"Run {rel}: read session.jsonl and write findings.candidates.json.",
        "qa-reviewer": (
            f"Run {rel}: review findings.candidates.json against session.jsonl and write "
            "findings.reviewed.json."
        ),
        "persona-evaluator": (
            f"Run {rel}: score the session for persona consistency and write evaluation.json."
        ),
    }[role.name]


# --------------------------------------------------------------------------
# hooks and tools
# --------------------------------------------------------------------------

def intent_from_call(call) -> guard.Intent:
    args = dict(call.args or {})
    if call.canonical_path and not any(k in args for k in ("TargetFile", "AbsolutePath")):
        args["TargetFile"] = call.canonical_path
    return hookio.intent_from_payload({"toolCall": {"name": str(call.name), "args": args}})


def make_hooks(role: roles.Role, run: RunDir, max_continuations: int, log) -> list:
    @agy_hooks.pre_tool_call_decide
    async def run_guard(call):
        reason = guard.evaluate(intent_from_call(call), cwd=KIT, run=run, role=role, kit=KIT)
        if reason:
            log(f"  [{role.name}] denied {call.name}: {reason.splitlines()[0]}")
            return agy_types.HookResult(allow=False, message=reason)
        return agy_types.HookResult(allow=True)

    @agy_hooks.stop
    async def review_gate(args):
        report = gate.run_gate(run, role.stage)
        if report.ok:
            return agy_types.StopHookResult()
        if args.continuation_count >= max_continuations:
            log(f"  [{role.name}] gate still failing after {max_continuations} continuations")
            return agy_types.StopHookResult()
        log(f"  [{role.name}] gate (stage {role.stage}) refused the stop; continuing")
        return agy_types.StopHookResult(
            decision=agy_types.StopDecision.CONTINUE,
            reason=(
                f"The review gate refused this stop (stage: {role.stage}).\n\n"
                + report.render(f"review gate: {run.path.name}")
                + "\n\nFix the artifacts listed above. Do not report the work as complete "
                "while this check is failing."
            ),
        )

    return [run_guard, review_gate]


def make_record_step(run: RunDir, log):
    def record_step(record: str) -> str:
        """Append one record to this run's session log (session.jsonl).

        Args:
            record: exactly one JSON object matching schemas/session-record.schema.json,
                for example {"kind":"observation","step":1,"action":{...},"expected":"...",
                "observed":"...","felt":"..."}. `ts` is filled in when omitted.

        Returns:
            Confirmation, possibly followed by your persona summary when a
            re-injection is due -- or REFUSED and the reason, in which case
            rewrite the record and call again.
        """
        ok, message = live.record(run, record)
        log(f"  [user-agent] record_step: {'ok' if ok else 'REFUSED'}"
            + ("" if ok else f" ({message.splitlines()[0]})"))
        return message if ok else f"REFUSED: {message}"

    return record_step


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------

def pqa(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, NO_COLOR="1")
    return subprocess.run([PQA, *args], capture_output=True, text=True, env=env, cwd=KIT)


def setup_run(args) -> RunDir:
    """qa-orchestrator's mechanical half: write the manifest and seal it."""
    command = ["new-run", "--persona", args.persona, "--target", args.target,
               "--build", args.build, "--budget", str(args.budget),
               "--reinject-every", str(args.reinject_every)]
    for flag, value in (("--entrypoint", args.entrypoint), ("--platform", args.platform),
                        ("--run-id", args.run_id)):
        if value:
            command += [flag, value]
    created = pqa(*command)
    if created.returncode != 0:
        raise PqaError(created.stderr.strip() or created.stdout.strip())
    path = Path(created.stdout.split()[1])  # "wrote <dir>/run.json"
    sealed = pqa("seal", str(path.parent))
    if sealed.returncode != 0:
        raise PqaError(sealed.stderr.strip())
    return RunDir.open(path.parent)


async def run_role(role: roles.Role, run: RunDir, manifest: dict, args, log) -> str:
    definition = roles.load_definition(role.name)
    tools = [make_record_step(run, log)] if role.name == "user-agent" else []
    config = LocalAgentConfig(
        system_instructions=preamble(role, run, manifest) + definition.body,
        capabilities=CapabilitiesConfig(enable_subagents=False, disabled_tools=disabled_tools(role)),
        policies=[agy_policy.allow_all()],  # every call still passes run_guard first
        hooks=make_hooks(role, run, args.max_continuations, log),
        tools=tools,
        workspaces=[str(KIT)],
        # Workspace hooks (.agents/hooks.json) read these, so an interactive
        # hook that fires inside this agent gates it at its own stage.
        env={"PQA_ROLE": role.name, "PQA_RUN_DIR": str(run.path)},
        **({"model": args.model} if args.model else {}),
    )
    async with Agent(config) as agent:
        response = await agent.chat(task_prompt(role, run))
        return await response.text()


async def pipeline(args) -> int:
    log = (lambda msg: print(msg, flush=True))
    run = RunDir.open(args.run_dir) if args.run_dir else setup_run(args)
    manifest = json.loads(run.run_json.read_text(encoding="utf-8"))
    log(f"run {run.path.name}: persona {manifest['persona']['id']}, "
        f"budget {manifest['stopping_conditions']['step_budget']}")

    for name in roles.PIPELINE:
        role = roles.ROLES[name]
        if args.from_role and roles.PIPELINE.index(name) < roles.PIPELINE.index(args.from_role):
            continue
        log(f"\n== {name} (gate stage: {role.stage})  {iso_now()}")
        text = await run_role(role, run, manifest, args, log)
        if text.strip():
            log("  " + text.strip().splitlines()[-1][:200])

        checked = pqa("gate", str(run.path), "--stage", role.stage)
        log(checked.stdout.rstrip())
        if checked.returncode != 0:
            log(f"\n{name} stopped without satisfying its gate stage; the pipeline stops here.")
            return 2

    final = pqa("gate", str(run.path), "--stage", "evaluation")
    log("\n" + final.stdout.rstrip())
    return 0 if final.returncode == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--persona")
    parser.add_argument("--target")
    parser.add_argument("--build")
    parser.add_argument("--entrypoint")
    parser.add_argument("--platform")
    parser.add_argument("--run-id")
    parser.add_argument("--budget", type=int, default=40)
    parser.add_argument("--reinject-every", type=int, default=8)
    parser.add_argument("--run-dir", help="resume an existing, sealed run instead of creating one")
    parser.add_argument("--from-role", choices=roles.PIPELINE,
                        help="with --run-dir, start at this role")
    parser.add_argument("--model", help="model name passed to LocalAgentConfig")
    parser.add_argument("--max-continuations", type=int, default=3,
                        help="Stop-hook continuations per role before the runner gives up")
    args = parser.parse_args(argv)

    if Agent is None:
        print("run_pipeline: the Antigravity SDK is not installed. In a virtualenv:\n\n"
              "    pip install -r adapters/antigravity/requirements.txt\n", file=sys.stderr)
        return 1
    if not args.run_dir and not (args.persona and args.target and args.build):
        parser.error("pass --persona, --target and --build (or --run-dir to resume)")

    try:
        return asyncio.run(pipeline(args))
    except PqaError as exc:
        print(f"run_pipeline: {exc}", file=sys.stderr)
        return 1
    except agy_types.AntigravityValidationError as exc:
        # Most often: no GEMINI_API_KEY. The run directory already exists and
        # is sealed; resume it with --run-dir once the SDK can connect.
        print(f"run_pipeline: the Antigravity SDK could not start an agent: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
