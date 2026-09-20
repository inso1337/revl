"""Three fidelity gaps behind the checker-alignment buckets (issue #1169, F1
to F3).

`formal/harness/diff_corpus.py` ends by compiling every modeled corpus file
with the real checker and filing the pair (checker code, formal verdicts) in
a bucket. Two of those buckets, `formal-strict` (the model refuses what the
checker accepts) and `formal-found-other` (the model refuses a file the
checker refuses for an unrelated reason), were informational, and three
files sat in them for reasons that were harness defects, not findings about
revl:

  * F1: `checker_code` compiled a bare string, so any file with a `use` was
    refused before checking for lack of a module directory, and the model's
    verdict on it landed where nobody reads it (`examples/app/notes.rvl`);
  * F2: the `U` row handed the `emit` marker context to the whole argument
    subtree, so a plain method evaluated to build an emit's argument was
    modeled as "emit on a non-emission" (`NotesConsole` in the same file).
    The checker's marker covers the head call alone (issue #1175: one
    `emit` per crossing, the head's arguments lower in the enclosing mode),
    so an argument-position call carries its own `emitarg` context and is
    judged as a plain position is: a plain method there is admitted, an
    emission there is refused for its missing marker;
  * F3: a provide method emitting DIRECTLY through an emission extern
    exported the unnameable `*` on the bound side too, where the reference
    names the extern and measures it against the declared `emission[...]`
    entries (`Inner` and `AuditSink` in `examples/interpose_observe.rvl`).

`make formal` needs a Lean toolchain; this module runs in the plain
`pytest tests/` job and pins each fix on the file that exposed it, plus the
reference behaviour each fix follows, so none of the three can quietly come
back. Its sibling `test_formal_attenuation_namespace.py` pins the `Seam`
bound case that F3 must not cost.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

NOTES = "examples/app/notes.rvl"
INTERPOSE = "examples/interpose_observe.rvl"

#: The two direct-extern provide methods, with the extern each emits through.
DIRECT_EXTERN_CASES = (
    ((INTERPOSE, "Inner", "inner_db", "Db", "execute"), "wire"),
    ((INTERPOSE, "AuditSink", "audit", "Audit", "record"), "log_line"),
)

#: The activation-surface twin of F2: an emitting fn evaluated to build an
#: emit's argument. The checker accepts it, and its `A` surface is the head
#: crossing alone (`lower._emit_step_caps_pairs` reads the step's target).
ARG_POSITION_SOURCE = """\
service A { emission fn send(q: Str) -> Str }
extern emission fn wire(q: Str) -> Str = @py { return "row" }
fn helper(q: Str) -> Str = wire(q)
component C requires a: A {
  emit a.send(helper("x"))
}
"""

#: The four shapes the marker discipline decides (issue #1175). Each is one
#: synthetic corpus file; `b.fetch` is an emission, `x.plain` is not.
NESTED_SHAPES = {
    # a MARKED emission inside an emit head's argument list: refused outright
    "nested_emit_expression.rvl": """\
service A { emission fn send(q: Str) -> Str }
service B { emission fn fetch() -> Str }
component C requires a: A, b: B {
  emit a.send(emit b.fetch())
}
""",
    # an UNMARKED emission under an emit head: refused, one marker per crossing
    "nested_emission.rvl": """\
service A { emission fn send(q: Str) -> Str }
service B { emission fn fetch() -> Str }
component C requires a: A, b: B {
  emit a.send(b.fetch())
}
""",
    # a plain method under an emit head: admitted (NotesConsole's shape)
    "nested_plain.rvl": """\
service A { emission fn send(q: Str) -> Str }
service X { fn plain() -> Str }
component C requires a: A, x: X {
  emit a.send(x.plain())
}
""",
    # the same emission reached in PLAIN position, no emit head over it:
    # still refused, "call to emission `b.fetch` must be marked `emit`"
    "plain_position.rvl": """\
service A { emission fn send(q: Str) -> Str }
service B { emission fn fetch() -> Str }
service K { fn f() -> Str }
component C requires a: A, b: B provides k: K {
  provide k {
    fn f() {
      let r = b.fetch()
      return emit a.send(r)
    }
  }
}
""",
}

#: The reference's own statement of F3, as a refusal: the SAME direct-extern
#: shape, declared under a scope that does not name the extern.
UNDECLARED_EXTERN_SOURCE = """\
service Db { emission[other] fn execute(q: Str) -> Str }
extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn other(q: Str) -> Str = @py { return "row" }
component Inner provides inner_db: Db {
  provide inner_db { fn execute(q) = emit wire(q) }
}
"""


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus_alignment",
        ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows]


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(["\t".join(r) for r in tsv])


def _rows(tsv, kind, rel=None):
    return [r for r in tsv
            if r and r[0] == kind and (rel is None or r[1] == rel)]


def _synthetic_corpus(harness, root: Path, sources: dict[str, str]):
    """Export `sources` as the whole corpus and decide it on BOTH sides:
    the python reference always, the Lean oracle when `lake` is on PATH
    (it is on the formal gate's machines; elsewhere the gate itself is the
    Lean check). Returns `(tsv rows, reference, formal-or-None)`. The
    harness's corpus root is patched only for the export."""
    corpus = root / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, text in sources.items():
        (corpus / name).write_text(text, encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", root)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        rows, _facts, _census = harness.export()
    ref = harness.reference_from_tsv(rows)
    formal = None
    if shutil.which("lake") is not None:
        tsv_path = root / "corpus.tsv"
        tsv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        text = harness.run_oracle(tsv_path, root / "formal_verdicts.tsv")
        formal = harness.parse_verdicts(text)
    return [r.split("\t") for r in rows], ref, formal


@pytest.fixture(scope="module")
def nested(harness, tmp_path_factory):
    return _synthetic_corpus(harness, tmp_path_factory.mktemp("nested"),
                             NESTED_SHAPES)


# ----------------------------------------------------------- F1: the door

def test_the_bare_string_door_refuses_a_use_before_checking_anything():
    """The premise. `compile_source` on a `use`-bearing file answers a
    `REVL` about module resolution, not a verdict on the composition. If
    this stops refusing, F1 has been fixed one level down and the test
    below is measuring nothing."""
    from revl.compiler import compile_source
    from revl.errors import RevlError

    text = (ROOT / NOTES).read_text(encoding="utf-8")
    assert any(line.startswith("use ") for line in text.splitlines())
    with pytest.raises(RevlError) as excinfo:
        compile_source(text, NOTES)
    assert "`use` declarations need `modules=`" in str(excinfo.value)


def test_the_alignment_asks_the_checker_through_the_cli_door(harness):
    """The exit criterion of F1: the harness's own checker verdict on the
    file is what `revl check` says, an accept."""
    assert harness.checker_code(NOTES) == ("accept", "")


# ---------------------------------------------- F2: the marker is the head's

def test_the_marker_context_is_the_head_call_s_only(tsv):
    """`emit webui.add_entry(..., { strategy: ranking.strategy(), signals:
    ranking.signals() })`: one `emit` fact for the head, the two `Ranker`
    calls evaluated to build its argument in the `emitarg` position (admitted
    because `Ranker` declares them plain), and the two plain delegations in
    the `console` provision plain."""
    facts = {tuple(r[3:]) for r in _rows(tsv, "U", NOTES)
             if r[2] == "NotesConsole"}
    assert facts == {
        ("emit", "webui", "WebUI", "add_entry"),
        ("emitarg", "ranking", "Ranker", "strategy"),
        ("emitarg", "ranking", "Ranker", "signals"),
        ("plain", "ranking", "Ranker", "score"),
        ("plain", "ranking", "Ranker", "bump"),
    }


def test_the_reference_marker_rule_admits_the_component(verdicts):
    assert verdicts.comps[(NOTES, "NotesConsole")] == "ok"


def test_the_checker_s_marker_covers_the_head_call_alone():
    """The premise of `emitarg` (issue #1175): under an emit head a plain
    method is admitted, and an unmarked emission is refused for its missing
    marker exactly as the same emission in plain position is. The checker
    refuses it: the head's arguments lower in the enclosing mode."""
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    compile_source(NESTED_SHAPES["nested_plain.rvl"], "nested_plain.rvl")
    for name in ("nested_emission.rvl", "plain_position.rvl"):
        with pytest.raises(RevlError) as excinfo:
            compile_source(NESTED_SHAPES[name], name)
        assert classify(excinfo.value)["code"] == "G4"
        assert "call to emission `b.fetch` must be marked `emit`" in str(excinfo.value)
    with pytest.raises(RevlError) as excinfo:
        compile_source(NESTED_SHAPES["nested_emit_expression.rvl"],
                       "nested_emit_expression.rvl")
    assert classify(excinfo.value)["code"] == "G4"
    assert "one marker admits one crossing" in str(excinfo.value)


def test_the_model_judges_every_crossing_by_its_own_marker(nested):
    """Both sides, per shape: the plain method under an emit head is
    `g4=ok`, the unmarked emission under one is `g4=fail` like the
    plain-position `b.fetch()`. The U facts show WHERE: `emitarg` under the
    head, `plain` outside it; the verdict reads the declaration alone."""
    tsv, ref, formal = nested
    want = {"corpus/nested_emit_expression.rvl": "fail",
            "corpus/nested_emission.rvl": "fail",
            "corpus/nested_plain.rvl": "ok",
            "corpus/plain_position.rvl": "fail"}
    assert {rel: ref.comps[(rel, "C")] for rel in want} == want
    if formal is not None:
        assert {rel: formal.comps[(rel, "C")] for rel in want} == want
    ctx = {(r[1], r[5], r[6]): r[3] for r in _rows(tsv, "U")}
    assert ctx[("corpus/nested_emit_expression.rvl", "B", "fetch")] == "emitnested"
    assert ctx[("corpus/nested_emission.rvl", "B", "fetch")] == "emitarg"
    assert ctx[("corpus/nested_plain.rvl", "X", "plain")] == "emitarg"
    assert ctx[("corpus/plain_position.rvl", "B", "fetch")] == "plain"
    assert ctx[("corpus/plain_position.rvl", "A", "send")] == "emit"


def test_an_argument_position_call_is_not_on_the_activation_surface(
        harness, tmp_path, monkeypatch):
    """The `walk_reach` twin of the `walk_calls` leak. Under an `emit` the
    exporter switched the whole subtree to the marked region, so an
    emitting fn evaluated inside the argument list contributed `*` to the
    component's `A` surface. The reference's emit-step fold reads the head
    target only, and the checker accepts the program."""
    from revl.compiler import compile_source

    compile_source(ARG_POSITION_SOURCE, "arg_position.rvl")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "arg_position.rvl").write_text(ARG_POSITION_SOURCE,
                                             encoding="utf-8")
    monkeypatch.setattr(harness, "REPO", tmp_path)
    monkeypatch.setattr(harness, "CORPUS_DIRS", ("corpus",))
    rows, _facts, _census = harness.export()
    surface = {r[3] for r in (x.split("\t") for x in rows)
               if r[0] == "A" and r[2] == "C"}
    assert surface == {harness._wire_cap("a")}


# ------------------------------------ F3: a direct extern is nameable, once

@pytest.mark.parametrize("key, extern", DIRECT_EXTERN_CASES,
                         ids=[k[1] for k, _ in DIRECT_EXTERN_CASES])
def test_a_direct_extern_emission_names_the_extern_on_the_bound(
        tsv, key, extern):
    """`fn execute(q) = emit wire(q)` under `Db.execute` declared
    `emission[wire, ...]`: the F row's bound column (8) names `wire`, the
    way `_emitting_capabilities` seeds the reference's fixed point with the
    extern's own name. The attenuation column (7) stays the unnameable `*`:
    `_emit_step_caps_pairs` gives every non-`req` target `Cap("*")`, so the
    fold across a component edge never sees the extern's name."""
    rel, comp, pkey, svc, meth = key
    pairs = {(r[6], r[7]) for r in _rows(tsv, "F", rel)
             if (r[2], r[3], r[4], r[5]) == (comp, pkey, svc, meth)}
    assert pairs == {("*", extern)}


@pytest.mark.parametrize("key, extern", DIRECT_EXTERN_CASES,
                         ids=[k[1] for k, _ in DIRECT_EXTERN_CASES])
def test_the_direct_extern_bound_is_admitted(verdicts, key, extern):
    assert verdicts.providers[key] == "ok"


def test_the_fold_column_follows_the_reference_s_star():
    """The `cap` decision, pinned to the reference rather than asserted: a
    lowered emit step whose target is not a `req` resolves to `*` in the
    attenuation fold, whatever extern it names."""
    from revl import lower

    step = {"expr": {"kind": "fn", "name": "wire", "args": []}}
    caps = lower._emit_step_caps_pairs(step, {}, {})
    assert [c.token for c in caps] == ["*"]


def test_the_reference_measures_the_extern_by_name():
    """Non-vacuity for the bound column: the checker accepts
    `interpose_observe.rvl` because `Db.execute` names `wire`, and refuses
    the same shape under a scope that does not, naming the extern."""
    from revl.compiler import compile_files, compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile_files([str(ROOT / INTERPOSE)])
    with pytest.raises(RevlError) as excinfo:
        compile_source(UNDECLARED_EXTERN_SOURCE, "undeclared_extern.rvl")
    info = classify(excinfo.value)
    assert (info["code"], info["category"]) == ("G4", "emission-capability")
    assert "emits through `wire`" in str(excinfo.value)
