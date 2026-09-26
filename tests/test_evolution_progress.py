"""The progress half of the self-evolution reward (issue #1224, item 545).

Four properties are under test, each shown from both sides: an input that
satisfies it and one that fails it.

  1. THE EMPTY DIFF IS NOT RETAINED. It preserves everything and improves
     nothing, so the `progress` component fails on it. So does a diff that is
     not empty but moves no counter.
  2. A REGRESSION STILL FAILS, even beside an improvement. Progress is added to
     preservation, never traded against it.
  3. PROGRESS CANNOT BE BOUGHT CHEAPLY. For each counter, the trivial ways to
     move it (delete, substitute, rename, gut the document in place, move the
     instrument, repoint the corpus, pad with a no-op document or an empty
     test) are each shown NOT to register as an improvement. The one thing no
     counter can see alone, a table edited to a lie, is shown too, with the
     ratchet that refuses it named.
  4. PROGRESS IS JUDGED AGAINST THE CANDIDATE'S OWN BASE, the merge base of its
     `HEAD` and the trunk ref, not the trunk's tip at scoring time.

Everything runs on real git repositories built in `tmp_path`, because every
rule here is a rule about a diff, and a mocked diff would test the mock.
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

CENSUS_STUB = '''\
"""A stand-in for tools/gate_reference_census.py: the two literals the
census counter reads out of it so the corpus walk cannot drift from the tool."""
CORPUS_DIRS = ("examples",)
_SKIP_DIRS = frozenset({"__pycache__", ".venv"})
'''

RATCHET_BODY = '''

def test_the_residual_is_located_in_lower_not_in_the_emitter():
    assert recompute() == LOWER_GAP_DOCS
'''

EMIT_TEST_BODY = '''

def test_every_document_is_byte_exact():
    assert all(CORPUS)
'''


def _emit(arms):
    body = ["def emit(node):", "    kind = node.get('kind')"]
    for arm in arms:
        body.append(f"    if kind == {arm!r}:")
        body.append(f"        return {arm!r}")
    body.append("    return ''")
    return "\n".join(body) + "\n"


def _write(tree: Path, rel: str, text: str) -> None:
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(tree: Path, *args):
    proc = subprocess.run(["git", "-C", str(tree)] + list(args),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _commit(tree: Path, message: str) -> str:
    _git(tree, "add", "-A")
    _git(tree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
         message)
    return _git(tree, "rev-parse", "HEAD").strip()


def build_tree(tree: Path, *,
               allowance=("examples/a.rvl", "examples/b.rvl"),
               documents=("a", "b", "c", "d", "e"),
               residual=("x.rvl", "y.rvl"),
               corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl", "y.rvl"),
               corpus_dir="emit_py_corpus",
               gaps=("kind=a", "kind=b"),
               arms=("a", "b", "c", "d"),
               ratchet=RATCHET_BODY) -> None:
    """Every artifact the three counters read, and every instrument they guard.

    The defaults give census 2 over 5, native-chain 2 over 5 and reach 2 over
    4, so each counter has room to move in both directions and each can serve
    as the unchanged control while another moves. Rebuilt rather than patched:
    a mutation that shrinks a corpus has to actually remove documents from the
    tree, or the deletion tests measure nothing.
    """
    tree.mkdir(parents=True, exist_ok=True)
    for stale in list((tree / "examples").glob("*.rvl")) + list(
            (tree / "tests/fixtures").glob("*/*.rvl")):
        stale.unlink()
    _write(tree, "tools/gate_reference_census.py", CENSUS_STUB)
    _write(tree, "tools/gate_reference_census_baseline.json", json.dumps(
        {"buckets": {"false-admit/T1": list(allowance)},
         "corpus_dirs": ["examples"]}))
    for name in documents:
        _write(tree, f"examples/{name}.rvl", f"fn {name}() -> Int = 1\n")

    _write(tree, "tests/test_selfhost_compile.py",
           "LOWER_GAP_DOCS = {\n"
           "    \"py\": (" + "".join(f"{n!r}, " for n in residual) + "),\n"
           "}\n" + ratchet)
    _write(tree, "tests/test_selfhost_emit_py.py",
           "from pathlib import Path\n"
           "ROOT = Path(__file__).resolve().parents[1]\n"
           f"CORPUS_DIR = ROOT / \"tests\" / \"fixtures\" / {corpus_dir!r}\n"
           "CORPUS = [" + ", ".join(repr(n) for n in corpus) + "]\n"
           + EMIT_TEST_BODY)
    for name in sorted(set(corpus) | {"p.rvl", "q.rvl", "r.rvl", "x.rvl",
                                      "y.rvl"}):
        _write(tree, f"tests/fixtures/emit_py_corpus/{name}",
               f"// {name}: a document the native chain is measured on\n")

    _write(tree, "tests/fixtures/oracle_construct_reach_ledger.json",
           json.dumps({"_about": ["stub"], "emit_py": list(gaps)}))
    _write(tree, "backends/python/emit.py", _emit(arms))
    # The instruments the credit rule guards. Present so a test can move one.
    _write(tree, "src/revl/checker.py", "def check(program):\n    return []\n")
    _write(tree, "tools/oracle_construct_reach.py", "LEDGER = 'ledger'\n")
    _write(tree, "tools/selfhost_coverage.py", "TIERS = {}\n")


@pytest.fixture
def tiny(tmp_path):
    """A git repository at `base`, ready to be mutated into a candidate."""
    tree = tmp_path / "tiny"
    build_tree(tree)
    _git(tree, "init", "-q", "-b", "main")
    _commit(tree, "base")
    return tree


def by_counter(progress, tree, base="HEAD"):
    return {d.counter: d for d in progress.measure(tree, base)}


def directions(progress, tree, base="HEAD"):
    return {name: d.direction for name, d in by_counter(progress, tree, base).items()}


# ------------------------------------------------- the four-way rule, alone

def _reading(progress, failing, members, subjects=None):
    return progress.reading("c", failing, members, "", (), subjects)


def test_the_direction_rule_has_exactly_four_answers(progress):
    assert {progress.IMPROVED, progress.UNCHANGED, progress.REGRESSED,
            progress.UNREADABLE} == {"improved", "unchanged", "regressed",
                                     "unreadable"}
    assert progress.NON_REGRESSING == {"improved", "unchanged"}


def test_an_unreadable_side_is_not_an_unchanged_counter(progress):
    readable = _reading(progress, {"a"}, {"a", "b"})
    gone = progress.unreadable("c", "the artifact is not in the tree")
    assert progress.direction_of(gone, readable).direction == progress.UNREADABLE
    assert progress.direction_of(readable, gone).direction == progress.UNREADABLE
    assert progress.direction_of(gone, gone).direction == progress.UNREADABLE


def test_a_member_leaving_the_surface_is_a_regression_even_when_failing_fell(
        progress):
    """The whole point of the pair. Deleting the measured surface lowers the
    value, and a counter that called that an improvement would pay for it."""
    base = _reading(progress, {"a", "b"}, {"a", "b", "c"})
    head = _reading(progress, {"a"}, {"a", "c"})
    delta = progress.direction_of(base, head)
    assert delta.direction == progress.REGRESSED
    assert "LEFT the measured surface" in delta.detail


def test_a_substitution_is_a_regression_although_the_counts_improve(progress):
    """What the first slice's COUNT rule got wrong. Drop the failing member,
    add any passing one: the value falls and the universe holds, so counts say
    `improved`. Identity says a member left, which is what happened."""
    base = _reading(progress, {"a", "b"}, {"a", "b", "c"})
    head = _reading(progress, {"a"}, {"a", "c", "filler"})
    assert (head.value, head.universe) == (base.value - 1, base.universe)
    assert progress.direction_of(base, head).direction == progress.REGRESSED


def test_a_grown_surface_with_an_unmoved_failing_set_is_unchanged(progress):
    base = _reading(progress, {"a"}, {"a", "b"})
    head = _reading(progress, {"a"}, {"a", "b", "c"})
    assert progress.direction_of(base, head).direction == progress.UNCHANGED


def test_a_newly_failing_member_is_a_regression(progress):
    base = _reading(progress, {"a"}, {"a", "b"})
    head = _reading(progress, {"a", "b"}, {"a", "b"})
    assert progress.direction_of(base, head).direction == progress.REGRESSED


def test_retiring_two_while_adding_one_is_a_regression_not_net_progress(progress):
    """Counts would net this to one improvement. It is a new failure."""
    base = _reading(progress, {"a", "b"}, {"a", "b", "c"})
    head = _reading(progress, {"c"}, {"a", "b", "c"})
    assert progress.direction_of(base, head).direction == progress.REGRESSED


def test_a_crossing_is_an_improvement_and_names_what_crossed(progress):
    base = _reading(progress, {"a", "b"}, {"a", "b", "c"})
    head = _reading(progress, {"a"}, {"a", "b", "c"})
    delta = progress.direction_of(base, head)
    assert delta.direction == progress.IMPROVED
    assert delta.crossed == ("b",)


def test_a_failing_member_is_always_on_the_surface(progress):
    """A stale table entry naming nothing on the surface cannot be dropped for
    free: it is counted as surface, so dropping it is a member leaving."""
    base = _reading(progress, {"stale"}, set())
    assert "stale" in base.members
    head = _reading(progress, set(), set())
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
        return progress.reading("other", set(), {"a"}, "")

    readings = progress.read_counters(
        progress.WorkingTreeView(tmp_path), tmp_path, counters={"c": liar})
    assert readings["c"].readable is False


# ------------------------------------------ the component: improve, or fail

def _deltas(progress, *pairs):
    out = []
    for counter, direction in pairs:
        reading = progress.reading(counter, {"a"}, {"a", "b"}, "")
        out.append(progress.Delta(counter, direction, reading, reading, ""))
    return out


def test_the_component_requires_an_improvement(progress):
    """Property 1 at the rule level. Unmoved everywhere is the empty diff's
    signature, and it fails; one improvement with nothing broken passes."""
    flat = progress.progress_verdict(
        _deltas(progress, ("a", "unchanged"), ("b", "unchanged")))
    assert flat.verified is False
    assert "did not advance" in flat.reason
    moved = progress.progress_verdict(
        _deltas(progress, ("a", "improved"), ("b", "unchanged")))
    assert moved.verified is True


def test_an_improvement_does_not_buy_back_a_regression(progress):
    """Property 2 at the rule level: nothing is netted."""
    for bad in ("regressed", "unreadable"):
        verdict = progress.progress_verdict(
            _deltas(progress, ("a", "improved"), ("b", bad)))
        assert verdict.verified is False
        assert bad in verdict.reason


def test_no_counters_is_not_a_pass(progress):
    assert progress.progress_verdict([]).verified is False


def test_the_component_and_advance_are_the_same_rule(progress):
    """`advanced` survives as the name `promote` restates over the serialised
    ledger. Since issue #1224 it IS the component's rule; pinned so the two
    cannot drift."""
    names = ("improved", "unchanged", "regressed", "unreadable")
    for first in names:
        for second in names:
            deltas = _deltas(progress, ("a", first), ("b", second))
            assert progress.advanced(deltas) is \
                progress.progress_verdict(deltas).verified


def test_the_component_speaks_the_vocabulary_of_the_conservation_half(progress):
    """`tools/evolution_reward.py` folds components with `all()` over objects
    carrying exactly these fields. The ledger travels beside them, not in them."""
    verdict = progress.progress_verdict(_deltas(progress, ("a", "improved")))
    assert verdict.component == "progress"
    assert set(verdict.as_dict()) == {"component", "verdict", "reason", "evidence"}
    assert verdict.as_dict()["verdict"] == "verified"
    failing = progress.progress_verdict(_deltas(progress, ("a", "regressed")))
    assert failing.as_dict()["verdict"] == "failed"
    assert [d["direction"] for d in verdict.ledger()["deltas"]] == ["improved"]


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


# ------------------------------------------ property 1: the empty diff fails

def test_the_empty_diff_is_not_retained(progress, tiny):
    """THE property of item 545. Every counter flat, the component FAILS."""
    verdict = progress.probe(tiny, "HEAD")
    assert {d.direction for d in verdict.deltas} == {progress.UNCHANGED}
    assert verdict.verified is False
    assert "empty diff" in verdict.reason


def test_a_diff_that_moves_no_counter_is_not_retained_either(progress, tiny):
    """The empty diff's cheapest disguise: a real change that is nowhere near a
    counter. `scope` refuses the literally empty diff; only `progress` refuses
    this one."""
    _write(tiny, "README.md", "a sentence nobody measures\n")
    _write(tiny, "src/revl/checker.py",
           "def check(program):\n    # clearer now\n    return []\n")
    verdict = progress.probe(tiny, "HEAD")
    assert {d.direction for d in verdict.deltas} == {progress.UNCHANGED}
    assert verdict.verified is False


@pytest.mark.parametrize("counter,mutate,control", [
    # The divergent document stopped diverging; the census allowance entry left.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl",)),
     ("native-chain-residual", "reach-gaps")),
    # `y.rvl` is reproduced by the native chain now; it left the residual and
    # stayed in the corpus, byte-identical.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",)),
     ("census-allowance", "reach-gaps")),
    # A corpus document reaches `kind=b` now; the ledger line left.
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",)),
     ("census-allowance", "native-chain-residual")),
])
def test_a_real_crossing_is_retained_and_the_others_stay_flat(
        progress, tiny, counter, mutate, control):
    """Property 1, the satisfying side: one genuine crossing is enough."""
    mutate(tiny)
    verdict = progress.probe(tiny, "HEAD")
    seen = {d.counter: d.direction for d in verdict.deltas}
    assert seen[counter] == progress.IMPROVED
    for other in control:
        assert seen[other] == progress.UNCHANGED
    assert verdict.verified is True
    assert progress.improvements(verdict.deltas) == (counter,)


# ----------------------------------- property 2: a regression still fails

@pytest.mark.parametrize("counter,mutate", [
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl", "examples/b.rvl",
                                        "examples/c.rvl"))),
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl", "y.rvl", "p.rvl"))),
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a", "kind=b", "kind=c"))),
])
def test_a_counter_that_grew_regresses_and_fails_the_component(
        progress, tiny, counter, mutate):
    mutate(tiny)
    verdict = progress.probe(tiny, "HEAD")
    assert {d.counter: d.direction for d in verdict.deltas}[counter] \
        == progress.REGRESSED
    assert not verdict.verified
    assert counter in verdict.reason


def test_an_improvement_beside_a_regression_fails(progress, tiny):
    """Property 2 end to end: the reach ledger shrank genuinely AND the census
    allowance grew. One up, one down is not zero, it is a failure."""
    build_tree(tiny, gaps=("kind=a",),
               allowance=("examples/a.rvl", "examples/b.rvl", "examples/c.rvl"))
    verdict = progress.probe(tiny, "HEAD")
    seen = {d.counter: d.direction for d in verdict.deltas}
    assert seen["reach-gaps"] == progress.IMPROVED
    assert seen["census-allowance"] == progress.REGRESSED
    assert verdict.verified is False


# ------------------------------- property 3: progress cannot be bought cheaply

@pytest.mark.parametrize("counter,mutate", [
    # census: the divergent document is DELETED; the allowance falls with it.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl",),
                          documents=("a", "c", "d", "e"))),
    # native: the residual document leaves the corpus altogether.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",),
                          corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl"))),
    # reach: the unreached dispatch arm is deleted. Nothing reaches an unreached
    # arm, so no golden changes and no conservation component fires: the
    # surface is the only thing that sees it.
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",), arms=("a", "c", "d"))),
])
def test_deleting_the_measured_surface_regresses_rather_than_improves(
        progress, tiny, counter, mutate):
    mutate(tiny)
    delta = by_counter(progress, tiny)[counter]
    assert delta.direction == progress.REGRESSED
    assert delta.head.value < delta.base.value, (
        "the gaming path under test must actually lower the value, or this "
        "test proves nothing")


@pytest.mark.parametrize("counter,mutate", [
    # census: delete the divergent document, add a trivially passing one so the
    # corpus COUNT holds. Counts: 2/5 to 1/5, improved. Identity: `b` left.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl",),
                          documents=("a", "c", "d", "e", "filler"))),
    # native: drop the residual document, add a filler name so the corpus COUNT
    # holds.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",),
                          corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl",
                                  "filler.rvl"))),
    # reach: delete the unreached arm, add a dummy arm so the table COUNT holds.
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",), arms=("a", "c", "d", "dummy"))),
])
def test_a_substitution_that_keeps_the_count_does_not_improve(
        progress, tiny, counter, mutate):
    """The gap the first slice left open: its universe was a COUNT."""
    mutate(tiny)
    delta = by_counter(progress, tiny)[counter]
    assert (delta.head.universe, delta.head.value) == \
        (delta.base.universe, delta.base.value - 1), \
        "the substitution must hold the count, or this test proves nothing"
    assert delta.direction == progress.REGRESSED


@pytest.mark.parametrize("counter,mutate", [
    # census: the divergent document is renamed; its baseline id goes with it.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl",),
                          documents=("a", "b2", "c", "d", "e"))),
    # native: the residual document is renamed in the corpus and not in the
    # residual table.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",),
                          corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl",
                                  "y2.rvl"))),
    # reach: the unreached arm is renamed, so the ledger line "retires".
    ("reach-gaps",
     lambda t: build_tree(t, gaps=("kind=a",), arms=("a", "b2", "c", "d"))),
])
def test_a_rename_is_not_progress(progress, tiny, counter, mutate):
    mutate(tiny)
    assert directions(progress, tiny)[counter] == progress.REGRESSED


def test_repointing_the_corpus_directory_is_not_progress(progress, tiny):
    """The residual is by NAME in the table, so a candidate could point
    `CORPUS_DIR` at a directory of easier documents with the same names. The
    member is the resolved path, so every member leaves the surface."""
    build_tree(tiny, residual=("x.rvl",), corpus_dir="easy_corpus")
    for name in ("p.rvl", "q.rvl", "r.rvl", "x.rvl", "y.rvl"):
        _write(tiny, f"tests/fixtures/easy_corpus/{name}", "// trivial\n")
    assert directions(progress, tiny)["native-chain-residual"] \
        == progress.REGRESSED


@pytest.mark.parametrize("counter,mutate,document", [
    # census: the failing document is gutted until the gate agrees, then its
    # allowance entry is dropped. The path survives; the program does not.
    ("census-allowance",
     lambda t: build_tree(t, allowance=("examples/a.rvl",)),
     "examples/b.rvl"),
    # native: the residual document is rewritten to something the chain can
    # already lower, then moved out of the residual.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",)),
     "tests/fixtures/emit_py_corpus/y.rvl"),
])
def test_gutting_the_failing_document_in_place_is_not_progress(
        progress, tiny, counter, mutate, document):
    mutate(tiny)
    (tiny / document).write_text("fn main() -> Int = 0\n")
    delta = by_counter(progress, tiny)[counter]
    assert delta.direction == progress.REGRESSED
    assert "EDITED" in delta.detail


@pytest.mark.parametrize("counter,mutate,instrument,text", [
    # census: weaken the REFERENCE until it agrees with the gate. The
    # divergence is gone and no gate got better.
    ("census-allowance", lambda t: build_tree(t, allowance=("examples/a.rvl",)),
     "src/revl/checker.py", "def check(program):\n    return None\n"),
    # census: drop a hand-written boundary program from the census tool, and
    # its allowance entry with it. The corpus walk never counted it, so only
    # the instrument rule sees this one.
    ("census-allowance", lambda t: build_tree(t, allowance=("examples/a.rvl",)),
     "tools/gate_reference_census.py", CENSUS_STUB + "PROGRAMS = ()\n"),
    # native: change the reference emitter the chain is compared against.
    ("native-chain-residual", lambda t: build_tree(t, residual=("x.rvl",)),
     "backends/python/emit.py", _emit(("a", "b", "c", "d")) + "# relaxed\nX = 1\n"),
    # native: change the ratchet itself, beside its table.
    ("native-chain-residual",
     lambda t: build_tree(t, residual=("x.rvl",),
                          ratchet=RATCHET_BODY.replace("==", "or")),
     None, None),
    # native: deselect the ratchet from the pytest configuration.
    ("native-chain-residual", lambda t: build_tree(t, residual=("x.rvl",)),
     "pyproject.toml", "[tool.pytest.ini_options]\naddopts = '--deselect x'\n"),
    # reach: change the tool that decides what is reached.
    ("reach-gaps", lambda t: build_tree(t, gaps=("kind=a",)),
     "tools/oracle_construct_reach.py", "LEDGER = 'ledger'\nSKIP = {'kind=b'}\n"),
    # reach: change the reference IR producer, which is what reaches.
    ("reach-gaps", lambda t: build_tree(t, gaps=("kind=a",)),
     "src/revl/lower.py", "def lower(program):\n    return {'kind': 'b'}\n"),
])
def test_a_crossing_beside_a_moved_instrument_is_not_credited(
        progress, tiny, counter, mutate, instrument, text):
    mutate(tiny)
    if instrument is not None:
        _write(tiny, instrument, text)
    delta = by_counter(progress, tiny)[counter]
    assert delta.direction == progress.UNREADABLE
    assert "instruments" in delta.detail
    assert progress.probe(tiny, "HEAD").verified is False


def test_an_instrument_moved_alone_claims_nothing_and_blocks_nothing(
        progress, tiny):
    """The other side of the instrument rule. A reference change that does not
    shrink a failing set is not this component's business: it is `unchanged`,
    and the conservation components judge it."""
    _write(tiny, "src/revl/checker.py", "def check(program):\n    return [1]\n")
    assert set(directions(progress, tiny).values()) == {progress.UNCHANGED}


def test_only_the_table_literal_may_change_in_a_table_file(progress, tiny):
    """The satisfying side of the table rule. The residual table and the corpus
    list live inside instrument files, and a real crossing edits them: moving
    `y.rvl` out of the residual and adding a new passing corpus document is
    table-only, and it is credited."""
    build_tree(tiny, residual=("x.rvl",),
               corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl", "y.rvl", "z.rvl"))
    delta = by_counter(progress, tiny)["native-chain-residual"]
    assert delta.direction == progress.IMPROVED
    assert delta.crossed == ("py:tests/fixtures/emit_py_corpus/y.rvl",)


def test_a_moved_instrument_file_is_seen_under_its_old_name(progress, tiny):
    """With rename detection on, `git diff --name-only` prints only the
    destination, so moving the reference out of `src/revl/` would look like an
    addition somewhere else."""
    build_tree(tiny, allowance=("examples/a.rvl",))
    _git(tiny, "mv", "src/revl/checker.py", "checker_moved.py")
    assert directions(progress, tiny)["census-allowance"] == progress.UNREADABLE


@pytest.mark.parametrize("padding", [
    # a corpus document that no counter's failing set mentions
    lambda t: _write(t, "examples/noop.rvl", "fn noop() -> Int = 0\n"),
    # a test that asserts nothing
    lambda t: _write(t, "tests/test_padding.py", "def test_nothing():\n    pass\n"),
    # the same, registered in the native chain's corpus
    lambda t: build_tree(t, corpus=("p.rvl", "q.rvl", "r.rvl", "x.rvl",
                                    "y.rvl", "noop.rvl")),
])
def test_padding_moves_no_counter(progress, tiny, padding):
    """A grown surface is `unchanged`, never `improved`, so padding the corpus
    or the suite is worth nothing here."""
    padding(tiny)
    verdict = progress.probe(tiny, "HEAD")
    assert {d.direction for d in verdict.deltas} == {progress.UNCHANGED}
    assert verdict.verified is False


@pytest.mark.parametrize("counter,mutate", [
    ("census-allowance", lambda t: build_tree(t, allowance=("examples/a.rvl",))),
    ("native-chain-residual", lambda t: build_tree(t, residual=("x.rvl",))),
    ("reach-gaps", lambda t: build_tree(t, gaps=("kind=a",))),
])
def test_a_table_edited_to_a_lie_is_what_the_counters_cannot_see(
        progress, tiny, counter, mutate):
    """The limit of this component, pinned so it is not forgotten. Deleting a
    failing entry with nothing fixed looks, from the artifacts, exactly like a
    fix: the tables ARE the artifacts. Every one of these tables is held to the
    tree by a ratchet another component runs, and that is what refuses the lie:

      census-allowance       `tools/gate_reference_census.py --check`, run by
                             `no-new-false-admits`, fails on a divergence that
                             is not baselined
      native-chain-residual  `tests/test_selfhost_compile.py::test_the_residual_
                             is_located_in_lower_not_in_the_emitter`, run by
                             `tests`, recomputes the residual
      reach-gaps             `tests/test_oracle_construct_reach.py::test_the_
                             committed_ledger_matches_this_tree`, run by `tests`

    This test is the reason `progress` is a conjunct and not the reward."""
    mutate(tiny)
    assert directions(progress, tiny)[counter] == progress.IMPROVED


def test_the_ratchets_that_refuse_a_lie_exist_where_the_docstring_says(progress):
    source = (ROOT / "tests/test_selfhost_compile.py").read_text()
    assert "def test_the_residual_is_located_in_lower_not_in_the_emitter(" in source
    reach = (ROOT / "tests/test_oracle_construct_reach.py").read_text()
    assert "def test_the_committed_ledger_matches_this_tree(" in reach
    census = (ROOT / "tools/gate_reference_census.py").read_text()
    assert "--check" in census


def test_the_scorer_never_runs_the_candidates_measuring_code(progress, tiny):
    """The reach surface is sized by the SCORER's `tools/selfhost_coverage.py`.
    The candidate's copy in the tiny tree is a stub with no reader at all; if
    it were loaded, the reach counter would be unreadable."""
    _write(tiny, "tools/selfhost_coverage.py",
           "raise SystemExit('the candidate copy must never be executed')\n")
    assert directions(progress, tiny)["reach-gaps"] == progress.UNCHANGED
    assert "_evolution_progress_selfhost_coverage" not in sys.modules


@pytest.mark.parametrize("artifact", [
    "tools/gate_reference_census_baseline.json",
    "tests/test_selfhost_compile.py",
    "tests/test_selfhost_emit_py.py",
    "tests/fixtures/oracle_construct_reach_ledger.json",
])
def test_a_deleted_artifact_is_unreadable_and_fails_the_component(
        progress, tiny, artifact):
    (tiny / artifact).unlink()
    verdict = progress.probe(tiny, "HEAD")
    assert progress.UNREADABLE in {d.direction for d in verdict.deltas}
    assert not verdict.verified


def test_an_emptied_residual_table_is_absent_not_zero(progress, tiny):
    """A candidate that deletes the `LOWER_GAP_DOCS` mapping outright has not
    reached a residual of zero, and reading it as zero would be the single
    cheapest false advance available."""
    (tiny / "tests/test_selfhost_compile.py").write_text("PY_DOCS = []\n")
    assert directions(progress, tiny)["native-chain-residual"] \
        == progress.UNREADABLE


def test_an_emptied_reach_ledger_is_absent_not_zero(progress, tiny):
    (tiny / "tests/fixtures/oracle_construct_reach_ledger.json").write_text(
        json.dumps({"_about": ["stub"]}))
    assert directions(progress, tiny)["reach-gaps"] == progress.UNREADABLE


def test_a_base_ref_that_does_not_resolve_fails_rather_than_passing(
        progress, tiny):
    verdict = progress.probe(tiny, "no-such-ref")
    assert {d.direction for d in verdict.deltas} == {progress.UNREADABLE}
    assert not verdict.verified


# ------------------------------------------- property 4: the candidate's base

@pytest.fixture
def forked(tiny):
    """`main` at M0, a candidate branch forked from it, and `main` moving on to
    M1, which retires the census allowance entry for `b` (someone else's work).

    Returns `(tree, m0, m1)` with the candidate branch checked out.
    """
    m0 = _git(tiny, "rev-parse", "HEAD").strip()
    _git(tiny, "checkout", "-q", "-b", "candidate")
    _git(tiny, "checkout", "-q", "main")
    build_tree(tiny, allowance=("examples/a.rvl",))
    m1 = _commit(tiny, "someone else retires b")
    _git(tiny, "checkout", "-q", "candidate")
    return tiny, m0, m1


def _naive(progress, tree, ref):
    """What comparing against the ref's TIP would say, for contrast."""
    with __import__("tempfile").TemporaryDirectory() as raw:
        base = progress.read_counters(progress.RefView(tree, ref), Path(raw))
        head = progress.read_counters(progress.WorkingTreeView(tree), Path(raw))
    return {d.counter: d.direction for d in progress.compare(base, head)}


def test_trunk_work_merged_into_the_candidate_is_not_its_credit(progress, forked):
    """The candidate merges `main` and does nothing else. Against its fork
    point it would be credited with M1's retirement; against its own base, the
    merge base with `main`, that work is on both sides and credits nobody."""
    tree, m0, m1 = forked
    _git(tree, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q",
         "--no-edit", "main")
    assert _naive(progress, tree, m0)["census-allowance"] == progress.IMPROVED, \
        "the fork point must credit the merged work, or this test proves nothing"
    sha, deltas = progress.measure_ledger(tree, "main")
    assert sha == m1
    assert {d.direction for d in deltas} == {progress.UNCHANGED}
    assert not progress.progress_verdict(deltas).verified


def test_trunk_work_that_landed_after_the_fork_is_not_charged_to_it(
        progress, forked):
    """`main` moved on past the candidate, which never merged it, and the
    candidate did its own real work on the reach ledger. Against `main`'s tip
    the candidate lacks M1's retirement and reads as a census regression;
    against its own base it is judged on its own diff and retained."""
    tree, m0, m1 = forked
    build_tree(tree, gaps=("kind=a",))
    naive = _naive(progress, tree, "main")
    assert naive["census-allowance"] == progress.REGRESSED, \
        "the tip must charge the candidate for M1, or this test proves nothing"
    verdict = progress.probe(tree, "main")
    assert verdict.base == m0
    seen = {d.counter: d.direction for d in verdict.deltas}
    assert seen == {"census-allowance": progress.UNCHANGED,
                    "native-chain-residual": progress.UNCHANGED,
                    "reach-gaps": progress.IMPROVED}
    assert verdict.verified is True


def test_the_resolved_base_is_recorded_in_the_ledger(progress, forked):
    tree, m0, _m1 = forked
    build_tree(tree, gaps=("kind=a",))
    ledger = progress.probe(tree, "main").ledger()
    assert ledger["base"] == m0
    assert ledger["improved"] == ["reach-gaps"]


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
    """Even a scorecard that CLAIMS retention for an unmoved candidate (an
    older scorer, or a hand-assembled record) does not promote: the rule
    re-reads the directions."""
    result = progress.promote(
        [_entry(f"c{i}", True, ["unchanged", "unchanged"]) for i in range(10)])
    assert result.promoted is False
    assert "did not advance" in result.reason


def test_one_retained_advance_promotes_the_generation(progress):
    result = progress.promote([
        _entry("a", False, ["unchanged", "unchanged"]),
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
    boolean the producer wrote."""
    entry = _entry("a", True, ["unchanged"])
    entry["advanced"] = True
    entry["progress"]["advanced"] = True
    entry["progress"]["improved"] = ["census-allowance"]
    entry["reason"] = "I improved the census allowance substantially"
    assert progress.promote([entry]).promoted is False


def test_a_missing_progress_block_is_not_an_advance(progress):
    entry = {"candidate": "a", "retained": True}
    assert progress.promote([entry]).promoted is False


def test_the_serialised_rule_and_the_object_rule_agree(progress):
    """`advanced` runs over `Delta` objects and the promotion rule runs over the
    serialised scorecard. Two implementations of one rule drift, so they are
    pinned against each other over every combination of two directions."""
    names = ("improved", "unchanged", "regressed", "unreadable")
    for first in names:
        for second in names:
            deltas = _deltas(progress, ("a", first), ("b", second))
            entry = _entry("x", True, [first, second])
            assert progress.advanced(deltas) is progress.promote([entry]).promoted


# ------------------------------------------------------------ the real tree

def test_the_counters_read_this_repository(progress, tmp_path):
    """Non-vacuity on the tree that matters. Every counter must resolve to a
    real pair here, with a failing set inside a strictly larger surface: a
    counter that silently answered nothing over nothing would satisfy every
    rule above and measure nothing."""
    readings = progress.read_counters(progress.WorkingTreeView(ROOT), tmp_path)
    assert set(readings) == set(progress.COUNTERS)
    for name, reading in readings.items():
        assert reading.readable, f"{name} is unreadable on this tree: {reading.detail}"
        assert reading.universe > 0, name
        assert reading.failing < reading.members, name


def test_the_census_counter_is_the_baseline_file(progress, tmp_path):
    """Recomputed here from the artifact itself, so the counter cannot drift
    into measuring something adjacent to the gate's own allowance."""
    baseline = json.loads(
        (ROOT / "tools/gate_reference_census_baseline.json").read_text())
    expected = {i for ids in baseline["buckets"].values() for i in ids}
    reading = progress.census_allowance(progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.failing == expected


def _module_literal(path, name):
    for node in ast.parse(path.read_text()).body:
        target = (node.targets[0] if isinstance(node, ast.Assign) else
                  node.target if isinstance(node, ast.AnnAssign) else None)
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(node.value)
    return None


def test_the_native_chain_counter_is_the_residual_over_its_own_corpus(
        progress, tmp_path):
    """The failing set is `LOWER_GAP_DOCS`, and the surface is the union of the
    tier corpora the ratchet recomputes it over, NOT the neighbouring `*_DOCS`
    tables the first slice summed (those are other tests' inputs)."""
    table = _module_literal(ROOT / "tests/test_selfhost_compile.py",
                            "LOWER_GAP_DOCS")
    reading = progress.native_chain_residual(
        progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.value == sum(len(set(v)) for v in table.values())
    corpus = sum(len(set(_module_literal(
        ROOT / f"tests/test_selfhost_emit_{tier}.py", "CORPUS"))) for tier in table)
    assert reading.universe == corpus
    for tier, names in table.items():
        for name in names:
            assert any(m.startswith(f"{tier}:") and m.endswith(name.split("/")[-1])
                       for m in reading.failing)


def test_the_reach_counter_is_the_emitter_half_of_the_ledger(progress, tmp_path):
    ledger = json.loads(
        (ROOT / "tests/fixtures/oracle_construct_reach_ledger.json").read_text())
    expected = {f"{o}:{c}" for o in progress.REACH_ORACLES if o in ledger
                for c in ledger[o]}
    reading = progress.reach_gaps(progress.WorkingTreeView(ROOT), tmp_path)
    assert reading.failing == expected
    assert reading.universe > reading.value


def test_every_instrument_pattern_names_a_file_in_this_tree(progress):
    """An instrument pattern that matches nothing guards nothing, which is the
    gate-that-cannot-fire shape. Configuration files are exempt: their absence
    is the normal state, and their appearance is the thing being watched."""
    import re

    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    optional = {r"pytest\.ini", r"setup\.cfg", r"tox\.ini"}
    for counter, patterns in progress.INSTRUMENTS.items():
        for pattern in patterns:
            if pattern in optional:
                continue
            rx = re.compile(pattern + r"\Z")
            assert any(rx.match(p) for p in tracked), (counter, pattern)


# -------------------------------------------------------------------- the cli

def test_the_cli_fails_an_unchanged_tree(tiny):
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD"],
        capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "did not advance" in proc.stdout


def test_the_cli_exits_nonzero_when_a_counter_regressed(tiny):
    build_tree(tiny, allowance=("examples/a.rvl", "examples/b.rvl",
                                "examples/c.rvl"))
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD"],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "census-allowance" in proc.stdout


def test_the_cli_writes_a_ledger_the_promotion_rule_reads(
        tiny, tmp_path, progress):
    build_tree(tiny, allowance=("examples/a.rvl",))
    out = tmp_path / "progress.json"
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--tree", str(tiny), "--base", "HEAD",
         "--json", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
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
