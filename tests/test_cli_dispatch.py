"""Characterization tests for CLI dispatch (roadmap item 111).

`main()`'s per-command handlers were extracted into `revl.cli.{change,interop,
observe}`; these tests pin the dispatch wiring for the handlers whose behavior
was otherwise only exercised at the module level (`mcp.canary`, `recovery`,
`mcp.repair`), not through the CLI entry point.

They target the deterministic, runtime-free preflight/error paths so they hold
wherever the suite runs (no cordis toolchain required): each proves that the
subcommand routes to its handler and returns the handler's exit code and
diagnostic. Additive only — added alongside the refactor, never to paper over
a behavior change.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402


def test_recover_missing_wal_is_a_read_error(capsys):
    """`revl recover --wal FILE` routes to the recover handler; a missing WAL is
    a named read error with a nonzero exit, no runtime needed."""
    rc = main(["recover", "--wal", str(ROOT / "does-not-exist.wal")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "WAL" in err


def test_canary_unreadable_baseline_is_refused(capsys):
    """`revl canary` compiles its baseline first; an unreadable source is a
    refusal (exit 1) from the canary handler before any replay runs."""
    rc = main(["canary", str(ROOT / "no-such.rvl"),
               "--candidate", str(ROOT / "no-such-cand.rvl"),
               "--slice", "R"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


def test_repair_unreadable_source_is_refused(capsys):
    """`revl repair` compiles the running composition first; an unreadable
    source is a refusal (exit 1) from the repair handler before boot."""
    rc = main(["repair", str(ROOT / "no-such.rvl"), "--component", "X"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
