"""Append-only evidence for session.jsonl.

The session log is the only source every later stage reads, so editing an
earlier line is not housekeeping -- it is changing the evidence after someone
has started reacting to it. The obvious trigger is the drift tripwire itself:
tell an agent "you used a forbidden word" and the cheapest response is to go
back and remove the word, which leaves a clean log and a drift report with
nothing in it.

Each time the drift hook runs it appends one line here: how many lines the
session had and a digest of exactly those lines. A later edit to any of them
changes that digest. The gate re-derives every entry, so a log that was only
ever appended to reproduces all of them and a log that was rewritten does not.

This is a raised cost, not a proof. An agent with write access to the run
directory can rewrite the ledger too. What it cannot do is follow an honest
instruction to "fix the earlier record" without that showing up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .core import Report, digest_bytes, iso_now

__all__ = ["Entry", "session_lines", "prefix_digest", "read", "append", "verify"]


@dataclass(frozen=True)
class Entry:
    lines: int
    digest: str


def session_lines(path: Path) -> list[bytes]:
    """Non-blank lines of the log, each with its newline normalised.

    Blank lines are skipped for the same reason read_jsonl skips them: they
    carry no record, and a trailing newline added by an editor is not tampering.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return [line.rstrip(b"\r") + b"\n" for line in raw.split(b"\n") if line.strip()]


def prefix_digest(lines: list[bytes], count: int) -> str:
    return digest_bytes(b"".join(lines[:count]))


def read(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            entries.append(Entry(int(data["lines"]), str(data["digest"])))
        except (ValueError, KeyError, TypeError):
            entries.append(Entry(-1, ""))
    return entries


def append(ledger: Path, session: Path) -> Entry:
    lines = session_lines(session)
    entry = Entry(len(lines), prefix_digest(lines, len(lines)))
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "lines": entry.lines, "digest": entry.digest, "ts": iso_now(),
        }) + "\n")
    return entry


def verify(ledger: Path, session: Path, report: Report, where: str = "session.jsonl") -> int:
    """Check every recorded prefix still matches. Returns the verified line count."""
    entries = read(ledger)
    lines = session_lines(session)
    report.checks_run += 1
    verified = 0
    previous = 0

    for i, entry in enumerate(entries, start=1):
        if entry.lines < 0:
            report.error(ledger.name, f"line {i} is not a valid ledger entry")
            continue
        if entry.lines < previous:
            report.error(
                ledger.name,
                f"line {i} records {entry.lines} session lines after an earlier "
                f"entry recorded {previous}",
                "the log only grows; a shrinking count means lines were removed",
            )
        previous = max(previous, entry.lines)
        if entry.lines > len(lines):
            report.error(
                where,
                f"has {len(lines)} records but the ledger saw {entry.lines} -- "
                "records were deleted after they were written",
                "the session log is append-only. Correct a mistaken step by "
                "appending, never by editing or removing what is already there",
            )
            continue
        if prefix_digest(lines, entry.lines) != entry.digest:
            report.error(
                where,
                f"the first {entry.lines} records changed after they were written",
                "the session log is append-only. Editing an earlier record -- "
                "including to remove a forbidden word -- rewrites the evidence "
                "every later stage depends on",
            )
            continue
        verified = max(verified, entry.lines)
    return verified
