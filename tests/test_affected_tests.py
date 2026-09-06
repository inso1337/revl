"""Unit tests for tools/affected_tests.py — the pre-merge affected-test selector.

These pin the load-bearing SOUNDNESS behaviour so the selector cannot rot into
fail-open: a core-file change picks the FULL gate, an unmapped file picks FULL, a
single backend emitter picks only that tier (+ folded goldens), and a single
stdlib module picks only the tests that touch its public API.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "revl_affected_tests", ROOT / "tools" / "affected_tests.py"
)
at = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(at)


def sel(*changed):
    return at.select(list(changed), ROOT)


# --- backend emitter: only that tier + folded goldens ---------------------- #
def test_wasm_emitter_selects_only_wasm_suite():
    r = sel("backends/wasm/emit.py")
    assert r["full"] is False
    assert set(r["backends"]) == {"wasm"}
    for other in ("python", "go", "rust", "java"):
        assert other not in r["backends"]
    assert "tests/test_goldens.py" in r["pytest"]
    assert "tests/test_wasm_backend.py" in r["pytest"]
    assert "conformance" in r["gates"]


def test_go_emitter_selects_only_go_suite():
    r = sel("backends/go/emit.py")
    assert r["full"] is False
    assert set(r["backends"]) == {"go"}
    assert "tests/test_goldens.py" in r["pytest"]


# --- core frontend file -> FULL (soundness) -------------------------------- #
def test_parser_change_is_full():
    r = sel("src/revl/parser.py")
    assert r["full"] is True
    assert "core" in r["reason"]


def test_compiler_and_lower_are_full():
    assert sel("src/revl/compiler.py")["full"] is True
    assert sel("src/revl/lower.py")["full"] is True
    assert sel("src/revl/typecheck.py")["full"] is True


def test_compile_reachable_nonleaf_is_full():
    # run_go is on the lazy compile-reachable graph -> full even though it looks
    # tier-specific. Fail-safe over-selection is intended.
    assert sel("src/revl/run_go.py")["full"] is True


# --- stdlib module -> only tests touching its public API ------------------- #
def test_stdlib_json_selects_json_tests_only():
    r = sel("stdlib/json.rvl")
    assert r["full"] is False
    assert r["backends"] == []
    assert "tests/test_json_stdlib.py" in r["pytest"]
    assert "tests/test_json_try_parse.py" in r["pytest"]
    # tightness: a test that never touches json's public API is NOT dragged in.
    assert "tests/test_lexer.py" not in r["pytest"]


# --- unmapped / structural -> FULL (fail safe, never fail open) ------------ #
def test_unmapped_file_is_full():
    assert sel("weird/random_thing.xyz")["full"] is True


def test_structural_changes_are_full():
    assert sel("Makefile")["full"] is True
    assert sel("tools/pre_merge.sh")["full"] is True
    assert sel("tests/conftest.py")["full"] is True
    assert sel(".github/workflows/ci.yml")["full"] is True
    assert sel("examples/user_cache.ir.json")["full"] is True


def test_core_dominates_a_mixed_changeset():
    # A safe backend change mixed with a core change must still be FULL.
    r = sel("backends/wasm/emit.py", "src/revl/parser.py")
    assert r["full"] is True


def test_empty_changeset_is_full():
    assert sel()["full"] is True


# --- added / deleted test files (issue #162) -------------------------------- #
def _override(changed, added=(), deleted=()):
    return at._test_add_delete_override(
        list(changed), set(added), set(deleted), ROOT
    )


def test_deleted_test_file_is_still_full():
    # A survivor may import it (test_selfhost_lower is imported by three other
    # test modules), so the blast radius is not visible from the path alone.
    r = _override(["tests/test_newthing.py"], deleted={"tests/test_newthing.py"})
    assert r is not None and r["full"] is True


def test_added_test_file_alone_is_not_full():
    # Nothing else changed, so nothing but the new test can newly fail.
    r = _override(["tests/test_newthing.py"], added={"tests/test_newthing.py"})
    assert r is not None and r["full"] is False
    assert r["pytest"] == ["tests/test_newthing.py"]
    assert r["backends"] == []


def test_added_test_file_beside_its_subject_is_not_full():
    # The regression issue #162 names: a tier change plus the test that covers
    # it must stay narrow, not escalate to the whole tree.
    r = _override(
        ["backends/wasm/emit.py", "tests/test_wasm_newthing.py"],
        added={"tests/test_wasm_newthing.py"},
    )
    assert r is not None and r["full"] is False
    assert "tests/test_wasm_newthing.py" in r["pytest"]
    assert set(r["backends"]) == {"wasm"}


def test_added_test_file_with_no_touched_subject_is_full():
    # The fallback must survive: a new test that maps to nothing the diff
    # touched is the ambiguity the blanket escalation was standing in for.
    r = _override(
        ["docs/guide-humans.md", "tests/test_unrelated_newthing.py"],
        added={"tests/test_unrelated_newthing.py"},
    )
    assert r is not None and r["full"] is True


def test_added_test_file_beside_a_core_change_is_still_full():
    r = _override(
        ["src/revl/parser.py", "tests/test_parser_newthing.py"],
        added={"tests/test_parser_newthing.py"},
    )
    assert r is not None and r["full"] is True


def test_no_added_or_deleted_test_file_defers_to_select():
    assert _override(["backends/wasm/emit.py"]) is None


# --- selector self-change runs its own unit test --------------------------- #
def test_selector_change_runs_self_test():
    r = sel("tools/affected_tests.py")
    assert r["full"] is False
    assert "tests/test_affected_tests.py" in r["pytest"]


# --- machine emit round-trips the decision --------------------------------- #
def test_machine_emit_is_parseable():
    r = sel("stdlib/json.rvl")
    out = at._emit(r, "abc123", "machine")
    lines = dict(ln.split(" ", 1) for ln in out.splitlines())
    assert lines["FULL"] == "0"
    assert "test_json_stdlib.py" in lines["PYTEST"]
    full = at._emit(sel("src/revl/parser.py"), "abc123", "machine")
    assert "FULL 1" in full


# --- selfhost/<stem>.rvl -> narrow self-host oracle set (issue #431) -------- #
def test_selfhost_lower_selects_ir_oracle_not_the_slow_descent_test():
    # The whole point of issue #431: a lower.rvl edit must run the fast IR oracle
    # + the line-coverage gate, NOT the FULL suite, and NOT the >120s descent
    # test that made the FULL fallback time out under the hook's --timeout.
    r = sel("selfhost/lower.rvl")
    assert r["full"] is False
    assert "tests/test_selfhost_lower_ir.py" in r["pytest"]
    assert "tests/test_selfhost_line_coverage.py" in r["pytest"]
    assert "tests/test_selfhost_lower.py" not in r["pytest"]
    assert r["backends"] == []


def test_selfhost_emit_selects_only_its_own_oracle():
    r = sel("selfhost/emit_py.rvl")
    assert r["full"] is False
    assert "tests/test_selfhost_emit_py.py" in r["pytest"]
    assert "tests/test_selfhost_line_coverage.py" in r["pytest"]
    # tightness: a sibling emitter's oracle is not dragged in.
    assert "tests/test_selfhost_emit_ts.py" not in r["pytest"]


def test_selfhost_checker_and_parser_get_oracle_plus_coverage():
    for stem, oracle in (("checker", "tests/test_selfhost_checker.py"),
                         ("parser", "tests/test_selfhost_parser.py")):
        r = sel(f"selfhost/{stem}.rvl")
        assert r["full"] is False
        assert oracle in r["pytest"]
        assert "tests/test_selfhost_line_coverage.py" in r["pytest"]


def test_selfhost_non_source_change_is_full():
    assert sel("selfhost/scratch.bin")["full"] is True


def test_selfhost_oracle_map_covers_the_tree():
    """Every selfhost/*.rvl file must map to an oracle and every mapped test must
    exist, so a new self-host file (or a renamed oracle) cannot silently fall
    back to the unmapped FULL gate the hook was timing out on (issue #431)."""
    from tools.affected_tests import SELFHOST_ALWAYS, SELFHOST_ORACLE_TESTS

    root = Path(__file__).resolve().parent.parent
    on_disk = {p.stem for p in (root / "selfhost").glob("*.rvl")}
    mapped = set(SELFHOST_ORACLE_TESTS)
    assert mapped == on_disk, (
        "SELFHOST_ORACLE_TESTS has drifted from selfhost/*.rvl.\n"
        f"  unmapped self-host files (would fall back to FULL): "
        f"{sorted(on_disk - mapped)}\n"
        f"  stale keys (no such selfhost file): {sorted(mapped - on_disk)}\n"
        "Update tools/affected_tests.py::SELFHOST_ORACLE_TESTS."
    )
    for stem, tests in SELFHOST_ORACLE_TESTS.items():
        for t in tuple(tests) + tuple(SELFHOST_ALWAYS):
            assert (root / t).is_file(), f"{stem} maps to a missing test {t}"


def test_bench_dependent_tests_is_the_actual_set_of_bench_readers():
    """`bench/` selects BENCH_DEPENDENT_TESTS instead of the FULL gate, so that
    tuple has to BE the set of test modules that depend on a bench artifact.

    Recomputed from the tree rather than trusted: if someone adds a test that
    reads `bench/results/...` and does not extend the tuple, a bench change
    would stop selecting it and the regression it guards would ship green.
    That is the whole failure class this selector exists to avoid, so the list
    is checked rather than maintained by hand.
    """
    from tools.affected_tests import BENCH_DEPENDENT_TESTS

    root = Path(__file__).resolve().parent.parent
    actual = {
        f"tests/{p.name}"
        for p in sorted((root / "tests").glob("test_*.py"))
        if "bench/" in p.read_text(encoding="utf-8")
    }
    declared = set(BENCH_DEPENDENT_TESTS)

    assert declared == actual, (
        "BENCH_DEPENDENT_TESTS has drifted from the tree.\n"
        f"  missing from the tuple (a bench change would NOT select these): "
        f"{sorted(actual - declared)}\n"
        f"  stale entries (no longer mention bench/): {sorted(declared - actual)}\n"
        "Update tools/affected_tests.py::BENCH_DEPENDENT_TESTS."
    )
