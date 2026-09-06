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


# --- CI-3: a .md / docs change compiles the doc snippets (issue #550) ------- #
def test_markdown_change_selects_doc_examples():
    """Before #550 a `.md` edit matched no pytest rule and selected ZERO tests,
    so a rotted ```revl block in README/docs landed green. Every doc edit must
    re-run tests/test_doc_examples.py, which sweeps README.md + docs/**.md."""
    for f in ("README.md", "docs/guide-humans.md", "docs/conformance.md"):
        r = sel(f)
        assert r["full"] is False, f"{f} should be a targeted selection"
        assert "tests/test_doc_examples.py" in r["pytest"], (
            f"{f} does not select the doc-examples compiler"
        )
        assert "docs" in r["gates"] and "conformance" in r["gates"]


def test_doc_examples_test_exists():
    """Anti-vacuity: the node the .md rule selects must be a real file."""
    assert (ROOT / "tests" / "test_doc_examples.py").is_file()


# --- CI-3: stdlib change pulls the self-host oracles that `use` it (#550) ---- #
def test_stdlib_change_selects_selfhost_oracles_that_use_it():
    """stdlib/*.rvl is `use`d by the self-host compiler sources, which are
    compiled inside the self-host oracle tests. Nothing under tests/ names those
    `use` imports, so before #550 a stdlib edit that broke only the self-host
    emitters shipped green. value.rvl is used by all six emitters, so its change
    must select their oracles + the line-coverage gate."""
    r = sel("stdlib/value.rvl")
    assert r["full"] is False
    for tier in ("go", "java", "py", "rust", "ts", "wasm"):
        assert f"tests/test_selfhost_emit_{tier}.py" in r["pytest"], (
            f"stdlib/value.rvl misses the emit_{tier} self-host oracle"
        )
    assert "tests/test_selfhost_line_coverage.py" in r["pytest"]


def test_stdlib_change_follows_use_graph_transitively():
    """json.rvl is `use`d only by compile.rvl, so a json.rvl edit must select
    the compile oracle. render.rvl is `use`d only by emit_ts.rvl; a change to it
    must not drag in unrelated emitter oracles."""
    rj = sel("stdlib/json.rvl")
    assert "tests/test_selfhost_compile.py" in rj["pytest"]
    rr = sel("stdlib/render.rvl")
    assert "tests/test_selfhost_emit_ts.py" in rr["pytest"]
    # tightness: render is used by no other selfhost file, so a sibling
    # emitter's oracle is not selected.
    assert "tests/test_selfhost_emit_go.py" not in rr["pytest"]


# --- CI-3: selfhost change follows the `use` graph to dependents (#550) ------ #
def test_selfhost_lexer_change_selects_its_dependents_oracles():
    """lexer.rvl is `use`d by parser/lower/checker (and lower/emitters feed
    compile), so a lexer.rvl edit changes what all of them compile to. Before
    #550 only the lexer oracle ran while the rest silently changed output."""
    r = sel("selfhost/lexer.rvl")
    assert r["full"] is False
    for t in ("lexer", "parser", "checker"):
        assert f"tests/test_selfhost_{t}.py" in r["pytest"], (
            f"lexer.rvl change misses the {t} dependent oracle"
        )
    # lower is a dependent, but via its fast IR oracle, NOT the slow descent
    # test the FULL fallback timed out on (issue #431).
    assert "tests/test_selfhost_lower_ir.py" in r["pytest"]
    assert "tests/test_selfhost_lower.py" not in r["pytest"]
    assert "tests/test_selfhost_compile.py" in r["pytest"]
    assert r["backends"] == []


def test_selfhost_emitter_change_selects_compile_dependent():
    """compile.rvl `use`s emit_py/emit_rust/emit_ts + lower, so a change to one
    of those emitters must also run the compile oracle. It must NOT drag in a
    sibling emitter that compile reaches independently."""
    r = sel("selfhost/emit_py.rvl")
    assert "tests/test_selfhost_emit_py.py" in r["pytest"]
    assert "tests/test_selfhost_compile.py" in r["pytest"]
    # emit_go is not on emit_py's reverse-reachable set.
    assert "tests/test_selfhost_emit_go.py" not in r["pytest"]


def test_selfhost_leaf_emitter_has_no_extra_dependents():
    """emit_go/emit_java/emit_wasm are `use`d by nothing, so their change stays
    exactly their own oracle (+ line coverage) — the narrow #431 behaviour."""
    r = sel("selfhost/emit_go.rvl")
    assert sorted(x for x in r["pytest"] if "selfhost" in x) == [
        "tests/test_selfhost_emit_go.py",
        "tests/test_selfhost_line_coverage.py",
    ]


def test_selfhost_use_graph_is_parsed_from_the_tree():
    """The `use` edges are recomputed from selfhost/*.rvl on disk, so a new
    import cannot silently escape the dependent selection."""
    sh_deps, std_deps = at._selfhost_use_graph(ROOT)
    # every selfhost file is a node
    on_disk = {p.stem for p in (ROOT / "selfhost").glob("*.rvl")}
    assert set(sh_deps) == on_disk
    # the composition backbone the selector relies on
    assert "lexer" in sh_deps["parser"]
    assert {"lexer", "parser"} <= sh_deps["lower"]
    assert {"lower", "emit_py"} <= sh_deps["compile"]
    assert "value" in std_deps["emit_py"]


# --- CI-3: a modified test file also runs its importers (#550) -------------- #
def test_modified_test_file_selects_its_importers():
    """Tests import one another (five gate tests `import test_selfhost_lower as
    oracle`), so editing a shared test module must run the importers too, not
    just the file itself."""
    r = sel("tests/test_selfhost_lower.py")
    assert r["full"] is False
    assert "tests/test_selfhost_lower.py" in r["pytest"]
    for imp in ("test_gate_crate_admit", "test_gate_wasm_vector",
                "test_inprocess_gate_rust"):
        assert f"tests/{imp}.py" in r["pytest"], (
            f"modified test_selfhost_lower.py does not select importer {imp}"
        )


def test_modified_test_file_without_importers_is_only_itself():
    """A leaf test nothing imports still selects just itself (no over-selection).
    A synthetic name is used so the assertion does not rot when tests are added."""
    r = sel("tests/test__no_importer_sentinel__.py")
    assert r["pytest"] == ["tests/test__no_importer_sentinel__.py"]


def test_test_importers_ignores_prefix_siblings():
    """The sibling-import match is anchored: an edit to a module whose name is a
    strict PREFIX of an imported one must not be matched. test_selfhost_lower is
    imported `as oracle` by five gate tests; querying the prefix `test_selfhost`
    must not sweep those in."""
    lower = at._test_importers(ROOT, "test_selfhost_lower")
    assert lower, "expected test_selfhost_lower to have sibling importers"
    prefix = at._test_importers(ROOT, "test_selfhost")
    assert not (prefix & lower), (
        "prefix query test_selfhost matched test_selfhost_lower's importers; "
        "the import forms are not anchored"
    )


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
