"""The escape demo runs from a clean checkout (issue #2691).

The thin gate wrapper: `demo/escape/run_demo.py` holds the substance, shelling
out to the real `revl` CLI over three refused, explained and repaired
components. This file makes `pytest tests/` carry the demo, the same way
`tests/test_flagship_demo_525.py` carries the flagship.

It never skips. The demo needs the compiler and nothing else: no cordis
runtime, no network, no host process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "escape" / "run_demo.py"


def _run(args: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEMO), *(args or [])],
        cwd=str(ROOT),
        env={**os.environ, "NO_COLOR": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=600)


def test_the_escape_demo_passes_from_a_clean_checkout():
    proc = _run()
    assert proc.returncode == 0, proc.stdout
    assert "escape demo OK" in proc.stdout, proc.stdout


def test_every_scenario_is_refused_then_admitted():
    """Three scenarios, each asserting its refusal code first and then an
    admitted repair. A demo that only showed refusals would be a catalogue of
    prohibitions; the repair is the half a reader can act on."""
    proc = _run()
    out = proc.stdout
    assert proc.returncode == 0, out
    for code in ("G4", "G9", "G-SECRET-FLOW"):
        assert f"the diagnostic carries the refusal code {code}" in out, (code, out)
    assert out.count("`revl compile` ADMITS the repaired") == 3, out
    assert out.count("the audit names the permitted crossing") >= 7, out


def test_the_demo_counts_its_checks():
    """A demo that printed OK without a count could pass while doing nothing."""
    proc = _run()
    line = next(ln for ln in proc.stdout.splitlines() if "escape demo OK" in ln)
    count = int(line.split("-")[1].strip().split()[0])
    assert count >= 20, line


def test_a_failed_check_exits_nonzero():
    proc = _run(["--nonexistent-flag"])
    assert proc.returncode != 0


def test_the_demo_leaves_the_checkout_untouched():
    """Everything goes to a throwaway temp dir, so a second run passes as
    cleanly as the first and no artifact lands in the tree."""
    before = {p.name for p in (ROOT / "demo" / "escape").iterdir()
              if p.name != "__pycache__"}
    proc = _run()
    assert proc.returncode == 0, proc.stdout
    after = {p.name for p in (ROOT / "demo" / "escape").iterdir()
             if p.name != "__pycache__"}
    assert after == before
    assert not (ROOT / "1_refused.rvl").exists()
