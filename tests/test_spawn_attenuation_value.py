"""Spawn attenuation reads every crossing a child makes, in every position.

Issue #1562. A spawn may narrow a child's capabilities, never widen them, and
the check (`_check_spawn_attenuation` in `lower.py`) bounds the child by the
boundaries its code reaches. That reach was read off `emit` STEPS alone, so a
child that crossed `kv_b` as a value (`let r = emit kv_b.write_b(..)`, a
`return`, an expression body, an `if` arm, a plain call's argument, a
compensation, a host extern, its own spawn handle) was spawned by a parent
holding only `kv_a`. The reach now reads value-position crossings through the
same resolver the marker and the approval floor use (`_emit_step_caps_pairs`),
not a walker of its own.

tests/fixtures/spawn_attenuation_value/ holds one document per spelling and its
control: `g4_` is refused because the parent lacks the boundary, `ok_` is the
same child under a parent that holds it, so a fix that works by refusing the
spelling shows up. Every `g4_` document was admitted on the base. The census
holds the self-host gate to the same verdicts
(tests/test_gate_reference_census.py).
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "spawn_attenuation_value"
SPELLINGS = ("argument", "compensate", "expression_body", "host_extern",
             "if_arm", "let", "return", "spawn_handle")

_TAIL = " — a spawn may narrow a child's capabilities, never widen them"
_KV_B = ("`Supervisor` spawns `Leaker`, granting it `kv_b`, "
         "but `Supervisor` holds only `kv_a`" + _TAIL)
_HOST = ("`Supervisor` spawns `Leaker`, granting it an unnameable host "
         "boundary, but `Supervisor` holds only `kv_a`" + _TAIL)
REFUSAL = {
    "argument": _KV_B,
    "compensate": ("`Supervisor` spawns `Leaker`, granting it `kv_b`, "
                   "but `Supervisor` holds only `audit`, `kv_a`" + _TAIL),
    "expression_body": _KV_B,
    "host_extern": _HOST,
    "if_arm": _KV_B,
    "let": _KV_B,
    "return": _KV_B,
    "spawn_handle": _HOST,
}


def test_the_corpus_is_one_refusal_and_one_control_per_spelling():
    names = sorted(p.stem for p in CORPUS.glob("*.rvl"))
    want = sorted([f"g4_{s}" for s in SPELLINGS] + [f"ok_{s}" for s in SPELLINGS])
    assert names == want


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_child_crossing_outside_its_parent_is_refused(spelling):
    src = (CORPUS / f"g4_{spelling}.rvl").read_text()
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, f"g4_{spelling}.rvl")
    assert excinfo.value.message == REFUSAL[spelling]
    assert len(getattr(excinfo.value, "errors", [excinfo.value])) == 1


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_same_child_under_a_parent_that_holds_it_is_admitted(spelling):
    src = (CORPUS / f"ok_{spelling}.rvl").read_text()
    compile_source(src, f"ok_{spelling}.rvl")


def test_the_value_form_reaches_what_the_statement_form_reaches():
    """The two spellings of one crossing grant the child the same boundary."""
    head = ("service StoreA {\n  emission[kv_a] fn write_a(row: Str) -> Int\n}\n"
            "service StoreB {\n  emission[kv_b] fn write_b(row: Str) -> Int\n}\n"
            "service Task {\n  emission fn go() -> Int\n}\n"
            "component Leaker requires kv_b: StoreB provides task: Task {\n"
            "  provide task {\n    fn go() {\n")
    tail = ("    }\n  }\n}\n"
            "component Supervisor requires kv_a: StoreA {\n"
            "  let l = effect spawn Leaker with { } undo l.dispose()\n}\n")
    messages = []
    for body in ('      emit kv_b.write_b("x")\n      return 0\n',
                 '      let r = emit kv_b.write_b("x")\n      return r\n'):
        with pytest.raises(RevlError) as excinfo:
            compile_source(head + body + tail, "t.rvl")
        messages.append(excinfo.value.message)
    assert messages == [_KV_B, _KV_B]
