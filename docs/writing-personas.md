# Writing a persona card

A card is a YAML file in `personas/`, validated against
[`schemas/persona.schema.json`](../schemas/persona.schema.json). The schema
covers the structure. This covers the parts that are judgment.

```bash
./bin/pqa validate --self-check   # validates every card
./bin/pqa personas                # lists them with their behavioural dials
```

---

## Write a person who fails differently, not a person who is worse

The value of a persona is the failures it reaches that your existing tests do
not. A persona who is simply less competent finds the same defects later. A
persona whose *situation* differs finds different ones.

`time-starved-parent` is not useful because she is impatient. She is useful
because she is interrupted mid-task, and no scripted suite covers being
interrupted at step three of five. The interruption is the persona; the
impatience is decoration.

Before writing a card, finish this sentence: **"This person reaches a state that
a careful user never reaches, because ..."** If you cannot, you are about to
write a difficulty setting rather than a persona.

## The behavioural dials are the load-bearing part

```yaml
behavior:
  patience: very-low          # very-low | low | medium | high
  reading: ignores-text       # ignores-text | skims | reads-carefully
  error_response: >-
    Assumes it was their own mistake. Taps the same thing once more ...
  retry_tolerance: 1
  tech_literacy: low
```

**`retry_tolerance` is the single most useful field**, because it is the only
one the evaluator can count. An agent's default behaviour is to retry until it
succeeds or exhausts its budget; almost no real person does that. When a session
retries past this number, drift has occurred and it is measurable rather than
arguable.

Set it to what the person would really do. For most consumer personas the honest
answer is 0 or 1.

**`reading` is where drift shows up first.** An `ignores-text` persona that
starts quoting error messages back accurately has drifted, even with a clean
lexical check.

## Calibrating `never_uses`

This is the mechanical tripwire. Terms are matched case-insensitively, on whole
words, with an optional trailing `s`/`es`, and multi-word terms tolerate any
separator — so `race condition` matches `race-condition`. There is deliberately
no stemming: `cache` does not match `caching`. List variants explicitly.

The schema requires at least five terms, because an empty list silently disables
the check while still appearing to run.

**Include terms that name things the person cannot observe.** The strongest
entries are words for internal mechanisms: `race condition`, `cache
invalidation`, `session state`, `endpoint`. A user sees a blank screen; only an
engineer sees a failed request. When these words appear, the agent has stopped
reporting and started diagnosing.

**Exclude words the person might plausibly use.** A near-miss that fires on
legitimate phrasing gets the whole check disabled, and a disabled check is worse
than no check because it still looks like it ran. `cache` is a bad entry for a
persona who has been told to "clear your cache" by a support line. When in
doubt, leave it out and add it after you see it in a log.

**Watch for terms your own card introduces.** The re-injected summary is session
context, so a forbidden term appearing in `summary` or `goals` will trip the
tripwire it exists to prevent. `tests/test_cli.py` asserts this for the shipped
cards.

Calibrate a new card with the vocabulary check downgraded to a warning:

```bash
./bin/pqa new-run --persona new-card --target app --build x --lenient-vocabulary
```

Run it two or three times, read the warnings, then tighten the list and drop the
flag. A card whose list has never been tested against a real session is a guess.

## `success_criteria` must be observable from outside

These become the run's stopping condition, checked mechanically. "The user feels
confident" is not a stopping condition. "A confirmation screen appeared naming
the amount" is.

Write them as things a third party reading the session log could confirm without
asking the agent what it believed.

A criterion that turns out to be unreachable is a result, not a failed run —
`goal_unreachable` is a legitimate termination reason, and for
`screen-reader-commuter` on an inaccessible build it is the expected one.

## `out_of_scope` prevents borrowed findings

List the surfaces this person would never touch. Anything found there is an
artifact of the agent rather than the persona, and `qa-reviewer` will reject it
as `persona_drift_artifact`.

Be specific. "Advanced features" bounds nothing. "Settings screens beyond
notifications" bounds something.

## Version cards like code

`version` is semver and the card's digest is bound into every run manifest that
uses it. Editing a card invalidates in-flight runs by design — a run's drift and
consistency results describe the card as it was when the session happened.

- **patch** — wording, typos, added `tells`
- **minor** — new `never_uses` terms, new goals
- **major** — changed behavioural dials or success criteria, which makes runs
  before and after incomparable

## Pair opposing cards

`cautious-first-timer` is deliberately the inverse of `time-starved-parent` on
every dial: high patience against very-low, reads-carefully against
ignores-text, retry tolerance 0 against 1.

Running both against one build is the cheapest way to answer the question that
otherwise has no answer: **is this finding about the app, or about the persona?**
A defect both personas hit is about the app. A defect only one hits is about the
intersection — which is still real, and now you know which users it costs you.
