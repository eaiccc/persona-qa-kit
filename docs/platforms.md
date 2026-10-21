# Running on Claude Code, Codex CLI, and Antigravity

The kit's argument (DESIGN.md) holds on any agent platform: what you depend on
has to be enforced outside the agent that gains from breaking it. What changes
between platforms is *where* that enforcement can sit. Each platform offers a
different set of hook events, and those events have different powers.

This page says, per platform, what is enforced, what is only requested, and
what was checked against a real run rather than read off the platform's docs.

## What is shared and what is per platform

Everything that decides anything is shared:

| Shared | |
|---|---|
| `tools/pqa/` | every check: gate, drift tick, guard, `record` |
| `schemas/`, `personas/` | the artifact contracts and persona cards |
| `.claude/agents/*.md` | the **only** copy of each role's instructions |
| `tools/pqa/roles.py` | each role's gate stage and the files it may write |

Each platform gets an envelope and nothing more:

| Platform | Hook wiring | Shim | Roles come from |
|---|---|---|---|
| Claude Code | `.claude/settings.json` | `adapters/claude/hook.sh` | `.claude/agents/*.md` (native) |
| Codex CLI | `.codex/hooks.json` | `adapters/codex/hook.sh` | `.codex/agents/*.toml`, **generated** by `adapters/codex/generate.py` |
| Antigravity | `.agents/hooks.json` (interactive) | `adapters/antigravity/hook.sh` | `adapters/antigravity/run_pipeline.py`, which reads the `.md` files at runtime |

The shims choose an output format and fail closed: if `pqa` itself cannot run,
each one prints its platform's "block" or "deny" rather than passing. The checks
behind them are the same three commands:

```bash
pqa gate   [run] --stage auto|session|candidates|review|final --format text|claude|codex|agy
pqa drift-tick [run] --format claude|codex|agy
pqa guard  [run] --format claude|codex|agy [--role <role>]
```

With a hook format and no run argument, the run is located on disk, never taken
from the agent: `$PQA_RUN_DIR` if the harness was launched with it, else the run
`pqa seal` marked active in `runs/.active` (the final gate clears the mark). No
active run means no QA run is in progress and every hook passes. The newest
`run.json` on disk is not guessed: that answered for whatever run was lying
around, and blocked unrelated sessions in the checkout.

### Gate stages

`pqa gate` used to check everything up to the review, whichever agent was
stopping. That made user-agent's SubagentStop impossible to pass: it demanded
`findings.reviewed.json` from the role whose output that file reviews. Stages
are cumulative:

| Stage | Checks | Applied when this role stops |
|---|---|---|
| `session` | manifest + seal, persona binding, session log, drift | user-agent |
| `candidates` | + `findings.candidates.json` | qa-agent |
| `review` | + `findings.reviewed.json` | qa-reviewer |
| `final` | + `evaluation.json`, checked against the run and the deterministic drift scan (the default) | persona-evaluator, qa-orchestrator, main-thread Stop |

`--stage auto` takes the role from the hook payload (`agent_type`, or Codex's
`agent_role`), then from `$PQA_ROLE`. A subagent that is not a pipeline role
(a search helper, say) is not gated: it produced none of the run's artifacts,
and the run's own final Stop checks everything. A SubagentStop that names no
agent at all is gated on whatever artifacts exist so far. A main-thread Stop
before `session.jsonl` exists passes, so a sealed run does not wedge the
session about to dispatch it.

After three consecutive refusals at one stage (`PQA_GATE_MAX_BLOCKS`) the gate
lets the agent stop, says so to the user, and leaves the run failing -- one
unfixable artifact must not loop a session forever.

## Capability matrix

| Mechanism | Claude Code | Codex CLI | Antigravity: IDE hooks | Antigravity: SDK runner |
|---|---|---|---|---|
| One agent per role, own instructions | subagents | custom agents (`spawn_agent(agent_type=…)`) | none (single agent) | one `Agent` per role |
| Agent identity reaches hooks | `agent_type` | `agent_type` / `agent_role` ⚠︎ | **no** | `PQA_ROLE` per agent, plus in-process hooks |
| Gate when a role stops | SubagentStop, staged | SubagentStop, staged | **no SubagentStop** | Stop hook per agent, staged; runner re-gates between roles |
| Gate when the run stops | Stop, exit 2 | Stop, `decision: block` | Stop, `decision: continue` | runner, `--stage final` |
| `never_uses` tripwire (record kept, breach recorded, session ends) | PostToolUse, exit 2 | PostToolUse `decision: block`, or `pqa record` | PreInvocation message (cannot block) | `record_step` result |
| Session log append-only | ledger, re-derived by the gate | ledger + PreToolUse guard | ledger + PreToolUse guard | ledger + decide hook |
| Persona re-injection | PostToolUse `additionalContext` | PostToolUse `additionalContext` + `pqa record` output | PreInvocation `injectSteps.ephemeralMessage` | in the `record_step` tool result |
| Run-artifact guard (frozen log, sealed manifest, lock) | available, not wired (see below) | PreToolUse deny | PreToolUse deny | in-process decide hook |
| Judging roles read-only except their output | tool allowlist in the `.md` (request: `Write` is allowed) | PreToolUse guard, **only if** the role reaches the hook ⚠︎ | no | enforced: decide hook + read-only shell allowlist |
| Seal, digests, evidence, fingerprints | gate | gate | gate | gate |

⚠︎ = depends on a payload field that was not observed in a real hook call; see
[What was verified](#what-was-verified).

### Enforced vs requested

**Claude Code.** Unchanged from the original design: the review gate and the
drift tripwire are constraints. Read-only judging roles are a request. Each
`.md` lists `Write`, which is the role's own output, but nothing restricts
*which* file. `adapters/claude/hook.sh guard` makes that a constraint too. It is
not wired by default, to keep Claude Code's behaviour as it was. To opt in, add
it to `.claude/settings.json`:

```json
"PreToolUse": [{ "matcher": "Write|Edit|MultiEdit|Bash",
  "hooks": [{ "type": "command", "command": "$CLAUDE_PROJECT_DIR/adapters/claude/hook.sh guard" }] }]
```

The one behaviour change is the stage: a stopping subagent is now gated at its
own role's stage instead of always at `review`.

**Codex CLI.** Same hook model as Claude Code, so the same things are
constraints: the staged gate on SubagentStop and Stop, and the drift tripwire on
PostToolUse. Two differences matter:

- A custom agent file cannot narrow its own sandbox. `sandbox_mode` and
  `approval_policy` in `.codex/agents/*.toml` do not override the parent
  session. So "qa-reviewer may write only findings.reviewed.json" can only be
  enforced by the PreToolUse guard, and only when the hook payload says which
  agent is acting. Without that, the guard still applies its role-independent
  rules: a frozen log, an edit or overwrite of the session log, a sealed
  manifest, and a hand-written `run.lock` or ledger are all denied. A role writing another role's file
  is then caught only by the gate's digest and mapping checks.
- user-agent can append records with `./bin/pqa record <run> -`, which
  validates each one and runs the drift tick on it; the re-injected persona and
  any due perturbation come back as the command's output.

**Antigravity.** The hook contract is narrower, so enforcement moves:

- PostToolUse returns `{}` and nothing else. It cannot block or inject, so
  nothing is wired there.
- Re-injection runs on **PreInvocation** (`injectSteps[].ephemeralMessage`),
  before each model call. That is a transient message, which is closer to the
  point of re-injection (recency) than a PostToolUse context block.
- Blocking runs on **PreToolUse** (`decision: deny`). It refuses an illegal
  write before it happens: any edit or overwrite of the session log, a write to
  a frozen log, a sealed manifest, `run.lock` or the ledger.
- A `never_uses` breach is not refused before the write, on any platform. A
  refused record is retried with a synonym and the drift leaves no trace; the
  record is kept, the drift tick records a `drift_breach` beside it, and the
  session must end there with reason `persona_drift`. On Antigravity the
  breach notice arrives as the next PreInvocation message, since nothing there
  can block after a write; the gate rejects a session that carried on.
- The gate runs on **Stop** (`decision: continue`).
- There is no SubagentStop and no agent identity in any payload. Role
  separation therefore cannot come from hooks, and comes from the SDK runner.

In the IDE, then, Antigravity enforces artifact integrity and the final gate,
but **requests** role separation. Use the runner when separation matters, and
it always matters for a run you intend to believe (DESIGN.md section 1).

The runner enforces in-process what the hooks cannot:

- each role is a separate `Agent` with its `.md` body as system instructions
  and no subagents
- a `pre_tool_call_decide` hook runs `pqa guard` with the role known. The
  judging roles are read-only except for their output file, and their
  `run_command` is limited to a read-only allowlist plus
  `pqa fingerprint|digest|drift|validate|gate|…`
- a `stop` hook runs `pqa gate --stage <role's stage>` and continues the agent
  with the gate's report, up to `--max-continuations` times
- user-agent has no `create_file` or `edit_file`; `record_step` is its only way
  to write the log
- between roles the runner calls `pqa gate --stage` again. A role that used up
  its continuations still cannot pass a broken artifact downstream.

## Codex CLI

Requirements: codex-cli 0.163 or later, with the `hooks` and `multi_agent`
features.

One-time setup, which is yours to do because Codex will not run untrusted hooks:

1. Open the kit in interactive `codex` and accept the project as trusted.
2. Run `/hooks` and trust the four persona-qa-kit hooks. Trust is keyed to a
   hash of each hook definition, so it has to be repeated whenever
   `.codex/hooks.json` changes.

After editing any `.claude/agents/*.md`, regenerate:

```bash
python3 adapters/codex/generate.py
```

`make test` fails if you forget.

Start a run by giving the root session the generated orchestrator prompt and
the run's parameters:

```bash
codex exec --sandbox workspace-write --enable multi_agent - <<EOF
$(cat adapters/codex/orchestrator.md)

Start a run: persona time-starved-parent, target my-app, build $(git rev-parse --short HEAD),
entrypoint http://127.0.0.1:8765/, budget 8, --reinject-every 4.
EOF
```

`qa-orchestrator` is the root session, not a custom agent. In Codex the
session that spawns subagents is the root, so its definition becomes that
session's prompt. The prompt spawns each role with `fork_turns: "none"`, so no
judging role inherits the transcript it is judging.

**Hook matchers.** Shell calls match as `Bash`, and so does unified
`exec_command`. File edits match as `apply_patch`, which also answers to `Edit`
and `Write`. The patch body arrives in `tool_input.command`, and the guard
reads the `*** Add/Update/Delete File:` headers from it. Hosted tools such as
web search do not reach hooks.

## Antigravity

```bash
uv venv .venv-agy && uv pip install --python .venv-agy/bin/python -r adapters/antigravity/requirements.txt
export GEMINI_API_KEY=...            # the SDK refuses to start an agent without it
.venv-agy/bin/python adapters/antigravity/run_pipeline.py \
    --persona time-starved-parent --target smoke-shopping-list --build "$(git rev-parse --short HEAD)" \
    --entrypoint http://127.0.0.1:8765/ --budget 8 --reinject-every 4
```

The runner creates and seals the run itself from these flags, so no model picks
the stopping conditions. If it stops partway, resume with
`--run-dir runs/<id> --from-role <role>`.

**No browser in the SDK.** The SDK's built-in tools include
`read_url_content` and `run_command`, but nothing that clicks. Against a
JavaScript app, user-agent sees served markup, not a screen, and is told to
describe what a person would see. That is weaker evidence than a real browser
session. For interactive apps, add a browser MCP server to the user-agent's
`LocalAgentConfig(mcp_servers=…)`, or run the session interactively in the IDE
with `.agents/hooks.json`.

**`.agents/hooks.json`** wires `drift` (PreInvocation), `guard` (PreToolUse,
matcher `*`), and `gate` (Stop). Hooks run with the working directory set to
`.agents/`, hence the `../adapters/...` paths. The matcher is `*` because the
docs leave unclear whether alternation is written `a|b` or `a\|b`. The guard
ignores read-only tools, so the only cost is a process per tool call.

## A smoke target

`examples/smoke-app/` is a one-screen shopping list with one planted defect:
the list exists only in memory, so it is gone after a reload. That is exactly
what the time-starved-parent card's success criteria test for.

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory examples/smoke-app
```

## What was verified

Checked against real runs on 2026-10-21 (codex-cli 0.163.0,
google-antigravity 0.1.22):

| Claim | How it was checked | Result |
|---|---|---|
| Claude Code behaviour preserved | `make test`; shim tests assert exit 2 + stderr on a failing gate | pass |
| Codex loads `.codex/agents/*.toml` and spawns by `agent_type` | real `codex exec` smoke run | **verified**: subagents ran as `/root/user_agent` etc. with the generated instructions |
| Codex full pipeline, all four roles in order | same run | **gate passes** at what is now `--stage final` (40 checks). But its one accepted finding, "app never opens", is a harness artifact (no browser under exec), not an app defect. qa-agent proposed it, qa-reviewer accepted it, and the evaluator rated the run `trustworthy`. That is the correlated blind spot DESIGN.md's closing section describes: role separation does not catch a shared misreading of the environment |
| Codex subagent identity | that run's session metadata | `agent_role: "user_agent"`, `agent_type: null`. The hooks accept both fields |
| `pqa record` from a Codex subagent | same run | **verified**: records appended and validated |
| Codex browser_use under `codex exec` | same run | **not available**: "Browser is not available: iab". The in-app browser exists only in the Codex app. Exec fell back to computer use on Chrome, which failed ("Unable to load browser request-header policy"). The persona recorded `goal_unreachable`, honestly |
| Codex runs project hooks | same run, with `$PQA_HOOK_LOG` set | **not exercised**: no hook fired. Project hooks need the one-time `/hooks` trust above, and the bypass flag was not used |
| Codex hook envelopes (`decision: block`, `hookSpecificOutput`, exit codes) | Codex hooks documentation, plus tests | documented contract; not yet observed in a live hook call |
| Antigravity SDK accepts the runner's config | real runner start | **verified**: capabilities, policies, decide/stop hooks and the `record_step` tool all accepted |
| Antigravity runner hooks behave | real SDK types, no model (`HookContext`, `ToolCall`, `StopArgs`) | **verified**: guard denies, Stop continues, then allows after N continuations |
| Antigravity end to end | real runner start | **blocked**: "A Gemini API key is required. Set it via GEMINI_API_KEY". The run was created and sealed, then stopped before user-agent |
| Antigravity IDE hook envelopes | Antigravity hooks documentation, plus tests | documented contract. Whether `{}` from PreToolUse means "no opinion" is **unverified**; the alternative, `allow`, would skip the IDE's own permission prompt |

Set `PQA_HOOK_LOG=/some/file` before starting a platform. Every hook payload
is then appended to that file, which is the quickest way to check the ⚠︎ rows
above on your own setup.
