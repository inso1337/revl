"""The progress half of the self-evolution reward (issue #1224, item 545).

Three things are under test and they are deliberately separated.

  * THE FOUR-WAY DIRECTION RULE, on its own, because every fail-open shape this
    repository has measured lived in a default. `unreadable` must not read as
    `unchanged`, and a value that fell because the measured surface was deleted
    must not read as `improved`.

  * THE COUNTERS END TO END, on real git repositories built in `tmp_path`, so
    each one is shown FAILING on a tree where it regressed, PASSING on a tree
    where it improved, NOT ADVANCING on an empty diff, and flat on a control
    that is identical on both sides. A progress term that has not been shown
    failing is the twelfth check that cannot fail.

  * THE COMPOSITION, because the point of the item is that progress must not
    become a scalar that trades against the conservation components. A candidate
    that advanced a counter and was NOT retained must not promote a generation.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "evolution_progress.py"


@pytest.fixture(scope="module")
def progress():
    import importlib.util

    spec = importlib.util.spec_from_file_location("evolution_progress", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- a tiny tree

COVERAGE_STUB = '''\
"""A stand-in for tools/selfhost_coverage.py with the two names the reach
counter uses: a tier table and a reference-construct reader over an emitter."""
import ast

TIERS = {"py": ("python", "emit_py", "emit_py_corpus")}


def reference_constructs(path):
    found = {}
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \\
                and node.left.id == "kind":
            for value in node.comparators:
                if isinstance(value, ast.Constant):
                    found.setdefault("kind=" + value.value, 1)
    return found
'''

CENSUS_STUB = '''\
"""A stand-in for tools/gate_reference_census.py: the two literals the
census counter reads out of it so the corpus walk cannot drift from the tool."""
CORPUS_DIRS = ("examples",)
_SKIP_DIRS = frozenset({"__pycache__", ".venv"})
'''


def _emit(arms):
    body = ["def emit(node):", "    kind = node.get('kind')"]
    for arm in arms:
        body.append(f"    if kind == {arm!r}:")
        body.append(f"        return {arm!r}")
    body.append("    return ''")
    return "\n".join(body) + "\n"


def _compile_test(residual, corpus):
    return (
        "LOWER_GAP_DOCS = {\n"
        "    \"py\": (" + ", ".join(repr(n) for n in residual) + ",),\n"
        "}\n"
        "PY_FUNCTION_DOCS = [" + ", ".join(repr(n) for n in corpus) + "]\n")


def _write(tree: Path, rel: str, text: str) -> None:
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(tree: Path, *args):
    proc = subprocess.run(["git", "-C", str(tree)] + list(args),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def build_tree(tree: Path, *, allowance=("a.rvl", "b.rvl"),
               documents=("a", "b", "c", "d", "e"),
               residual=("x.rvl", "y.rvl"), corpus=("p.rvl", "q.rvl", "r.rvl"),
               gaps=("kind=a", "kind=b"),
               arms=("a", "b", "c", "d")) -> None:
    """Every artifact the three counters read, with the sizes under test.

    The defaults give census 2/5, native-chain 2/5 and reach 2/4, so each
    counter has room to move in both directions and each can serve as the
    unchanged control while another moves.
    """
    tree.mkdir(parents=True, exist_ok=True)
    # Rebuilt, not patched: a mutation that shrinks the corpus has to actually
    # remove the documents from the tree, or the deletion tests measure nothing.
    for stale in (tree / "examples").glob("*.rvl"):
        stale.unlink()
    _write(tree, "tools/gate_reference_census.py", CENSUS_STUB)
    _write(tree, "tools/gate_reference_census_baseline.json", json.dumps(
        {"buckets": {"false-admit/T1": list(allowance)},
         "corpus_dirs": ["examples"]}))
    for name in documents:
        _write(tree, f"examples/{name}.rvl", f"fn {name}() -> Int = 1\n")
    _write(tree, "tests/test_selfhost_compile.py", _compile_test(residual, corpus))
    _write(tree, "tests/fixtures/oracle_construct_reach_ledger.json",
           json.dumps({"_about": ["stub"], "emit_py": list(gaps)}))
    _write(tree, "tools/selfhost_coverage.py", COVERAGE_STUB)
    _write(tree, "backends/python/emit.py", _emit(arms))


@pytest.fixture
def tiny(tmp_path):
    """A git repository at `base`, ready to be mutated into a candidate."""
    tree = tmp_path / "tiny"
    build_tree(tree)
    _git(tree, "init", "-q")
    _git(tree, "add", "-A")
    _git(tree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return tree


def directions(progress, tree):
    return {d.counter: d.direction for d in progress.measure(tree, "HEAD")}


# ------------------------------------------------- the four-way rule, alone

def test_the_direction_rule_has_exactly_four_answers(progress):
    assert {progress.IMPROVED, progress.UNCHANGED, progress.REGRESSED,
            progress.UNREADABLE} == {"improved", "unchanged", "regressed",
                                     "unreadable"}
    assert progress.NON_REGRESSING == {"improved", "unchanged"}


def test_an_unreadable_side_is_not_an_unchanged_counter(progress):
    readable = progress.Reading("c", 5, 10, "")
    gone = progress.unreadable("c", "the artifact is not in the tree")
    assert progress.direction_of(gone, readable).direction == progress.UNREADABLE
    assert progress.direction_of(readable, gone).direction == progress.UNREADABLE
    assert progress.direction_of(gone, gone).direction == progress.UNREADABLE


def test_a_shrunk_universe_is_a_regression_even_when_the_value_fell(progress):
    """The whole point of the pair. Deleting the measured surface lowers the
    value, and a counter that called that an improvement would pay for it."""
    base = progress.Reading("c", 9, 557, "")
    head = progress.Reading("c", 1, 300, "")
    delta = progress.direction_of(base, head)
    assert delta.direction == progress.REGRESSED
    assert "SHRANK" in delta.detail


def test_a_grown_universe_with_an_unmoved_value_is_unchanged_not_improved(progress):
    base = progress.Reading("c", 9, 557, "")
    head = progress.Reading("c", 9, 600, "")
    assert progress.direction_of(base, head).direction == progress.UNCHANGED


def test_a_risen_value_is_a_regression(progress):
    base = progress.Reading("c", 9, 557, "")
    head = progress.Reading("c", 10, 557, "")
    assert progress.direction_of(base, head).direction == progress.REGRESSED


def test_a_counter_missing_from_a_side_is_unreadable_not_dropped(progress):
    deltas = progress.compare({}, {}, counters={"c": None})
    assert [d.direction for d in deltas] == [progress.UNREADABLE]


def test_a_raising_counter_is_unreadable_rather_than_an_abort(progress, tmp_path):
    def boom(view, scratch):
        raise RuntimeError("no")

    readings = progress.read_counters(
        progress.WorkingTreeView(tmp_path), tmp_path, counters={"c": boom})
    assert readings["c"].readable is False
    assert "RuntimeError" in readings["c"].detail


def test_a_counter_answering_for_another_counter_is_unreadable(progress, tmp_path):
    def liar(view, scratch):
        return progress.Reading("other", 0, 1, "")

    readings = progress.read_counters(
        progress.WorkingTreeView(tmp_path), tmp_path, counters={"c": liar})
    assert readings["c"].readable is False


# ------------------------------------------------- the component and advance

def _deltas(progress, *pairs):
    out = []
    for counter, direction in pairs:
        reading = progress.Reading(counter, 1, 2, "")
        out.append(progress.Delta(counter, direction, reading, reading, ""))
    return out


def test_the_component_verifies_only_when_nothing_regressed(progress):
    assert progress.progress_verdict(
        _deltas(progress, ("a", "improved"), ("b", "unchanged"))).verified
    assert not progress.progress_verdict(
        _deltas(progress, ("a", "improved"), ("b", "regressed"))).verified
    assert not progress.progress_verdict(
        _deltas(progress, ("a", "improved"), ("b", "unreadable"))).verified


def test_the_component_does_not_require_an_improvement(progress):
    """Retention is non-regression. Requiring every candidate to move a counter
    would make a correct refactor unretainable and make the cheapest counter the
    target, which is why advancement lives at the generation level instead."""
    verdict = progress.progress_verdict(
        _deltas(progress, ("a", "unchanged"), ("b", "unchanged")))
    assert verdict.verified
    assert progress.advanced(
        _deltas(progress, ("a", "unchanged"), ("b", "unchanged"))) is False


def test_advance_requires_an_improvement_and_no_regression(progress):
    assert progress.advanced(_deltas(progress, ("a", "improved"))) is True
    assert progress.advanced(
        _deltas(progress, ("a", "improved"), ("b", "regressed"))) is False
    assert progress.advanced(
        _deltas(progress, ("a", "improved"), ("b", "unreadable"))) is False
    assert progress.advanced([]) is False


def test_the_component_speaks_the_vocabulary_of_the_conservation_half(progress):
    """`tools/evolution_reward.py` folds components with `all()` over objects
    carrying exactly these fields. The ninth conjunct has to be registrable in
    that table without adaptation, or the composition is prose."""
    verdict = progress.progress_verdict(_deltas(progress, ("a", "unchanged")))
    assert verdict.component == "progress"
    assert set(verdict.as_dict()) == {"component", "verdict", "reason", "evidence"}
    assert verdict.as_dict()["verdict"] == "verified"
    failing = progress.progress_verdict(_deltas(progress, ("a", "regressed")))
    assert failing.as_dict()["verdict"] == "failed"


def test_no_scalar_progress_score_is_exported(progress):
    """The conservation half exports no top-level number and asserts it. A
    progress term that exported one would hand back the trade that decision
    refused."""
    public = {name: getattr(progress, name) for name in dir(progress)
              if not name.startswith("_")}
    numbers = {name for name, value in public.items()
               if isinstance(value, (int, float)) and not isinstance(value, bool)}
    assert not numbers, f"a scalar leaked into the module surface: {numbers}"
    assert not hasattr(progress, "score")


# ------------------------------------------------------------- the counters

def test_every_counter_is_flat_on_a_control_tree(progress, tiny):
    """An empty diff: the component verifies, and NOTHING advanced. That is the
    generation-of-empty-diffs case the item asks to be recorded as no advance."""
    deltas = progress.measure(tiny, "HEAD")
    assert {d.direction for d in deltas} == {progress.UNCHANGED}
    assert progress.progress_verdict(deltas).verified
    assert progress.advanced(deltas) is False


@pytest.mark.parametrize("counter,mutate,control", [
    ("census-allowance",
     lambda t: build_tree(t, allowance=("a.rvl",)),
     ("native-chain-residual", "reach-gaps")),
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",), corpus=("p.rvl", "q.rvl",
                                                          "r.rvl", "y.rvl")),
     ("census-allowance", "reach-gaps")),
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",)),
     ("census-allowance", "native-chain-residual")),
])
def test_a_counter_that_closed_a_gap_improves_and_the_others_stay_flat(
        progress, tiny, counter, mutate, control):
    mutate(tiny)
    seen = directions(progress, tiny)
    assert seen[counter] == progress.IMPROVED
    for other in control:
        assert seen[other] == progress.UNCHANGED
    deltas = progress.measure(tiny, "HEAD")
    assert progress.progress_verdict(deltas).verified
    assert progress.advanced(deltas) is True
    assert progress.improvements(deltas) == (counter,)


@pytest.mark.parametrize("counter,mutate", [
    ("census-allowance",
     lambda t: build_tree(t, allowance=("a.rvl", "b.rvl", "c.rvl"))),
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl", "y.rvl", "z.rvl"))),
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a", "kind=b", "kind=c"))),
])
def test_a_counter_that_grew_regresses_and_fails_the_component(
        progress, tiny, counter, mutate):
    mutate(tiny)
    deltas = progress.measure(tiny, "HEAD")
    assert directions(progress, tiny)[counter] == progress.REGRESSED
    verdict = progress.progress_verdict(deltas)
    assert not verdict.verified
    assert counter in verdict.reason
    assert progress.advanced(deltas) is False


@pytest.mark.parametrize("counter,mutate", [
    # The allowance falls from 2 to 1 because the divergent document was
    # DELETED from the corpus, not because it stopped diverging.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("a.rvl",),
                          documents=("a", "b", "c", "d"))),
    # The residual falls from 2 to 1 because the document left the measured
    # corpus entirely.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",), corpus=("p.rvl", "q.rvl"))),
    # The ledger shrinks from 2 to 1 because the unreached dispatch arm was
    # deleted. Nothing reaches an unreached arm, so no golden changes and no
    # conservation component fires: the universe is the only thing that sees it.
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",), arms=("a", "b", "c"))),
])
def test_deleting_the_measured_surface_regresses_rather_than_improves(
        progress, tiny, counter, mutate):
    mutate(tiny)
    delta = {d.counter: d for d in progress.measure(tiny, "HEAD")}[counter]
    assert delta.direction == progress.REGRESSED
    assert delta.head.value < delta.base.value, (
        "the gaming path under test must actually lower the value, or this "
        "test proves nothing")
    assert delta.head.universe < delta.base.universe
    assert not progress.progress_verdict(progress.measure(tiny, "HEAD")).verified


@pytest.mark.parametrize("artifact", [
    "tools/gate_reference_census_baseline.json",
    "tests/test_selfhost_compile.py",
    "tests/fixtures/oracle_construct_reach_ledger.json",
])
def test_a_deleted_artifact_is_unreadable_and_fails_the_component(
        progress, tiny, artifact):
    (tiny / artifact).unlink()
    deltas = progress.measure(tiny, "HEAD")
    assert progress.UNREADABLE in {d.direction for d in deltas}
    assert not progress.progress_verdict(deltas).verified


def test_an_emptied_residual_table_is_absent_not_zero(progress, tiny):
    """A candidate that deletes the `LOWER_GAP_DOCS` mapping outright has not
    reached a residual of zero, and reading it as zero would be the single
    cheapest false advance available."""
    (tiny / "tests/test_selfhost_compile.py").write_text("PY_FUNCTION_DOCS = []\n")
    delta = {d.counter: d for d in progress.measure(tiny, "HEAD")}
    assert delta["native-chain-residual"].direction == progress.UNREADABLE


def test_an_emptied_reach_ledger_is_absent_not_zero(progress, tiny):
    (tiny / "tests/fixtures/oracle_construct_reach_ledger.json").write_text(
        json.dumps({"_about": ["stub"]}))
    delta = {d.counter: d for d in progress.measure(tiny, "HEAD")}
    assert delta["reach-gaps"].direction == progress.UNREADABLE


def test_a_base_ref_that_does_not_resolve_fails_rather_than_passing(
        progress, tiny):
    deltas = progress.measure(tiny, "no-such-ref")
    assert {d.direction for d in deltas} == {progress.UNREADABLE}
    assert not progress.progress_verdict(deltas).verified


# ------------------------------------------------------- generation promotion

def _entry(name, retained, directions_):
    return {
        "candidate": name,
        "retained": retained,
        "blockers": [] if retained else ["tests"],
        "progress": {"deltas": [{"counter": f"c{i}", "direction": d}
                                for i, d in enumerate(directions_)]},
    }


def test_an_empty_generation_is_not_promoted(progress):
    result = progress.promote([])
    assert result.promoted is False
    assert "empty" in result.reason


def test_a_generation_of_empty_diffs_is_recorded_as_no_advance(progress):
    """The exact failure mode of item 545: every candidate is perfectly
    conservative, every one is retained, and the generation did not advance."""
    result = progress.promote(
        [_entry(f"c{i}", True, ["unchanged", "unchanged"]) for i in range(10)])
    assert result.promoted is False
    assert result.retained == 10
    assert "did not advance" in result.reason


def test_one_retained_advance_promotes_the_generation(progress):
    result = progress.promote([
        _entry("a", True, ["unchanged", "unchanged"]),
        _entry("b", True, ["improved", "unchanged"]),
    ])
    assert result.promoted is True
    assert result.witnesses == ("b",)


def test_an_unretained_advance_cannot_witness_a_promotion(progress):
    """The anti-trade proof. A candidate that moved a counter but failed a
    conservation component is not eligible, so progress can never buy back a
    failed component -- which is what a scalar would have permitted."""
    result = progress.promote([_entry("a", False, ["improved", "improved"])])
    assert result.promoted is False
    assert result.retained == 0
    assert "eligible" in result.reason


def test_a_retained_candidate_that_also_regressed_cannot_witness(progress):
    result = progress.promote([_entry("a", True, ["improved", "regressed"])])
    assert result.promoted is False


def test_an_unreadable_counter_cannot_witness_a_promotion(progress):
    result = progress.promote([_entry("a", True, ["improved", "unreadable"])])
    assert result.promoted is False


def test_retention_must_be_the_literal_true(progress):
    for claimed in ("true", 1, "yes", [1], None):
        entry = _entry("a", True, ["improved"])
        entry["retained"] = claimed
        assert progress.promote([entry]).promoted is False


def test_a_candidate_cannot_declare_its_own_advance(progress):
    """The promotion rule reads the recorded counter directions, never a
    boolean the producer wrote. Item 536's first requirement is that the reward
    be a repository fact rather than the model's account of its own work."""
    entry = _entry("a", True, ["unchanged"])
    entry["advanced"] = True
    entry["progress"]["advanced"] = True
    entry["reason"] = "I improved the census allowance substantially"
    assert progress.promote([entry]).promoted is False


def test_a_missing_progress_block_is_not_an_advance(progress):
    entry = {"candidate": "a", "retained": True}
    assert progress.promote([entry]).promoted is False


def test_the_serialised_rule_and_the_object_rule_agree(progress):
    """`advanced` runs over `Delta` objects and the promotion rule runs over the
    serialised scorecard. Two implementations of one rule drift, so they are
    pinned against each other over every combination of three directions."""
    names = ("improved", "unchanged", "regressed", "unreadable")
    for first in names:
        for second in names:
            deltas = _deltas(progress, ("a", first), ("b", second))
            entry = _entry("x", True, [first, second])
            assert progress.advanced(deltas) is progress.promote([entry]).promoted


# ------------------------------------------------------------ the real tree

def test_the_counters_read_this_repository(progress, tmp_path):
    """Non-vacuity on the tree that matters. Every counter must resolve to a
    real pair here, with a value inside a strictly larger universe: a counter
    that silently answered 0 over 0 would satisfy every rule above and measure
    nothing."""
    readings = progress.read_counters(progress.WorkingTreeView(ROOT), tmp_path)
    assert set(readings) == set(progress.COUNTERS)
    for name, reading in readings.items():
        assert reading.readable, f"{name} is unreadable on this tree: {reading.detail}"
        assert reading.universe > 0, name
        assert 0 <= reading.value < reading.universe, name


def test_the_census_counter_is_the_baseline_file_total(progress, tmp_path):
    """Recomputed here from the artifact itself, so the counter cannot drift
    into measuring something adjacent to the gate's own allowance."""
    baseline = json.loads(
        (ROOT / "tools/gate_reference_census_baseline.json").read_text())
    expected = sum(len(ids) for ids in baseline["buckets"].values())
    reading = progress.census_allowance(progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.value == expected


def test_the_native_chain_counter_is_the_lower_gap_table_total(progress, tmp_path):
    tree = ast.parse((ROOT / "tests/test_selfhost_compile.py").read_text())
    expected = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = (node.targets[0] if isinstance(node, ast.Assign)
                      else node.target)
            if isinstance(target, ast.Name) and target.id == "LOWER_GAP_DOCS":
                expected = sum(len(v.elts) for v in node.value.values)
    assert expected is not None
    reading = progress.native_chain_residual(
        progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.value == expected


def test_the_reach_counter_is_the_emitter_half_of_the_ledger(progress, tmp_path):
    ledger = json.loads(
        (ROOT / "tests/fixtures/oracle_construct_reach_ledger.json").read_text())
    expected = sum(len(ledger[o]) for o in progress.REACH_ORACLES if o in ledger)
    reading = progress.reach_gaps(progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.value == expected
    assert reading.universe > reading.value


# -------------------------------------------------------------------- the cli

def test_the_cli_reports_no_advance_on_an_unchanged_tree(tiny):
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "did not advance" in proc.stdout


def test_the_cli_exits_nonzero_when_a_counter_regressed(tiny):
    build_tree(tiny, allowance=("a.rvl", "b.rvl", "c.rvl"))
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD"],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "census-allowance" in proc.stdout


def test_the_cli_writes_a_scorecard_block_the_promotion_rule_reads(
        tiny, tmp_path, progress):
    build_tree(tiny, allowance=("a.rvl",))
    out = tmp_path / "progress.json"
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD",
         "--json", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    block = json.loads(out.read_text())
    entry = {"candidate": "c", "retained": True, "progress": block["progress"]}
    assert progress.promote([entry]).promoted is True


def test_the_generation_cli_exits_nonzero_when_nothing_advanced(tmp_path):
    generation = tmp_path / "generation.json"
    generation.write_text(json.dumps([
        {"candidate": "a", "retained": True,
         "progress": {"deltas": [{"counter": "c", "direction": "unchanged"}]}}]))
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--generation", str(generation)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "DO NOT PROMOTE" in proc.stdout
