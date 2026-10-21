"""The pipeline's roles, the stage each one completes, and what each may write.

This table is the single place that says "user-agent is done when the session
log passes" and "qa-reviewer may write findings.reviewed.json and nothing else".
Every platform adapter reads it: the Claude Code and Codex hooks map a stopping
subagent's `agent_type` to a gate stage through it, the PreToolUse guard uses
`writes` to keep the judging roles read-only, and the Antigravity runner uses
both to configure one SDK agent per role.

The agent *instructions* live in `.claude/agents/*.md`; `load_definition` reads
them so that no adapter keeps its own copy of the prompt text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core import PqaError, kit_root

__all__ = [
    "Role", "ROLES", "PIPELINE", "STAGES", "DEFAULT_STAGE",
    "lookup", "stage_for", "AgentDefinition", "load_definition",
]

# Gate stages, in pipeline order. Each stage includes every check of the stages
# before it. `final` adds the evaluation and is the default: a run is not done
# until something has said whether to believe it.
STAGES = ("session", "candidates", "review", "final")
DEFAULT_STAGE = "final"


@dataclass(frozen=True)
class Role:
    name: str
    stage: str
    # Paths relative to the run directory that this role may create or change.
    # A trailing slash means "anything under this directory".
    writes: tuple[str, ...]
    # Whether this role may run arbitrary shell commands. The judging roles get
    # a read-only allowlist instead: they need `pqa fingerprint` and `cat`, and
    # nothing that could alter the evidence they are judging.
    shell: bool = False


ROLES: dict[str, Role] = {
    "qa-orchestrator": Role("qa-orchestrator", "final", ("run.json",), shell=True),
    "user-agent": Role("user-agent", "session", ("session.jsonl", "screenshots/"), shell=True),
    "qa-agent": Role("qa-agent", "candidates", ("findings.candidates.json",)),
    "qa-reviewer": Role("qa-reviewer", "review", ("findings.reviewed.json",)),
    "persona-evaluator": Role("persona-evaluator", "final", ("evaluation.json",)),
}

# Dispatch order after the manifest is sealed.
PIPELINE = ("user-agent", "qa-agent", "qa-reviewer", "persona-evaluator")


def _normalize(name: str) -> str:
    # Codex agent names are identifiers (user_agent); Claude Code's are
    # kebab-case (user-agent). Both refer to the same role.
    return name.strip().lower().replace("_", "-")


def lookup(name: str | None) -> Role | None:
    if not name:
        return None
    # Plugin-scoped agents arrive as "plugin:agent-name".
    return ROLES.get(_normalize(name.rsplit(":", 1)[-1]))


def stage_for(agent_type: str | None) -> str | None:
    """The gate stage that applies when `agent_type` stops, or None.

    None means "not a pipeline role": a search helper or other subagent that
    finished while a run was active produced none of the run's artifacts, and
    holding it to the final gate would only block it until the hook gave up.
    It does not lighten anything -- the run's own final Stop checks it all.
    """
    role = lookup(agent_type)
    return role.stage if role else None


# --------------------------------------------------------------------------
# agent definitions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    tools: tuple[str, ...]
    model: str | None
    body: str
    path: Path


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def load_definition(name: str) -> AgentDefinition:
    """Parse `.claude/agents/<name>.md`. The frontmatter is flat `key: value`."""
    role = lookup(name)
    filename = role.name if role else name
    path = kit_root() / ".claude" / "agents" / f"{filename}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PqaError(f"no agent definition at {path}") from exc

    match = _FRONTMATTER.match(text)
    if not match:
        raise PqaError(f"{path}: missing --- frontmatter ---")

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    return AgentDefinition(
        name=meta.get("name", filename),
        description=meta.get("description", ""),
        tools=tuple(t.strip() for t in meta.get("tools", "").split(",") if t.strip()),
        model=meta.get("model") or None,
        body=match.group(2).strip() + "\n",
        path=path,
    )
