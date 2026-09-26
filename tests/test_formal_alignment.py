"""Four fidelity gaps behind the checker-alignment buckets, and the
promotion that makes those buckets able to fire (issue #1169, F1 to F4).

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
    entries (`Inner` and `AuditSink` in `examples/interpose_observe.rvl`);
  * F4: the arm chain `accept / G4 / G2,G3 / A9 / A2 / else` had nothing
    for a G5 or a G6 checker code, so `g5_undo_handle_emission.rvl` landed
    in `formal-found-other` and eleven more G5 refusals in the generic
    `out-of-fragment`. There is a G5 arm now, and a documented reason there
    is no `agree-G6` (below).

And the promotion the first four were the precondition for: `formal-strict`
and `formal-found-other` are in `FATAL_BUCKETS`. Informational is how the
three files survived, and a bucket that cannot fire is not a gate.

And the level below that, which the F4 work could not reach: the two
`out-of-fragment-G5` / `out-of-fragment-G6` buckets record an ABSENCE, so
neither can disagree with anything and neither could fail. What is
checkable without judging their contents is MEMBERSHIP, so both are held to
`formal/out_of_fragment_ledger.json`, which shrinks only: a file joining a
bucket without a line in it fails the gate, and a line no longer in its
bucket fails the gate until it is deleted. The tests at the end name the
input that makes each direction fire.

`make formal` needs a Lean toolchain; this module runs in the plain
`pytest tests/` job and pins each fix on the file that exposed it, plus the
reference behaviour each fix follows, so none of the three can quietly come
back. Its sibling `test_formal_attenuation_namespace.py` pins the `Seam`
bound case that F3 must not cost.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import sys
import warnings
from contextlib import redirect_stdout
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
#: emit's argument. Its `A` surface is the head crossing alone
#: (`lower._emit_step_caps_pairs` reads the step's target). Since issue #1427
#: the checker refuses the program, because `helper` is a second crossing under
#: one marker; the model refuses it through the `G` row, not by widening `A`.
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
def exported(harness):
    """`(tsv rows, file facts, census)` for the whole corpus, exported once."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return harness.export()


@pytest.fixture(scope="module")
def rows(exported):
    return exported[0]


@pytest.fixture(scope="module")
def tsv(rows):
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
    target only. The checker refuses the program for the unmarked nested
    crossing (issue #1427), which is the marker rule's business; the surface
    must still not count the argument."""
    from revl.compiler import compile_source
    from revl.errors import RevlError

    with pytest.raises(RevlError, match="call to emission `helper` must be marked"):
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
    assert surface == {harness._undeclared_cap("A")}


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


# ------------------------- F4: a G5 arm, and no G6 arm on the confinement row

#: The G5 fixture whose `undo` reaches its emission through a NAMED fn, so the
#: model's `Prog` resolves it and the `U5` row counts the registration.
G5_BY_U5 = "examples/rejections/g5_undo_fn_emission.rvl"
#: The G5 fixture the `U5` row cannot see (`undo w.task.run(...)` reads the
#: crossing off a spawn handle) but the marker rule refuses through the `G`
#: row: an unmarked call to a method the service declares `emission`. This is
#: the file that sat in `formal-found-other`.
G5_BY_G_ROW = "examples/rejections/g5_undo_handle_emission.rvl"
#: A G5 fixture no row sees: `undo dispatch1(ref)`, where `dispatch1` calls
#: its own parameter, so the reach fold leaves the `Prog` at the first hop.
G5_OUT_OF_PROG = "examples/rejections/g5_undo_handle_ref_arg.rvl"
#: The corpus's only G6-coded file.
G6 = "examples/rejections/g6_method_local_shadows_component.rvl"

#: A program the checker ACCEPTS whose `C` row nonetheless `fail`s: `Map` is a
#: host root, not a declared require, so the confinement surface reports a
#: leak. The evidence that an `agree-G6` keyed on a `C` fail could not fail.
HOST_ROOT_SOURCE = """\
service K { fn f() -> Int }
component C provides k: K {
  let store = effect Map.new() undo store.drop()
  provide k { fn f() = 1 }
}
"""


@pytest.fixture
def align(harness, monkeypatch, tmp_path):
    """Run `checker_alignment` over ONE corpus file, with its no-manifest
    writer pointed at a scratch directory, and return
    `(non-zero bucket counts, fatal findings, the printed report)`."""
    (tmp_path / "harness" / "out").mkdir(parents=True)
    monkeypatch.setattr(harness, "FORMAL", tmp_path)

    def run(rel, verdicts, rows):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fatal = harness.checker_alignment({rel: {}}, [], verdicts, rows)
        out = buf.getvalue()
        counts = {k: int(n) for k, n in re.findall(
            r"^  ([a-zA-Z0-9-]+)\s+(\d+)(?:\s+FATAL)?$", out, re.MULTILINE)
            if int(n)}
        return counts, fatal, out
    return run


def test_the_checker_refuses_each_g5_fixture_with_code_g5(harness):
    """The premise. All three arms below are about files revl answers `G5`
    for; if one stops being a G5 the test under it is measuring nothing."""
    for rel in (G5_BY_U5, G5_BY_G_ROW, G5_OUT_OF_PROG):
        assert harness.checker_code(rel)[0] == "G5", rel


def test_a_resolvable_undo_agrees_by_the_u5_row(align, verdicts, rows):
    counts, fatal, out = align(G5_BY_U5, verdicts, rows)
    assert counts == {"agree-G5": 1}
    assert fatal == []
    assert f"ALIGN agree-G5 via U5: {G5_BY_U5}" in out


def test_a_handle_undo_agrees_by_the_marker_row(align, verdicts, rows):
    """`undo w.task.run("bye")` is an unmarked call to an `emission` method,
    so the `G` row refuses the component even though the `U5` row counts
    nothing. This file is why the arm exists: it was `formal-found-other`,
    which is now fatal, so without the arm the gate would red."""
    counts, fatal, out = align(G5_BY_G_ROW, verdicts, rows)
    assert counts == {"agree-G5": 1}
    assert fatal == []
    assert f"ALIGN agree-G5 via G: {G5_BY_G_ROW}" in out


def test_an_undo_that_leaves_the_prog_is_out_of_fragment_not_missed(
        harness, align, verdicts, rows):
    """`missed-G5` would be a claim that the model went blind. Here the model
    has no fact at all: `dispatch1` calls its own parameter, which is not a
    declared fn or extern, so the reach fold has nothing to follow. Named,
    not counted, and not fatal."""
    assert G5_OUT_OF_PROG not in harness.g5_files_the_prog_resolves(rows)
    counts, fatal, out = align(G5_OUT_OF_PROG, verdicts, rows)
    assert counts == {"out-of-fragment-G5": 1}
    assert fatal == []
    assert f"ALIGN out-of-fragment-G5: {G5_OUT_OF_PROG}" in out


def test_missed_g5_is_a_gate_failure(harness):
    assert "missed-G5" in harness.FATAL_BUCKETS


def test_missed_g5_fires_when_the_prog_resolves_the_undo(
        harness, align, verdicts, rows):
    """The input that still FAILS after the arm. `g5_undo_fn_emission.rvl`
    reaches its emission through `wrap`, a declared fn whose whole callee
    closure is declared, so the `Prog` resolves the `undo` and a zero count
    is the fold going blind rather than an absent fact. Blind the `U5` row
    and the file is `missed-G5`, fatal, exactly as a `missed-G4` is."""
    assert G5_BY_U5 in harness.g5_files_the_prog_resolves(rows)
    blind = verdicts._replace(g5reg={
        k: (0 if isinstance(v, int) else v) for k, v in verdicts.g5reg.items()})
    counts, fatal, _out = align(G5_BY_U5, blind, rows)
    assert counts == {"missed-G5": 1}
    assert fatal == [f"missed-G5: {G5_BY_U5}"]


def test_the_confinement_row_fails_on_a_program_the_checker_accepts(
        harness, tmp_path_factory):
    """Why there is no `agree-G6` keyed on a `C` fail. The model's `C` row is
    the issue-276 confinement surface: a statement head whose root is not a
    declared require or require-held binding is a `fail`, and a host root
    like `Map` never is one. The checker accepts this program, so an
    agreement resting on a `C` fail would be an agreement that cannot fail."""
    from revl.compiler import compile_source

    compile_source(HOST_ROOT_SOURCE, "host_root.rvl")
    _rows, ref, _formal = _synthetic_corpus(
        harness, tmp_path_factory.mktemp("hostroot"),
        {"host_root.rvl": HOST_ROOT_SOURCE})
    assert any(x == "fail" for x in ref.confinements.values())


def test_a_g6_refusal_is_out_of_fragment_with_the_model_clean(
        align, verdicts, rows, harness):
    """revl's G6 is purity outside effect forms and the duplicate-binding
    refusal; the model states neither. Its verdicts on the file are all
    `ok`, and the `C` rows that do `fail` are about something else."""
    assert harness.checker_code(G6)[0] == "G6"
    assert verdicts.comps[(G6, "C")] == "ok"
    assert any(x == "fail" for k, x in verdicts.confinements.items()
               if k[0] == G6)
    counts, fatal, out = align(G6, verdicts, rows)
    assert counts == {"out-of-fragment-G6": 1}
    assert fatal == []
    assert f"ALIGN out-of-fragment-G6: {G6}" in out


# ----------------------------------- the promotion: both buckets can now fire

def test_both_informational_buckets_are_fatal(harness):
    assert {"formal-strict", "formal-found-other"} <= set(harness.FATAL_BUCKETS)


def test_a_model_refusal_on_an_accepted_file_fails_the_gate(
        align, verdicts, rows):
    """The input that still FAILS for `formal-strict`: the checker accepts
    `notes.rvl` and a model row says `fail`. Before the promotion this
    printed and returned nothing."""
    strict = verdicts._replace(
        comps={**verdicts.comps, (NOTES, "NotesConsole"): "fail"})
    counts, fatal, out = align(NOTES, strict, rows)
    assert counts == {"formal-strict": 1}
    assert fatal == [f"formal-strict: {NOTES}"]
    assert f"ALIGN formal-strict: {NOTES}" in out


def test_a_model_refusal_under_an_unmodelled_code_fails_the_gate(
        align, verdicts, rows):
    """The input that still FAILS for `formal-found-other`: revl refuses the
    G6 file for a rule the model does not state, and a model row refuses it
    for some other reason. `out-of-fragment-G6` is only for a CLEAN model."""
    dirty = verdicts._replace(comps={**verdicts.comps, (G6, "C"): "fail"})
    counts, fatal, _out = align(G6, dirty, rows)
    assert counts == {"formal-found-other": 1}
    assert fatal == [f"formal-found-other: {G6}"]


# ------------------------- STATUS.md's claim is generated, not typed

@pytest.fixture(scope="module")
def status(harness, exported):
    """The census block this corpus produces, and the alignment it rests on.
    Runs the real `checker_alignment` over every modeled file (its
    no-manifest writer targets the git-ignored `formal/harness/out/`)."""
    rows, facts, census = exported
    ref = harness.reference_from_tsv(rows)
    buf = io.StringIO()
    with redirect_stdout(buf):
        fatal = harness.checker_alignment(
            facts, census["componentless"], ref, rows)
    block = harness.status_block(census, facts, census["componentless"],
                                 census["refusals"], ref, harness._ALIGN)
    return (block, fatal, dict(harness._ALIGN),
            {k: list(v) for k, v in harness._ALIGN_SAMPLES.items()})


def test_the_corpus_has_no_disagreeing_bucket(status):
    """The exit criterion of the issue: with F1 to F4 in, every bucket that
    now fails the gate is empty, which is what makes the promotion a gate
    rather than a permanent red."""
    _block, fatal, align, _samples = status
    assert fatal == []
    assert {k: v for k, v in align.items() if k in
            ("formal-strict", "formal-found-other")} == {}


def test_status_md_carries_the_block_this_run_produces(harness, status):
    """The second half of the issue. `formal/STATUS.md` asserted 0 formal-
    strict and 0 formal-found-other with nothing comparing the assertion to
    the gate's output. The numbers are rendered by the run that measures
    them, and a stale checkout of the block fails here and in `make
    formal`."""
    block, _fatal, _align, _samples = status
    assert harness.sync_status(block, write=False) is None
    assert "| `formal-strict` | 0 | **FATAL** |" in block
    assert "| `formal-found-other` | 0 | **FATAL** |" in block


def test_a_stale_block_is_reported_as_drift(harness, status, tmp_path,
                                            monkeypatch):
    """The check bites. Edit the checked-in block and `sync_status` says so
    rather than agreeing; `--write-status` puts it back."""
    block, _fatal, _align, _samples = status
    copy = tmp_path / "STATUS.md"
    copy.write_text(
        harness.STATUS_PATH.read_text(encoding="utf-8").replace(
            "| `formal-strict` | 0 |", "| `formal-strict` | 7 |"),
        encoding="utf-8")
    monkeypatch.setattr(harness, "STATUS_PATH", copy)
    assert harness.sync_status(block, write=False) is not None
    assert harness.sync_status(block, write=True) is None
    assert harness.sync_status(block, write=False) is None
    assert "| `formal-strict` | 0 |" in copy.read_text(encoding="utf-8")


def test_a_document_with_no_markers_is_drift_too(harness, status, tmp_path,
                                                 monkeypatch):
    """Deleting the markers must not read as agreement."""
    block, _fatal, _align, _samples = status
    copy = tmp_path / "STATUS.md"
    copy.write_text("nothing generated here\n", encoding="utf-8")
    monkeypatch.setattr(harness, "STATUS_PATH", copy)
    assert harness.sync_status(block, write=False) is not None
    assert harness.sync_status(block, write=True) is not None


# ------------- the two buckets that recorded an absence, given a firing condition

#: A name that is not in the corpus, standing in for a fixture somebody adds
#: tomorrow whose `undo` the `Prog` cannot resolve.
NEWCOMER = "examples/rejections/g5_undo_method_ref_map.rvl"


@pytest.fixture
def ledger(harness, tmp_path, monkeypatch):
    """Point the ratchet at a scratch ledger and return
    `(write(samples), check(samples))`."""
    path = tmp_path / "out_of_fragment_ledger.json"
    monkeypatch.setattr(harness, "OOF_LEDGER_PATH", path)

    def write(samples):
        with redirect_stdout(io.StringIO()):
            harness.out_of_fragment_ratchet(samples, write=True)
        return path

    def check(samples):
        with redirect_stdout(io.StringIO()):
            return harness.out_of_fragment_ratchet(samples)
    return write, check


def test_the_committed_ledger_is_this_corpus_s_membership(harness, status):
    """The ledger in the tree names exactly the files this corpus puts in
    the two buckets. This is the assertion that turns both of them from a
    printed list into something that can be wrong."""
    _block, _fatal, _align, samples = status
    with redirect_stdout(io.StringIO()):
        assert harness.out_of_fragment_ratchet(samples) == []
    committed = json.loads(
        harness.OOF_LEDGER_PATH.read_text(encoding="utf-8"))
    for bucket in harness.OOF_RATCHET_BUCKETS:
        assert committed[bucket] == sorted(samples.get(bucket, [])), bucket


def test_a_file_joining_an_out_of_fragment_bucket_fails_the_gate(
        harness, status, ledger):
    """The input that makes `out-of-fragment-G5` FAIL, which issue #1169's
    own report could not name. A new G5 fixture whose `undo` leaves the
    `Prog` lands in the bucket, the ledger does not have it, and the gate
    reds until somebody models it or writes the hole down."""
    _block, _fatal, _align, samples = status
    write, check = ledger
    write(samples)
    assert check(samples) == []
    joined = {**samples,
              "out-of-fragment-G5": [*samples["out-of-fragment-G5"], NEWCOMER]}
    findings = check(joined)
    assert findings == [f for f in findings if f.startswith(
        "joined out-of-fragment-G5: ")]
    assert len(findings) == 1 and NEWCOMER in findings[0]


def test_a_file_joining_the_g6_bucket_fails_the_gate(harness, status, ledger):
    """The same input for `out-of-fragment-G6`. The model states no rule
    about purity outside an effect form, so it cannot judge a new G6
    fixture's contents; it can still refuse to let one in unannounced."""
    _block, _fatal, _align, samples = status
    write, check = ledger
    write(samples)
    newcomer = "examples/rejections/g6_new_shape.rvl"
    findings = check({**samples, "out-of-fragment-G6": [
        *samples["out-of-fragment-G6"], newcomer]})
    assert len(findings) == 1
    assert findings[0].startswith(f"joined out-of-fragment-G6: {newcomer}")


def test_a_stale_ledger_line_fails_the_gate_and_must_be_deleted(
        harness, status, ledger):
    """The other direction, which is what makes it SHRINK-ONLY. Teach the
    `U5` fold to follow a handle and these files leave the bucket; their
    lines are then a claim that the model has no fact where it now has one,
    so each one reds until it is removed."""
    _block, _fatal, _align, samples = status
    write, check = ledger
    write(samples)
    shrunk = {**samples,
              "out-of-fragment-G5": samples["out-of-fragment-G5"][1:]}
    findings = check(shrunk)
    assert len(findings) == 1
    assert findings[0].startswith(
        f"left out-of-fragment-G5: {samples['out-of-fragment-G5'][0]}")
    write(shrunk)
    assert check(shrunk) == []


def test_a_missing_ledger_is_a_failure_not_a_pass(harness, status, ledger):
    """Deleting the ratchet must not read as nothing to check. That is the
    same failure mode as an informational bucket, spelled as a file."""
    _block, _fatal, _align, samples = status
    _write, check = ledger
    findings = check(samples)
    assert len(findings) == 1 and "is missing" in findings[0]


def test_a_ledger_without_a_name_list_is_a_failure(harness, status, ledger):
    """A bucket key replaced by something that is not a list of names is not
    an empty expectation; it is an unreadable ratchet."""
    _block, _fatal, _align, samples = status
    write, check = ledger
    path = write(samples)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["out-of-fragment-G6"] = 1
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    findings = check(samples)
    assert len(findings) == 1 and "no list of names" in findings[0]


def test_the_ledger_records_names_only(harness):
    """Names, never counts or line numbers, so a `--write-ledger` under the
    3.14 developer venv and under CI's 3.11 are the same bytes (the shape
    PR #1214's construct-reach ledger settled on)."""
    doc = json.loads(harness.OOF_LEDGER_PATH.read_text(encoding="utf-8"))
    assert set(doc) == {"_about", *harness.OOF_RATCHET_BUCKETS}
    for bucket in harness.OOF_RATCHET_BUCKETS:
        assert doc[bucket] == sorted(doc[bucket])
        for rel in doc[bucket]:
            assert isinstance(rel, str) and rel.endswith(".rvl")
            assert (ROOT / rel).is_file(), rel


def test_the_write_is_byte_stable(harness, status, ledger):
    """Two writes of the same membership are the same bytes, and the bytes
    in the tree are what this corpus writes."""
    _block, _fatal, _align, samples = status
    write, _check = ledger
    first = write(samples).read_bytes()
    assert write(samples).read_bytes() == first
    assert harness.OOF_LEDGER_PATH.read_bytes() == first


def test_the_census_writer_does_not_widen_the_ratchet(harness):
    """`--write-status` is routine; widening the set of files the model
    admits it has no fact about is not, and must not ride along on it. The
    two writers are separate flags, and only one of them touches the
    ledger."""
    source = (ROOT / "formal" / "harness" / "diff_corpus.py").read_text(
        encoding="utf-8")
    body = source.split("def write_status(", 1)[1].split("\ndef ", 1)[0]
    assert "ledger" in body
    assert "out_of_fragment_ratchet(_ALIGN_SAMPLES, write=True)" in body
    assert re.search(r'if "--write-ledger" in _argv', source)


def test_the_alignment_arms_do_not_run_the_ratchet(align, verdicts, rows):
    """The ratchet is a statement about the WHOLE corpus, so it lives in
    `main()`. Run over one file it would read every other name in the
    ledger as stale, which is why `checker_alignment` does not call it."""
    _counts, fatal, _out = align(G5_OUT_OF_PROG, verdicts, rows)
    assert fatal == []


def test_the_gate_reports_the_ratchet_as_a_gate_failure(harness):
    """`main()` appends the ratchet's findings to the fatal list, so they
    print as `GATE-FAILURE` and the gate exits non-zero."""
    source = (ROOT / "formal" / "harness" / "diff_corpus.py").read_text(
        encoding="utf-8")
    body = source.split("\ndef main()", 1)[1]
    assert "fatal.extend(out_of_fragment_ratchet(_ALIGN_SAMPLES))" in body
    assert "return 1 if (mismatches or fatal) else 0" in body


def test_status_md_calls_the_two_buckets_ratcheted(status):
    """The document stops saying `out-of-fragment*` is informational for the
    two that are now held to a ledger.

    The LABEL is the claim this holds. The two ratcheted COUNTS are held
    exactly, because those come from the ledger rather than from the corpus
    and the ledger only ever shrinks, so a closed hole has to be read in this
    diff. The plain bucket's count is not: it is a census of the corpus and it
    moves whenever a `.rvl` file lands anywhere in the tree.

    Pinning that census here is how this test reached main red. `38` was
    written when the corpus was 470 files; six files landed, the census read
    40, and a test whose subject is a word failed on a number that no change
    to the ratchet can move. `STATUS.md` is generated, so the block already
    follows the corpus on its own; this asserts the part of it that a
    regeneration cannot repair.
    """
    block, _fatal, _align, _samples = status
    ledger = json.loads(
        (ROOT / "formal" / "out_of_fragment_ledger.json").read_text(
            encoding="utf-8"))
    for bucket in ("out-of-fragment-G5", "out-of-fragment-G6"):
        assert f"| `{bucket}` | {len(ledger[bucket])} | ratcheted |" in block
    assert re.search(r"^\| `out-of-fragment` \| \d+ \| informational \|$",
                     block, re.M), block
    assert "out_of_fragment_ledger.json" in block
