"""The pre-merge gate resolves interpreters it can actually run (issue #2074).

Run from a git worktree, `tools/pre_merge.sh` failed two steps for reasons that
had nothing to do with the diff:

* the conformance step exec'd bare `python3` from PATH. On the box the issue was
  measured on that is `/opt/homebrew/bin/python3`, which has no pytest, so
  `tools/conformance.py --check-readme` died with `ModuleNotFoundError: No module
  named 'pytest'` -- the divergence register it reads imports pytest.
* the `PYTEST` candidate chain tested existence (`-x`) only. The primary
  checkout's `.venv` exists but was built without the `[test]` extras, so it won
  the chain and the frontend step died at COLLECTION instead of taking the
  loud-skip branch the script already has for "no interpreter" -- strictly worse
  than the case it handles deliberately.

The pins below EXECUTE the real resolution block rather than grepping it: the
block is sliced out of `tools/pre_merge.sh` (up to the `# --affected:` marker)
and run under `sh` in a throwaway layout, with `git` stubbed on PATH so
`main_root` is a directory this test owns. A chain that goes back to testing
existence only fails case (b), and one that stops resolving `PYTHON` fails every
case, so neither defect can return silently.

The venvs are REAL venvs whose site-packages carry stub modules: `modules` names
exactly what each candidate can import, so importability is controlled without a
network, a wheel cache, or any machine-specific interpreter. `git` is stubbed
for the same reason -- `git rev-parse --git-common-dir` otherwise resolves to
this machine's primary checkout and the assertions become machine-dependent.
"""

from __future__ import annotations

import re
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "pre_merge.sh"

if sys.platform == "win32":  # the gate is a POSIX shell script
    pytest.skip("tools/pre_merge.sh runs under sh", allow_module_level=True)

# The modules `pytest tests/` imports hard, from pyproject.toml's `[test]` extra
# (`yaml` is pyyaml, `pytest_asyncio` backs the async tests). A candidate that
# cannot import all of them dies at collection, which is the red this issue is
# about; one that can run the suite must be accepted.
SUITE_MODULES = ("pytest", "pytest_asyncio", "yaml", "cryptography", "llguidance")
# The primary checkout's venv in the issue: built without the `[test]` extras.
INCOMPLETE_MODULES = tuple(m for m in SUITE_MODULES if m != "llguidance")

# The block ends where the `--affected` argument parsing begins. Anchored on a
# marker the script has had since the selector landed, and asserted below, so a
# restructure fails this test loudly instead of silently slicing the wrong text.
_BLOCK_END = "\n# --affected: the FAST inner-loop gate."

_PROBE = (
    "\nprintf 'PYTEST=%s\\n' \"${PYTEST:-<empty>}\"\n"
    "printf 'PYTHON=%s\\n' \"${PYTHON:-<empty>}\"\n"
)

# `git` is stubbed so the block sees the layout this test built. The real calls
# are `git rev-parse --show-toplevel` (the `root=` line) and `git rev-parse
# --git-common-dir` (the `main_root=` line); anything else is an error, not a
# silent fallback to the machine's repository.
_STUB_GIT = (
    "#!/bin/sh\n"
    "case \"$1 $2\" in\n"
    "    'rev-parse --show-toplevel') printf '%s\\n' \"$PM_GIT_TOPLEVEL\" ;;\n"
    "    'rev-parse --git-common-dir') printf '%s\\n' \"$PM_GIT_COMMON_DIR\" ;;\n"
    "    *) exit 1 ;;\n"
    "esac\n"
)


def _run(args, cwd, env=None, timeout=120):
    return subprocess.run(args, cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=timeout)


def _stub_venv(path: Path, modules) -> Path:
    """A real venv that can `import` exactly `modules`, with a `bin/pytest`.

    Returns the venv directory. Nothing here is installed: the stub packages are
    written into the venv's own site-packages, so this needs no network and no
    interpreter from the machine.
    """
    venv.create(path, with_pip=False, symlinks=True)
    python = path / "bin" / "python"
    purelib = Path(_run([str(python), "-c",
                         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                        path).stdout.strip())
    for mod in modules:
        if mod == "yaml":  # a plain module, not a package
            (purelib / "yaml.py").write_text("", encoding="utf-8")
            continue
        package = purelib / mod
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
    script = path / "bin" / "pytest"
    script.write_text("#!%s\nimport sys\nsys.exit(0)\n" % python, encoding="utf-8")
    script.chmod(0o755)
    return path


def _resolution_block(tmp_path: Path) -> Path:
    """`tools/pre_merge.sh` up to the selection code, plus the PYTEST/PYTHON probe."""
    text = SCRIPT.read_text(encoding="utf-8")
    head, marker, _ = text.partition(_BLOCK_END)
    assert marker, (
        f"the `{_BLOCK_END.strip()}` marker is gone from tools/pre_merge.sh, so "
        "this pin cannot find the interpreter-resolution block. Re-anchor it "
        "rather than deleting the pin (issue #2074)."
    )
    assert "main_root=" in head and "PYTEST=" in head, (
        "the slice before the `--affected` marker no longer holds the interpreter "
        "resolution; the pin would pass vacuously"
    )
    block = tmp_path / "resolution.sh"
    block.write_text(head + _PROBE, encoding="utf-8")
    return block


class _Layout:
    """A stubbed worktree/primary pair plus the `sh` runner for the block."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.shim = tmp_path / "shim"
        self.shim.mkdir()
        git = self.shim / "git"
        git.write_text(_STUB_GIT, encoding="utf-8")
        git.chmod(0o755)
        self.wt = tmp_path / "wt"
        self.wt.mkdir()
        self.primary = tmp_path / "primary"
        # `main_root` is derived as `<git-common-dir>/..`, so this must exist.
        (self.primary / ".git").mkdir(parents=True)
        self.block = _resolution_block(tmp_path)

    def venv(self, where: str, modules) -> Path:
        """Build a stub venv at `<worktree|primary>/.venv`; return the venv dir."""
        base = self.wt if where == "worktree" else self.primary
        return _stub_venv(base / ".venv", modules)

    def on_path(self, modules) -> Path:
        """A pytest-capable interpreter reachable as `python3` and `pytest`."""
        tool = _stub_venv(self.tmp / "toolvenv", modules)
        for name, target in (("python3", tool / "bin" / "python"),
                             ("pytest", tool / "bin" / "pytest")):
            (self.shim / name).symlink_to(target)
        return tool

    def resolve(self) -> dict[str, str]:
        """Run the real block and parse its PYTEST/PYTHON probe."""
        env = {
            "PATH": f"{self.shim}:/usr/bin:/bin",
            "PM_GIT_COMMON_DIR": str(self.primary / ".git"),
            "PM_GIT_TOPLEVEL": str(self.wt),
        }
        proc = _run(["sh", str(self.block)], self.wt, env)
        assert proc.returncode == 0, (
            f"the resolution block did not run cleanly under sh:\n"
            f"{proc.stdout}{proc.stderr}"
        )
        out = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in ("PYTEST", "PYTHON"):
                out[key] = value
        assert set(out) == {"PYTEST", "PYTHON"}, proc.stdout
        return out


@pytest.fixture
def layout(tmp_path):
    return _Layout(tmp_path)


def test_a_complete_worktree_venv_is_used_and_its_python_is_resolved(layout):
    """The ordinary case: this worktree has its own complete `.venv`.

    Non-vacuity for everything else -- if the chain stopped selecting a
    perfectly good candidate, this is where it shows.
    """
    venv_dir = layout.venv("worktree", SUITE_MODULES)
    got = layout.resolve()
    assert got["PYTEST"] == ".venv/bin/pytest", got
    assert got["PYTHON"] == str(venv_dir / "bin" / "python"), got


def test_an_incomplete_primary_venv_is_rejected_so_the_frontend_loud_skips(layout):
    """The regression: existence is not importability.

    The primary checkout's `.venv` exists (its `bin/pytest` is executable) but
    was built without the `[test]` extras, so running the suite under it dies at
    collection. The chain must reject it and leave `PYTEST` empty, which is the
    loud-skip branch; the old `-x`-only chain selects it and this fails.
    """
    primary = layout.venv("primary", INCOMPLETE_MODULES)
    got = layout.resolve()
    assert got["PYTEST"] == "<empty>", (
        "the chain selected a pytest whose interpreter cannot import the "
        "suite's hard dependencies; the frontend step would die at collection "
        "instead of loud-skipping (issue #2074 defect 2). Selected: "
        f"{got['PYTEST']}"
    )
    # ... and the weaker bar still lets that same venv run the pure-python tool
    # steps, which need pytest and nothing else.
    assert got["PYTHON"] == str(primary / "bin" / "python"), got


def test_a_complete_primary_venv_is_still_used(layout):
    """Validation must not cost the primary-checkout fallback (non-vacuity)."""
    primary = layout.venv("primary", SUITE_MODULES)
    got = layout.resolve()
    assert got["PYTEST"] == f"{layout.primary}/.venv/bin/pytest", got
    assert got["PYTHON"] == str(primary / "bin" / "python"), got


def test_a_complete_worktree_venv_outranks_the_primary_checkout(layout):
    """The order the script documents: this worktree's venv first."""
    worktree = layout.venv("worktree", SUITE_MODULES)
    layout.venv("primary", SUITE_MODULES)
    got = layout.resolve()
    assert got["PYTEST"] == ".venv/bin/pytest", got
    assert got["PYTHON"] == str(worktree / "bin" / "python"), got


def test_no_venv_at_all_falls_through_to_pytest_on_path(layout):
    """No venvs anywhere: the bare `pytest` on PATH is the last candidate."""
    tool = layout.on_path(SUITE_MODULES)
    got = layout.resolve()
    assert Path(got["PYTEST"]).name == "pytest", got
    assert got["PYTEST"] == str(layout.shim / "pytest"), got
    assert got["PYTHON"] == str(tool / "bin" / "python"), got


def test_no_usable_interpreter_leaves_pytest_empty_for_the_loud_skip(layout):
    """Nothing to run the suite with: `PYTEST` empty, not a broken candidate.

    `PYTHON` still has to be something runnable -- the tool steps must not
    inherit the empty `PYTEST`.
    """
    tool = layout.on_path(INCOMPLETE_MODULES)  # pytest, but not the suite's deps
    got = layout.resolve()
    assert got["PYTEST"] == "<empty>", got
    assert got["PYTHON"] == str(tool / "bin" / "python"), got


def test_a_venv_missing_only_pytest_asyncio_is_rejected(layout):
    """The async half of the `[test]` extra is part of the bar too.

    `pytest_asyncio` is what the asyncio-marked tests need at collection, so a
    venv without it is the same false red as one without llguidance.
    """
    partial = tuple(m for m in SUITE_MODULES if m != "pytest_asyncio")
    layout.venv("primary", partial)
    got = layout.resolve()
    assert got["PYTEST"] == "<empty>", got


def test_the_chain_accepts_a_venv_without_coverage(layout):
    """`coverage` is imported inside functions, so it is not part of the bar.

    Pinning the edge the bar deliberately does NOT include: requiring it would
    loud-skip a venv that runs the suite perfectly well, a false skip where the
    issue is about false reds.
    """
    assert "coverage" not in SUITE_MODULES
    layout.venv("worktree", SUITE_MODULES)
    got = layout.resolve()
    assert got["PYTEST"] == ".venv/bin/pytest", got


# --- the completeness pins -------------------------------------------------- #

_STEP = re.compile(r'\s*step\s+"([^"]*)"\s*(.*)$')


def _step_commands():
    text = SCRIPT.read_text(encoding="utf-8")
    steps = [(m.group(1), m.group(2).split()) for m in
             (_STEP.match(line) for line in text.splitlines()) if m]
    assert len(steps) >= 10, (
        f"only {len(steps)} `step` invocations were found in tools/pre_merge.sh; "
        "this pin is reading the wrong text"
    )
    return steps


def test_no_step_invokes_a_bare_python():
    """Every step must exec an interpreter the gate resolved, never PATH's.

    A `step ... python3 ...` line is the shape of defect 1: it ignores the
    resolved `$PYTHON` and picks up whatever `python3` the committer's PATH
    happens to hold, which on the box in the issue is a python without pytest.
    """
    offenders = [(label, cmd[0]) for label, cmd in _step_commands()
                 if cmd and cmd[0] in ("python", "python3")]
    assert not offenders, (
        "these steps exec a bare interpreter instead of the resolved one: "
        f"{offenders} (issue #2074 defect 1)"
    )


def test_the_resolution_block_is_posix_sh():
    """`local`, `[[` and `function` are bashisms: the gate is `#!/bin/sh`."""
    head = SCRIPT.read_text(encoding="utf-8").partition(_BLOCK_END)[0]
    for bashism in ("local ", "[[", "function "):
        assert bashism not in head, f"{bashism!r} is not POSIX sh: {head}"
