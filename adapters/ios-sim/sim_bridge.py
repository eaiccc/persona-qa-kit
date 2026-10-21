#!/usr/bin/env python3
"""A file bridge that lets a sandboxed agent drive one iOS Simulator.

Codex's workspace-write sandbox cannot reach CoreSimulatorService, so `axe`
fails inside it. Running the whole pipeline unsandboxed would hand every role
unrestricted shell access to the machine. This bridge is the narrower door: it
runs outside the sandbox, watches a request directory inside the workspace, and
executes a fixed allowlist of simulator actions against ONE simulator and ONE
app, chosen when the bridge starts -- never by the request.

    python3 adapters/ios-sim/sim_bridge.py --udid <UDID> --bundle-id <id>

The agent side is `adapters/ios-sim/sim`, which writes a request file and waits
for the answer. Every request and its outcome are appended to bridge.log, so a
reviewer can see exactly what the agent did to the device.

Nothing here goes through a shell. Each action builds an argv list from
validated values; anything that does not validate is refused with a message
the agent can act on.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))

from pqa.core import active_run, iso_now  # noqa: E402

MAX_TEXT = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
_FRAME = re.compile(r"\{\{([-\d.]+), ([-\d.]+)\}, \{([-\d.]+), ([-\d.]+)\}\}")
_BUTTONS = ("home", "lock", "side-button", "siri")
_SCROLLS = {"up": "scroll-up", "down": "scroll-down", "left": "scroll-left", "right": "scroll-right"}


class Refused(Exception):
    """A request that is not allowed or not well-formed."""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _number(value: Any, name: str, low: float = 0, high: float = 2000) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise Refused(f"{name} must be a number, got {value!r}") from None
    if not low <= number <= high:
        raise Refused(f"{name} must be between {low} and {high}, got {number}")
    return f"{number:g}"


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise Refused("text must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise Refused(f"text is longer than {MAX_TEXT} characters")
    return value


# --------------------------------------------------------------------------
# the UI tree, compacted
# --------------------------------------------------------------------------

def compact_ui(tree: Any) -> str:
    """One line per element worth seeing: type, label, value, centre point.

    The raw describe-ui JSON is tens of kilobytes of nested nulls. An agent
    playing a person needs what is on screen and where to tap it.
    """
    lines: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        label = node.get("AXLabel") or node.get("title") or ""
        value = node.get("AXValue")
        kind = node.get("type") or node.get("role") or "?"
        match = _FRAME.match(str(node.get("AXFrame") or ""))
        if (label or value) and match:
            x, y, w, h = (float(n) for n in match.groups())
            parts = [kind, repr(label)] if label else [kind]
            if value not in (None, "", label):
                parts.append(f"value={str(value)[:120]!r}")
            parts.append(f"at ({x + w / 2:.0f}, {y + h / 2:.0f}) size {w:.0f}x{h:.0f}")
            lines.append(" | ".join(parts))
        walk(node.get("children"))

    walk(tree)
    return "\n".join(lines) or "(no labelled elements on screen)"


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, udid: str, bundle_id: str, root: Path):
        self.udid = udid
        self.bundle_id = bundle_id
        self.root = root
        self.requests = root / "requests"
        self.responses = root / "responses"
        self.log = root / "bridge.log"
        for directory in (self.requests, self.responses):
            directory.mkdir(parents=True, exist_ok=True)

    def _run(self, argv: list[str], timeout: float = 60, stdin: str | None = None) -> str:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise Refused(f"{argv[0]} {argv[1]} failed: {' / '.join(tail)}")
        return result.stdout

    def _axe(self, *args: str, timeout: float = 60) -> str:
        return self._run(["axe", *args, "--udid", self.udid], timeout=timeout)

    def handle(self, request: dict[str, Any]) -> str:
        cmd = request.get("cmd")
        args = request.get("args") or []
        if not isinstance(args, list):
            raise Refused("args must be a list")

        if cmd == "ui":
            return compact_ui(json.loads(self._axe("describe-ui")))
        if cmd == "ui-raw":
            return self._axe("describe-ui")
        if cmd == "tap":
            x, y = (_number(a, n, high=1500) for a, n in zip(args[:2], ("x", "y")))
            self._axe("tap", "-x", x, "-y", y)
            return f"tapped ({x}, {y})"
        if cmd == "tap-label":
            label = _text(args[0] if args else None)
            self._axe("tap", "--label", label, "--wait-timeout", "3")
            return f"tapped the element labelled {label!r}"
        if cmd == "long-press":
            x, y = (_number(a, n, high=1500) for a, n in zip(args[:2], ("x", "y")))
            self._axe("touch", "-x", x, "-y", y, "--down", "--up", "--delay", "1.0")
            return f"long-pressed ({x}, {y})"
        if cmd == "type":
            text = _text(args[0] if args else None)
            if not text.isascii():
                raise Refused("type only handles ASCII; use `sim paste <text>` for Chinese")
            self._run(["axe", "type", "--stdin", "--udid", self.udid], stdin=text)
            return f"typed {len(text)} characters"
        if cmd == "paste":
            text = _text(args[0] if args else None)
            self._run(["xcrun", "simctl", "pbcopy", self.udid], stdin=text)
            self._axe("key-combo", "--modifiers", "227", "--key", "25")
            return f"pasted {len(text)} characters into the focused field"
        if cmd == "swipe":
            names = ("start-x", "start-y", "end-x", "end-y")
            if len(args) != 4:
                raise Refused("swipe takes four numbers: x1 y1 x2 y2")
            values = [_number(a, n, high=1500) for a, n in zip(args, names)]
            flags = [f for pair in zip((f"--{n}" for n in names), values) for f in pair]
            self._axe("swipe", *flags)
            return "swiped"
        if cmd == "scroll":
            direction = _SCROLLS.get(str(args[0]) if args else "")
            if not direction:
                raise Refused(f"scroll direction must be one of {', '.join(_SCROLLS)}")
            self._axe("gesture", direction)
            return f"scrolled {args[0]}"
        if cmd == "button":
            button = str(args[0]) if args else ""
            if button not in _BUTTONS:
                raise Refused(f"button must be one of {', '.join(_BUTTONS)}")
            self._axe("button", button)
            return f"pressed {button}"
        if cmd == "screenshot":
            name = str(args[0]) if args else ""
            if not _NAME.match(name):
                raise Refused("screenshot needs a plain file name, e.g. step-012")
            run = active_run()
            if run is None:
                raise Refused("no active run: seal the run before taking screenshots")
            target = run / "screenshots" / f"{name}.png"
            target.parent.mkdir(exist_ok=True)
            self._axe("screenshot", "--output", str(target))
            return f"saved screenshots/{target.name} (cite it as an artifact of this step)"
        if cmd == "launch":
            self._run(["xcrun", "simctl", "launch", self.udid, self.bundle_id])
            return f"launched {self.bundle_id}"
        if cmd == "terminate":
            self._run(["xcrun", "simctl", "terminate", self.udid, self.bundle_id])
            return f"closed {self.bundle_id}"
        raise Refused(
            f"unknown command {cmd!r}. Allowed: ui, ui-raw, tap, tap-label, long-press, "
            "type, paste, swipe, scroll, button, screenshot, launch, terminate"
        )

    def serve_once(self) -> int:
        handled = 0
        for path in sorted(self.requests.glob("*.json")):
            rid = path.stem
            request: dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                request = loaded if isinstance(loaded, dict) else {}
                output, ok = self.handle(request), True
            except Refused as exc:
                output, ok = str(exc), False
            except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as exc:
                output, ok = f"bridge error: {exc}", False
            path.unlink(missing_ok=True)
            tmp = self.responses / f".{rid}.tmp"
            tmp.write_text(json.dumps({"ok": ok, "output": output}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.rename(self.responses / f"{rid}.json")
            with self.log.open("a", encoding="utf-8") as log:
                log.write(json.dumps({
                    "ts": iso_now(), "id": rid, "cmd": request.get("cmd"),
                    "args": request.get("args"), "ok": ok,
                    "output": output if not ok or len(output) < 300 else output[:300] + "...",
                }, ensure_ascii=False) + "\n")
            handled += 1
        return handled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--root", default=str(KIT / "runs" / ".sim-bridge"),
                        help="request/response directory (default: runs/.sim-bridge)")
    args = parser.parse_args(argv)

    bridge = Bridge(args.udid, args.bundle_id, Path(args.root))
    print(f"sim-bridge: {args.udid} / {args.bundle_id}, watching {bridge.requests}", flush=True)
    while True:
        if not bridge.serve_once():
            time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
