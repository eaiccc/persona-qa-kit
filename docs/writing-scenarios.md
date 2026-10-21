# Writing a scenario

A persona card says **who**. A scenario says **what**: the task the person came
to do, where the session starts, what counts as done, and what will happen to the
session on the way. A run is persona × scenario.

A scenario is a YAML file in `scenarios/`, validated against
[`schemas/scenario.schema.json`](../schemas/scenario.schema.json). The schema
covers the structure. This covers the parts that are judgment.

```bash
./bin/pqa scenarios               # lists them, with their perturbations
./bin/pqa validate --self-check   # validates every card and scenario
./bin/pqa new-run --persona time-starved-parent --scenario note-capture-offline \
    --target notes-app --build $(git rev-parse --short HEAD)
```

---

## Why scenarios are separate from personas

Without scenarios, the task lives in the card. `time-starved-parent`'s success
criterion is "the task I came to do is saved", which can't be tested against
anything specific. Pointing her at checkout instead of note-taking would mean
editing the card. That changes its digest and version, so every earlier run of
her stops being comparable with every later one. The person didn't change. Only
the errand did.

With the task in its own file, both halves version independently:

- the same persona can be run against several scenarios, and her results stay
  comparable across all of them, because her card's digest never moved
- the same scenario can be given to several personas. This is the
  [opposing-cards](writing-personas.md#pair-opposing-cards) argument applied to
  a task: a defect every persona hits under one scenario is about the app.

`pqa report` lays the two out as a persona × scenario matrix, and the empty
cells are your coverage gaps.

## `task` is said the way the person would say it

```yaml
task: >-
  Write down something you need to remember, while the phone keeps losing its
  signal, and make sure it is still there afterwards.
```

The task is re-injected into the session along with the persona summary. When
a scenario is bound, it replaces the card's `goals` line, so the reminder points
the persona at this run's errand and not at the card's default one. It gets the
same treatment as anything else in the persona's head, so:

- **keep it free of every likely persona's `never_uses` terms.** `pqa new-run`
  warns when the task or a success criterion contains one of the bound card's
  forbidden words. A task that says "verify the API persisted the record" primes
  exactly the drift the tripwire then catches.
- **say what the person wants, not what to test.** "Make sure it is still there
  afterwards" gives the persona a reason to check. "Test draft persistence"
  turns them into a QA engineer.

## `success_criteria` must be observable from outside

These replace the card's `success_criteria` in `run.json` (`criteria_source:
scenario`). The card's criteria are only the fallback for runs without a
scenario. The gate checks that a scenario run carries the scenario's criteria
exactly, so a hand-edited manifest can't quietly swap in easier ones.

The rule is the same as for cards. Each criterion should be something a third
party reading the session log could confirm:

| Not observable | Observable |
|---|---|
| The note was saved | The new note appears in the list after the connection comes back |
| Checkout worked | A confirmation screen appears showing an order number |
| No duplicate charge | Exactly one new order appears in the order history |

Write at least one criterion that makes the scenario's likely failure visible.
`checkout-interrupted` exists to find double orders, so one of its criteria
counts orders.

## `entry` and `preconditions` make runs start from the same place

```yaml
entry:
  surface: checkout/basket
  state: the basket, with one item in it
preconditions:
  - One in-stock item is already in the basket
```

They aren't checked mechanically. They describe the fixture. Two runs of the
same scenario that start from different states aren't comparable, and the
mismatch won't show up anywhere except as unexplained differences in the
findings.

## `in_scope_surfaces` bounds the scenario

Surface prefixes: `checkout` covers `checkout/payment`. The gate **warns** (it
doesn't fail) when a step lands outside them, because wandering off-scenario is
sometimes exactly what the persona would do. The card's `out_of_scope` list
still applies on top.

A warning on every run means the surfaces are too narrow. No warnings ever may
mean they're too broad to bound anything.

## Perturbations: interruptions decided in advance

```yaml
perturbations:
  - at_step: 2
    do: offline          # background | offline | slow_network | rotate |
    for: 3 steps         #   interrupt | lock_screen | kill
    note: the train goes into a tunnel
  - at_step: 6
    do: background
    for: 30s
    note: a phone call comes in and takes over the screen
```

This is the step budget argument ([DESIGN.md](../DESIGN.md) section 3) applied
to interruptions. If an agent chooses when to be interrupted, it chooses the
least damaging moment, or forgets. A scenario seals the moment before the run
starts.

**How it runs.** `pqa new-run` copies the perturbations into `run.json`, and
`pqa seal` hashes them along with everything else. During the session, the
`persona-drift-check` hook runs `pqa drift-tick` after every write. Once step N
is on the log (an `observation`, or the `outcome` half of a two-phase step),
the tick:

1. appends a `perturbation` record to `session.jsonl`, carrying the index,
   `do`, `at_step` and the scenario digest
2. hands user-agent an instruction as `additionalContext`: what to do to the
   device, for how long, and the in-world `note`

The instruction is addressed to user-agent as the device's operator. The
persona doesn't get a warning and mustn't narrate one. What they notice
afterwards goes in the log in the ordinary way.

The tick reads perturbations from the sealed `run.json`, never from the scenario
file, so what gets injected is what was sealed.

**What the gate checks.** A perturbation is *due* once the session records a
step after `at_step`. A session that ends earlier owes nothing. For each due
perturbation, the gate checks for exactly one record that:

- matches the sealed `do` and `at_step`, with this run's scenario digest
- landed after step `at_step` was recorded and before anything from a later
  step. A late record means the steps in between ran unperturbed, and the
  session didn't test what the scenario sealed.

A perturbation record that matches nothing sealed, or any perturbation record in
a run without a scenario, also fails the gate: the hook didn't write it.

**`for`** is `45s`, `2m`, or `3 steps`. Leave it out for instantaneous ones
(`rotate`, `kill`). For a step count, the injected instruction says when to
restore the device ("until you have recorded step 5"). Restoring it is
user-agent's job. Nothing re-injects at the end of the window.

**Choosing `at_step`.** Put it where the interruption can do the most damage:
after input and before it's saved, or after payment is submitted and before
it's confirmed. `pqa new-run` refuses a scenario whose perturbation can't be
reached within the step budget.

## Two-phase steps: `intent` then `outcome`

A step can be recorded as one `observation`, or as an `intent` (planned action
plus `expected`) followed by an `outcome` (action plus `observed` and `felt`).
The second form is optional. It's worth using when the expectation matters,
which in perturbation scenarios it usually does.

`observation.expected` is supposed to be written before acting, but nothing can
tell when it was written. With two records you can see the order. When intent
and outcome are used, the gate checks that:

- every outcome has an intent for the same step, written before it, with no
  other step's records between them
- the intent's timestamp isn't later than the outcome's
- a step has at most one intent. Writing a second one is revising the
  expectation.
- an outcome carries no `expected` field. Its schema has no slot for one, so
  the expectation can't be restated after the fact.
- a step isn't recorded both ways
- an intent with no outcome appears only right before `terminated` (the persona
  decided what to do next, then gave up)

An intent whose planned action class differs from the outcome's only produces a
warning. People change their minds, but the expectation then says nothing about
the step that actually happened.

**What this doesn't buy.** Ordering in an append-only log is real evidence that
the expectation came first. Ordering in a file the agent can rewrite is weaker:
an agent rewriting the whole log can put records in any order it likes. Pair
two-phase steps with an append-only session log check where you have one.

## Version scenarios like cards

The scenario's digest is bound into `run.json` and verified by the gate, the
same as the card's. Editing a scenario after a run invalidates that run.

- **patch**: wording in `title`, `notes`, `note`
- **minor**: an added precondition or in-scope surface
- **major**: a changed task, success criterion or perturbation. Runs before and
  after test different things, and `pqa report` shows them as the same scenario
  only if the id is unchanged, so consider a new id instead.

## Reading the results across runs

```bash
./bin/pqa report runs/                 # Markdown
./bin/pqa report runs/ --format json   # for feedback-synthesizer
```

The report aggregates `findings.reviewed.json` from every run that passes the
gate. It groups defects by fingerprint and shows which personas and scenarios
hit each one, the build it was first seen in, and whether the latest build still
shows it. A defect that's missing from the latest build is "not re-tested"
unless a run on that build covered a cell where it was seen before. It isn't
reported as fixed. The improvement suggestions are templated per defect and
quote `user_voice` verbatim.

For judgment-bearing recommendations, run the `feedback-synthesizer` agent. It
reads only the report and the reviewed findings, writes `improvements.json`, and
`pqa check-improvements` checks every quote and fingerprint in that file against
the reviewed findings.
