"""Paths, digests, and the run-directory layout shared by every command."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "kit_root", "schema_path", "runs_dir", "active_run", "set_active", "clear_active",
    "RunDir", "Problem", "Report",
    "digest_bytes", "digest_file", "iso_now", "read_json", "read_jsonl",
    "colorize", "PqaError",
]


class PqaError(Exception):
    """A user-facing failure: bad path, unreadable file, missing artifact."""


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

def kit_root() -> Path:
    """Root of this checkout. `PQA_HOME` overrides, for vendored installs."""
    override = os.environ.get("PQA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def schema_path(name: str) -> Path:
    if not name.endswith(".schema.json"):
        name = f"{name}.schema.json"
    path = kit_root() / "schemas" / name
    if not path.exists():
        raise PqaError(f"no such schema: {path}")
    return path


def personas_dir() -> Path:
    return kit_root() / "personas"


def runs_dir() -> Path:
    override = os.environ.get("PQA_RUNS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return kit_root() / "runs"


# --------------------------------------------------------------------------
# the active run
# --------------------------------------------------------------------------
#
# Hooks need to know which run they are guarding. An environment variable set
# by an agent's Bash call does not reach them -- hooks inherit the harness's
# environment, not the tool's -- so `pqa seal` records the run in a pointer file
# instead, and the final gate clears it. "Newest run.json on disk" was the old
# fallback; it guessed wrong whenever two runs coexisted, and it made every
# session in this checkout answer to whatever run happened to be lying around.

def active_pointer() -> Path:
    return runs_dir() / ".active"


def active_run() -> Path | None:
    """PQA_RUN_DIR if set, else the run `pqa seal` marked active, else None."""
    explicit = os.environ.get("PQA_RUN_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    pointer = active_pointer()
    try:
        target = Path(pointer.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        return None
    return target if (target / "run.json").exists() else None


def set_active(run_path: Path) -> Path | None:
    """Mark a run active. Returns the run it displaced, if any."""
    previous = active_run() if not os.environ.get("PQA_RUN_DIR") else None
    pointer = active_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{run_path.resolve()}\n", encoding="utf-8")
    return previous if previous and previous != run_path.resolve() else None


def clear_active(run_path: Path) -> None:
    """Clear the pointer, but only if it still names this run."""
    pointer = active_pointer()
    try:
        if Path(pointer.read_text(encoding="utf-8").strip()) == run_path.resolve():
            pointer.unlink()
    except (FileNotFoundError, OSError):
        pass


# --------------------------------------------------------------------------
# digests
# --------------------------------------------------------------------------

def digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def digest_file(path: str | Path) -> str:
    p = Path(path)
    try:
        return digest_bytes(p.read_bytes())
    except FileNotFoundError as exc:
        raise PqaError(f"cannot digest missing file: {p}") from exc


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def read_json(path: str | Path) -> Any:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PqaError(f"file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise PqaError(f"{p}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def read_jsonl(path: str | Path) -> tuple[list[tuple[int, Any]], list[Problem]]:
    """Returns (records, parse_problems). Bad lines are reported, not raised."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PqaError(f"file not found: {p}") from exc

    records: list[tuple[int, Any]] = []
    problems: list[Problem] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append((i, json.loads(line)))
        except json.JSONDecodeError as exc:
            problems.append(Problem(f"{p.name}:{i}", f"invalid JSON: {exc.msg}"))
    return records, problems


# --------------------------------------------------------------------------
# run directory
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RunDir:
    """The on-disk layout of a single run.

    Every artifact has a fixed name. The hook scripts locate files by these
    names and nothing else, so an agent cannot satisfy the gate by writing its
    output somewhere the gate does not look.
    """

    path: Path

    @classmethod
    def open(cls, path: str | Path) -> "RunDir":
        p = Path(path).resolve()
        if not p.is_dir():
            raise PqaError(f"not a run directory: {p}")
        return cls(p)

    @property
    def run_json(self) -> Path:
        return self.path / "run.json"

    @property
    def lock(self) -> Path:
        return self.path / "run.lock"

    @property
    def session(self) -> Path:
        return self.path / "session.jsonl"

    @property
    def candidates(self) -> Path:
        return self.path / "findings.candidates.json"

    @property
    def reviewed(self) -> Path:
        return self.path / "findings.reviewed.json"

    @property
    def evaluation(self) -> Path:
        return self.path / "evaluation.json"

    @property
    def ledger(self) -> Path:
        """Prefix digests of session.jsonl, written by the drift hook."""
        return self.path / "session.ledger.jsonl"

    @property
    def gate_blocks(self) -> Path:
        """How many times the hook gate has refused each stage in a row."""
        return self.path / ".gate-blocks.json"

    def resolve(self, relative: str) -> Path:
        """Resolve an artifact reference, refusing to escape the run directory.

        Compared component-wise rather than by string prefix, so a sibling
        directory whose name merely starts with this one's cannot pass.
        """
        target = (self.path / relative).resolve()
        if self.path != target and self.path not in target.parents:
            raise PqaError(f"artifact path escapes the run directory: {relative}")
        return target


# --------------------------------------------------------------------------
# problem reporting
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Problem:
    where: str
    message: str
    hint: str = ""

    def render(self, color: bool = False) -> str:
        head = f"{colorize(self.where, 'bold', color)}: {self.message}"
        if self.hint:
            head += f"\n      {colorize('hint: ' + self.hint, 'dim', color)}"
        return head


@dataclass
class Report:
    """Accumulated errors and warnings for one command."""

    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    checks_run: int = 0

    def error(self, where: str, message: str, hint: str = "") -> None:
        self.errors.append(Problem(where, message, hint))

    def warn(self, where: str, message: str, hint: str = "") -> None:
        self.warnings.append(Problem(where, message, hint))

    def extend(self, other: "Report") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.checks_run += other.checks_run

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self, title: str, color: bool | None = None) -> str:
        use_color = _color_enabled() if color is None else color
        lines: list[str] = []
        for problem in self.errors:
            lines.append(f"  {colorize('FAIL', 'red', use_color)}  {problem.render(use_color)}")
        for problem in self.warnings:
            lines.append(f"  {colorize('WARN', 'yellow', use_color)}  {problem.render(use_color)}")

        if self.ok and not self.warnings:
            lines.append(
                f"  {colorize('OK', 'green', use_color)}    {title} "
                f"({self.checks_run} checks)"
            )
        else:
            verdict = "failed" if self.errors else "passed with warnings"
            lines.append("")
            lines.append(
                f"  {title}: {verdict} -- "
                f"{len(self.errors)} error(s), {len(self.warnings)} warning(s), "
                f"{self.checks_run} checks"
            )
        return "\n".join(lines)


_COLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
    "dim": "\033[2m",
}


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def colorize(text: str, style: str, enabled: bool = True) -> str:
    if not enabled or style not in _COLORS:
        return text
    return f"{_COLORS[style]}{text}\033[0m"


def as_lines(items: Iterable[str], bullet: str = "  - ") -> str:
    return "\n".join(f"{bullet}{item}" for item in items)
