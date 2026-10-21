"""Hook payloads in, hook decisions out, for each supported platform.

The checks themselves (gate, drift tick, guard) are platform-neutral. What
differs is the envelope: what arrives on stdin, and what a hook must print or
exit with to block, continue, or inject context. All of that lives here, so a
platform adapter is a one-line shim that names its format.

    claude   Claude Code. snake_case payloads; exit 2 + stderr blocks Stop,
             SubagentStop and PostToolUse; hookSpecificOutput.additionalContext
             injects context.
    codex    Codex CLI. Same payload shape and the same hookSpecificOutput as
             Claude Code; blocking is `{"decision": "block", "reason": ...}` on
             stdout. Plain text on stdout is invalid for Stop/SubagentStop, so a
             passing gate prints nothing.
    agy      Antigravity. camelCase payloads; every hook prints one JSON object
             and exits 0. PostToolUse can only return `{}`, so drift
             re-injection runs on PreInvocation (injectSteps.ephemeralMessage),
             blocking runs on PreToolUse (decision: deny), and the gate runs on
             Stop (decision: continue). There is no SubagentStop and no agent
             identity in any payload.
    text     no hook: human-readable output and the documented 0/1/2 exit codes.

See docs/platforms.md for what each platform can enforce and what it can only
request.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guard import Intent, parse_apply_patch

__all__ = ["FORMATS", "HookOutcome", "read_payload", "agent_type", "payload_cwd",
           "intent_from_payload", "inject", "block", "deny", "allow"]

FORMATS = ("text", "claude", "codex", "agy")


@dataclass
class HookOutcome:
    stdout: str = ""
    stderr: str = ""
    code: int = 0

    def emit(self) -> int:
        if self.stdout:
            sys.stdout.write(self.stdout if self.stdout.endswith("\n") else self.stdout + "\n")
            sys.stdout.flush()
        if self.stderr:
            sys.stderr.write(self.stderr if self.stderr.endswith("\n") else self.stderr + "\n")
        return self.code


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def read_payload() -> dict[str, Any]:
    """The hook's stdin JSON, or {} when there is none or it is not JSON.

    Optionally appended to $PQA_HOOK_LOG, which is how docs/platforms.md's
    payload notes were checked against real runs.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    log = os.environ.get("PQA_HOOK_LOG")
    if log and raw.strip():
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(raw.strip().replace("\n", " ") + "\n")
        except OSError:
            pass
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def agent_type(payload: dict[str, Any]) -> str | None:
    """The acting role: the payload's agent_type when the platform sends one,
    otherwise $PQA_ROLE, which the Antigravity runner sets per agent.

    Codex records a custom agent's name as `agent_role` in its session metadata
    (with `agent_type` null), so that spelling is accepted too.
    """
    for key in ("agent_type", "agentType", "agent_role", "agentRole"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return os.environ.get("PQA_ROLE") or None


def payload_cwd(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    paths = payload.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return Path(paths[0])
    return Path.cwd()


_WRITE_WORDS = ("write", "create", "edit", "replace", "delete", "move", "patch", "notebook")
_PATH_KEYS = ("file_path", "notebook_path", "path", "TargetFile", "AbsolutePath", "FilePath")


def intent_from_payload(payload: dict[str, Any]) -> Intent:
    """Normalise a PreToolUse payload from any platform into an Intent."""
    call = payload.get("toolCall")
    if isinstance(call, dict):  # Antigravity
        name = str(call.get("name", ""))
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
    else:  # Claude Code, Codex
        name = str(payload.get("tool_name", ""))
        args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}

    intent = Intent(tool=name)
    lowered = name.lower()

    if lowered == "apply_patch":
        patch = args.get("command") or args.get("input") or args.get("patch") or ""
        if isinstance(patch, list):
            patch = "\n".join(str(p) for p in patch)
        intent.writes = parse_apply_patch(str(patch))
        return intent

    if lowered in ("bash", "shell", "exec_command", "run_command", "local_shell"):
        command = args.get("command") or args.get("CommandLine") or args.get("cmd") or ""
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        intent.command = str(command)
        return intent

    if not any(word in lowered for word in _WRITE_WORDS):
        return intent

    path = next((args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)), None)
    if path is None:
        path = next((v for k, v in args.items()
                     if isinstance(v, str) and ("file" in k.lower() or "path" in k.lower())), None)
    if path is None:
        return intent

    texts = []
    # Text being removed (Edit's old_string, a replacement's TargetContent) is
    # already on disk; only what the call would add is scanned.
    def added(key: str) -> bool:
        return not key.lower().startswith(("old", "target"))

    for key, value in args.items():
        if isinstance(value, str) and value != path and added(key):
            texts.append(value)
        elif isinstance(value, list):  # MultiEdit edits, Antigravity replacement chunks
            for item in value:
                if isinstance(item, dict):
                    texts.extend(v for k, v in item.items() if isinstance(v, str) and added(k))
    intent.writes = [(path, "\n".join(texts) or None)]
    return intent


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def inject(fmt: str, event: str, text: str) -> HookOutcome:
    """Put `text` in front of the model without blocking anything."""
    if fmt == "agy":
        return HookOutcome(json.dumps({"injectSteps": [{"ephemeralMessage": text}]}))
    if fmt in ("claude", "codex"):
        return HookOutcome(json.dumps({
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": text}
        }))
    return HookOutcome(text)


def block(fmt: str, event: str, reason: str) -> HookOutcome:
    """Refuse to let the agent stop, or fail the step that just ran."""
    if fmt == "agy":
        if event == "Stop":
            return HookOutcome(json.dumps({"decision": "continue", "reason": reason}))
        # PreInvocation cannot block; the most it can do is say so, loudly.
        return HookOutcome(json.dumps({"injectSteps": [{"ephemeralMessage": reason}]}))
    if fmt == "codex":
        return HookOutcome(json.dumps({"decision": "block", "reason": reason}))
    return HookOutcome(stderr=reason, code=2)


def deny(fmt: str, reason: str) -> HookOutcome:
    """Refuse one tool call before it runs (PreToolUse)."""
    if fmt == "agy":
        return HookOutcome(json.dumps({"decision": "deny", "reason": reason}))
    if fmt in ("claude", "codex"):
        return HookOutcome(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
    return HookOutcome(stderr=reason, code=2)


def allow(fmt: str, text: str = "") -> HookOutcome:
    """No objection. Never an explicit `allow`: that would also skip the
    platform's own permission prompt, which is not this kit's call to make."""
    if fmt == "agy":
        return HookOutcome("{}")
    if fmt == "codex":
        return HookOutcome()
    return HookOutcome(text)
