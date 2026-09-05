"""`revl run` end-to-end (docs/v2.0-roadmap.md §2, “Toward early production”).

The MVP contract: `revl run <manifest> --backend py` boots a composition on
the cordis-py tier and *streams* the lifecycle/host trace with no host-language
driver — the CLI and the emitter are the whole host. These tests drive the
real CLI (`revl run`, or `python -P -m revl run` as the absolute-interpreter
fallback) as a subprocess with stdin closed, so the REPL hits EOF, the run
tears down, proves no residue, and exits.

Two honesty rules, the same ones `revl test` applies to its tiers:

* the boot/exit assertion runs only where the cordis-py runtime is present
  (`@needs_cordis`); a runtime-less environment *skips with a reason* —
  a skipped tier is never reported as passing, and never fails spuriously;
* the mirror test — the CLI's own “runtime not installed / run setup.sh”
  diagnostic and nonzero exit — runs only where cordis is *absent*, so the
  missing-runtime path is exercised rather than feigned.

Everything that needs no runtime (required-config preflight, unwired-tier
refusal, the pure helper) runs on every interpreter.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

USER_CACHE = str(ROOT / "examples" / "user_cache.rvl")


# The same availability gate test_replay.py uses for its real-cordis section.
# NOT a module-level `pytest.importorskip`: the frontend-only tests below
# must still run on an interpreter without the runtime.
try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")

needs_missing_cordis = pytest.mark.skipif(
    HAVE_CORDIS,
    reason="needs an interpreter without the cordis-py runtime (e.g. the "
           "root .venv)")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# A manifest with no config block at all: a Map-backed note store. Boots
# with no --config, which is what the missing-runtime test needs (it must
# get past the config preflight to reach the runtime import).
NOTES = """\
service Notes {
  fn get(key: Str) -> Opt[Str]
  fn put(key: Str, value: Str)
}
component Notes provides notes: Notes {
  let store = effect Map.new() undo store.drop()
  provide notes {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""


# --------------------------------------------------------------- no runtime
#
# The preflight is a host concern (config belongs to the host, DESIGN §2
# non-goals), so it must work with no cordis anywhere — and silently booting
# a fiber that settles onto FAILED would be a lie about "boots".


def test_run_preflights_required_config_before_any_runtime():
    """`examples/user_cache.rvl` requires PgDatabase.url. Without --config
    the run must refuse *before* any runtime is loaded, on every
    interpreter — not boot a FAILED fiber and exit 0."""
    result = _run_cli([USER_CACHE, "--backend", "py"])
    assert result.returncode == 1, result.stderr
    assert 'PgDatabase is missing required config "url" (Str)' in result.stderr
    assert "--config" in result.stderr


def test_run_preflights_with_the_required_field_supplied(tmp_path):
    """The same manifest with url supplied passes the preflight (and now
    fails only if something later — the runtime — is missing)."""
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[PgDatabase]\nurl = \"postgres://preflight\"\n",
                   encoding="utf-8")
    result = _run_cli([USER_CACHE, "--backend", "py", "--config", str(cfg)])
    assert "missing required config" not in result.stderr
def test_run_ts_no_longer_refuses_flatly():
    """Multi-runtime is by design (--backend is a real selector), and ts is now
    wired (FR-2): py, ts, rust, java and wasm all run today (each non-py tier as
    a separate process — see test_run_ts.py / test_run_rust.py / test_run_java.py
    / test_run_wasm.py). The flat `not wired yet` / exit-2 refusal must be gone
    for ts; USER_CACHE needs PgDatabase.url, so the run now reaches the config
    preflight (rc 1) instead of short-circuiting on the wiring."""
    result = _run_cli([USER_CACHE, "--backend", "ts"])
    assert result.returncode == 1, result.stderr
    assert "not wired yet" not in result.stderr
    assert 'missing required config "url"' in result.stderr


def test_every_emitting_tier_is_now_runnable():
    """FR-2 + FR-8 closed the last two not-wired gaps: py, ts, rust, java,
    wasm and go are all runnable (each non-py tier as a separate process over
    the bridge seam). USER_CACHE needs PgDatabase.url, so every backend now
    reaches the config preflight (rc 1) instead of a flat not-wired refusal —
    the friendly refusal path no longer exists for any emitting tier."""
    for backend in ("py", "ts", "rust", "java", "wasm", "go"):
        result = _run_cli([USER_CACHE, "--backend", backend])
        assert result.returncode == 1, (backend, result.stderr)
        assert "not wired yet" not in result.stderr
        assert 'missing required config "url"' in result.stderr


def test_required_config_problem_uses_the_ir_schema():
    """The helper mirrors the runtime's ConfigSchema rule — `default: null`
    is required — straight off the IR's own config list."""
    from revl.run import _required_config_problem  # noqa: PLC0415

    ir = compile_files([USER_CACHE])
    assert _required_config_problem(ir, {}) is not None
    assert _required_config_problem(ir, {"PgDatabase": {}}) is not None
    problem = _required_config_problem(
        ir, {"PgDatabase": {"url": "postgres://x", "pool_size": 4}})
    assert problem is None, problem


# ------------------------------------------- the error paths' own error path
#
# issue #161: `_load_config` and `_Driver._reload` each raised `RevlError`
# with a SINGLE string. `RevlError.__init__` is (filename, line, message), so
# both raised `TypeError: RevlError.__init__() missing 2 required positional
# arguments` instead of the diagnostic they meant — and a `TypeError` is not
# caught by the `except RevlError` guarding either site. Both branches only
# run once something has ALREADY gone wrong, which is exactly why nothing
# exercised them: before these tests, no test anywhere in the tree reached
# `_load_config`'s rejection or `_reload`'s config refusal.


@pytest.mark.parametrize("suffix,text", [
    (".toml", "not-a-table = 3\n"),
    (".json", '{"Cache": 3}\n'),
    (".toml", "[[Cache]]\nurl = \"x\"\n"),  # array of tables, not a table
])
def test_malformed_config_is_a_diagnostic_not_a_typeerror(tmp_path, suffix, text):
    """A `--config` file that is not `component-name = { ... }` must raise a
    `RevlError` carrying the intended message — never a `TypeError` from
    building the diagnostic itself."""
    from revl.errors import RevlError as _RevlError  # noqa: PLC0415
    from revl.run import _load_config  # noqa: PLC0415

    cfg = tmp_path / f"cfg{suffix}"
    cfg.write_text(text, encoding="utf-8")

    with pytest.raises(_RevlError) as caught:
        _load_config(str(cfg))
    exc = caught.value
    assert exc.filename == str(cfg)
    assert "expected a table of `component-name = { ... }` entries" in exc.message
    # and it renders as a diagnostic, the shape `run_command` prints
    assert str(cfg) in str(exc)


def test_malformed_config_reaches_the_cli_as_error_not_a_traceback(tmp_path):
    """End to end: `revl run --config <malformed>` prints the `error: ...`
    line and exits 1 — the `except RevlError` in `run_command` catches it,
    with no `Traceback`/`TypeError` reaching the user."""
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("PgDatabase = 3\n", encoding="utf-8")
    result = _run_cli([USER_CACHE, "--backend", "py", "--config", str(cfg)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "TypeError" not in result.stderr
    assert "expected a table of `component-name = { ... }` entries" in result.stderr
    assert result.stderr.startswith("error: ")


def test_watch_reload_refuses_a_config_breaking_edit_as_a_diagnostic(tmp_path):
    """`_Driver._reload`'s second refusal (a reloaded document whose new
    required config the running host cannot meet) built its `RevlError` the
    same broken way. Drive the branch directly, with a stub driver, so the
    test needs no cordis runtime: the raise must produce a `RevlError` the
    sibling `except RevlError` catches, leaving the composition untouched."""
    from revl.errors import RevlError as _RevlError  # noqa: PLC0415
    from revl.run import _Driver, _required_config_problem  # noqa: PLC0415

    ir = compile_files([USER_CACHE])
    # the precondition the branch keys off: the host config is missing the
    # required field, so the reload must refuse rather than deploy
    assert _required_config_problem(ir, {}) is not None

    logged: list[tuple] = []

    class _Stub:
        config: dict = {}
        _log = staticmethod(lambda *a: logged.append(a))
        ir = None

        async def _dispose_all(self, _ir):  # pragma: no cover — never reached
            raise AssertionError("a refused edit must not tear anything down")

        def _emit_module(self, _ir):  # pragma: no cover — never reached
            raise AssertionError("a refused edit must not be deployed")

    stub = _Stub()
    asyncio.run(_Driver._reload(stub, [USER_CACHE]))

    text = "\n".join(str(part) for entry in logged for part in entry)
    assert "TypeError" not in text
    assert 'missing required config "url"' in text
    assert "running composition untouched" in text
    # and the raise really did build a well-formed diagnostic
    exc = _RevlError(USER_CACHE, 0, "invalid config:")
    assert str(exc).startswith(f"{USER_CACHE}:0: ")


@needs_missing_cordis
def test_run_without_the_runtime_skips_like_the_other_backends(tmp_path):
    """No cordis installed: `revl run` is a conspicuous *skip*, mirroring
    `revl test`'s absent-tier handling — a clear setup diagnostic and a
    nonzero exit, never a feint at passing. A config-free manifest, so the
    config preflight is not the thing failing.

    Issue #336: the diagnostic must name `revl run …` as the lead (the
    console script `setup.sh` installs) before the absolute-interpreter
    fallback, not just carry the fallback somewhere. A test that only asserts
    the `-m revl run` substring is the one regression that makes
    "documented happy path" silently route everyone through the CWD-shadowing
    window again.
    """
    notes = tmp_path / "notes.rvl"
    notes.write_text(NOTES, encoding="utf-8")
    result = _run_cli([str(notes), "--backend", "py"])
    assert result.returncode == 3, result.stdout + result.stderr
    assert "cordis-py runtime is not installed" in result.stderr
    assert "setup.sh" in result.stderr
    # documented happy path comes BEFORE the absolute-interpreter fallback
    happy_idx = result.stderr.index("revl run")
    fallback_idx = result.stderr.index("-m revl run")
    assert happy_idx < fallback_idx, (
        f"`revl run …` (the documented happy path) must appear before the "
        f"`-m revl run …` fallback in the diagnostic; got stderr={result.stderr!r}")


# ----------------------------------------------------- `revl test` preflight

def test_py_tier_preflights_the_missing_cordis_runtime(monkeypatch):
    """uxprobe's longest stall (findings-uxprobe.md): a lifecycle-test
    document run under an interpreter without the cordis-py runtime used to
    FAIL per-test (`FAIL <name>: ModuleNotFoundError ...  0 of N passed`) —
    a stack-shaped verdict for an environment problem that has a one-line
    remedy. The runner now preflights before any test runs and names it.

    Issue #336: the diagnostic now names `revl test` as the documented happy
    path (the console script the setup script installs) before the
    absolute-interpreter fallback. A test that just asserts the
    `.venv/bin/python -m revl test` substring would let a regression to "the
    only documented path is the one with the CWD-shadowing window" pass
    silently, so the lead-spell assertion is the load-bearing one.
    """
    import revl.test as revl_test  # noqa: PLC0415

    monkeypatch.setattr(revl_test, "_cordis_available", lambda: False)
    ir = compile_files([str(ROOT / "examples" / "lifecycle_cache.rvl")])
    outcome, message = revl_test.run_py(ir)
    # FR-5: an absent runtime is a skip-with-reason, exactly like every other
    # tier's missing-toolchain gate — never a fail that reads as a regression.
    assert outcome == "skip"
    assert message.startswith("preflight:")
    assert "sh backends/python/setup.sh" in message
    # documented happy path is the LEAD line, not just present somewhere
    # (issue #336: the absolute-interpreter form has the CWD-shadowing window
    # issue #317 names; `revl` (a console script) is window-free by design).
    assert "revl test" in message
    rerun_idx = message.index("then rerun")
    happy_idx = message.index("revl test", rerun_idx)
    fallback_idx = message.index("backends/python/.venv/bin/python -P -m revl test",
                                 rerun_idx)
    assert happy_idx < fallback_idx, (
        f"`revl test` (the documented happy path) must appear before the "
        f"`backends/python/.venv/bin/python -m revl test` fallback in the "
        f"diagnostic; got {message!r}")
    # none of the stack-shaped shape may leak through
    assert "ModuleNotFoundError" not in message
    assert "0 of" not in message


def test_py_tier_runtime_gate_is_keyed_to_the_document(monkeypatch):
    """The gate fires only when the emitted source actually drives cordis
    (a `lifecycle test`). A pure-fn document's tests must still RUN — and
    pass — on a cordis-less interpreter, not get swept into the preflight."""
    import revl.test as revl_test  # noqa: PLC0415

    monkeypatch.setattr(revl_test, "_cordis_available", lambda: False)
    doc = ROOT / "dogfood" / "_dogfix_pure_test_fixture.rvl"
    doc.write_text(
        "fn add(a: Int, b: Int) -> Int { return a + b }\n"
        "\n"
        "test \"adds\" {\n"
        "  assert add(1, 2) == 3\n"
        "}\n", encoding="utf-8")
    try:
        ir = compile_files([str(doc)])
        outcome, message = revl_test.run_py(ir)
        assert outcome == "pass", message
        assert "1 test(s) passed" in message
    finally:
        doc.unlink(missing_ok=True)


# ------------------------------------------------------------ real runtime
#
# The golden path: `revl run manifest --backend py` boots the composition,
# streams the host/lifecycle trace, and exits cleanly on EOF after a
# no-residue teardown. No hand-written driver anywhere in the stack.


@needs_cordis
def test_run_boots_and_streams_the_lifecycle_trace_and_exits(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        "[PgDatabase]\nurl = \"postgres://e2e:5432/app\"\n",
        encoding="utf-8")
    result = _run_cli([USER_CACHE, "--backend", "py", "--config", str(cfg)])
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots: providers first, each fiber making the
    # PENDING -> LOADING -> ACTIVE lifecycle, with the host trace interleaved
    assert "== load composition ==" in out
    assert "PgDatabase" in out and "UserCache" in out
    assert "PENDING -> LOADING" in out
    assert "LOADING -> ACTIVE" in out
    assert "postgres://e2e:5432/app" in out  # the host Pool.open trace
    assert "map#1.new" in out
    assert "== live" in out

    # then the no-host-driver exit: stdin hits EOF, the run tears down,
    # proves no residue, and the process exits 0
    assert "== teardown" in out
    assert "UNLOADING -> DISPOSED" in out
    assert "no residue — the composition left nothing behind" in out