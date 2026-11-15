"""The local gate runs the cordis-runtime half of `tests/` (issue #2207).

Before this, `tools/pre_merge.sh` ran `tests/` only from the frontend venv, which
has no cordis-py, so every test that needs the runtime skipped in every local
gate, and tests/conftest.py turned some failures into skips on top. A mutant
that flips the WAL-less abort replay in `SessionOwner.finalize_abort` from LIFO
to FIFO passed every local gate.

The pins below EXECUTE the real gate code: the `cordis_step` block and the
verdict tail are sliced out of tools/pre_merge.sh and run under `sh`, with
candidate interpreters that are stub scripts, so the outcome does not depend on
which venvs this machine has.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pre_merge.sh"
DRIVER = ROOT / "tools" / "cordis_half.py"

if sys.platform == "win32":  # the gate is a POSIX shell script
    pytest.skip("tools/pre_merge.sh runs under sh", allow_module_level=True)


def _slice(start: str, end: str | None) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert start in text, f"marker {start!r} is gone from tools/pre_merge.sh"
    body = text[text.index(start):]
    if end is not None:
        assert end in body, f"marker {end!r} is gone from tools/pre_merge.sh"
        body = body[:body.index(end) + len(end)]
    return body


def _stub_python(tmp: Path, missing: str = "", pytest_rc: int = 0,
                 cordis_dir: str = "") -> Path:
    """A candidate interpreter: the import probe reports `missing`, the cordis
    location probe reports `cordis_dir`, and `-m pytest` exits `pytest_rc`
    after recording its arguments."""
    stub = tmp / "stubpy"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then case "$2" in\n'
        f'    *spec.origin*) echo "{cordis_dir}" ;;\n'
        f'    *) echo "{missing}" ;;\n'
        "esac; exit 0; fi\n"
        f'echo "$@" > "{tmp}/pytest_args"\n'
        f"exit {pytest_rc}\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _tree(tmp: Path) -> Path:
    root = tmp / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tools" / "cordis_half.py").write_text(
        DRIVER.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "tests" / "test_needs_runtime.py").write_text(
        "import importlib.util\n"
        "CORDIS = importlib.util.find_spec('cordis')\n"
        "def test_x():\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_plain.py").write_text(
        "def test_y():\n    pass\n", encoding="utf-8")
    return root


def _run_step(tmp: Path, candidate: Path, log: str = "",
              pin: str | None = "b" * 40,
              setup_sh: str | None = None) -> subprocess.CompletedProcess:
    """The cordis step against a throwaway tree whose setup.sh pins `pin`
    (or holds `setup_sh` verbatim). No pin, no setup.sh."""
    root = _tree(tmp)
    if setup_sh is None and pin is not None:
        setup_sh = f'CORDIS_PY_PIN="${{CORDIS_PY_PIN:-{pin}}}"\n'
    if setup_sh is not None:
        (root / "backends" / "python").mkdir(parents=True)
        (root / "backends" / "python" / "setup.sh").write_text(
            setup_sh, encoding="utf-8")
    head = (
        f"root='{root}'\nmain_root='{tmp / 'nomain'}'\n"
        f"PYTHON='{sys.executable}'\nSEL_PYTEST='tests/'\nRUN_ALL=1\n"
        f"REVL_CORDIS_PYTHON='{candidate}'\nCORDIS_LOG='{log}'\n"
        f"cd '{root}'\n")
    block = _slice("\nfail=0\n", "\n# note \"<label>\"")
    cordis = _slice("# >>> cordis-step", "# <<< cordis-step")
    tail = ('\ncordis_step\n'
            'echo "ran=$ran fail=$fail missing=$cordis_missing"\n')
    script = head + block + "\n" + cordis + tail
    env = {k: v for k, v in os.environ.items()
           if k != "CORDIS_PY_PIN" and not k.startswith("GIT_")}
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                          timeout=120, check=False, env=env)


def test_step_reports_missing_runtime_as_not_ok(tmp_path):
    out = _run_step(tmp_path, _stub_python(tmp_path, missing="cordis watchdog"))
    assert "MISSING, NOT OK" in out.stdout, out.stdout + out.stderr
    assert "cannot import cordis, watchdog" in out.stdout
    assert "missing=1" in out.stdout
    assert " ok" not in out.stdout.split("MISSING", 1)[0]


def test_verdict_is_not_ok_when_the_runtime_half_is_missing():
    tail = _slice('echo "pre-merge: $ran ran, $skipped skipped"', None)
    script = "ran=3\nskipped=0\nfail=0\ncordis_missing=1\nRUN_ALL=1\n" + tail
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                         timeout=60, check=False)
    assert out.returncode != 0
    assert "NOT OK" in out.stdout
    assert "pre-merge: ok" not in out.stdout


def test_step_runs_the_logged_files_under_the_cordis_venv(tmp_path):
    log = tmp_path / "skips.log"
    log.write_text("tests/test_plain.py::test_y\n", encoding="utf-8")
    clone, rev = _git_checkout(tmp_path)
    out = _run_step(tmp_path, _stub_python(tmp_path, cordis_dir=str(clone)),
                    log=str(log), pin=rev)
    assert "ok (1 file(s))" in out.stdout, out.stdout + out.stderr
    assert "ran=1 fail=0 missing=0" in out.stdout
    args = (tmp_path / "pytest_args").read_text(encoding="utf-8").split()
    assert args[:3] == ["-m", "pytest", "tests/test_plain.py"]


def test_step_without_a_log_selects_the_files_that_mention_cordis(tmp_path):
    clone, rev = _git_checkout(tmp_path)
    out = _run_step(tmp_path, _stub_python(tmp_path, pytest_rc=1,
                                           cordis_dir=str(clone)), pin=rev)
    assert "FAILED" in out.stdout and "fail=1" in out.stdout, out.stdout
    args = (tmp_path / "pytest_args").read_text(encoding="utf-8").split()
    assert "tests/test_needs_runtime.py" in args
    assert "tests/test_plain.py" not in args


def test_frontend_step_writes_the_skip_log():
    text = SCRIPT.read_text(encoding="utf-8")
    # Issue #2216 added `$SLOW_ENV` to this same command: it expands to nothing
    # on the full gate and to REVL_SKIP_SLOW=1 under `--affected`. The cordis
    # skip log is still passed on both paths, beside the new word.
    assert ('env REVL_CORDIS_SKIP_LOG="$CORDIS_LOG" $SLOW_ENV "$PYTEST" '
            '$SEL_PYTEST') in text
    assert "\n    cordis_step\n" in text


# --- the conftest net ------------------------------------------------------

def _conftest():
    want = str(ROOT / "tests" / "conftest.py")
    for mod in list(sys.modules.values()):
        if getattr(mod, "__file__", None) == want:
            return mod
    pytest.fail("tests/conftest.py is not loaded")


def _failure(path: Path, message: str):
    item = SimpleNamespace(path=path, location=(str(path), 0, "t"))
    call = SimpleNamespace(excinfo=SimpleNamespace(value=AssertionError(message)))
    report = SimpleNamespace(when="call", failed=True)
    return item, call, report


def test_net_keeps_a_failure_red_in_a_file_about_the_missing_runtime(
        tmp_path, monkeypatch):
    conf = _conftest()
    monkeypatch.setattr(conf, "_cordis_is_absent", lambda: True)
    msg = "error: the cordis-py runtime is not installed ('cordis' missing)."
    about = tmp_path / "test_about_diagnostic.py"
    about.write_text(f"EXPECT = {msg!r}\n", encoding="utf-8")
    guardless = tmp_path / "test_guardless.py"
    guardless.write_text("def test_z():\n    pass\n", encoding="utf-8")
    assert not conf._net_converts(*_failure(about, msg))
    assert conf._net_converts(*_failure(guardless, msg))
    assert "cordis" in conf._NET_REASON.split(":", 1)[0]


def test_net_and_guard_skips_are_logged_for_the_gate(tmp_path, monkeypatch):
    conf = _conftest()
    log = tmp_path / "log"
    monkeypatch.setenv("REVL_CORDIS_SKIP_LOG", str(log))
    conf._log_cordis_skip(SimpleNamespace(
        skipped=True, nodeid="tests/test_a.py::t",
        longrepr=("p", 1, "Skipped: " + conf._NET_REASON)))
    conf._log_cordis_skip(SimpleNamespace(
        skipped=True, nodeid="tests/test_b.py",
        longrepr=("p", 1, "Skipped: needs the cordis-py runtime")))
    conf._log_cordis_skip(SimpleNamespace(
        skipped=True, nodeid="tests/test_c.py::t",
        longrepr=("p", 1, "Skipped: no go toolchain")))
    assert log.read_text(encoding="utf-8").split() == [
        "tests/test_a.py::t", "tests/test_b.py"]


# --- review findings on PR #2256 -----------------------------------------------

def _git_checkout(tmp: Path) -> tuple[Path, str]:
    """A throwaway git checkout standing in for a venv's cordis-py clone."""
    repo = tmp / "cordis-clone"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_NOSYSTEM="1")
    for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], env=env, check=True,
                       capture_output=True, timeout=60)
    rev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], env=env,
                         check=True, capture_output=True, text=True,
                         timeout=60).stdout.strip()
    return repo, rev


def test_an_empty_log_means_nothing_to_rerun(tmp_path):
    # The frontend step ran and nothing skipped for cordis: the step must not
    # fall back to every selected file that mentions cordis.
    log = tmp_path / "skips.log"
    log.write_text("", encoding="utf-8")
    out = _run_step(tmp_path, _stub_python(tmp_path), log=str(log))
    assert "nothing to run" in out.stdout, out.stdout + out.stderr
    assert "ran=0 fail=0 missing=0" in out.stdout
    assert not (tmp_path / "pytest_args").exists()


def test_driver_reports_status_none_for_an_empty_log(tmp_path, capsys):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import cordis_half
    finally:
        sys.path.pop(0)
    root = _tree(tmp_path)
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    assert cordis_half.main(["--root", str(root), "--log", str(log)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "STATUS none"
    # A --log that does not exist is a broken frontend step, not a reason to
    # fall back to the selection.
    rc = cordis_half.main(["--root", str(root), "--log", str(tmp_path / "gone")])
    assert rc == 1
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("STATUS error")


def test_a_venv_off_the_pinned_cordis_revision_is_rejected(tmp_path):
    clone, rev = _git_checkout(tmp_path)
    stub = _stub_python(tmp_path, cordis_dir=str(clone))
    stale = _run_step(tmp_path, stub, pin="a" * 40)
    assert "MISSING, NOT OK" in stale.stdout, stale.stdout + stale.stderr
    assert "CORDIS_PY_PIN is aaaaaaaaaaaa" in stale.stdout
    assert not (tmp_path / "pytest_args").exists()


def test_a_venv_at_the_pinned_cordis_revision_runs(tmp_path):
    clone, rev = _git_checkout(tmp_path)
    stub = _stub_python(tmp_path, cordis_dir=str(clone))
    out = _run_step(tmp_path, stub, pin=rev)
    assert "ok (1 file(s))" in out.stdout, out.stdout + out.stderr


def test_only_cordis_py_skips_are_logged(tmp_path, monkeypatch):
    conf = _conftest()
    log = tmp_path / "log"
    monkeypatch.setenv("REVL_CORDIS_SKIP_LOG", str(log))
    for nodeid, reason in (
            ("tests/test_py.py::t", "the py runner needs the pinned cordis fork"),
            ("tests/test_imp.py", "could not import 'cordis': No module named 'cordis'"),
            ("tests/test_ts.py::t", "cordis-ts is not installed (npm ci)"),
            ("tests/test_java.py::t", "needs cordis4j and javac"),
            ("tests/test_rs.py::t", "the cordis-rs crate is not vendored")):
        conf._log_cordis_skip(SimpleNamespace(
            skipped=True, nodeid=nodeid, longrepr=("p", 1, "Skipped: " + reason)))
    assert log.read_text(encoding="utf-8").split() == [
        "tests/test_py.py::t", "tests/test_imp.py"]


# --- issue #2257 ----------------------------------------------------------------

def test_no_pin_found_is_missing_and_names_the_file(tmp_path):
    # A setup.sh whose pin line changed shape: no pin is found, and that must
    # not turn the pin check off. The candidate would pass every other probe.
    clone, _ = _git_checkout(tmp_path)
    out = _run_step(tmp_path, _stub_python(tmp_path, cordis_dir=str(clone)),
                    setup_sh="CORDIS_PY_REV=1c5e6f17abf5\n")
    assert "MISSING, NOT OK" in out.stdout, out.stdout + out.stderr
    assert "no CORDIS_PY_PIN found in backends/python/setup.sh" in out.stdout
    assert "missing=1" in out.stdout
    assert not (tmp_path / "pytest_args").exists()


def test_no_setup_sh_is_missing_too(tmp_path):
    clone, _ = _git_checkout(tmp_path)
    out = _run_step(tmp_path, _stub_python(tmp_path, cordis_dir=str(clone)),
                    pin=None)
    assert "MISSING, NOT OK" in out.stdout, out.stdout + out.stderr
    assert "no CORDIS_PY_PIN found" in out.stdout


@pytest.mark.parametrize("reason", [
    "cordis (TS): node not installed",
    "cordis (TS): node_modules/cordis not installed (run npm ci in backends/typescript)",
    "cordisc (or node) not available",
    "REVL_CORDIS4J_CLASSES is not set",
])
def test_other_runtimes_skips_are_not_logged(tmp_path, monkeypatch, reason):
    conf = _conftest()
    log = tmp_path / "log"
    monkeypatch.setenv("REVL_CORDIS_SKIP_LOG", str(log))
    conf._log_cordis_skip(SimpleNamespace(
        skipped=True, nodeid="tests/test_other.py::t",
        longrepr=("p", 1, "Skipped: " + reason)))
    assert not log.exists() or log.read_text(encoding="utf-8") == ""
