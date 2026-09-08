"""`revl run` names the lifecycle stage a failure hit (roadmap item 461,
issue #724).

Reproducing a failure has repeatedly needed to know *where in the run* it
happened. Before item 461 every failure printed a bare ``error: <exc>`` with no
stage, so a compile refusal and a boot fault read the same at a glance and an
automation could not route on the stage at all. Now each `revl run` failure
appends a labelled ``(lifecycle stage: <id> — <gloss>)`` line *underneath* the
diagnostic, so the ``.rvl`` source location on the first line is untouched and
the failing stage is on the record.

These tests exercise the stages that are decidable with no runtime installed
(compile / admission / config), so they are deterministic on any interpreter;
the boot stage is checked only where the cordis-py runtime is absent (the path
that names it here).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import lifecycle  # noqa: E402

try:  # the same availability gate test_run.py uses
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


def _stage_line(stderr: str, stage: str) -> str | None:
    """The `(lifecycle stage: <stage> …)` line, or None if it is absent."""
    needle = f"(lifecycle stage: {stage} "
    for line in stderr.splitlines():
        if needle in line:
            return line
    return None


# ------------------------------------------------------------- the pure module


def test_stage_ids_are_ordered_and_stable():
    """The vocabulary is the single source of truth: the ids the CLI and any
    consumer route on, in run order."""
    assert lifecycle.STAGES == (
        "compile", "admission", "config", "boot", "run", "teardown")
    # every stage has a human gloss, and an unknown one degrades to its id
    for stage in lifecycle.STAGES:
        assert lifecycle.gloss(stage) and lifecycle.gloss(stage) != stage
    assert lifecycle.gloss("nonesuch") == "nonesuch"


def test_render_keeps_the_diagnostic_first_line_byte_identical():
    """The renderer never rewrites the diagnostic — a reader's first line, and
    any tool keyed to it, is exactly what it was before item 461; the stage
    rides underneath on its own line."""
    diag = "app.rvl:12: something went wrong\n  a hint"
    out = lifecycle.render(diag, lifecycle.BOOT)
    assert out.startswith(diag + "\n")
    assert out.splitlines()[0] == "app.rvl:12: something went wrong"
    assert out.splitlines()[-1] == lifecycle.annotate(lifecycle.BOOT)
    assert "lifecycle stage: boot " in out


# --------------------------------------------------------------- compile stage


def test_compile_failure_names_the_stage_and_the_rvl_source_line(tmp_path):
    """A syntax error refuses at the compile stage, and the diagnostic still
    leads with `<file>.rvl:<line>:` — the source location the whole exit test
    turns on."""
    src = tmp_path / "syntax.rvl"
    src.write_text(
        "service S {\n"
        "  fn f(n: Int) -> Int\n"
        "}\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn f(n) = %\n"     # line 6: not an expression
        "  }\n"
        "}\n", encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py"])
    assert result.returncode == 1, result.stderr
    # the first line is the unchanged diagnostic, naming the .rvl source line
    first = result.stderr.splitlines()[0]
    assert first.startswith("error: ")
    assert f"{src}:6:" in first
    # and the stage is named underneath
    assert _stage_line(result.stderr, "compile") is not None, result.stderr


# ------------------------------------------------------------- admission stage


def test_admission_failure_names_the_stage(tmp_path):
    """A composition that compiles but still carries a typed hole is refused at
    the admission stage — a distinct stage from compile, on the same run."""
    src = tmp_path / "hole.rvl"
    src.write_text(
        "service S {\n"
        "  fn f(n: Int) -> Int\n"
        "}\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        '    fn f(n) = hole "the real f"\n'
        "  }\n"
        "}\n", encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py"])
    assert result.returncode == 1, result.stderr
    assert "admission refused" in result.stderr
    assert _stage_line(result.stderr, "admission") is not None, result.stderr
    # admission is NOT reported as compile — the stages are distinguished
    assert _stage_line(result.stderr, "compile") is None


# ---------------------------------------------------------------- config stage


def test_required_config_failure_names_the_stage():
    """`examples/user_cache.rvl` needs `PgDatabase.url`; run without --config it
    refuses at the config stage, before any runtime is imported (so this holds
    on an interpreter with no cordis)."""
    result = _run_cli([str(ROOT / "examples" / "user_cache.rvl"), "--backend", "py"])
    assert result.returncode == 1, result.stderr
    assert 'missing required config "url"' in result.stderr
    assert _stage_line(result.stderr, "config") is not None, result.stderr


def test_malformed_config_file_names_the_config_stage(tmp_path):
    """A `--config` file that is not a table refuses at the config stage."""
    src = tmp_path / "notes.rvl"
    src.write_text(
        "service Notes {\n"
        "  fn get(key: Str) -> Opt[Str]\n"
        "}\n"
        "component Notes provides notes: Notes {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide notes {\n"
        "    fn get(key) = store.get(key)\n"
        "  }\n"
        "}\n", encoding="utf-8")
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("not-a-table = 3\n", encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py", "--config", str(cfg)])
    assert result.returncode == 1, result.stderr
    assert _stage_line(result.stderr, "config") is not None, result.stderr


# ------------------------------------------------------------------ boot stage


@pytest.mark.skipif(
    HAVE_CORDIS,
    reason="needs an interpreter without cordis-py (the path that names boot)")
def test_missing_runtime_names_the_boot_stage(tmp_path):
    """With no cordis-py, a config-free composition gets past compile /
    admission / config and fails booting the runtime — the boot stage, named
    (exit code 3, the toolchain-missing signal, is unchanged)."""
    src = tmp_path / "notes.rvl"
    src.write_text(
        "service Notes {\n"
        "  fn get(key: Str) -> Opt[Str]\n"
        "}\n"
        "component Notes provides notes: Notes {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide notes {\n"
        "    fn get(key) = store.get(key)\n"
        "  }\n"
        "}\n", encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py"])
    assert result.returncode == 3, result.stderr
    assert "cordis-py runtime is not installed" in result.stderr
    assert _stage_line(result.stderr, "boot") is not None, result.stderr
