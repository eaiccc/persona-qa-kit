# persona-qa-kit

Persona-based exploratory QA with LLM agents, built so that the parts you depend
on are enforced by code rather than requested in a prompt.

A single agent given a persona and an app will produce a convincing report on
the first run. It will also fail in four specific, reproducible ways. This kit
is a set of agents, schemas, and hooks arranged to make each of those failures
either impossible or visible.

**[DESIGN.md](DESIGN.md) is the primary document.** It argues for each of the
four failure modes, describes the mitigation, and — importantly — states what
the mitigation does not fix. Everything in this repository is downstream of it.
If you read one file here, read that one.

---

## The shape of it

```
qa-orchestrator     writes run.json, seals it            stopping conditions fixed up front
      |
      v
user-agent          uses the app as the persona          records; cannot judge
      |             -> session.jsonl
      v
qa-agent            proposes candidate defects           never touched the app
      |             -> findings.candidates.json
      v
qa-reviewer         accepts / rejects each one           did not propose them
      |             -> findings.reviewed.json
      v
persona-evaluator   scores the run, not the app          -> evaluation.json

  [qa-review-gate]  a shell hook, not an instruction

across runs:
pqa report          aggregates reviewed findings         defects x personas x scenarios
feedback-synthesizer  writes improvement recommendations -> improvements.json (checked)
```

A run is **persona × scenario**. The persona card says who is using the app; an
optional scenario (`scenarios/*.yaml`) says what they came to do, where they
start, what counts as done, and which interruptions will hit the session at
which step. Both are digest-bound into the sealed manifest.

Four agents instead of one, because an agent asked to both perform work and
judge that work optimizes for a coherent narrative rather than an accurate one.
Splitting the roles means the agent doing the rejecting has no prior output to
contradict.

## Quickstart

No install, no dependencies — the enforcement layer is stdlib Python 3 and runs
wherever the pipeline runs.

```bash
./bin/pqa validate --self-check
```

Look at the worked examples before starting your own run. Both are complete,
gate-passing runs against a fictional notes app, one build apart. The second
binds the `note-capture-offline` scenario: its session has hook-written
`perturbation` records and two-phase `intent`/`outcome` steps.

```bash
./bin/pqa gate examples/runs/2026-08-15-notes-app
./bin/pqa gate examples/runs/2026-08-22-notes-app-offline
./bin/pqa report examples/runs
```

Then start a real one:

```bash
./bin/pqa personas
./bin/pqa scenarios
./bin/pqa new-run --persona time-starved-parent --scenario note-capture-offline \
    --target my-app --build $(git rev-parse --short HEAD)
./bin/pqa seal runs/2026-08-15-my-app
```

`--scenario` is optional. Without it, the card's own success criteria are the
completion test.

`seal` refuses to run once `session.jsonl` exists. That is the point: stopping
conditions chosen after seeing the results are not stopping conditions. It also
marks the run active, which is how the hooks find it; the final gate clears the
mark. To run two sessions at once, launch each with `PQA_RUN_DIR` pointing at its
own run.

Then dispatch `qa-orchestrator`, which drives the rest. When it finishes:

```bash
./bin/pqa gate runs/2026-08-15-my-app
./bin/pqa signals runs/
./bin/pqa report runs/ -o report.md
```

`pqa gate --stage session|candidates|review` checks a run only as far as it has
got, which is what the hook does as each agent stops.

## What is actually enforced

The distinction that runs through this repository is between a **request** and a
**constraint**. "Do not skip the review step" is a request. A hook that refuses
to advance the pipeline is a constraint. Anything you actually need to be true
should be a constraint.

| Failure mode | What stops it | Where |
|---|---|---|
| Agent certifies its own work | The session log schema has no `severity`, `pass`, or `status` field, and `additionalProperties` is false | [schemas/session-record.schema.json](schemas/session-record.schema.json) |
| Review gets skipped | Missing or invalid `findings.reviewed.json` blocks completion. Each stopping role is gated at its own stage, so the pipeline can advance but cannot skip | [tools/pqa/gate.py](tools/pqa/gate.py), [tools/pqa/hook.py](tools/pqa/hook.py) |
| Trust verdict omitted | The final stage requires `evaluation.json`, and its lexical violations must include every breach the string match finds | [tools/pqa/gate.py](tools/pqa/gate.py) |
| A candidate is quietly dropped | Every candidate must have exactly one review; the mapping is checked in both directions | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Findings are unfalsifiable prose | Every finding validates against a schema; free text is confined to named fields | [schemas/finding.schema.json](schemas/finding.schema.json) |
| Fabricated defects | `evidence_refs` must resolve to real session steps and real files | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Defects lose identity between runs | `fingerprint` is derived from constrained values and recomputed by the gate | [tools/pqa/fingerprint.py](tools/pqa/fingerprint.py) |
| Stopping conditions moved mid-run | `run.json` is hashed into `run.lock` before execution and rechecked after | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Reviewer rewrites the defect | Accepted findings must match their candidate's fingerprint; only severity and confidence may be adjusted | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Persona drift | `never_uses` terms are string-matched against each new record mid-session; a hit is recorded and ends the session, and nothing from that step on may be accepted | [tools/pqa/hook.py](tools/pqa/hook.py) |
| Evidence edited after the fact | `session.jsonl` is append-only: the drift hook keeps a ledger of prefix digests that the gate re-derives, and the PreToolUse guard refuses edits and overwrites | [tools/pqa/ledger.py](tools/pqa/ledger.py), [tools/pqa/guard.py](tools/pqa/guard.py) |
| Persona card edited mid-run | The card digest is bound into `run.json` and verified | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Scenario edited, or easier criteria swapped in | The scenario digest is bound into `run.json`; the sealed task, perturbations and success criteria must match the file | [tools/pqa/gate.py](tools/pqa/gate.py) |
| An interruption silently skipped | Every sealed perturbation that fell due needs exactly one hook-written record, on time, matching what was sealed | [tools/pqa/gate.py](tools/pqa/gate.py) |
| Expectation written to fit the result | With `intent`/`outcome` steps, the intent must precede its outcome, and an outcome has no `expected` field | [tools/pqa/validate.py](tools/pqa/validate.py) |
| Recommendations paraphrase or invent | `check-improvements` requires accepted fingerprints, verbatim `user_voice` quotes, and no silently dropped blockers | [tools/pqa/report.py](tools/pqa/report.py) |

Each row has a test that breaks exactly that one thing and asserts the gate
refuses. A check nobody has watched fail is a check you do not know you have.

```bash
make test
```

## Detection signals

Some properties cannot be enforced per-run, only observed across runs.
`pqa signals` reports the four the design document names:

```bash
./bin/pqa signals runs/
```

1. **Acceptance rate stuck at 100%** — the proposer/reviewer separation has
   collapsed; either the reviewer rubber-stamps or the proposer has learned to
   propose only safe findings.
2. **Fingerprint churn** — the same defect getting a new identity every run, so
   cross-run comparison is not actually working.
3. **Every run ending at the step budget** — goal satisfaction never fires and
   the budget is doing all the work.
4. **Consistency declining within runs** — drift is progressive and systematic.

These describe the pipeline, not the app. All four firing on a green gate means
the output is well-formed and nobody should believe it.

## Improvement feedback

`pqa report` is the other cross-run view. It describes the app, built only from
`findings.reviewed.json` of runs that pass the gate:

```bash
./bin/pqa report runs/                  # Markdown to stdout
./bin/pqa report runs/ --format json    # input for feedback-synthesizer
```

- **defects by fingerprint**: which personas and scenarios hit each one, the
  build it was first seen in, and whether the latest build still shows it. A
  defect missing from the latest build is "not re-tested" unless a run on that
  build covered a cell where it was seen before.
- **a persona × scenario coverage matrix**, with empty cells listed as gaps and
  each run's own `coverage_note` underneath
- **improvement suggestions**, one per defect, ranked by severity and then by
  how many personas hit it, each quoting `user_voice` verbatim

For recommendations that take judgment (grouping defects into one change,
ordering them), dispatch `feedback-synthesizer`. It reads only the report and
writes `improvements.json`, and `pqa check-improvements` rejects the file if it
cites an unaccepted defect, paraphrases a quote, or drops a blocker without
saying why. [examples/improvements.json](examples/improvements.json) is a
passing example.

## Layout

```
DESIGN.md              the argument; read this first
personas/*.yaml        persona cards, versioned independently of agent code
scenarios/*.yaml       tasks, success criteria and sealed perturbations
schemas/*.json         the artifact contracts
.claude/agents/*.md    the six agent definitions -- the only copy, on every platform
.claude/settings.json  Claude Code hook wiring
.codex/                Codex CLI hook wiring and generated custom agents
.agents/hooks.json     Antigravity hook wiring (interactive use)
adapters/<platform>/   thin hook shims; the Codex generator; the Antigravity SDK runner
tools/pqa/             the CLI and every deterministic check
examples/runs/         two complete, gate-passing runs (one with a scenario)
examples/improvements.json  a feedback-synthesizer output that passes its check
examples/smoke-app/    a one-screen app for smoke-testing an adapter
tests/                 283 tests, stdlib unittest
```

## Other platforms

The same pipeline runs on Codex CLI and Google Antigravity. The checks, schemas,
personas and agent instructions are shared; each platform gets a hook shim, and
Antigravity, whose hooks cannot tell agents apart, gets an SDK runner that gives
each role its own agent. What each platform can enforce and what it can only
request differs, and [docs/platforms.md](docs/platforms.md) says exactly which
is which.

## Writing a persona

[docs/writing-personas.md](docs/writing-personas.md) covers the parts that are
not obvious, particularly calibrating `never_uses` — the list that turns drift
detection from a judgment call into a string match.

Three cards ship as starting points: `time-starved-parent` (interrupted,
impatient, low tech literacy), `cautious-first-timer` (its opposite on every
behavioural dial), and `screen-reader-commuter`.

## Writing a scenario

[docs/writing-scenarios.md](docs/writing-scenarios.md) covers tasks, observable
success criteria, perturbations and two-phase steps. Two scenarios ship:
`note-capture-offline` (the signal drops out mid-note, then a call comes in) and
`checkout-interrupted` (a call and a backgrounding during payment).

## What this does not buy

Ground truth. Every layer here is an LLM and correlated errors pass through all
of them. Schemas constrain form, not truth: a well-formed finding can still be
wrong, and requiring evidence references raises the cost of fabrication without
making it impossible.

Role separation defeats motivated reasoning, not shared ignorance. If `qa-agent`
and `qa-reviewer` both fail to consider an accessibility failure a defect, no
amount of separation surfaces it — which is why one of the shipped personas
routes around that particular blind spot rather than relying on the agents to
overcome it.

This kit produces evidence of a specific and well-characterized quality: good
enough to route attention, not good enough to be the last gate before release.
It complements conventional testing rather than replacing it, and its output
should be read as a prioritized list of things worth a human's attention rather
than a verdict.

## Requirements

Python 3.9+ and bash. PyYAML is used if present and not needed if absent.

## License

MIT
