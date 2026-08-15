"""persona-qa-kit enforcement tooling.

The kit's argument, in one line: in a multi-agent system, every property you
actually depend on should be enforced outside the agent that benefits from
violating it. This package is the "outside".

Nothing here calls a model. Every check is deterministic, so the enforcement
layer cannot exhibit the failure modes it is checking for.
"""

__version__ = "0.1.0"

__all__ = [
    "core", "jsonschema_lite", "yamlite", "fingerprint",
    "personas", "drift", "validate", "gate", "signals", "cli",
]
