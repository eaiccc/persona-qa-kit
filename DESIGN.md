# Design notes: four failure modes of agentic QA

This document explains why `persona-qa-kit` is shaped the way it is. If you only
read one file in this repository, read this one — the agent definitions are
downstream of the arguments here.

## The naive design, and why it doesn't work

The obvious way to build persona-based QA with an LLM agent is a single loop:

> Give an agent a user persona and access to the app. Let it explore. Ask it to
> report what went wrong.

This produces output on the first run and looks convincing. It also fails in
four specific, reproducible ways. Each failure is a consequence of the same
underlying property: **an LLM agent asked to both perform work and judge that
work will optimize for a coherent narrative rather than an accurate one.**

The four sections below name each failure, explain its mechanism, describe the
mitigation in this kit, and — importantly — state what the mitigation does *not*
fix. A design document that only lists successes is marketing.

---

## 1. Self-certification

**Symptom.** The agent finishes its session and reports that the app works well,
with two or three minor cosmetic observations. Run it against a build with a
known crash and it still reports success, or mentions the crash as a footnote
under "areas for future polish."

**Mechanism.** When one agent both executes a task and evaluates the outcome,
the evaluation is conditioned on the transcript the agent just produced. The
agent has already committed to a sequence of actions; describing those actions
as having failed requires contradicting its own prior output. Coherence pressure
wins. This is not lying in any meaningful sense — the model is completing a
document, and documents that end in self-indictment are rare in the training
distribution.

The failure is amplified by task framing. "Test this app and report any issues"
implies a successful test run is the deliverable. An agent that returns nothing
has, by that framing, failed at its job.

**Mitigation.** Separate execution from judgment at the agent boundary, then
enforce that separation outside the agent's control:

- `user-agent` may only record. It has no vocabulary for verdicts — its output
  schema has no `severity`, no `pass`, no `status` field. It records what it
  did, what it observed, and how it felt. It cannot certify anything because it
  has nowhere to write a certification.
- `qa-agent` reads the session log and proposes candidate defects. It never
  touched the app, so it has no transcript to defend.
- `qa-reviewer` accepts or rejects each candidate. It did not propose them, so
  rejecting them costs it nothing.
- `qa-review-gate` is a shell hook, not an instruction. If
  `findings.reviewed.json` is absent or fails schema validation, the pipeline
  stops. The agent cannot decline to be reviewed, because the decision is not
  the agent's to make.

The last point is the one that matters. Prompt-level instructions like *"do not
skip the review step"* are requests. A hook that refuses to advance the pipeline
is a constraint. **Anything you actually need to be true should be a constraint.**

**Detection signal.** If `qa-reviewer` accepts 100% of candidates across several
runs, the separation has collapsed — either the reviewer is rubber-stamping, or
`qa-agent` has learned to propose only safe findings. A healthy rejection rate
is nonzero.

**Residual risk.** The reviewer is still an LLM and shares priors with the
proposer. Correlated blind spots survive this design: if both agents fail to
consider an accessibility failure as a defect, no amount of separation surfaces
it. Separation defeats motivated reasoning, not shared ignorance.

---

## 2. Unfalsifiable reports

**Symptom.** Each run produces a readable narrative. Nobody can answer the
question "is this build better or worse than last week's?" The reports are all
plausible and mutually incomparable.

**Mechanism.** Prose is the model's default output format and its most fluent
one. Left unconstrained, an agent will produce a report that reads well and
resists diffing. Two runs against identical builds yield two different
narratives with different emphases, different groupings, and different names for
the same problem. Without stable identity for a defect, there is no regression
detection — only impressions.

**Mitigation.** Findings are structured records, not paragraphs:

- Every finding validates against `schemas/finding.schema.json`. Free text is
  confined to named fields with defined roles.
- `user_voice` and `technical_hypothesis` are separate fields. The user's
  framing ("I came back and my stuff was gone") is preserved verbatim rather
  than being pre-translated into engineering language, which is where most of
  the diagnostic signal is lost.
- `severity` and `confidence` are separate scales. Agents routinely conflate
  "I'm not sure this is real" with "this isn't serious," which silently
  downgrades genuine high-severity findings.
- `evidence_refs` must point at concrete artifacts — session log line ranges,
  screenshots. A finding with no evidence reference fails schema validation.
  This is the cheapest available guard against fabricated defects.
- `fingerprint` gives a defect stable identity across runs, so "this regressed"
  and "this has been open for three weeks" become queryable facts.

**Detection signal.** Fingerprint churn. If the same underlying defect produces a
new fingerprint on every run, the fingerprinting inputs are too sensitive and
cross-run comparison is not actually working.

**Residual risk.** Schemas constrain form, not truth. A well-formed finding can
still be wrong, and requiring evidence references only raises the cost of
fabrication — it does not make it impossible. The `evidence_refs` values are
worth spot-checking against the actual log.

---

## 3. Absent termination conditions

**Symptom.** Two opposite behaviors from the same cause. Either the agent
declares the session complete after four interactions, or it keeps exploring
until the context window is exhausted, revisiting the same three screens.

**Mechanism.** Exploratory testing has no natural stopping point — that is what
distinguishes it from scripted testing. A human tester stops on judgment
("I've covered the interesting surface"). An agent without an explicit stopping
rule either adopts the first plausible one it invents or never adopts one at
all. Both are unstable across runs, which makes coverage incomparable, which
feeds directly back into failure mode 2.

**Mitigation.** `qa-orchestrator` writes the stopping conditions into `run.json`
before `user-agent` starts, and they are checked mechanically rather than by the
agent's own assessment:

- **Step budget** — a hard ceiling on interactions.
- **Goal satisfaction** — the persona's `success_criteria` are the completion
  test. The session ends when they are met or provably unreachable.
- **Blocked state** — repeated interaction with no observable state change
  terminates the run and is itself recorded as a finding. An agent stuck in a
  loop has usually found the defect.
- **Scope bounds** — the persona's `out_of_scope` list prevents drift into
  surfaces this persona would never touch.

Defining stopping conditions *before* execution rather than letting the agent
decide during execution is the same principle as failure mode 1: take the
judgment out of the hands of the party with an interest in the outcome.

**Detection signal.** Step-count distribution across runs. If every run
terminates at the step budget, the goal-satisfaction check is not firing and the
budget is doing all the work.

**Residual risk.** A step budget is a blunt instrument. Set it too low and deep
flows go untested; too high and cost scales without returning findings. The
right value is empirical and app-specific, which is why it is configuration
rather than a constant.

---

## 4. Persona drift

**Symptom.** Early in the session the agent behaves like the persona. By the
midpoint it is behaving like a competent QA engineer wearing the persona's name
— reading error messages carefully, retrying methodically, inferring intent from
UI affordances that the real persona would never have noticed.

**Mechanism.** The persona is a small instruction competing against a growing
context of the agent's own transcript. The persona is stated once; the
transcript grows every turn and is full of methodical exploration. Recency and
volume favor the transcript. The agent regresses toward its dominant prior,
which for a testing task is "helpful, thorough, technically literate" — exactly
the opposite of most personas worth testing.

Drift is the most damaging of the four failure modes, because the output remains
useful-looking. A drifted run produces real defects. It just produces the
defects a QA engineer would find, which you already had covered.

**Mitigation.**

- **Externalized persona cards.** Personas live in `personas/*.yaml`, versioned
  and independent of agent code, so they can be reviewed, diffed, and reused.
- **Periodic re-injection.** `persona-drift-check` reinserts a persona summary
  every N steps and records each injection point.
- **Negative vocabulary as a mechanical tripwire.** Each card declares
  `vocabulary.never_uses` — terms the persona would not have. When a
  time-starved parent's session log contains "state synchronization" or "race
  condition," that is drift, detectable by string matching rather than judgment.
  A cheap deterministic check catches a failure mode that would otherwise
  require another model call to notice.
- **Post-hoc evaluation.** `persona-evaluator` scores each run for persona
  consistency and writes it to `evaluation.json`. This produces a meta-layer the
  rest of the pipeline lacks: an assessment not of the product, but of whether
  this run is trustworthy evidence about the product.

**Detection signal.** Consistency scores trending down within a run — drift is
progressive, so the second half of a session should be checked separately from
the first.

**Residual risk.** Negative vocabulary catches lexical drift, not behavioral
drift. An agent can avoid every forbidden term while still troubleshooting with
a patience the persona does not have. The evaluator's judgment is the only guard
against that, and it is a soft one.

---

## What this design buys, and what it doesn't

**Buys:** reports that can be compared across runs; defects with stable
identity; a rejection path for hallucinated findings; a signal for when a run
should not be trusted; and stopping behavior that doesn't vary with the model's
mood.

**Doesn't buy:** ground truth. Every layer here is an LLM, and correlated errors
pass through all of them. This kit produces evidence of a specific and
well-characterized quality — good enough to route attention, not good enough to
be the last gate before release. It is a complement to conventional testing, not
a replacement, and it should be read as a prioritized list of things worth a
human's attention rather than a verdict.

**The underlying principle**, which generalizes beyond QA: in a multi-agent
system, every property you actually depend on should be enforced outside the
agent that benefits from violating it. Prompts express intent. Schemas, hooks,
and role boundaries express requirements. When the two disagree, the requirement
is whatever the code enforces.
