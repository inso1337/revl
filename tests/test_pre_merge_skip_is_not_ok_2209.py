"""`make pre-merge` never reports "ok" for a gate that skipped (issue #2209).

`step` in tools/pre_merge.sh printed "ok" for any exit 0 and dropped the
output, and two gates exit 0 when they check nothing: formal/scripts/run_gate.sh
without lake, and `tools/regen_goldens.py --check` without gofmt. Both read as
"ok" and counted as "ran". Now run_gate.sh exits 77 for its skip, regen_goldens
runs with --strict (exit 3 for a skip) behind `skip_on 3`, and `step` maps 77
to a SKIPPED line that counts as skipped. A skip still does not fail the gate.

Each case slices the real `step` block (and the real gate line) out of
tools/pre_merge.sh and runs it under sh.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pre_merge.sh"
RUN_GATE = ROOT / "formal" / "scripts" / "run_gate.sh"

if sys.platform == "win32":  # the gate is a POSIX shell script
    pytest.skip("tools/pre_merge.sh runs under sh", allow_module_level=True)


def _slice(start: str, end: str | None = None) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert start in text, f"marker {start!r} is gone from tools/pre_merge.sh"
    body = text[text.index(start):]
    if end is not None:
        assert end in body, f"marker {end!r} is gone from tools/pre_merge.sh"
        body = body[:body.index(end) + len(end)]
    return body


def _line(prefix: str) -> str:
    """The one line of tools/pre_merge.sh that starts with `prefix`."""
    lines = [ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
             if ln.startswith(prefix)]
    assert len(lines) == 1, f"{prefix!r}: {lines}"
    return lines[0]


def _run(body: str, env: dict | None = None, cwd: Path | None = None):
    script = ("RUN_ALL=1\n" + _slice("\nfail=0\n", '\n# note "<label>"')
              + "\n" + body + '\necho "ran=$ran skipped=$skipped fail=$fail"\n')
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                          timeout=120, check=False, env=env, cwd=cwd)


#: A python gate `run_gate.sh` invokes directly, as `python3 scripts/<name>`.
#: The stubs below are read off the script itself rather than listed here: a
#: new gate added to `run_gate.sh` used to turn these tests red with "can't
#: open file ...", before the gate reached its lake-skip path (PR #3119 added
#: `oracle_summary.py` that way).
_GATE_SCRIPT = re.compile(r"^python3 scripts/([A-Za-z0-9_]+\.py)",
                          re.MULTILINE)


def _formal_copy(tmp: Path) -> Path:
    """`run_gate.sh` in a throwaway formal/ with a passing stub for every
    python gate the script runs, so it reaches its elan/lake guard."""
    scripts = tmp / "formal" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUN_GATE, scripts / "run_gate.sh")
    names = sorted(_GATE_SCRIPT.findall(RUN_GATE.read_text(encoding="utf-8")))
    assert names, "run_gate.sh names no python gate script"
    for name in names:
        (scripts / name).write_text("print('ok')\n", encoding="utf-8")
    return scripts / "run_gate.sh"


def test_the_formal_copy_stubs_every_gate_run_gate_runs(tmp_path):
    """The stubbed set is the script's own, not a list that can fall behind.

    Deliberately not gated on lake's absence like the two skip tests below:
    on a machine WITH lake those two are skipped, which is how a missing stub
    would go unnoticed until CI.
    """
    gate = _formal_copy(tmp_path)
    wanted = set(_GATE_SCRIPT.findall(RUN_GATE.read_text(encoding="utf-8")))
    assert {"layering_gate.py", "nonvacuity_gate.py",
            "oracle_summary.py"} <= wanted
    assert wanted == {p.name for p in gate.parent.iterdir()} - {
        "run_gate.sh"}


@pytest.mark.skipif(shutil.which("lake", path="/usr/bin:/bin") is not None,
                    reason="a system lake would be found on the bare PATH")
def test_formal_gate_without_lake_is_skipped_not_ok(tmp_path):
    gate = _formal_copy(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    home = tmp_path / "home"  # no ~/.elan/bin/lake to fall back on
    home.mkdir()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)}
    label = _line('    step "formal ').split('"')[1]
    out = _run(f'step "{label}" sh "{gate}"', env=env)
    line = next(ln for ln in out.stdout.splitlines() if label in ln)
    assert "SKIPPED" in line, out.stdout + out.stderr
    assert "ok" not in line.split(label, 1)[1]
    assert "elan/lake not installed" in out.stdout
    assert "ran=0 skipped=1 fail=0" in out.stdout


@pytest.mark.skipif(shutil.which("lake", path="/usr/bin:/bin") is not None,
                    reason="a system lake would be found on the bare PATH")
def test_run_gate_exits_77_when_it_skips(tmp_path):
    gate = _formal_copy(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(["sh", str(gate)], capture_output=True, text=True,
                          timeout=120, check=False,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)})
    assert done.returncode == 77, done.stdout + done.stderr
    assert "SKIP (loud)" in done.stdout


def _fake_regen(tmp: Path, skip_rc: int) -> Path:
    """A stand-in for `$PYTHON tools/regen_goldens.py --check [--strict]` with
    gofmt missing: the real driver's skip output, exit 3 only under --strict.
    `skip_rc` overrides the --strict exit (1 is a drift)."""
    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "regen_goldens.py").write_text(
        "import sys\n"
        "print('SKIP   go: needs gofmt on PATH, NOTHING was compared for this target')\n"
        "print('regen-goldens: go SKIPPED for a missing tool.')\n"
        f"sys.exit({skip_rc} if '--strict' in sys.argv else 0)\n",
        encoding="utf-8")
    return tmp


@pytest.mark.parametrize("rc, verdict, counts", [
    (3, "SKIPPED", "ran=0 skipped=1 fail=0"),
    (1, "FAILED", "ran=1 skipped=0 fail=1"),
])
def test_goldens_skip_is_skipped_and_a_drift_still_fails(tmp_path, rc, verdict,
                                                         counts):
    cwd = _fake_regen(tmp_path, rc)
    gate = _line('step "goldens ')
    label = gate.split('"')[1]
    env = dict(os.environ, PYTHON=sys.executable)
    out = _run(gate, env=env, cwd=cwd)
    line = next(ln for ln in out.stdout.splitlines() if label in ln)
    assert verdict in line, out.stdout + out.stderr
    assert "ok" not in line.split(label, 1)[1]
    assert counts in out.stdout


def test_verdict_names_the_skipped_steps():
    tail = _slice('echo "pre-merge: $ran ran, $skipped skipped"')
    script = "ran=3\nskipped=1\nfail=0\ncordis_missing=0\nRUN_ALL=1\n" + tail
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                         timeout=60, check=False)
    assert out.returncode == 0  # a skip reports, it does not block
    assert out.stdout.strip().splitlines()[-1] == (
        "pre-merge: ok, but 1 step(s) SKIPPED: what needs a missing tool was "
        "not checked (see above)")
