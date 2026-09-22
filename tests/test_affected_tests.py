"""Unit tests for tools/affected_tests.py — the pre-merge affected-test selector.

These pin the load-bearing SOUNDNESS behaviour so the selector cannot rot into
fail-open: a core-file change picks the FULL gate, an unmapped file picks FULL, a
single backend emitter picks only that tier (+ folded goldens), and a single
stdlib module picks only the tests that touch its public API.
"""
from __future__ import annotations

import ast
import subprocess
import importlib.util
import io
import sys
import tokenize
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


def test_reference_emitter_map_covers_every_tier():
    """The reverse of the self-host mapping: every backend tier's REFERENCE
    emitter must have its self-host port's oracle selected by a
    `backends/<tier>/**` change, and nothing in that mapping may point at a file
    that is not on disk.

    Issue #854: PR #850 changed `backends/python/emit.py` alone. The oracle that
    holds that file byte-identical to `selfhost/emit_py.rvl` was still selected
    (it names the path in prose), but `tests/test_selfhost_differential_survey.py`
    reaches the same file through a path built at runtime, so it was selected by
    nothing. A tier whose oracle is not named here can regress the same way, so
    this recomputes the tier set from the tree instead of trusting the table."""
    from tools.affected_tests import (
        BACKEND_TIERS,
        REFERENCE_EMITTER_ALWAYS,
        REFERENCE_EMITTER_ORACLE,
        SELFHOST_ALWAYS,
    )

    root = Path(__file__).resolve().parent.parent
    on_disk = {
        p.parent.name
        for p in (root / "backends").glob("*/emit.py")
    }
    mapped = set(REFERENCE_EMITTER_ORACLE)
    assert mapped == on_disk == set(BACKEND_TIERS), (
        "REFERENCE_EMITTER_ORACLE has drifted from backends/*/emit.py.\n"
        f"  tiers with an emitter but no oracle mapping: "
        f"{sorted(on_disk - mapped)}\n"
        f"  stale keys (no such backend emitter): {sorted(mapped - on_disk)}\n"
        f"  tier not listed in BACKEND_TIERS: {sorted(on_disk - set(BACKEND_TIERS))}\n"
        "Update tools/affected_tests.py::REFERENCE_EMITTER_ORACLE."
    )
    for tier, stem in REFERENCE_EMITTER_ORACLE.items():
        r = sel(f"backends/{tier}/emit.py")
        assert r["full"] is False, f"{tier} escalates a reference-emitter change"
        for t in (f"tests/test_selfhost_{stem}.py",) + tuple(SELFHOST_ALWAYS) \
                + tuple(REFERENCE_EMITTER_ALWAYS):
            assert (root / t).is_file(), f"{tier} maps to a missing test {t}"
            assert t in r["pytest"], (
                f"{t} guards backends/{tier}/emit.py and is not selected by it: "
                f"reason={r['reason']!r}"
            )


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
    """compile.rvl `use`s ALL SIX emitters + lower since roadmap item 146 gap 2,
    so a change to any one of them must also run the compile oracle. It must NOT
    drag in a sibling emitter that compile reaches independently."""
    for tier in ("py", "ts", "go", "java", "rust", "wasm"):
        r = sel(f"selfhost/emit_{tier}.rvl")
        assert f"tests/test_selfhost_emit_{tier}.py" in r["pytest"], tier
        assert "tests/test_selfhost_compile.py" in r["pytest"], tier
        # a sibling emitter is not on this emitter's reverse-reachable set
        sibling = "rust" if tier != "rust" else "py"
        assert f"tests/test_selfhost_emit_{sibling}.py" not in r["pytest"], tier


def test_selfhost_emitter_dependents_stay_narrow():
    """An emitter is `use`d only by compile.rvl, so its change stays its own
    oracle, the compile oracle and line coverage — the narrow #431 behaviour.
    Before item 146 gap 2, emit_go/emit_java/emit_wasm were `use`d by NOTHING and
    this list was two entries; the composition is what added the third."""
    r = sel("selfhost/emit_go.rvl")
    assert sorted(x for x in r["pytest"] if "selfhost" in x) == [
        "tests/test_selfhost_compile.py",
        # Added by the named-file rule (issue #1342): this module reads
        # `selfhost/emit_go.rvl` to measure which reference constructs the
        # self-host emitters cover, so the file's content is its input. The
        # list is one entry longer than the #431 shape and still narrow.
        "tests/test_selfhost_coverage.py",
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


def bench_dependants(root: Path) -> set[str]:
    """The test modules that read a `bench/` path in code, comments excluded.

    A comment cannot read an artifact, and counting one reds the whole root
    suite for a prose cross-reference: #860 added such a mention to
    `tests/test_gate_crate_admit.py` and this consistency check went red on
    main. Only mentions that survive comment stripping are dependants.
    """
    found = set()
    for p in sorted((root / "tests").glob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            mentions = any(
                t.type != tokenize.COMMENT and "bench/" in t.string for t in tokens
            )
        except (tokenize.TokenError, IndentationError, SyntaxError):
            mentions = "bench/" in text
        if mentions:
            found.add(f"tests/{p.name}")
    return found


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
    actual = bench_dependants(root)
    declared = set(BENCH_DEPENDENT_TESTS)

    assert declared == actual, (
        "BENCH_DEPENDENT_TESTS has drifted from the tree.\n"
        f"  missing from the tuple (a bench change would NOT select these): "
        f"{sorted(actual - declared)}\n"
        f"  stale entries (no longer mention bench/): {sorted(declared - actual)}\n"
        "Update tools/affected_tests.py::BENCH_DEPENDENT_TESTS."
    )


def test_a_comment_mentioning_bench_is_not_a_bench_dependant(tmp_path):
    """Pin the comment exclusion directly. It is the whole difference between a
    prose cross-reference reding main and not: #860 added one and this file's
    sibling check went red on main."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_prose.py").write_text(
        "# reads bench/results/latency.json\nVALUE = 1\n", encoding="utf-8"
    )
    (tests / "test_reader.py").write_text(
        'PATH = "bench/results/latency.json"\n', encoding="utf-8"
    )
    assert bench_dependants(tmp_path) == {"tests/test_reader.py"}


# --- the committed site wheel: the selector must cover the builder's inputs - #
def _build_wheel():
    """The real `playground/build_wheel.py`, loaded as a module."""
    spec = importlib.util.spec_from_file_location(
        "revl_build_wheel_for_gate", ROOT / "playground" / "build_wheel.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_site_wheel_input_selects_the_site_wheel_gate():
    """The committed playground/site wheel is a generated artifact, and the only
    thing that notices it has rotted is the `site-wheel` gate. So every file the
    builder READS has to select that gate — otherwise a change stales the wheel
    with nothing going red until the post-merge job on main.

    That is not hypothetical. The selector covered `src/revl/**.py` and
    `tools/check_site_wheel.py` and nothing else, while the builder also vendors
    `backends/python/*.py`. PR #1092 changed backends/python/revl_fs_workspace.py,
    passed every check, and left `site wheel drift` red on main across four
    consecutive merges.

    The input list is DERIVED from the builder (`wheel_inputs()`, which reports
    the same table `main()` builds from) rather than restated here. A second
    hand-written copy would only move the drift into this file: add a tree to the
    builder and the copy would silently stop describing it. Read from the builder,
    a new tree shows up here the moment it ships.
    """
    inputs = _build_wheel().wheel_inputs()
    assert inputs, "playground/build_wheel.py reported no inputs at all"
    missing = sorted(f for f in inputs if "site-wheel" not in sel(f)["gates"])
    assert not missing, (
        "playground/build_wheel.py reads these files, so a change to one stales "
        "the committed wheel, but the selector does not select the `site-wheel` "
        f"gate for them:\n  {missing}\n"
        "Add a rule in tools/affected_tests.py::select (or let the path fall to "
        "the fail-safe FULL gate, which carries every gate)."
    )


def test_every_selection_carries_the_vocabulary_gate():
    """Issue #1332. `tools/check_vocabulary_mirrors.py` walks every `.py` under
    `src/revl` and `tools` and reports a RELATION between two of them, so the
    commit that creates a mirror is routinely neither of the two files the
    ledger will name. There is no path set to select it on.

    Four classes reached `main` that way and reddened the required `lint`
    check. The four introducing commits selected, between them, `ruff`,
    `conformance`, `docs`, `site-wheel` and once the FULL gate -- and the FULL
    gate did not carry it either, because `tools/pre_merge.sh` did not run the
    tool in any mode. So the rule is "always", and the inputs below are chosen
    to be as far from a vocabulary as the tree gets.
    """
    for f in ("backends/rust/emit.py", "stdlib/json.rvl", "README.md",
              "src/revl/lexer.py", "tools/heldout_scoring.py",
              "docs/process.md", "tests/test_goldens.py"):
        assert "vocabulary" in sel(f)["gates"], (
            f"{f} does not select the vocabulary-mirror gate. It reads the "
            "whole tree, so every selection has to carry it."
        )
    assert "vocabulary" in at.GATES_ALL, (
        "the FULL gate does not carry the vocabulary-mirror gate, which is the "
        "hole that let issue #1332's four classes past a FULL pre-merge run."
    )


def test_a_vendored_python_backend_module_selects_the_wheel_gate_narrowly():
    """The #1092 path itself: a py-tier module the wheel vendors picks up the
    wheel gate, and does so WITHOUT escalating to FULL. Widening the selector
    must not turn `site-wheel` into a full-gate trigger — site-wheel.yml's header
    is explicit that a wheel check on every source change is its own outage
    class."""
    vendored = [f for f in _build_wheel().wheel_inputs()
                if f.startswith("backends/python/")]
    assert vendored, "the wheel no longer vendors any backends/python module"
    for f in vendored:
        r = sel(f)
        assert r["full"] is False, f"{f} escalated to the FULL gate"
        assert "site-wheel" in r["gates"], f"{f} did not select the wheel gate"
        assert set(r["backends"]) == {"python"}, f"{f} widened the backend set"


def test_the_wheel_gate_stays_off_what_the_wheel_does_not_vendor():
    """The inverse failure. A gate that fires on everything is as useless as one
    that never fires, so nothing outside the builder's input set may pull
    `site-wheel` into a targeted selection."""
    for f in ("backends/rust/emit.py", "backends/go/emit.py", "stdlib/json.rvl",
              "bench/codegen/python/run.py", "backends/python/golden/x.py"):
        r = sel(f)
        assert r["full"] is False, f"{f} unexpectedly escalated to FULL"
        assert "site-wheel" not in r["gates"], (
            f"{f} is not an input to playground/build_wheel.py, but it selects "
            "the site-wheel gate. Every source change now pays for a wheel "
            "rebuild, which is the per-PR outage class roadmap 110c removed."
        )


# --- census x provenance x construct-reach (issue #1215) -------------------- #
def test_census_change_selects_the_construct_reach_ledger():
    """The shadowing this pins: the census/provenance coupling rule (item 542)
    matched `tools/gate_reference_census.py` and `continue`d, so the issue-#1215
    rule sitting below it never ran and a census change silently stopped
    selecting tests/test_oracle_construct_reach.py — the one test covering the
    `gate_census` row that imports the census for its corpus walk and its fast
    engine. Both couplings belong to the same file, so both must be selected."""
    r = sel("tools/gate_reference_census.py")
    assert r["full"] is False
    for node in ("tests/test_gate_reference_census.py",
                 "tests/test_corpus_provenance.py",
                 "tests/test_oracle_construct_reach.py",
                 "tests/test_evolution_reward.py"):
        assert node in r["pytest"], (
            f"a change to the census does not select {node}. The item-542 "
            "provenance coupling, the issue-#1215 construct-reach coupling "
            "and the issue-#1328 self-evolution reward coupling all hang off "
            "this one file; a rule that answers only some of them is the "
            "shadowing this test exists to catch."
        )


def test_provenance_change_keeps_its_own_coupling_only():
    """The other half of the merge. `corpus_provenance.py` is not what the
    `gate_census` row imports, so widening the census rule must not hand the
    construct-reach ledger to every file the coupling rule matches."""
    r = sel("tools/corpus_provenance.py")
    assert r["full"] is False
    assert "tests/test_gate_reference_census.py" in r["pytest"]
    assert "tests/test_corpus_provenance.py" in r["pytest"]
    assert "tests/test_oracle_construct_reach.py" not in r["pytest"], (
        "corpus_provenance.py picked up the construct-reach ledger, which only "
        "the census's own coupling calls for"
    )


# --- the corpus provenance manifest: issue #1331 --------------------------- #
def _census():
    """The real `tools/gate_reference_census.py`, loaded as a module."""
    spec = importlib.util.spec_from_file_location(
        "revl_census_for_selector", ROOT / "tools" / "gate_reference_census.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_census_corpus_directory_reaches_the_provenance_manifest():
    """A document arriving in a scoring corpus must name its generation, and
    `tests/test_corpus_provenance.py` is the only thing that reads the manifest.
    No import graph reaches from a `.rvl` to that test, so it has to be SELECTED
    wherever a document can land, or the declaration requirement fires on main
    instead of on the branch, which is the red issue #1331 reports. Measured
    before this rule: a new document under `backends/<tier>/**` selected 243
    nodes and none of them was the manifest; every other corpus directory
    reached it only through the FULL fail-safe.

    The probe paths are DERIVED: the census's own directory list, and for each
    one the directory an existing document actually sits in, so the shape under
    test is `backends/go/scenarios/<new>.rvl` rather than a path no corpus
    document has ever used. A ninth corpus directory shows up here the day the
    census starts walking it.
    """
    census = _census()
    dirs = census.CORPUS_DIRS
    assert dirs, "gate_reference_census.py reported no corpus directories"
    probes = []
    for d in dirs:
        # The directory an existing document sits in, or the corpus root when
        # the directory holds none yet (`tck/` today, which the census walks
        # regardless and which a first document would land in).
        where = d
        for doc in sorted((ROOT / d).rglob("*.rvl")) if (ROOT / d).is_dir() else []:
            if set(census._SKIP_DIRS) & set(doc.parts):
                continue
            where = doc.parent.relative_to(ROOT).as_posix()
            break
        probes.append(f"{where}/zz_selector_probe.rvl")
    assert len(probes) == len(dirs)
    missing = []
    for f in probes:
        r = sel(f)
        if not r["full"] and "tests/test_corpus_provenance.py" not in r["pytest"]:
            missing.append(f)
    assert not missing, (
        "the census walks these directories for scoring documents, but a new "
        "`.rvl` beside an existing one selects neither the FULL gate nor "
        f"tests/test_corpus_provenance.py:\n  {missing}\n"
        "An undeclared document would then reach main green."
    )


def test_a_backend_scenario_document_selects_the_manifest_without_going_full():
    """The arm that was actually open. `backends/**` is a census corpus
    directory AND has its own narrow rule, so `backends/go/scenarios/<new>.rvl`
    selected the go suite and stopped: 243 nodes, none of them the manifest.
    Closing it must not turn a corpus document into a FULL trigger either --
    the manifest test is 0.3s and the go suite is already selected."""
    r = sel("backends/go/scenarios/zz_selector_probe.rvl")
    assert r["full"] is False, "a backend scenario document escalated to FULL"
    assert "tests/test_corpus_provenance.py" in r["pytest"]
    assert set(r["backends"]) == {"go"}, "the backend set widened"


def test_a_bench_document_is_not_a_scoring_corpus_document():
    """`bench/` is in the census's `EXTRA_DIRS`, which `load_corpus` walks only
    under `--everything`, and `corpus_provenance.enumerate_corpora` does not ask
    for it. A bench document is in no scoring corpus, needs no manifest line,
    and must not drag the manifest test into every bench change."""
    census = _census()
    assert "bench" in census.EXTRA_DIRS and "bench" not in census.CORPUS_DIRS
    r = sel("bench/codegen/zz_selector_probe.rvl")
    assert r["full"] is False
    assert "tests/test_corpus_provenance.py" not in r["pytest"]

# --- a test's substance can live outside tests/ (issue #1342) -------------- #
def test_a_wrapper_test_inherits_the_vocabulary_of_what_it_runs():
    """The measured gap. `tests/test_flagship_demo_525.py` is a wrapper around
    `demo/legacy_enterprise/run_demo.py`, and the demo is what shells out to
    `revl audit`. The wrapper never spells `audit`, so the leaf-module word
    heuristic — which read `tests/**` and nothing else — selected 133 tests for
    a `src/revl/audit.py` change and not the one test that runs `revl audit`
    end to end. `main` shipped a demo exiting 1 with every gate green."""
    r = sel("src/revl/audit.py")
    assert r["full"] is False
    assert "tests/test_flagship_demo_525.py" in r["pytest"], (
        "a change to src/revl/audit.py does not select the test that runs "
        "`revl audit` through demo/legacy_enterprise/run_demo.py"
    )


def test_the_derived_rule_does_not_replace_the_hand_written_tables():
    """The honest limit of the derived rule, pinned so nobody deletes a table
    believing this covers it.

    `companion_paths` sees a path a test SPELLS. It does not see one the test
    computes, walks to from a root it holds in a variable, or declares only in
    prose. Of the 18 modules in BENCH_DEPENDENT_TESTS, exactly three spell a
    `bench/` path, so the derived rule finds three of eighteen. It is a
    complement to those tables, not their replacement."""
    cp = at.companion_paths(ROOT)
    spelled = {t for t, paths in cp.items()
               if any(p.startswith("bench/") for p in paths)}
    declared = set(at.BENCH_DEPENDENT_TESTS)
    assert spelled, "no test spells a bench path; the extractor found nothing"
    assert len(spelled) < len(declared), (
        "the derived rule now finds at least as many bench-dependent modules "
        "as the table declares. Re-read the table: it may be replaceable, "
        "which would be worth doing deliberately."
    )


def test_prose_is_not_a_dependency():
    """A path named only in a docstring is a citation, not a read.
    `tests/test_71_codegen_perf_findings.py` cites `bench/codegen/python/run.py`
    in its module docstring and is declared in BENCH_DEPENDENT_TESTS for it;
    the derived rule must not double as a prose scanner, or every document that
    mentions a file would select every test that mentions the document."""
    named = at._named_paths(ROOT, '"""See bench/codegen/python/run.py."""\n')
    assert named == frozenset(), named
    assert "tests/test_71_codegen_perf_findings.py" in at.BENCH_DEPENDENT_TESTS


def test_a_changed_file_selects_every_test_that_names_it():
    """The reverse direction. `demo/legacy_enterprise/` is read by the item-521
    tripwire, which globs the directory for a stale `MEASURED GAP` label — a
    coupling no import graph and no word match can see."""
    hits = at.tests_naming(ROOT, "demo/legacy_enterprise/run_demo.py")
    assert "tests/test_ui_taint_classes_521.py" in hits
    assert "tests/test_flagship_demo_525.py" in hits


def test_the_slow_descent_test_stays_excluded_from_the_derived_rule():
    """Issue #431's exclusion is a cost decision the derived rule must not
    reopen: tests/test_selfhost_lower.py names selfhost/lower.rvl and runs for
    >120s, which is what made the FULL fallback abort under the hook."""
    r = sel("selfhost/lower.rvl")
    assert r["full"] is False
    assert "tests/test_selfhost_lower.py" not in r["pytest"]
    assert "tests/test_selfhost_lower_ir.py" in r["pytest"]


def test_named_paths_needs_a_separator_and_a_real_file():
    """A bare `"src"` or `"demo"` is too coarse to be evidence of a read, and a
    path that does not exist in the tree is prose. Both are dropped, or the
    rule degenerates into selecting the whole suite for every change."""
    named = at._named_paths(
        ROOT, 'P = ROOT / "demo" / "legacy_enterprise" / "run_demo.py"\n')
    assert "demo/legacy_enterprise/run_demo.py" in named
    assert "demo" not in named
    assert at._named_paths(ROOT, 'X = "src"\nY = "no/such/file.py"\n') == frozenset()


def test_a_directory_counts_only_when_the_test_walks_it():
    """Twenty modules name `src/revl`, almost all of them to put it on
    `sys.path`. Reading that as "depends on every file under it" made a
    one-module change select 213 tests against 92. A directory is evidence
    only when the test enumerates one."""
    holds = 'P = ROOT / "demo" / "legacy_enterprise"\n'
    assert at._named_paths(ROOT, holds) == frozenset()
    walks = holds + 'for f in P.glob("*.py"):\n    pass\n'
    assert "demo/legacy_enterprise" in at._named_paths(ROOT, walks)

# --- structural: no arm of the dispatch loop may be unreachable ------------ #
# issue #1315. `select()` dispatches each changed path through a flat list of
# `if` arms, nearly all of which end in `continue` or `return`. That shape lets
# a later arm be shadowed by an earlier one, and it did: a second arm matching
# `tools/gate_reference_census.py` sat below the arm that already matched it, so
# the selection issue #1215 added for it -- a census change runs
# tests/test_oracle_construct_reach.py -- had never once run. Two lanes found it
# by accident on the same day, which is not a detection strategy.
#
# The check is per-arm coverage over the selector's own source: derive witness
# paths from each arm's condition, run the selector on them, and trace which arm
# actually takes them. An arm that its own witness cannot reach is dead code.
# Literal arms are checked per literal, so an arm that names two files and is
# shadowed for one of them reds as loudly as one shadowed for both -- which is
# why the #1215 selection was folded INTO the arm that fires rather than ordered
# ahead of it.
_PROBE = "zzarmprobe"


def _is_f(node) -> bool:
    return isinstance(node, ast.Name) and node.id == "f"


def _members(module, node):
    """`SOME_SET` or `helper(root)` resolved to its strings, else None."""
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = getattr(module, node.func.id, None)
        value = fn(ROOT) if callable(fn) else None
    else:
        return None
    if isinstance(value, (set, frozenset, tuple, list)) and all(
            isinstance(x, str) for x in value):
        return sorted(value)
    return None


def _conjunction(calls):
    """A path satisfying a conjunction of `f.startswith` / `f.endswith` /
    `Path(f).name.startswith` calls, e.g. `tests/test_zzarmprobe.py`."""
    prefixes, name_prefix, suffix = [""], "", ""
    for call in calls:
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            return None
        method, receiver = call.func.attr, call.func.value
        arg = call.args[0] if len(call.args) == 1 else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            values = [arg.value]
        elif isinstance(arg, (ast.Tuple, ast.List)) and arg.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str)
                for e in arg.elts):
            values = [e.value for e in arg.elts]
        else:
            return None
        if _is_f(receiver) and method == "startswith":
            prefixes = values
        elif _is_f(receiver) and method == "endswith":
            suffix = values[0]
        elif method == "startswith" and ast.unparse(receiver) == "Path(f).name":
            name_prefix = values[0]
        else:
            return None
    return [p + name_prefix + _PROBE + suffix for p in prefixes]


#: Dispatch arms whose condition is a NAMED predicate rather than a shape this
#: checker can read off the AST. Each entry says how to build a path the
#: predicate accepts, derived from the module rather than hard-coded, so the
#: witness follows the predicate if its definition moves.
#:
#: This is a TABLE on purpose, exactly like `SELFHOST_TAG_CODES`: a predicate
#: absent from it yields no witness, `_witnesses` returns None, and the arm is
#: reported as unknown-reachability. Guessing a witness for an unrecognised
#: predicate would be the fail-open direction - it would certify an arm nobody
#: had shown to be reachable.
def _witness_scoring_corpus_document(module):
    """`_is_scoring_corpus_document(f, root)`: an `.rvl` the census walks."""
    dirs = module.census_corpus_dirs(ROOT)
    if not dirs:
        return None
    return [f"{sorted(dirs)[0].rstrip('/')}/zz_armprobe{_PROBE_RVL}"]


def _witness_named_by_a_test(module):
    """`named_by = tests_naming(root, f)` then `if named_by:` (issue #1342).

    The arm fires for any repository path some test names in its own source.
    The witness is ASKED of the selector rather than invented: walk the tracked
    tools and source files and return the first the selector itself says a test
    names. If nothing in the tree is named by any test, the rule is genuinely
    unreachable and this returns None, which reds - the right answer.
    """
    for rel in _tracked_candidates():
        try:
            if module.tests_naming(ROOT, rel):
                return [rel]
        except Exception:
            return None
    return None


def _tracked_candidates():
    """Tracked `tools/*.py` and `src/revl/*.py`, in a stable order."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "tools/*.py", "src/revl/*.py"],
        capture_output=True, text=True)
    return [line for line in out.stdout.split("\n") if line.strip()]


_PREDICATE_WITNESSES = {
    "_is_scoring_corpus_document": _witness_scoring_corpus_document,
}

#: Arms whose condition is a plain NAME bound just above from a call this
#: checker can attribute. Same table discipline as `_PREDICATE_WITNESSES`: a
#: name absent from it stays unknown rather than being guessed reachable.
_BOUND_NAME_WITNESSES = {
    "named_by": _witness_named_by_a_test,
}

_PROBE_RVL = ".rvl"


def _named_predicate_witnesses(module, cond):
    """Witnesses for `somepredicate(f, ...)`, or None if it is not in the
    table. A predicate this checker does not recognise stays unknown."""
    if not isinstance(cond, ast.Call) or not isinstance(cond.func, ast.Name):
        return None
    build = _PREDICATE_WITNESSES.get(cond.func.id)
    if build is None:
        return None
    if not cond.args or not _is_f(cond.args[0]):
        return None
    return build(module)


def _witnesses(module, cond):
    """Every path an arm's condition says it wants, or None when this checker
    cannot read the condition's shape."""
    if isinstance(cond, ast.BoolOp) and isinstance(cond.op, ast.Or):
        out = []
        for value in cond.values:
            part = _witnesses(module, value)
            if part is None:
                return None
            out.extend(part)
        return out
    if isinstance(cond, ast.BoolOp) and isinstance(cond.op, ast.And):
        return _conjunction(cond.values)
    if isinstance(cond, ast.Name):
        build = _BOUND_NAME_WITNESSES.get(cond.id)
        return build(module) if build is not None else None
    if isinstance(cond, ast.Call):
        named = _named_predicate_witnesses(module, cond)
        if named is not None:
            return named
        return _conjunction([cond])
    if isinstance(cond, ast.Compare) and _is_f(cond.left) and len(cond.ops) == 1:
        op, rhs = cond.ops[0], cond.comparators[0]
        if isinstance(op, ast.Eq) and isinstance(rhs, ast.Constant):
            return [rhs.value]
        if isinstance(op, ast.In):
            if isinstance(rhs, (ast.Tuple, ast.List, ast.Set)):
                if all(isinstance(e, ast.Constant) for e in rhs.elts):
                    return [e.value for e in rhs.elts]
                return None
            return _members(module, rhs)
    return None


def _dispatch_arms(module, source: str):
    """One record per arm of `select()`'s `for f in changed:` loop.

    `top` marks an arm of the dispatch itself. Arms nested inside one are
    collected too, but their condition has to be about `f` to be checkable: a
    nested `if oracle:` is a detail of an arm that already fired, while a nested
    `if f == ...` is a selection rule and shadows exactly like a flat one.
    """
    tree = ast.parse(source)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "select")
    loop = next(n for n in ast.walk(fn)
                if isinstance(n, ast.For) and getattr(n.target, "id", "") == "f")
    arms = []
    for node in loop.body:
        if not isinstance(node, ast.If):
            continue
        nested = [n for stmt in node.body for n in ast.walk(stmt)
                  if isinstance(n, ast.If)]
        for arm, top in [(node, True)] + [(n, False) for n in nested]:
            lines = {n.lineno for stmt in arm.body for n in ast.walk(stmt)
                     if hasattr(n, "lineno")}
            arms.append({
                "src": ast.unparse(arm.test),
                "line": arm.lineno,
                "lines": lines,
                "witnesses": _witnesses(module, arm.test),
                "top": top,
                "has_else": bool(arm.orelse),
            })
    return arms


def _lines_taken(module, path: str) -> set[int]:
    """Line numbers executed inside `select` for a one-file change list."""
    taken: set[int] = set()
    code = module.select.__code__

    def trace_lines(frame, event, arg):
        if event == "line":
            taken.add(frame.f_lineno)
        return trace_lines

    def trace_calls(frame, event, arg):
        return trace_lines if frame.f_code is code else None

    previous = sys.gettrace()
    sys.settrace(trace_calls)
    try:
        module.select([path], ROOT)
    finally:
        sys.settrace(previous)
    return taken


def unreachable_arms(module, source: str) -> list[str]:
    """`"<line>: <condition> (<witness>)"` for every arm no witness reaches."""
    dead = []
    for arm in _dispatch_arms(module, source):
        witnesses = arm["witnesses"]
        if witnesses is None:
            continue
        for witness in witnesses:
            if not (_lines_taken(module, witness) & arm["lines"]):
                dead.append(f"{arm['line']}: {arm['src']} ({witness!r})")
    return dead


def test_no_dispatch_arm_of_the_selector_is_unreachable():
    source = (ROOT / "tools" / "affected_tests.py").read_text(encoding="utf-8")
    arms = _dispatch_arms(at, source)
    assert len(arms) > 20, "the dispatch loop did not parse; check _dispatch_arms"

    for arm in arms:
        assert not (arm["top"] and arm["has_else"]), (
            f"line {arm['line']}: the dispatch arm `{arm['src']}` grew an else "
            "branch. This checker models a flat if/continue dispatch; teach it "
            "the new shape before trusting it again."
        )
        if arm["witnesses"] is None:
            assert not arm["top"], (
                f"line {arm['line']}: no witness path can be derived for the "
                f"dispatch arm `{arm['src']}`, so its reachability is unknown. "
                "Extend _witnesses() to read this condition's shape."
            )
            continue
        assert arm["witnesses"], (
            f"line {arm['line']}: the arm `{arm['src']}` matches nothing at "
            "all: its collection is empty, so the rule is dead on arrival."
        )

    dead = unreachable_arms(at, source)
    assert not dead, (
        "unreachable selector arm(s). A path the rule names never reaches it, "
        "because an arm above matches the same path and ends in `continue` or "
        "`return`, so the selection below has no effect and the tests it names "
        "run only in a FULL sweep. Fold the selection into the arm that fires. "
        "Do not simply move this arm up: that shadows the arm above for the "
        "same path, which is the same defect with a smaller blast radius.\n  "
        + "\n  ".join(dead)
    )


def test_the_unreachable_arm_check_reds_on_a_shadowed_arm(tmp_path):
    """The control. Shadow one arm in a copy of the selector and the checker has
    to name it, with its condition and the witness that no longer reaches it."""
    source = (ROOT / "tools" / "affected_tests.py").read_text(encoding="utf-8")
    anchor = '        if f == "tools/check_site_wheel.py":'
    assert source.count(anchor) == 1, "the anchor arm moved; re-point the control"
    shadowed = source.replace(anchor, (
        '        if f.startswith("tools/check_"):\n'
        '            reasons.append(f)\n'
        '            continue\n' + anchor), 1)

    copy = tmp_path / "affected_tests_shadowed.py"
    copy.write_text(shadowed, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("at_shadowed", copy)
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)

    dead = unreachable_arms(mutant, shadowed)
    assert any("tools/check_site_wheel.py" in d for d in dead), (
        "the checker did not notice a deliberately shadowed arm; it cannot be "
        f"trusted to notice the next real one. Reported: {dead}")


def test_a_candidate_the_filesystem_cannot_answer_for_is_not_an_exception():
    """The defect this shipped with (issue #1342, repaired here).

    A prose string containing a slash reaches `_named_paths` as ONE candidate,
    and handing a 1.6 KB paragraph to `stat()` raises ENAMETOOLONG on Linux.
    `Path.exists()` propagates that on python 3.12 and swallows it on 3.13+,
    where pathlib was rewritten to catch OSError broadly -- so this passed on a
    3.14 developer machine and raised in CI on 3.12, taking out all 40 tests in
    this module plus four others, every one of them a caller of `select()`.

    A selector decides whether a test runs. It may return the wrong answer and
    be caught by a test; it may not RAISE, because then no gate that calls it
    reports anything at all.
    """
    prose = '"""' + ("see bench/codegen/python/run.py " * 60) + '"""\nX = 1\n'
    assert at._named_paths(ROOT, prose) == frozenset()

    over_long = 'P = "' + "a" * 400 + '/b.py"\n'
    assert at._named_paths(ROOT, over_long) == frozenset()

    embedded_newline = 'P = "one/\\ntwo"\n'
    assert at._named_paths(ROOT, embedded_newline) == frozenset()

    # and the real shape: every test file in the tree, through the real walk.
    # This is what actually raised, so it is what has to be exercised.
    assert at.companion_paths(ROOT), "the walk found nothing; the probe is vacuous"


def test_select_never_raises_on_the_real_tree():
    """The non-vacuity anchor for the test above. `companion_paths` walks 623
    modules; the failure was in one of them, not in a synthetic string."""
    for changed in ("src/revl/parser.py", "src/revl/audit.py", "docs/arithmetic.md",
                    "backends/go/emit.py", "weird/random_thing.xyz"):
        r = sel(changed)
        assert isinstance(r["pytest"], list)
