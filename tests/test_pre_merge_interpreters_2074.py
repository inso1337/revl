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

The completeness pins at the bottom PARSE the real `step` lines (continuations
joined, wrapper prefixes peeled off) and assert that the token a step would
actually exec is one the gate resolved, so a bare interpreter cannot come back
behind `env`/`exec`/`nohup`/`sh -c` or a line continuation (issue #2077).
"""

from __future__ import annotations

import re
import shlex
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

# A bare interpreter in executable position is defect 1 of #2074: the step
# ignores the `$PYTHON` the gate resolved and picks up whatever `python3` the
# committer's PATH happens to hold (on the box in the issue, a python without
# pytest).
_BARE_INTERPRETERS = ("python", "python3")
# Tokens that run the command *after* them, so the interpreter is not `cmd[0]`.
# `env` is peeled apart from these because it also takes `NAME=value` words.
_WRAPPERS = ("command", "exec", "nohup", "time", "nice", "sudo", "stdbuf",
             "timeout", "xargs")
# `env` short options that take an operand (`env -u FOO python3 ...`).
_ENV_OPERAND_OPTIONS = ("-u", "-C", "-S")
# `command -v python3` looks a name up instead of exec'ing it.
_LOOKUP_OPTIONS = ("-v", "-V")
# A shell with `-c` takes the command as a single argument: `sh -c "python3 ..."`.
_SHELLS = ("sh", "bash", "dash", "zsh", "ksh")
# Where a shell would start a new command inside one `-c` payload.
_SEPARATORS = ("&&", "||", ";", "|")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _step_lines(source=None):
    """(label, command text) for every `step` invocation, continuations joined.

    A `step` line ending in `\\` continues onto the next physical line, so a
    per-line parse reads the command as `\\` and misses the interpreter the
    continuation carries -- `backend-python` is split that way in the real file.
    """
    text = SCRIPT.read_text(encoding="utf-8") if source is None else source
    lines = text.splitlines()
    steps = []
    i = 0
    while i < len(lines):
        match = _STEP.match(lines[i])
        if not match:
            i += 1
            continue
        command = match.group(2)
        while command.endswith("\\") and i + 1 < len(lines):
            i += 1
            command = command[:-1] + " " + lines[i].strip()
        steps.append((match.group(1), command))
        i += 1
    return steps


def _step_commands(source=None):
    """Every `step` invocation as (label, tokens), the way a shell reads it.

    `source` is for the synthetic cases below; the default reads the real gate.
    """
    steps = [(label, shlex.split(command)) for label, command in _step_lines(source)]
    if source is None:
        assert len(steps) >= 10, (
            f"only {len(steps)} `step` invocations were found in "
            "tools/pre_merge.sh; this pin is reading the wrong text"
        )
    return steps


def _segments(tokens):
    """One command line split where a shell would start a new command."""
    segments, current = [], []
    for token in tokens:
        if token in _SEPARATORS:
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [segment for segment in segments if segment]


def _executables(tokens):
    """Every token of a step command that would actually be exec'd as a program.

    `cmd[0]` only sees the bare `python3 ...` shape. A step that reaches the
    interpreter through a wrapper -- `env FOO=1 python3 ...`, `nohup python3 ...`,
    `nice -n 5 python3 ...`, `timeout 5 python3 ...`, `xargs -n 1 python3 ...` --
    or hands a whole line to a shell (`sh -c "python3 ..."`) puts it further in,
    so those prefixes are peeled off and a `-c` payload is walked too.

    Only executable positions come back: `python3` as an *argument* (a script
    path, a `--python python3` flag, an interpreter handed to a tool) and
    `command -v python3` (a name looked up, not run) are deliberately not
    executables, which is the false positive a blanket "no token is `python3`"
    ban would report.
    """
    executables = []
    for segment in _segments(tokens):
        rest = segment
        while rest:
            head = rest[0]
            # A leading `NAME=value` is an environment word in any segment, not
            # only in `env`'s argument list (`FOO=1 python3 ...`).
            if _ASSIGNMENT.match(head):
                rest = rest[1:]
                continue
            if head == "env":
                rest = rest[1:]
                while rest:
                    option = rest[0]
                    if option in _ENV_OPERAND_OPTIONS:
                        rest = rest[2:]
                        continue
                    if option.startswith("-") or _ASSIGNMENT.match(option):
                        rest = rest[1:]
                        continue
                    break
                continue
            if head == "command" and rest[1:2] and rest[1] in _LOOKUP_OPTIONS:
                rest = []
                continue
            if head in _WRAPPERS:
                rest = rest[1:]
                while rest and (rest[0].startswith("-") or rest[0].isdigit()):
                    rest = rest[1:]
                continue
            if head in _SHELLS:
                payload = next((rest[i + 1] for i, tok in enumerate(rest[1:], 1)
                                if tok == "-c" and i + 1 < len(rest)), None)
                if payload is None:
                    executables.append(head)
                else:
                    executables.extend(_executables(shlex.split(payload)))
                rest = []
                continue
            executables.append(head)
            rest = []
    return executables


def _bare_interpreter_offenders(steps):
    """The (label, token) pairs whose step execs a bare interpreter."""
    return [(label, executable) for label, cmd in steps
            for executable in _executables(cmd)
            if executable in _BARE_INTERPRETERS]


def _synthetic_steps(pairs):
    """A `step` block from (label, command) pairs, as `tools/pre_merge.sh` writes it."""
    return "".join(f'step "{label}" {command}\n' for label, command in pairs)


def test_no_step_invokes_a_bare_python():
    """Every step must exec an interpreter the gate resolved, never PATH's.

    A bare `python3` in executable position is the shape of defect 1: it ignores
    the resolved `$PYTHON` and picks up whatever `python3` the committer's PATH
    happens to hold, which on the box in the issue is a python without pytest.
    The executable is read *after* wrapper prefixes and line continuations are
    peeled off, so `env FOO=1 python3 ...`, `nohup python3 ...` and
    `sh -c "python3 ..."` are caught too (issue #2077).
    """
    steps = _step_commands()
    offenders = _bare_interpreter_offenders(steps)
    assert not offenders, (
        "these steps exec a bare interpreter instead of the resolved one: "
        f"{offenders} (issue #2074 defect 1)"
    )
    # Non-vacuity: peeling must reach real executables rather than return
    # nothing for every step. Every pure-python step execs the resolved `$PYTHON`.
    resolved = {executable for _, cmd in steps for executable in _executables(cmd)}
    assert "$PYTHON" in resolved, (
        "no step resolved to `$PYTHON`, so the executable extraction is reading "
        f"the wrong tokens: {sorted(resolved)}"
    )


def test_a_wrapped_or_continued_step_is_caught():
    """Every shape that hides the interpreter from `cmd[0]`, pinned as offenders.

    A wrapper prefix (`env FOO=1 python3 ...`, `nice -n 5 python3 ...`), a
    leading assignment (`FOO=1 python3 ...`) and a line continuation
    (`step "x" \\` + `python3 ...`) all put the interpreter where `cmd[0]`
    cannot see it, so the old pin passed while the defect was present.
    """
    shapes = (
        ("env prefix", "env FOO=1 python3 tools/regen_goldens.py --check"),
        ("env -i", "env -i python3 tools/regen_goldens.py"),
        ("env --", "env -- python3 tools/regen_goldens.py"),
        ("env -u", "env -u FOO python3 tools/regen_goldens.py"),
        ("assignment", "FOO=1 python3 tools/regen_goldens.py"),
        ("command", "command python3 tools/regen_goldens.py"),
        ("exec", "exec python3 tools/regen_goldens.py"),
        ("nohup", "nohup python3 tools/regen_goldens.py"),
        ("time", "time python3 tools/regen_goldens.py"),
        ("nice", "nice -n 5 python3 tools/regen_goldens.py"),
        ("sudo", "sudo python3 tools/regen_goldens.py"),
        ("stdbuf", "stdbuf -oL python3 tools/regen_goldens.py"),
        ("timeout", "timeout 5 python3 tools/regen_goldens.py"),
        ("xargs", "xargs -n 1 python3 tools/regen_goldens.py"),
        ("sh -c", "sh -c 'cd tools && python3 check.py'"),
    )
    offenders = _bare_interpreter_offenders(_step_commands(_synthetic_steps(shapes)))
    assert offenders == [(label, "python3") for label, _ in shapes], offenders

    continued = _bare_interpreter_offenders(_step_commands(
        "step \"continued\" \\\n    python3 tools/check.py\n"
    ))
    assert continued == [("continued", "python3")], continued


def test_an_interpreter_in_argument_position_is_not_an_offender():
    """The widening must not collapse into "no token is `python3`".

    `python3` legitimately appears as an *argument* -- a script path, a
    `--python python3` flag, an interpreter handed to a tool -- and such a step
    still execs the resolved `$PYTHON`, so it is not defect 1's shape. A blanket
    substring ban flags all of these, which is why they are pinned as passing.
    """
    steps = _step_commands(_synthetic_steps((
        ("flag", '"$PYTHON" tools/x.py --python python3'),
        ("argument", '"$PYTHON" tools/x.py python3 tools/y.py'),
        ("wrapped flag", 'env FOO=1 "$PYTHON" tools/x.py --python python3'),
        ("sh -c flag", 'sh -c \'"$PYTHON" tools/x.py --python python3\''),
    )))
    offenders = _bare_interpreter_offenders(steps)
    assert not offenders, (
        "`python3` in argument position is not the defect shape; this is the "
        f"false positive a blanket substring ban would report: {offenders}"
    )


def test_a_command_v_probe_is_not_an_executable():
    """`command -v python3` looks a name up; it execs nothing.

    The real gate's selector uses the idiom (`command -v go`), so a step
    adopting it must not red for a reason that is not defect 1's shape.
    """
    steps = _step_commands(_synthetic_steps((
        ("probe", "command -v python3"),
        ("probe -V", "command -V python3"),
        ("probe in a step", "command -v python3 >/dev/null 2>&1 || true"),
    )))
    offenders = _bare_interpreter_offenders(steps)
    assert not offenders, (
        "`command -v python3` is a lookup, not an interpreter in executable "
        f"position: {offenders}"
    )


def test_the_resolution_block_is_posix_sh():
    """`local`, `[[` and `function` are bashisms: the gate is `#!/bin/sh`."""
    head = SCRIPT.read_text(encoding="utf-8").partition(_BLOCK_END)[0]
    for bashism in ("local ", "[[", "function "):
        assert bashism not in head, f"{bashism!r} is not POSIX sh: {head}"
