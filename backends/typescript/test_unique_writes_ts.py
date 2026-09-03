"""The self-rebind (unique-ownership) lowering on the cordis-ts tier
(roadmap item 435 (d), on item 445's frontend marker).

`xs = xs.push(v)` renders `[...xs, v]`, a whole copy of the receiver, so the
ordinary `var out = []; while (..) { out = out.push(..) }` loop is EMITTED
O(n^2) where the developer wrote O(n) — measured exactly by
`bench/codegen/typescript`, which counts the elements the spread copies:
79,800 at n=400 against a hand-written `xs.push(v)`'s none.

The rewrite is `out.push(v)`, and every test below is about the PROOF that the
copy was unobservable rather than about the number. THIS TIER DOES NOT CARRY
THAT PROOF: it reads `"unique": True` off the `assign` step, which the frontend
(`src/revl/ownership.py`) stamps once for every tier, flow-sensitively, per
write. So what is pinned here is the LOWERING and its conservative fallback —
that an unmarked write keeps the copying form, which is always correct — plus
the two markers this tier deliberately declines.

Value correctness is asserted by EXECUTING the emitted module, in
`tests/unique_writes.test.ts`; these checks are toolchain-free.

Run with:
    .venv/bin/pytest backends/typescript/test_unique_writes_ts.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_unique", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EMIT = _load_ts_emit()


def emit_ts(source: str) -> str:
    return _EMIT.emit(compile_source(source, "<test>"))


# ---------------------------------------------------------------------------
# the shapes that DO qualify: born here, never reachable through a second name
# ---------------------------------------------------------------------------

def test_push_loop_becomes_a_destructive_push():
    src = emit_ts("""
fn build(n: Int) -> List[Int] {
  var out = []
  var i = 0
  while (i < n) { out = out.push(i) i = i + 1 }
  return out
}
""")
    assert "out.push(i)" in src
    assert "[...out, i]" not in src


def test_map_set_loses_its_copying_iife():
    src = emit_ts("""
fn tally(keys: List[Str]) -> Map[Str, Int] {
  var m = Map.empty()
  for (k of keys) { m = m.set(k, 1) }
  return m
}
""")
    assert "m.set(k, 1n)" in src
    assert "new Map(m)" not in src


def test_map_remove_becomes_delete():
    """`Map.remove` is TOTAL — an absent key is not an error — and JS
    `Map.delete` answers a discarded boolean rather than throwing."""
    src = emit_ts("""
fn without(keys: List[Str], gone: Str) -> Map[Str, Int] {
  var m = Map.empty()
  for (k of keys) { m = m.set(k, 1) }
  m = m.remove(gone)
  return m
}
""")
    assert "m.delete(gone)" in src


def test_record_update_becomes_one_object_assign():
    """`Object.assign` builds the whole override object BEFORE writing any of
    it, so `{ p | x = p.y, y = p.x }` stays the simultaneous swap the spread
    was — a field-at-a-time write would read back what it just stored."""
    src = emit_ts("""
type Pt = { x: Int, y: Int }
fn swaps(n: Int) -> Pt {
  var p = { x: 1, y: 2 }
  var i = 0
  while (i < n) { p = { p | x = p.y, y = p.x } i = i + 1 }
  return p
}
""")
    assert "Object.assign(p, { x: p.y, y: p.x })" in src
    assert "{ ...p," not in src


def test_a_rebirth_from_a_fresh_literal_reopens_the_write():
    """Item 445 (a), the flow-sensitive half: `res` escapes into `out` at the
    end of each outer pass, but the next pass re-declares it from a fresh
    literal, so the escaped object can never reach a later write."""
    src = emit_ts("""
fn chunks(n: Int) -> List[List[Int]] {
  var out = []
  var i = 0
  while (i < n) {
    var res = []
    var j = 0
    while (j < 2) { res = res.push(j) j = j + 1 }
    out = out.push(res)
    i = i + 1
  }
  return out
}
""")
    assert "res.push(j)" in src
    assert "[...res, j]" not in src


# ---------------------------------------------------------------------------
# the shapes that do NOT: every one keeps the copying form
# ---------------------------------------------------------------------------

def test_a_receiver_handed_to_another_list_keeps_the_spread():
    """`out.push(xs)` retains `xs`, so the write after it would be observed by
    everything already in `out`."""
    src = emit_ts("""
fn snapshots(n: Int) -> List[List[Int]] {
  var xs = []
  var out = []
  var i = 0
  while (i < n) { out = out.push(xs) xs = xs.push(i) i = i + 1 }
  return out
}
""")
    assert "[...xs, i]" in src
    assert "xs.push(i)" not in src
    # ...and `out` itself is still owned, so the marker is per NAME
    assert "out.push(xs)" in src


def test_a_binding_born_off_a_parameter_keeps_the_spread():
    """The frontend marks this `unique: "copy"` — owned only if the tier
    materialises a defensive copy at the birth. This tier does not: the
    `unique_birth` shape `"Map"` covers both a revl `Map` (a JS `Map` here) and
    a record (a plain object here), so the copy has no single spelling and the
    marker is declined. Declining it keeps the copying form, which is correct."""
    src = emit_ts("""
fn appended(xs: List[Int], v: Int) -> List[Int] {
  var ys = xs
  ys = ys.push(v)
  return ys
}
""")
    assert "[...ys, v]" in src
    assert "ys.push(v)" not in src
    # and no birth copy was materialised either — the `let` is untouched
    assert "let ys = xs" in src


def test_a_receiver_handed_to_an_unsummarised_call_keeps_the_spread():
    src = emit_ts("""
extern pure fn sink(xs: List[Int]) -> Int = @ts { return 0n }
fn build(n: Int) -> List[Int] {
  var out = []
  var i = 0
  while (i < n) { let seen = sink(out) out = out.push(i + seen) i = i + 1 }
  return out
}
""")
    assert "out.push(" not in src


def test_a_captured_receiver_keeps_the_spread():
    src = emit_ts("""
fn build(n: Int) -> List[Int] {
  var out = []
  var i = 0
  while (i < n) {
    let f = () => out
    out = out.push(i + f().length())
    i = i + 1
  }
  return out
}
""")
    assert "out.push(" not in src


def test_concat_is_never_rewritten():
    """`concat` is defined on both Str and List, the receiver type is not known
    at that node, and a JS string cannot be mutated at all — so the frontend
    never marks it and nothing here has to know why."""
    src = emit_ts("""
fn joined(xs: List[Int], ys: List[Int]) -> List[Int] {
  var out = xs
  out = out.concat(ys)
  return out
}
""")
    assert "out = out.concat(ys)" in src


def test_an_unmarked_ir_is_emitted_exactly_as_before():
    """The lowering is gated on the marker and on nothing else, so stripping it
    from a lowered document restores the pre-445 output byte for byte."""
    source = """
fn build(n: Int) -> List[Int] {
  var out = []
  var i = 0
  while (i < n) { out = out.push(i) i = i + 1 }
  return out
}
"""
    ir = compile_source(source, "<test>")

    def strip(node):
        if isinstance(node, list):
            for item in node:
                strip(item)
        elif isinstance(node, dict):
            node.pop("unique", None)
            node.pop("unique_birth", None)
            for child in node.values():
                strip(child)

    marked = _EMIT.emit(ir)
    strip(ir)
    assert _EMIT.emit(ir) != marked
    assert "out = [...out, i]" in _EMIT.emit(ir)


def test_the_marker_alone_does_not_arm_an_unmodelled_value():
    """The lowering asks for BOTH the marker and a shape it can render. A
    `unique` write whose value is not one of the four is emitted normally
    rather than guessed at."""
    node = {"step": "assign", "name": "out", "unique": True,
            "value": {"kind": "builtin", "method": "concat",
                      "target": {"kind": "var", "name": "out"},
                      "args": [{"kind": "var", "name": "ys"}]}}
    assert _EMIT._ts_inplace_write(node) is None
    # ...and so is a `push` whose argument count does not match
    node["value"] = {"kind": "builtin", "method": "push",
                     "target": {"kind": "var", "name": "out"}, "args": []}
    assert _EMIT._ts_inplace_write(node) is None
    # ...and `"copy"`, which this tier declines
    node["unique"] = "copy"
    node["value"] = {"kind": "builtin", "method": "push",
                     "target": {"kind": "var", "name": "out"},
                     "args": [{"kind": "var", "name": "v"}]}
    assert _EMIT._ts_inplace_write(node) is None
