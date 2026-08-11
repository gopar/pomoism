"""Snapshot tests for CLI --help output.

When CLI flags change, these tests fail with a diff. Update the snapshot file
and README.md to match the new output.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"

REMINDER = "remember to update README.md to match"


def _assert_snapshot(name: str, args: list[str]) -> None:
    """Run `pomo.cli <args>` and compare stdout to the snapshot file."""
    result = subprocess.run(
        [sys.executable, "-m", "pomo.cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    actual = result.stdout.rstrip()

    snapshot_path = SNAPSHOTS_DIR / f"{name}.txt"
    if not snapshot_path.exists():
        snapshot_path.write_text(actual + "\n", encoding="utf-8")
        return

    expected = snapshot_path.read_text(encoding="utf-8").rstrip()

    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=str(snapshot_path),
                tofile="current output",
                lineterm="",
            )
        )
        raise AssertionError(f"help output changed for {name} ({REMINDER}):\n{diff}")


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="argparse help format differs before Python 3.13"
)
class TestPomoHelpSnapshot:
    def test_pomo_help(self):
        _assert_snapshot("pomo_help", ["--help"])

    def test_pomo_start_help(self):
        _assert_snapshot("pomo_start", ["start", "--help"])

    def test_pomo_break_help(self):
        _assert_snapshot("pomo_break", ["break", "--help"])

    def test_pomo_history_help(self):
        _assert_snapshot("pomo_history", ["history", "--help"])

    def test_pomo_projects_help(self):
        _assert_snapshot("pomo_projects", ["projects", "--help"])

    def test_pomo_stats_help(self):
        _assert_snapshot("pomo_stats", ["stats", "--help"])
