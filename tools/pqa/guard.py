"""PreToolUse guard: refuse writes that would break a run before they happen.

The review gate checks artifacts after the fact, through digests: a session log
edited after qa-agent read it no longer matches `session_digest`, and the run
fails. That is sufficient wherever a hook can stop the agent afterwards.

Some platforms cannot. Antigravity's PostToolUse hook returns `{}` and nothing
else, so a vocabulary breach written to session.jsonl cannot fail the step it
happened on. And Codex custom agents cannot narrow their own sandbox, so
"qa-reviewer is read-only except for its output" cannot be configured, only
enforced. Both move here, to the one hook every platform lets deny a call.

The rules, in order:

  * run.lock is written by `pqa seal` and by nothing else
  * run.json is frozen once run.lock exists
  * session.jsonl may not be written before the seal, nor after
    findings.candidates.json exists, nor with a never_uses term in it
  * findings.candidates.json is frozen once findings.reviewed.json exists
  * when the acting role is known, it may write only the files roles.ROLES
    gives it; the judging roles may not write anywhere else, and get a
    read-only shell allowlist

Shell commands are analysed heuristically -- a guard cannot parse every way a
shell can write a file. It catches the ordinary ones (redirects, tee, sed -i,
cp/mv/rm, Python open()), and the gate's digest checks remain the backstop for
anything it misses. File-tool writes are exact.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import drift, personas
from .core import PqaError, RunDir, read_json
from .roles import Role

__all__ = ["Intent", "evaluate", "parse_apply_patch", "PROTECTED"]

PROTECTED = (
    "run.json", "run.lock", "session.jsonl",
    "findings.candidates.json", "findings.reviewed.json", "evaluation.json",
)

# Kit paths no pipeline agent may change mid-run: editing a schema or a hook is
# the cheapest way to make a failing gate pass.
_KIT_PROTECTED = ("schemas", "personas", "tools", "hooks", "adapters", "bin",
                  ".claude", ".codex", ".agents")

_READ_ONLY_COMMANDS = {
    "cat", "head", "tail", "grep", "rg", "wc", "ls", "jq", "sort", "uniq",
    "diff", "pwd", "echo", "printf", "true", "nl", "cut", "tr", "file", "stat",
    "basename", "dirname", "realpath", "sed", "awk", "find", "python3", "python",
    "shasum", "sha256sum",
}
_READ_ONLY_PQA = {"fingerprint", "digest", "drift", "validate", "gate", "personas", "inject", "signals"}


@dataclass
class Intent:
    """What a tool call is about to do, independent of platform."""

    tool: str
    writes: list[tuple[str, str | None]] = field(default_factory=list)  # (path, new text)
    command: str | None = None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def evaluate(
    intent: Intent,
    *,
    cwd: Path,
    run: RunDir | None,
    role: Role | None,
    kit: Path | None = None,
) -> str | None:
    """Return a denial reason, or None when the call may proceed."""
    for path, text in intent.writes:
        reason = _check_file_write(Path(path), text, cwd=cwd, run=run, role=role, kit=kit)
        if reason:
            return reason

    if intent.command:
        return _check_command(intent.command, run=run, role=role)
    return None


# --------------------------------------------------------------------------
# file-tool writes: exact
# --------------------------------------------------------------------------

def _owning_run(path: Path) -> RunDir | None:
    for parent in list(path.parents)[:3]:
        if (parent / "run.json").exists():
            return RunDir(parent)
    return None


def _check_file_write(
    path: Path, text: str | None, *, cwd: Path, run: RunDir | None,
    role: Role | None, kit: Path | None,
) -> str | None:
    target = (path if path.is_absolute() else cwd / path).resolve()
    owner = _owning_run(target) or (run if run and _inside(target, run.path) else None)

    if owner is None:
        if role is None:
            return None
        if kit and _inside(target, kit):
            top = target.relative_to(kit).parts[0] if target != kit else ""
            if top in _KIT_PROTECTED:
                return (f"{role.name} may not change {target.relative_to(kit)} during a run. "
                        "Editing the kit is how a failing gate gets made to pass.")
        if not role.shell:
            return (f"{role.name} is read-only outside its output "
                    f"({', '.join(role.writes)}); it may not write {target}")
        return None

    rel = target.relative_to(owner.path).as_posix()
    reason = _check_artifact(owner, rel, text)
    if reason:
        return reason
    if role is not None and not _role_may_write(role, rel):
        return (f"{role.name} may write only {', '.join(role.writes)} in the run "
                f"directory, not {rel}. Each role's output is its own; see DESIGN.md section 1.")
    return None


def _check_artifact(run: RunDir, rel: str, text: str | None) -> str | None:
    if rel == "run.lock":
        return "run.lock is written by `pqa seal` and by nothing else."
    if rel == "run.json" and run.lock.exists():
        return ("run.json is sealed. Stopping conditions are fixed before execution; "
                "start a new run instead of editing this one (DESIGN.md section 3).")
    if rel == "session.jsonl":
        if not run.lock.exists():
            return "seal run.json (`pqa seal <run-dir>`) before the session starts."
        if run.candidates.exists():
            return ("the session log is frozen: findings.candidates.json was derived from "
                    "it, and changing it now detaches the candidates from their evidence.")
        if text:
            breach = _vocabulary_breach(run, text)
            if breach:
                return breach
    if rel == "findings.candidates.json" and run.reviewed.exists():
        return ("findings.candidates.json is frozen: qa-reviewer has already reviewed "
                "this candidate set.")
    return None


def _role_may_write(role: Role, rel: str) -> bool:
    for allowed in role.writes:
        if allowed.endswith("/") and rel.startswith(allowed):
            return True
        if rel == allowed:
            return True
    return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _vocabulary_breach(run: RunDir, text: str) -> str | None:
    try:
        manifest = read_json(run.run_json)
        if not manifest.get("drift_control", {}).get("enforce_never_uses", True):
            return None
        card = personas.load(manifest["persona"]["id"])
    except (PqaError, KeyError):
        return None

    records: list[dict[str, Any]] = []
    loose: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            loose.append(stripped)
            continue
        if isinstance(value, dict):
            records.append(value)

    violations = [str(v) for v in drift.scan(records, card.never_uses)]
    for chunk in loose:
        for term in card.never_uses:
            if drift.term_pattern(term).search(chunk):
                violations.append(f"contains {term!r}")
    if not violations:
        return None
    return (
        f"Persona drift: {card.data['display_name']} does not have these words:\n"
        + "\n".join(f"  - {v}" for v in violations[:3])
        + "\nDescribe what you saw and how it felt, in the persona's own words."
    )


# --------------------------------------------------------------------------
# shell commands: heuristic
# --------------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|\d?>\s*/dev/null")


def _writes_to(command: str, name: str) -> bool:
    n = re.escape(name)
    patterns = (
        rf">{{1,2}}\s*['\"]?[^\s;|&]*{n}",
        rf"\btee\b[^;|&]*{n}",
        rf"\b(?:sed|perl)\s+(?:-\w+\s+)*-i[^;|&]*{n}",
        rf"\b(?:mv|cp|rm|truncate|touch|install|ln|dd|rsync)\b[^;|&]*{n}",
        rf"open\([^)]*{n}[^)]*['\"][wax+]",
        rf"{n}[^;|&]*\.(?:write_text|write_bytes|unlink|rename|replace)\(",
    )
    return any(re.search(p, command) for p in patterns)


def _check_command(command: str, *, run: RunDir | None, role: Role | None) -> str | None:
    if run is not None:
        for name in PROTECTED:
            if name in command and _writes_to(command, name):
                reason = _check_artifact(run, name, command if name == "session.jsonl" else None)
                if reason:
                    return reason
                if role is not None and not _role_may_write(role, name):
                    return f"{role.name} may not write {name}; only {', '.join(role.writes)}."

    if role is None or role.shell:
        return None
    return _read_only_shell(command, role)


def _read_only_shell(command: str, role: Role) -> str | None:
    deny = (f"{role.name} has a read-only shell: it may read files and run "
            f"`pqa {'|'.join(sorted(_READ_ONLY_PQA))}`, and write only "
            f"{', '.join(role.writes)} with the file tools.")

    if "$(" in command or "`" in command:
        return deny + " Command substitution cannot be checked, so it is refused."
    if re.search(r">", _HARMLESS_REDIRECT.sub("", command)):
        return deny + " Output redirection is a write."

    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return deny
        while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):
            words = words[1:]  # leading VAR=value assignments
        if not words:
            continue
        head = Path(words[0]).name
        if head == "cd":
            continue
        if head == "pqa":
            if len(words) < 2 or words[1] not in _READ_ONLY_PQA:
                return deny
            continue
        if head not in _READ_ONLY_COMMANDS:
            return deny + f" `{head}` is not on the read-only list."
        if head == "sed" and any(w.startswith("-i") or w == "--in-place" for w in words[1:]):
            return deny
        if head == "find" and any(w in ("-delete", "-exec", "-execdir", "-ok") for w in words[1:]):
            return deny
        if head in ("python3", "python") and not (len(words) >= 3 and words[1] == "-m"
                                                    and words[2] == "json.tool"):
            return deny + " Only `python3 -m json.tool` is allowed."
        if head == "awk" and re.search(r"system|print\s*>|\|", segment):
            return deny
    return None


# --------------------------------------------------------------------------
# platform helpers
# --------------------------------------------------------------------------

_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$")


def parse_apply_patch(patch: str) -> list[tuple[str, str | None]]:
    """Files touched by a Codex apply_patch body, with their added lines."""
    files: list[tuple[str, list[str] | None]] = []
    for line in patch.splitlines():
        match = _PATCH_HEADER.match(line)
        if match:
            kind, path, moved = match.groups()
            files.append(((path or moved).strip(), None if kind == "Delete" else []))
            continue
        if files and files[-1][1] is not None and line.startswith("+"):
            files[-1][1].append(line[1:])
    return [(path, "\n".join(lines) if lines is not None else None) for path, lines in files]
