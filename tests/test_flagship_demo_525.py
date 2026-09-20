"""The flagship integrated demo runs from a clean checkout (roadmap item 525,
issue #1200, `docs/design/551-flagship-demo.md`).

The thin gate wrapper. `demo/legacy_enterprise/run_demo.py` holds the substance:
it shells out to the real `revl` CLI over a legacy-enterprise agent that spans
the typed API, a peer service and the computer-use family, and it asserts each
step's observable outcome. This file is what makes `pytest tests/` carry it.

Unlike `tests/test_e3_demo.py` this NEVER SKIPS. The flagship demo needs the
compiler and nothing else - no cordis runtime, no desktop, no network - because
every leg it exercises is an admission-time check. That is not a limitation
worked around: the computer-use substrate is a host obligation filed upstream
(roadmap item 539, `inso1337/revl-harness#11`), so a run-time leg here would
have to be invented. Section 5 of the design doc lists what the demo therefore
does not show, item by item.

The assertions below are deliberately about the demo's CONTRACT rather than its
prose. A step's own wording is the demo's to change; that it runs every step,
that it counts its checks, and that it refuses to report success on a failure
are what a gate has to hold.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "legacy_enterprise" / "run_demo.py"


def _run(args: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEMO), *(args or [])],
        cwd=str(ROOT),
        env={**os.environ, "NO_COLOR": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=600)


def test_the_flagship_demo_passes_from_a_clean_checkout():
    proc = _run()
    assert proc.returncode == 0, proc.stdout
    assert "flagship demo OK" in proc.stdout, proc.stdout


def test_every_step_runs_and_each_one_names_its_guarantee():
    """Ten steps, each printing the guarantee its leg discharges. A demo that
    described a guarantee instead of exercising one would have missed the
    item; this asserts the step headers are all present and all labelled."""
    proc = _run()
    assert proc.returncode == 0, proc.stdout
    headers = [ln for ln in proc.stdout.splitlines() if ln.startswith("[")]
    assert len(headers) == 10, proc.stdout
    assert proc.stdout.count("     guarantee: ") == 10, proc.stdout


def test_the_demo_reports_how_many_checks_it_made():
    """A demo that printed OK without a count could pass while doing nothing,
    which is the failure shape `pytest` itself has (a summary with no test
    count means zero collected)."""
    proc = _run()
    tail = proc.stdout.strip().splitlines()
    line = next(ln for ln in tail if "flagship demo OK" in ln)
    count = int(line.split("-")[1].strip().split()[0])
    assert count >= 40, line


def test_the_demo_shows_both_a_refusal_and_an_uncompensated_step():
    """The honest half of item 525's exit, asserted here rather than left to
    a reader of the output: at least one leg is REFUSED, and the irreversible
    leg is reported bare rather than as a clean rollback.

    `--verbose` because the default render heads each artifact; this asserts
    the artifact itself, not the excerpt."""
    proc = _run(["--verbose"])
    out = proc.stdout
    assert "`revl compile` refuses" in out, out
    assert "the boundary policy REFUSES admission" in out, out
    # `ui.download` is irreversible and `ui.click` is unknown; item 522 treats
    # unknown exactly as irreversible, so neither may report compensated - and
    # under item 522's five residue states neither may report `bare` either,
    # because that word also covers the reads. Both are `uncompensated`: an
    # inverse was possible to ask for and none exists.
    assert "[UNCOMPENSATED] LegacyAgent  host fetch_receipt()" in out, out
    assert "[UNCOMPENSATED] LegacyAgent  host actuate()" in out, out
    assert "Compensation is not inversion" in out, out
    # and the demo refuses the one-word summary item 546 rules out.
    assert "rolled back cleanly" in out, out


def test_the_residue_split_survives_the_default_render():
    """Step 9 points at item 522's residue split, so an excerpt that truncates
    before it leaves the step pointing at nothing. The DOES NOT PROVE block
    above it carries one clause per residue state, so it grows whenever a state
    is added - which is exactly when this would silently start cutting."""
    proc = _run()
    out = proc.stdout
    assert "[2] BOUNDARY CROSSINGS" in out, out
    assert "computer-use revert split (item 522)" in out, out
    assert "compensate LIFO: type_amount" in out, out


def test_the_demo_names_the_measured_gap_rather_than_hiding_it():
    """Item 521 slice 2 would derive a screen read's untrustedness from the
    capability. It has not landed, so the qualifier is the author's and the
    same program without it admits. The demo asserts that second outcome."""
    proc = _run()
    assert "MEASURED GAP" in proc.stdout, proc.stdout


def test_a_failed_check_exits_nonzero():
    """The direction that matters. A demo that reported a failure and carried
    on would be a document that prints."""
    proc = _run(["--nonexistent-flag"])
    assert proc.returncode != 0


def test_the_demo_leaves_the_checkout_untouched():
    """Everything goes to a throwaway temp dir, so a second run passes as
    cleanly as the first and no artifact lands in the tree."""
    before = {p for p in (ROOT / "demo" / "legacy_enterprise").iterdir()}
    proc = _run()
    assert proc.returncode == 0, proc.stdout
    after = {p for p in (ROOT / "demo" / "legacy_enterprise").iterdir()
             if p.name != "__pycache__"}
    assert after <= before | {p for p in after if p.name == "__pycache__"}
    assert not (ROOT / "ladder.rvl").exists()
