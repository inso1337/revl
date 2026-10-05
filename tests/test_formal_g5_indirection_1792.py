"""The G5 row follows a bracket inverse's indirections (issue #1792).

The checker refuses an `undo` that reaches an emission through a first-class
callable (`lower._walk_inverse_emissions`): a call to a `let`-bound arrow, a
reference to an emitting `fn` passed as an argument, and a read of an
`emission` service operation off a spawn handle, directly or through a `let`,
a second `let`, an `if` or `match` arm, a list element or a record field.

The model's `U5` row counts the inverse heads whose reach crosses a boundary
in the file's `Prog`, and the exporter used to carry only the calls written in
the slot (`dispatch1`, `f`, `app`), none of which the `Prog` can follow. So the
ten corpus documents refused that way sat in the ratcheted
`out-of-fragment-G5` bucket.

The exporter now resolves the indirections the way the checker does
(`inverse_reach_heads`) and adds what they reach to the `I` row's inverse
column: the arrow's body heads, the referenced fn, or the service operation,
which the `Prog` declares as an `emission` boundary named `<Service>.<op>`.
The `U5` rule itself is unchanged, and the ten documents file under
`agree-G5` through it.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `make formal` always.
"""

from __future__ import annotations

import io
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

#: Each document the ledger held, with the inverse head the indirection
#: resolves to.
RESOLVED = {
    "examples/rejections/g5_undo_arrow_emission.rvl": "send",
    "examples/rejections/g5_undo_fn_value_emission.rvl": "wrap",
    "examples/rejections/g5_undo_handle_ref_arg.rvl": "Task.run",
    "examples/rejections/g5_undo_handle_ref_let.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_alias.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_if_arm.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_let.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_list.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_match_arm.rvl": "Task.run",
    "examples/rejections/g5_undo_method_ref_record.rvl": "Task.run",
}

#: The same indirections over callables that cross nothing, and the call a
#: body already evaluated read back as a value. The checker admits every one,
#: so none may count a registration.
PURE_TWINS = """\
extern pure fn pure1(k: Str) -> Int = @py { return 1 }
extern emission fn mint(u: Str) -> Str = @py { return u }

fn dispatch1(f: (Str) -> Int) -> Int { return f("z") }
fn tidy(x: Str) -> Int { return pure1(x) }

service Cache { emission fn set(key: Str) }
service Task { fn peek(prompt: Str) -> Int }

component Worker provides task: Task {
  provide task { fn peek(prompt: Str) = 1 }
}

component C provides cache: Cache {
  let w = effect spawn Worker with { } undo w.dispose()
  provide cache {
    fn set(key) {
      let f = (x: Str) => pure1(x)
      let g = if (key == "a") { tidy } else { pure1 }
      let r = w.task.peek
      let t = emit mint(key)
      effect pure1(key)
      undo   f(key)
      effect pure1(t)
      undo   dispatch1(g)
      effect pure1("p")
      undo   dispatch1(r)
      effect pure1("q")
      undo   pure1(t)
    }
  }
}
"""


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g5_1792",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _effect_inverses(rows, rel):
    """`{index: [inverse heads]}` for one file's effect statements."""
    out = {}
    for row in rows:
        r = row.split("\t")
        if r[0] == "I" and r[1] == rel and r[4] == "effect":
            out[r[3]] = [h for h in r[6].split(",") if h]
    return out


def _synthetic(harness, root: Path, sources: dict[str, str]):
    corpus = root / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, text in sources.items():
        (corpus / name).write_text(text, encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", root)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        with redirect_stdout(io.StringIO()):
            out, _facts, _census = harness.export()
    ref = harness.reference_from_tsv(out)
    formal = None
    if shutil.which("lake") is not None:
        tsv_path = root / "corpus.tsv"
        tsv_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        formal = harness.parse_verdicts(
            harness.run_oracle(tsv_path, root / "formal_verdicts.tsv"))
    return out, ref, formal


def test_the_checker_refuses_every_document_under_g5(harness):
    for rel in RESOLVED:
        assert harness.checker_code(rel) == ("G5", "teardown"), rel


def test_each_indirection_resolves_to_the_head_it_reaches(rows):
    """One effect statement per file carries the resolved head beside the
    call written in the slot."""
    for rel, head in RESOLVED.items():
        inverses = _effect_inverses(rows, rel)
        assert any(head in heads for heads in inverses.values()), (
            rel, inverses)


def test_a_service_operation_is_declared_an_emission_boundary(rows):
    """`Task.run` is declared `emission[net]`, so the `Prog` carries it as an
    `emission` boundary, under a name no extern can have."""
    for rel, head in RESOLVED.items():
        if "." not in head:
            continue
        ex = [r.split("\t") for r in rows
              if r.startswith(f"EX\t{rel}\t{head}\t")]
        assert ex == [["EX", rel, head, "emission", "-", "-", "net"]], rel


def test_the_u5_row_counts_each_registration(verdicts):
    for rel in RESOLVED:
        counts = [x for k, x in verdicts.g5reg.items() if k[0] == rel]
        assert any(isinstance(x, int) and x > 0 for x in counts), (rel, counts)


def test_every_document_files_under_agree_g5_by_the_u5_row(
        harness, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in RESOLVED}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
        witness = dict(harness._G5_WITNESS)
    assert fatal == []
    assert set(samples.get("agree-G5", [])) == set(RESOLVED)
    assert {witness[rel] for rel in RESOLVED} == {"U5"}


def test_the_g5_ledger_list_is_empty(harness):
    ledger = harness.load_out_of_fragment_ledger()
    assert ledger["out-of-fragment-G5"] == []
    assert not (harness.OOF_LEDGER_PATH / "out-of-fragment-G5").exists()
    assert "out-of-fragment-G5" in harness.OOF_RATCHET_BUCKETS


@pytest.fixture(scope="module")
def twins(harness, tmp_path_factory):
    return _synthetic(harness, tmp_path_factory.mktemp("g5_twins"),
                      {"pure_twins.rvl": PURE_TWINS})


def test_the_checker_admits_the_pure_twins():
    from revl.compiler import compile_source

    compile_source(PURE_TWINS, "pure_twins.rvl")


def test_a_pure_indirection_counts_nothing(twins):
    out, ref, formal = twins
    counts = {k: x for k, x in ref.g5reg.items()
              if k[0] == "corpus/pure_twins.rvl"}
    assert len(counts) == 5 and set(counts.values()) == {0}, counts
    assert not any(r.startswith("EX\tcorpus/pure_twins.rvl\tTask.")
                   for r in out)
    if formal is not None:
        assert formal.g5reg == ref.g5reg


def test_a_value_computed_before_the_slot_is_not_a_crossing(harness):
    """`let t = emit mint(u)` ran in the body: `undo pure1(t)` hands the
    inverse a value, and `mint` is not one of its heads."""
    from revl.parser import Parser

    prog = Parser(PURE_TWINS, "pure_twins.rvl").parse()
    comp = next(c for c in prog.components if c.name == "C")
    method = next(s for s in comp.body
                  if type(s).__name__ == "ProvideStmt").methods[0]
    lets = {s.name: s.value for s in method.body
            if type(s).__name__ == "LetStmt"}
    ctx = harness._InverseCtx(lets, {"mint"}, {"mint": "emission"}, {}, {},
                              {}, {}, {})
    undo = [s.undo for s in method.body if type(s).__name__ == "EffectStmt"][-1]
    assert harness.inverse_reach_heads(undo, ctx) == []
    # the same call written in the slot is a crossing
    assert harness.inverse_reach_heads(lets["t"], ctx) == ["mint"]


def test_the_lean_oracle_agrees_on_the_resolved_documents(harness, rows,
                                                          verdicts, tmp_path):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    keep = [r for r in rows if r.split("\t")[1:2] and (
        r.split("\t")[1] in RESOLVED or r.split("\t")[0] in ("Z", "Y"))]
    tsv_path = tmp_path / "corpus.tsv"
    tsv_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv_path, tmp_path / "formal_verdicts.tsv"))
    mine = {k: x for k, x in verdicts.g5reg.items() if k[0] in RESOLVED}
    assert mine and {k: formal.g5reg[k] for k in mine} == mine
