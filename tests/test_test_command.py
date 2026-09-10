"""`revl test --backend`: one source's `test` blocks, proven across tiers.

The dispatch logic is tested deterministically (toolchain-gated runners are
monkeypatched); the real toolchains are exercised when present, matching how
the per-tier suites gate themselves.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import test as test_module  # noqa: E402

needs_wasmtime = pytest.mark.skipif(
    shutil.which("wasmtime") is None, reason="wasmtime not installed")


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "revl", "test", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def _write_rvl(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_default_backend_is_py(tmp_path):
    passing = _write_rvl(tmp_path, "passing.rvl", 'test "passes" { assert true }')
    result = _cli(tmp_path, str(passing))
    assert result.returncode == 0, result.stderr
    assert "PASS passes" in result.stdout
    assert "[py] pass: 1 test(s) passed" in result.stdout

    failing = _write_rvl(tmp_path, "failing.rvl", 'test "fails" { assert 1 == 2 }')
    result = _cli(tmp_path, str(failing))
    assert result.returncode == 1
    assert "FAIL fails" in result.stdout


@needs_wasmtime
def test_backend_wasm_runs_tests(tmp_path):
    source = _write_rvl(
        tmp_path, "x.rvl",
        'fn one() -> Int { return 1 }\n'
        'test "one is one" { assert one() == 1 }\n'
        'test "empty body passes" { }\n')
    result = _cli(tmp_path, str(source), "--backend", "wasm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS one is one" in result.stdout
    assert "[wasm] pass: wasmtime: 2 test(s) passed" in result.stdout


@needs_wasmtime
def test_backend_wasm_failing_assert_exits_1(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "boom" { assert 1 == 2 }')
    result = _cli(tmp_path, str(source), "--backend", "wasm")
    assert result.returncode == 1
    assert "FAIL boom" in result.stdout
    # the trap that the failed `assert` lowered to is surfaced, not swallowed
    assert "unreachable" in (result.stdout + result.stderr)


def test_backend_wasm_skips_without_wasmtime(monkeypatch):
    """Graceful degradation: no wasmtime binary -> skip with a reason, never a
    fake pass (the same gate every other toolchain-bound runner applies)."""
    monkeypatch.setattr(test_module.shutil, "which", lambda name: None)
    ir = compile_source('test "t" { assert true }')
    outcome, message = test_module.run_wasm(ir)
    assert outcome == "skip"
    assert "wasmtime not installed" in message


_VITEST = ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest"


@pytest.mark.skipif(not _VITEST.exists(), reason="vitest not installed in backends/typescript")
def test_backend_ts_runs_vitest(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "add works" { assert 1 + 2 == 3 }')
    result = _cli(tmp_path, str(source), "--backend", "ts")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ts] pass:" in result.stdout
    # the temp emitted test file is cleaned up afterwards
    generated = ROOT / "backends" / "typescript" / "tests" / "generated"
    assert not list(generated.glob("revl_test_*.test.ts"))


@pytest.mark.skipif(not _VITEST.exists(), reason="vitest not installed in backends/typescript")
def test_backend_ts_failing_assert_exits_1(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "boom" { assert 1 == 2 }')
    result = _cli(tmp_path, str(source), "--backend", "ts")
    assert result.returncode == 1


def test_all_aggregates_tiers(monkeypatch, capsys):
    """`--all` runs every tier and fails only on a tier that actually failed;
    skipped tiers (missing toolchains, by-design refusals) never fail the run,
    and each tier's line carries a pass/skip:reason/fail verdict (FR-5)."""
    ir = compile_source('test "t" { assert true }')

    def _skipping(_ir):
        return ("skip", "not available in this test")

    for name in ("ts", "rust", "java", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _skipping)

    # py genuinely runs — it needs no toolchain
    assert test_module.test_command(ir, "all") == 0
    out = capsys.readouterr().out
    assert "[py] pass:" in out
    assert "[ts] skip: not available in this test" in out
    assert "[wasm] skip: not available in this test" in out
    assert "summary: 2 pass, 4 skipped, 0 failed" in out
    assert "all tiers passed" in out
    assert capsys.readouterr().err == ""


def test_all_fails_when_a_tier_fails(monkeypatch, capsys):
    """A failing tier fails the run; the summary counts the verdicts instead
    of reporting every non-pass as a failure (FR-5)."""
    ir = compile_source('test "t" { assert true }')

    def _failing(_ir):
        return ("fail", "boom")

    for name in ("ts", "rust", "java", "wasm", "go"):
        monkeypatch.setitem(test_module.RUNNERS, name, _failing)
    assert test_module.test_command(ir, "all") == 1
    err = capsys.readouterr().err
    assert "summary: 1 pass, 0 skipped, 5 failed" in err
    assert "5 tier(s) failed" in err


def test_all_mixed_verdicts_summarize_skips_separately(monkeypatch, capsys):
    """The FR-5 signal: by-design refusals (skips) must not read as failures.
    A run with skips and no fails exits 0 and prints the verdict counts."""
    ir = compile_source('test "t" { assert true }')

    def _skipping(_ir):
        return ("skip", "lifecycle tests are not lowerable on this tier yet")

    # Mock every non-reference tier so the verdict counts are deterministic
    # regardless of which toolchains the runner happens to have (a frontend
    # job without wasmtime, for instance, would otherwise turn wasm's real
    # plain-test run into a skip and shift the summary).
    for name in ("ts", "rust", "java", "wasm", "go"):
        monkeypatch.setitem(test_module.RUNNERS, name, _skipping)
    assert test_module.test_command(ir, "all") == 0
    out = capsys.readouterr().out
    assert "[ts] skip: lifecycle tests are not lowerable on this tier yet" in out
    assert "summary: 1 pass, 5 skipped, 0 failed" in out




def test_lifecycle_refusals_on_followup_tiers_are_skips(monkeypatch, capsys):
    """A tier that cannot express a `lifecycle test` refuses it by name, and in
    `--all` that refusal is a skip-with-reason — the verdict column's whole
    point — never a tier failure. Both runners here are stand-ins for that
    conversion: what still refuses is wasm's scalar substrate and, on java,
    the pre-ir_version-3 dialects and an `advance` step (item 178(b))."""
    ir = compile_source('lifecycle test "live" { }')

    def _refusing_java(_ir):
        if test_module._lifecycle_refusal(
                _ir, Exception("lifecycle test 'live' is not lowerable on the "
                               "cordis4j tier: it drives a live composition "
                               "(load/call/unload) and asserts R4 "
                               "residue-freedom through the host runtime's "
                               "introspection, which only the reference tier "
                               "implements — run it with `revl test "
                               "--backend py`")):
            return ("skip", "lifecycle tests are a documented follow-up on this tier")
        return ("fail", "boom")

    # keep the other runners deterministic and fast; only the follow-up
    # tiers' refusal-to-skip conversion is under test
    for name in ("ts", "rust", "go"):
        monkeypatch.setitem(test_module.RUNNERS, name, lambda _ir: ("pass", "ok"))
    monkeypatch.setitem(test_module.RUNNERS, "py", lambda _ir: ("skip", "no cordis-py"))
    for name in ("java", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _refusing_java)
    assert test_module.test_command(ir, "all") == 0
    out = capsys.readouterr().out
    assert "[java] skip: lifecycle tests are a documented follow-up" in out
    assert "[wasm] skip: lifecycle tests are a documented follow-up" in out
    assert "summary: 3 pass, 3 skipped, 0 failed" in out

# --------------------------------------------------------------------------- #
# selection and collection (issue #843).                                        #
# --------------------------------------------------------------------------- #
#
# `revl test <files>` had one mode: run everything, report one aggregate count.
# `--list` and `--filter` add the two questions that mode cannot answer. Both
# are selection and reporting only, so the tests here pin two things beyond the
# happy path: an empty selection is never a silent green, and a command line
# carrying neither flag behaves exactly as it did before.

_THREE = ('test "add works" { assert 1 + 2 == 3 }\n'
          'test "add is commutative" { assert 1 + 2 == 2 + 1 }\n'
          'test "boom" { assert 1 == 2 }\n')


def test_list_prints_every_collected_name_and_runs_nothing(tmp_path):
    """`--list` answers "which tests does this compilation collect" and pays for
    nothing else: the file's tests include a failing one, so any execution at
    all would move the exit code or leave a verdict line behind."""
    source = _write_rvl(tmp_path, "x.rvl", _THREE)
    result = _cli(tmp_path, str(source), "--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "add works", "add is commutative", "boom", "3 test(s) collected"]
    # the three-state proof that no runner ran: no verdict line, no tier line,
    # and no failure from the test that fails when it executes
    assert "PASS" not in result.stdout
    assert "FAIL" not in result.stdout
    assert "[py]" not in result.stdout
    assert result.stderr == ""


def test_list_never_reaches_a_tier_runner(monkeypatch, capsys):
    """Collection is the whole of `--list`: no tier runner is called at all, so
    a missing toolchain or an emit failure cannot make a listing lie."""
    calls = []

    def _forbidden(_ir):
        calls.append("ran")
        raise AssertionError("--list must not run a tier")

    for name in list(test_module.RUNNERS):
        monkeypatch.setitem(test_module.RUNNERS, name, _forbidden)

    ir = compile_source(_THREE)
    assert test_module.test_command(ir, "all", list_tests=True) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "add works", "add is commutative", "boom", "3 test(s) collected"]
    assert captured.err == ""
    assert calls == []


def test_list_covers_every_named_test_section(tmp_path):
    """The listing spans `test`, `prop test` and `fault test`: the whole
    surface `--filter` can select over, not just the plain `test` blocks."""
    source = _write_rvl(
        tmp_path, "x.rvl",
        'prop test "identity holds" (a: Int) { assert a + 0 == a }\n'
        'test "add works" { assert 1 + 1 == 2 }\n')
    result = _cli(tmp_path, str(source), "--list")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "add works", "identity holds", "2 test(s) collected"]


def test_list_with_a_filter_lists_only_the_selection(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", _THREE)
    result = _cli(tmp_path, str(source), "--list", "--filter", "add")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "add works", "add is commutative", "2 test(s) collected"]


def test_filter_runs_only_the_selection(tmp_path):
    """A plain substring over test names, and the verdicts of what it selects
    are the verdicts those tests have in the unfiltered run."""
    source = _write_rvl(tmp_path, "x.rvl", _THREE)

    everything = _cli(tmp_path, str(source))
    assert everything.returncode == 1
    assert "[py] fail: 2 of 3 test(s) passed" in everything.stderr

    selected = _cli(tmp_path, str(source), "--filter", "add")
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert "PASS add works" in selected.stdout
    assert "PASS add is commutative" in selected.stdout
    assert "FAIL" not in selected.stdout
    assert "boom" not in selected.stdout
    assert "[py] pass: 2 test(s) passed" in selected.stdout

    # a selected test keeps its verdict: the failing one fails, and only it ran
    failing = _cli(tmp_path, str(source), "--filter", "boom")
    assert failing.returncode == 1
    assert "FAIL boom" in failing.stdout
    assert "[py] fail: 0 of 1 test(s) passed" in failing.stderr
    assert "add works" not in failing.stdout


def test_filter_selects_the_units_every_tier_receives(monkeypatch):
    """The selection is applied to the IR before the dispatch, so "only the
    selected tests run" holds for every tier and every mode that reads a test
    section, not just the py tier whose verdict lines we can read back."""
    seen = []

    def _recording(ir):
        seen.append(sorted(unit["name"] for unit in ir.get("tests") or []))
        return ("pass", "ok")

    for name in list(test_module.RUNNERS):
        monkeypatch.setitem(test_module.RUNNERS, name, _recording)

    ir = compile_source(_THREE)
    assert test_module.test_command(ir, "ts", filter_pattern="add") == 0
    assert seen == [["add is commutative", "add works"]]


def test_filter_matching_nothing_is_a_usage_error_not_a_green(tmp_path):
    """The ambiguity the issue is about: a green run over nothing must not look
    like a green run over passing tests. Exit 2, on stderr, and no test ran."""
    source = _write_rvl(tmp_path, "x.rvl", _THREE)
    result = _cli(tmp_path, str(source), "--filter", "zzz")
    assert result.returncode == 2
    assert result.stdout == ""
    assert ("error: --filter 'zzz' matched none of the 3 collected test "
            "unit(s); nothing ran") in result.stderr


def test_list_of_a_compilation_with_no_tests_is_a_usage_error(tmp_path):
    """Zero collected tests reads the same however you asked for a selection;
    the flagless run keeps its old aggregate behaviour and its old exit code."""
    source = _write_rvl(tmp_path, "x.rvl", "fn one() -> Int { return 1 }\n")

    listed = _cli(tmp_path, str(source), "--list")
    assert listed.returncode == 2
    assert "collects no test units" in listed.stderr

    filtered = _cli(tmp_path, str(source), "--filter", "anything")
    assert filtered.returncode == 2
    assert "collects no test units" in filtered.stderr

    plain = _cli(tmp_path, str(source))
    assert plain.returncode == 0
    assert "no tests to run" in plain.stdout


def test_filter_refuses_the_modes_that_do_not_run_named_units(tmp_path):
    """`--sweep` and `--schedule-*` sweep a composition's steps, not its named
    test units, so a filter cannot select inside them: say so (exit 2) rather
    than appear to select while the whole sweep runs anyway."""
    source = _write_rvl(tmp_path, "x.rvl", _THREE)

    swept = _cli(tmp_path, str(source), "--sweep", "--filter", "add")
    assert swept.returncode == 2
    assert "run them separately" in swept.stderr

    scheduled = _cli(tmp_path, str(source), "--schedule-seeds", "5", "--filter", "add")
    assert scheduled.returncode == 2
    assert "run them separately" in scheduled.stderr


def test_no_new_flags_keeps_the_aggregate_output_and_exit_codes(tmp_path):
    """Backwards compatibility, pinned: without `--list` / `--filter` the run is
    the same run: same verdict lines, same aggregate line, same exit codes."""
    source = _write_rvl(tmp_path, "x.rvl", _THREE)

    result = _cli(tmp_path, str(source))
    assert result.returncode == 1
    assert result.stdout.count("PASS ") == 2
    assert "FAIL boom" in result.stdout
    assert "[py] fail: 2 of 3 test(s) passed" in result.stderr

    passing = _write_rvl(tmp_path, "p.rvl", 'test "passes" { assert true }')
    result = _cli(tmp_path, str(passing))
    assert result.returncode == 0
    assert result.stdout.splitlines()[:2] == ["PASS passes",
                                             "[py] pass: 1 test(s) passed"]


_PROP_AND_TEST = ('prop test "identity holds" (a: Int) { assert a + 0 == a }\n'
                  'test "add works" { assert 1 + 1 == 2 }\n')


def test_filter_of_a_prop_name_is_a_tier_skip_not_an_emit_failure(tmp_path):
    """A `--filter` that keeps only `prop test` units prunes the IR's `tests`
    section, and `prop test` runs on the py reference tier only. Every emitter
    tier therefore has no unit left, and used to be handed a document with
    nothing in it at all: "emitter refused: IR document has no components,
    types, functions, externs, or tests", exit 1, on a file that passes every
    tier unfiltered (issue #843). The tier reports the skip its own prop note
    already documents instead."""
    source = _write_rvl(tmp_path, "x.rvl", _PROP_AND_TEST)

    for tier in ("ts", "rust", "java", "go", "wasm"):
        run = _cli(tmp_path, str(source), "--backend", tier,
                   "--filter", "identity holds")
        output = run.stdout + run.stderr
        assert run.returncode == 0, output
        assert (f"[{tier}] skip: --filter 'identity holds' selected no test unit "
                f"for the {tier} tier; 1 prop test(s) skipped (py tier only)"
                ) in run.stdout, output
        assert "emitter refused" not in output, output
        assert f"[{tier}] fail" not in output, output

    # and the tier that DOES run the selection keeps its verdict
    py = _cli(tmp_path, str(source), "--backend", "py", "--filter", "identity holds")
    assert py.returncode == 0, py.stdout + py.stderr
    assert "[py] pass: 1 of 1 prop test(s) held" in py.stdout


def test_filter_that_leaves_a_tier_nothing_never_prints_pass(tmp_path):
    """The wasm false green. Pruning the plain tests left the pure-test path
    with no test to emit and no lifecycle test to drive, and the tier printed
    `pass: no tests emitted by the backend` and exited 0 for a run that executed
    nothing, while the unfiltered run of the same file prints `pass: wasmtime: 1
    test(s) passed`. A run that executes nothing on a tier is a skip, never a
    pass (issue #843)."""
    source = _write_rvl(tmp_path, "x.rvl", _PROP_AND_TEST)

    result = _cli(tmp_path, str(source), "--backend", "wasm",
                  "--filter", "identity holds")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[wasm] pass" not in result.stdout
    assert "no tests emitted by the backend" not in result.stdout
    assert "[wasm] skip: --filter 'identity holds' selected no test unit " \
           "for the wasm tier" in result.stdout


def test_a_selection_a_tier_cannot_run_never_reaches_that_runner(monkeypatch, capsys):
    """The skip is decided before the dispatch, so the emitter is never handed
    the empty document and the tier's toolchain is never even consulted: the
    runner is the thing that refused."""
    calls = []

    def _forbidden(_ir):
        calls.append("ran")
        raise AssertionError("a tier with no selected unit must not run")

    for name in ("ts", "rust", "java", "go", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _forbidden)

    ir = compile_source(_PROP_AND_TEST)
    for name in ("ts", "rust", "java", "go", "wasm"):
        assert test_module.test_command(ir, name, filter_pattern="identity holds") == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "[rust] skip: --filter 'identity holds' selected no test unit for the " \
           "rust tier; 1 prop test(s) skipped (py tier only)" in out

    # the py reference tier DOES run the selection, and is the only one that does
    def _py(_ir):
        calls.append("py")
        return ("pass", "1 of 1 prop test(s) held")

    monkeypatch.setitem(test_module.RUNNERS, "py", _py)
    assert test_module.test_command(ir, "py", filter_pattern="identity holds") == 0
    assert calls == ["py"]


def test_all_with_a_prop_only_selection_summarises_skips_not_failures(tmp_path):
    """`--backend all` is the portability assertion: on a prop-only selection
    the py tier runs the property and every emitter tier skips with the reason,
    so nothing failed and the run is not the `3 failed` exit 1 it used to be."""
    source = _write_rvl(tmp_path, "x.rvl", _PROP_AND_TEST)

    result = _cli(tmp_path, str(source), "--backend", "all",
                  "--filter", "identity holds")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[py] pass: 1 of 1 prop test(s) held" in result.stdout
    assert "summary: 1 pass, 5 skipped, 0 failed" in result.stdout
    assert "all tiers passed" in result.stdout
    assert "tier(s) failed" not in (result.stdout + result.stderr)


def test_a_prop_only_document_without_a_filter_is_dispatched_unchanged(monkeypatch):
    """The fix is about the path a FILTER creates, not about prop-only files:
    without `--filter` the tier gate is off, so every tier is still handed
    exactly the IR the compilation produced and reports its own verdict."""
    seen = []

    def _recording(ir):
        seen.append(sorted(unit["name"] for unit in ir.get("tests") or []))
        return ("pass", "ok")

    for name in ("ts", "rust", "java", "go", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _recording)

    ir = compile_source('prop test "identity holds" (a: Int) { assert a + 0 == a }\n')
    for name in ("ts", "rust", "java", "go", "wasm"):
        assert test_module.test_command(ir, name) == 0
    assert seen == [[]] * 5
