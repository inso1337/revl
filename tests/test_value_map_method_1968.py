"""A non-builtin method on a value `Map` is refused, and a host handle is not
a value `Map`. Issue #1968, the follow-up to #1942.

#1942 refuses a non-builtin method on a stdlib value in a component body, named
or written in place — but it left `Map` out of the refused types, on purpose:
`Map` is the one value head a HOST handle shares its name with. An activation
`let store = effect hole[Map[Str, Int]] "…" undo store.drop()` gives the handle
the static type `Map[Str, Int]` with NO host marking, so refusing its host
verbs (`drop`, `insert`, `get`) broke the effect-acquire idiom. The cost was
that `return m.frob()` on a real value `Map` was still admitted, and py emits
it verbatim: `AttributeError` at run time.

The fix tells the two apart by the BINDING rather than the type, in both
engines: `lower.Env.host_handles` records every `effect`-acquired binding whose
acquisition yields a HOST family (`_HOST_FAMILIES` ∩ `_VALUE_METHOD_HEADS` is
just `Map`), and `_refuse_value_method` stands aside for those. The self-host
gate needs no such record — it withholds the type of every crossing binding
already — so its mirror is `Map` joining `value_method_head`.

The rule is a GAP IN A RULE, not an engine divergence: reference and gate
agreed before this change and agree after it, which is what the gate assertions
below pin. `tests/test_value_method_call_1942.py` carries the rest of the
corpus and `tests/test_gate_reference_census.py` the whole-corpus census.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "value_method_call"

_SURFACE = ("charAt, charCodeAt, checked_div_euclid, checked_div_floor, "
            "checked_div_trunc, checked_mod, codepoint_at, concat, div_euclid, "
            "div_floor, div_trunc, endsWith, field, has, indexOf, is_alnum, "
            "is_alpha, is_digit, is_space, join, keys, length, list, lookup, "
            "mod, push, remove, repeat, set, size, slice, split, startsWith, "
            "str, to_int, to_int32, to_str")


def _message(method: str, recv: str) -> str:
    return (f"no builtin method `{method}` on `{recv}` — the stdlib surface is "
            f"{_SURFACE} (docs/stdlib-2.0.md)")


# The issue's own four rows, verbatim in shape: a value `Map` from a `let`, a
# value `Map` from a record field, the host-handle control, and the `List[Int]`
# control row #1942 already refused.
FROM_LET = """
service Math { fn go(n: Int) -> Int }
component C provides math: Math {
  provide math {
    fn go(n) {
      let m: Map[Str, Int] = Map.empty()
      return m.frob()
    }
  }
}
"""

FROM_RECORD_FIELD = """
type Box = { m: Map[Str, Int] }
service Math { fn go(n: Int) -> Int }
component C provides math: Math {
  provide math {
    fn go(n) {
      let b: Box = { m: Map.empty() }
      return b.m.frob()
    }
  }
}
"""

HOST_HANDLE = """
component Counter {
  let store = effect hole[Map[Str, Int]] "effect-acquire" undo store.drop()
}
"""

LIST_CONTROL = """
service Math { fn go(n: Int) -> Int }
component C provides math: Math {
  provide math {
    fn go(n) {
      let xs: List[Int] = [1, 2]
      return xs.frob()
    }
  }
}
"""


def _reference(src: str):
    """The reference's verdict: `""` when admitted, else the refusal message."""
    try:
        compile_source(src, "<1968>")
    except RevlError as exc:
        return exc.message
    return ""


@pytest.fixture(scope="module")
def gate():
    """The self-host gate's `admit_src`, built once for the module."""
    module = load_by_path(
        "gate_reference_census", ROOT / "tools" / "gate_reference_census.py")
    return module.SelfhostEngine()


def _gate(gate, src: str):
    """The gate's verdict in the reference's terms: `""` when admitted, else
    the refusal message. The two engines report the same text."""
    verdict = next(gate.verdicts([src]))
    if verdict[0] in ("no_objection", "admitted"):
        return ""
    if verdict[0] == "refused":
        return verdict[1][1]
    raise AssertionError(f"the gate answered {verdict!r}, not a verdict")


# --- the issue's four rows -------------------------------------------------

def test_a_non_builtin_method_on_a_value_map_from_a_let_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(FROM_LET, "<1968>")
    assert excinfo.value.code == "T1"
    assert excinfo.value.message == _message("frob", "Map[Str, Int]")
    assert excinfo.value.hint == (
        "records carry data, not methods; call functions as `f(x)` (G6)")


def test_a_non_builtin_method_on_a_value_map_from_a_record_field_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(FROM_RECORD_FIELD, "<1968>")
    assert excinfo.value.code == "T1"
    assert excinfo.value.message == _message("frob", "Map[Str, Int]")


def test_the_list_control_row_is_refused_as_it_was():
    with pytest.raises(RevlError) as excinfo:
        compile_source(LIST_CONTROL, "<1968>")
    assert excinfo.value.message == _message("frob", "List[Int]")


@pytest.mark.parametrize("row", ["from_let", "from_record_field", "list_control"])
def test_the_gate_refuses_the_same_rows_with_the_same_message(gate, row):
    """The gate and the reference are pinned as AGREEING: this was a gap in a
    rule, not a divergence between engines, and it must not become one."""
    src = {"from_let": FROM_LET, "from_record_field": FROM_RECORD_FIELD,
           "list_control": LIST_CONTROL}[row]
    assert _gate(gate, src) == _reference(src) != ""


# --- the over-correction guard: the effect-acquire idiom stays admitted -----

HANDLE_WITH_VERBS = """
component Counter {
  let store = effect hole[Map[Str, Int]] "effect-acquire" undo store.drop()
  effect store.insert("k", 1) undo store.remove("k")
}
"""

HANDLE_WITH_VERBS_FROM_PROVIDE = """
service Health { fn status() -> Int }
component Counter provides health: Health {
  let store = effect hole[Map[Str, Int]] "effect-acquire" undo store.drop()
  provide health {
    fn status() {
      effect store.insert("k", 1) undo store.remove("k")
      return store.has("k") ? 1 : 0
    }
  }
}
"""

VALUE_MAP_INSERT = """
service Health { fn status() -> Int }
component Counter provides health: Health {
  provide health {
    fn status() {
      let m: Map[Str, Int] = Map.empty()
      return m.insert("k", 1) ? 1 : 0
    }
  }
}
"""

VALUE_MAP_BUILTIN = """
service Health { fn status() -> Int }
component Counter provides health: Health {
  provide health {
    fn status() {
      let m: Map[Str, Int] = Map.empty()
      return m.has("k") ? 1 : 0
    }
  }
}
"""


@pytest.mark.parametrize("src", [HOST_HANDLE, HANDLE_WITH_VERBS,
                                 HANDLE_WITH_VERBS_FROM_PROVIDE],
                         ids=["acquire", "acquire-and-host-verbs",
                              "host-verb-from-a-provide-method"])
@pytest.mark.parametrize("engine", ["reference", "gate"])
def test_a_host_handle_is_not_a_value_map(gate, src, engine):
    """The guard the whole issue turns on. A handle typed `Map[Str, Int]` by a
    typed hole has no other evidence it is a handle; `drop`, `insert` and
    `remove` are not in the builtin table, so refusing every `Map` receiver
    refuses this idiom. It is admitted, by both engines."""
    verdict = _reference(src) if engine == "reference" else _gate(gate, src)
    assert verdict == ""


def test_a_host_verb_is_read_by_the_binding_not_by_the_type(gate):
    """The sharpest statement of the issue, and its whole point: `insert` is
    the HOST spelling of a `Map` write and is not in the builtin table. On a
    handle it is the acquire idiom and stays admitted; on a value `Map` it is a
    non-builtin method and is refused. Same receiver type in both, so only the
    binding — `Env.host_handles` — can tell them apart, and the gate, which
    withholds a crossing binding's type instead, reaches the same answer."""
    assert _reference(VALUE_MAP_INSERT) == _gate(gate, VALUE_MAP_INSERT) == (
        _message("insert", "Map[Str, Int]"))
    assert _reference(HANDLE_WITH_VERBS_FROM_PROVIDE) == ""
    assert _gate(gate, HANDLE_WITH_VERBS_FROM_PROVIDE) == ""


def test_a_builtin_method_on_a_value_map_is_still_admitted(gate):
    """The other side of the guard: joining the refused heads refuses only the
    methods that are NOT builtins, so `has` on the same value `Map` is
    untouched."""
    assert _reference(VALUE_MAP_BUILTIN) == _gate(gate, VALUE_MAP_BUILTIN) == ""


# --- the corpus -------------------------------------------------------------

def test_the_corpus_carries_the_value_maps_and_the_handle(gate):
    """The fixtures this issue added or retitled, and the guard beside them. A
    `t1_` fixture is refused by BOTH engines with the same message; an `ok_`
    one is admitted by both — the corpus is the cross-engine pin."""
    refused = {
        "t1_named_map": ("frob", "Map[Str, Int]"),
        "t1_record_map_field": ("frob", "Map[Str, Int]"),
        "t1_inplace_map": ("frob", "Map[Str, Int]"),
    }
    for stem, (method, recv) in refused.items():
        src = (CORPUS / f"{stem}.rvl").read_text()
        assert _reference(src) == _gate(gate, src) == _message(method, recv), stem
    for stem in ("ok_typed_host_acquisition", "ok_host_handle_verbs"):
        src = (CORPUS / f"{stem}.rvl").read_text()
        assert _reference(src) == _gate(gate, src) == "", stem
