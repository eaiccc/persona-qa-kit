"""Shared fixtures: a disposable copy of the kit with the example run in it."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools"))

EXAMPLE = KIT / "examples" / "runs" / "2026-08-15-notes-app"
SCENARIO_EXAMPLE = KIT / "examples" / "runs" / "2026-08-22-notes-app-offline"


class KitTestCase(unittest.TestCase):
    """Copies schemas, personas, scenarios and an example run into a temp PQA_HOME.

    Tests mutate the copy freely. Every gate test works by taking a run that is
    known to pass and breaking exactly one thing, which is the only way to know
    a check is load-bearing rather than decorative.
    """

    example = EXAMPLE

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "kit"
        (self.root / "runs").mkdir(parents=True)
        shutil.copytree(KIT / "schemas", self.root / "schemas")
        shutil.copytree(KIT / "personas", self.root / "personas")
        shutil.copytree(KIT / "scenarios", self.root / "scenarios")
        self.run_path = self.root / "runs" / self.example.name
        shutil.copytree(self.example, self.run_path)

        self._prev_home = os.environ.get("PQA_HOME")
        os.environ["PQA_HOME"] = str(self.root)

        from pqa import validate

        validate._cache.store = {}  # schema validators are cached by name only

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("PQA_HOME", None)
        else:
            os.environ["PQA_HOME"] = self._prev_home
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def run_dir(self):
        from pqa.core import RunDir

        return RunDir.open(self.run_path)

    def gate(self):
        from pqa.gate import run_gate

        return run_gate(self.run_dir())

    def read(self, name: str):
        return json.loads((self.run_path / name).read_text())

    def write(self, name: str, data) -> None:
        (self.run_path / name).write_text(json.dumps(data, indent=2) + "\n")

    def session(self) -> list[dict]:
        path = self.run_path / "session.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def write_session(self, records: list[dict], redigest: bool = True) -> None:
        path = self.run_path / "session.jsonl"
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
        if redigest:
            self.redigest()

    def reseal(self) -> None:
        """Re-issue run.lock after a legitimate manifest edit."""
        from pqa.core import digest_file, iso_now

        manifest = self.read("run.json")
        self.write("run.lock", {
            "run_id": manifest["run_id"],
            "sealed_at": iso_now(),
            "run_digest": digest_file(self.run_path / "run.json"),
            "persona_card_digest": manifest["persona"]["card_digest"],
        })

    def redigest(self) -> None:
        """Recompute the digest chain after legitimate artifact edits."""
        from pqa.core import digest_file

        candidates = self.read("findings.candidates.json")
        candidates["session_digest"] = digest_file(self.run_path / "session.jsonl")
        self.write("findings.candidates.json", candidates)

        reviewed = self.read("findings.reviewed.json")
        reviewed["candidates_digest"] = digest_file(
            self.run_path / "findings.candidates.json"
        )
        self.write("findings.reviewed.json", reviewed)

    # -- assertions -------------------------------------------------------

    def assertBlocks(self, report, needle: str) -> None:
        joined = "\n".join(f"{p.where}: {p.message}" for p in report.errors)
        self.assertTrue(
            report.errors, f"expected the gate to block, but it passed cleanly"
        )
        self.assertIn(
            needle.lower(), joined.lower(),
            f"expected an error mentioning {needle!r}, got:\n{joined}",
        )

    def assertPasses(self, report) -> None:
        joined = "\n".join(f"{p.where}: {p.message}" for p in report.errors)
        self.assertTrue(report.ok, f"expected a clean gate, got:\n{joined}")
