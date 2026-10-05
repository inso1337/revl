"""A `verified` witnessed effect in a provide method (issue #1897, slice 3,
item 1 of the decision).

A batch write is a witnessed extern pair whose witness carries the
before-images. `verified effect` was refused in every provide-method body,
because its round trip was defined as activate-then-tear-down. The method's
own window is call-then-abort, and that is what the round-trip runner now
runs for a witnessed method effect (`fault._drive_method_roundtrip`).

Exit tests:
  * the refusal lifts for a witnessed method effect, and stays for a plain one;
  * a lifecycle test applies the batch in a method, aborts, and asserts the
    store is back and `no_residue`;
  * the round trip holds for a correct inverse and catches one that raises.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import fault as fault_mod  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the round trip runs on the live cordis-py runtime; install it with "
           "`sh backends/python/setup.sh`")

# the store is host-side state the externs share; the witness carries the
# before-image of every row the batch touches
_RESTORE = '''
extern pure fn restore_batch(w: BatchWitness) -> Unit = @py {
    store = globals().setdefault('_revl_batch_store', {})
    for entry in reversed(w['entries']):
        if entry['existed']:
            store[entry['id']] = entry['name']
        else:
            store.pop(entry['id'], None)
    return
}
'''
_BROKEN_RESTORE = '''
extern pure fn restore_batch(w: BatchWitness) -> Unit = @py {
    raise RuntimeError('restore failed')
}
'''
_BATCH = '''
type Row = { id: Str, name: Str }
type Before = { id: Str, existed: Bool, name: Str }
type BatchWitness = { entries: List[Before] }
type StoreError = { code: Str }
%(restore)s
extern witnessed[store] fn write_batch(rows: List[Row]) -> Result[BatchWitness, StoreError]
  undo restore_batch(result) = @py {
    store = globals().setdefault('_revl_batch_store', {})
    entries = []
    for row in rows:
        existed = row['id'] in store
        entries.append({'id': row['id'], 'existed': existed,
                        'name': store.get(row['id'], '')})
        store[row['id']] = row['name']
    return Ok({'entries': entries})
}

extern pure fn store_size() -> Int = @py {
    return len(globals().setdefault('_revl_batch_store', {}))
}

service Data {
  emission fn import_rows(rows: List[Row])
  fn size() -> Int
}

component Store provides data: Data {
  provide data {
    fn import_rows(rows) {
      verified effect write_batch(rows)
    }
    fn size() = store_size()
  }
}
'''
_LIFECYCLE = '''
lifecycle test "a verified batch write reverts on abort" {
  load Store
  let pre = call data.size()
  call data.import_rows([{ id: "a", name: "A" }, { id: "b", name: "B" }])
  let during = call data.size()
  assert during == pre + 2
  abort
  load Store
  let post = call data.size()
  assert post == pre
  unload Store
  assert no_residue
}
'''


def _source(restore: str = _RESTORE) -> str:
    return _BATCH % {"restore": restore}


def _method_step(ir: dict) -> dict:
    (provide,) = [s for s in ir["components"][0]["body"] if s["step"] == "provide"]
    (method,) = [m for m in provide["methods"] if m["name"] == "import_rows"]
    (step,) = method["body"]
    return step


# ------------------------------------------------ the refusal lifts


def test_a_verified_witnessed_method_effect_compiles_and_is_marked():
    step = _method_step(compile_source(_source(), "batch.rvl"))
    assert step["step"] == "effect" and step["verified"] is True
    assert step["acquire"]["name"] == "write_batch" and "undo" not in step


def test_a_verified_plain_method_effect_is_still_refused():
    src = '''
    service W { fn put(k: Str) }
    component Widget provides w: W {
      let store = effect Map.new() undo store.drop()
      provide w { fn put(k) { verified effect store.insert(k, "1") undo store.remove(k) } }
    }
    '''
    with pytest.raises(RevlError, match="only allowed on a witnessed effect"):
        compile_source(src, "plain.rvl")


def test_the_method_is_a_round_trip_unit():
    units = fault_mod.roundtrip_units(compile_source(_source(), "batch.rvl"))
    assert units == [{"component": "Store", "key": "data", "method": "import_rows",
                      "verified": ["step 1 (verified anonymous effect)"]}]


# ------------------------------------------------ the round trip


@needs_cordis
def test_the_call_then_abort_round_trip_holds_for_a_correct_inverse():
    ir = compile_source(_source(), "batch.rvl")
    failures, dossier = fault_mod.run_roundtrip_units(
        ir, fault_mod.roundtrip_units(ir), out=lambda _line: None, rounds=4)
    assert failures == 0, dossier
    (entry,) = dossier["components"]
    assert entry["method"] == "data.import_rows" and entry["status"] == "pass"


@needs_cordis
def test_the_round_trip_catches_an_inverse_that_raises():
    ir = compile_source(_source(_BROKEN_RESTORE), "broken.rvl")
    failures, dossier = fault_mod.run_roundtrip_units(
        ir, fault_mod.roundtrip_units(ir), out=lambda _line: None, rounds=4)
    assert failures == 1, dossier
    reason = dossier["components"][0]["counterexample"]["reason"]
    assert "restore-residue" in reason and "restore failed" in reason


# ------------------------------------------------ the lifecycle exit


@needs_cordis
def test_a_lifecycle_test_applies_the_batch_aborts_and_has_no_residue(tmp_path):
    path = tmp_path / "batch.rvl"
    path.write_text(_source() + _LIFECYCLE, encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PASS a verified batch write reverts on abort" in run.stdout
    assert "PASS Store data.import_rows" in run.stdout


# ------------------------------------------------ the gate corpus
#
# tests/fixtures/verified_method_effect/ is the corpus the gate census holds
# by name (`t1_` refused, `ok_` admitted). This pins the reference's verdict
# per document; tests/test_gate_reference_census.py holds the gate to it.

CORPUS = ROOT / "tests" / "fixtures" / "verified_method_effect"
_METHOD = ("`verified effect` in a provide-method body is only allowed on a "
           "witnessed effect (issue #1897)")
REFUSED = {
    "t1_plain_site_undo": (8, _METHOD),
    "t1_let_bound": (8, _METHOD),
    "t1_emit": (7, "expected `effect` after `verified`, found 'emit'"),
}


def test_the_corpus_is_the_refusals_and_the_witnessed_admission():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + ["ok_witnessed"])


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_the_reference_refuses_the_corpus_document(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source((CORPUS / f"{stem}.rvl").read_text(), f"{stem}.rvl")
    assert (excinfo.value.line, excinfo.value.message) == REFUSED[stem]


def test_the_reference_admits_the_witnessed_corpus_document():
    assert compile_source((CORPUS / "ok_witnessed.rvl").read_text(), "ok.rvl")
