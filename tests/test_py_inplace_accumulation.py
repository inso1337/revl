"""In-place accumulation on the cordis-py tier (roadmap item 436 F1).

`xs.push(v)` renders `(xs + [v])`, a whole copy of the receiver, so the
ordinary `var out = []; for (..) { out = out.push(..) }` loop is EMITTED
O(n^2) where the developer wrote O(n). `stdlib/list.rvl` writes `list_map`,
`list_filter` and `list_dedup` as push loops, so the defect is on `xs.map(f)`
and not only on hand-written loops. It is invisible to an opcode or profile
counter: `xs + [v]` is one bytecode instruction that copies `len(xs)` pointers
in C, and the audit measured the loop at 0.96x in ops against 500x in elements
copied.

The rewrite is `out.append(v)`, and every test below is about the PROOF that
the copy was unobservable rather than about the number. The emitter may write
through a local only when the object it names is reachable through no other
name: born here, and never escaped into a second holder. Each disqualifying
shape gets a test that pins BOTH the emitted spelling and the value, because a
wrong answer here is a silent aliasing bug and not a crash.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402


def emit_py(source: str) -> str:
    return backend_emitter("python").emit(compile_source(source, "<test>"))


def run(source: str) -> dict:
    namespace: dict = {}
    exec(compile(emit_py(source), "<emitted>", "exec"), namespace)
    return namespace


# ---------------------------------------------------------------------------
# the shapes that DO qualify
# ---------------------------------------------------------------------------

BUILD = """
fn build(n: Int) -> List[Int] {
  var out: List[Int] = []
  var i = 0
  while (i < n) { out = out.push(i) i += 1 }
  return out
}
"""


def test_push_loop_becomes_append():
    src = emit_py(BUILD)
    assert "out.append(i)" in src
    assert "(out + [i])" not in src
    assert run(BUILD)["build"](4) == [0, 1, 2, 3]


def test_map_set_becomes_subscript_assignment():
    source = """
fn tally(ks: List[Str]) -> Map[Str, Int] {
  var m: Map[Str, Int] = Map.empty()
  var i = 0
  for (k of ks) { m = m.set(k, i) i += 1 }
  return m
}
"""
    src = emit_py(source)
    assert "m[k] = i" in src
    assert "{**m" not in src
    assert run(source)["tally"](["a", "b"]) == {"a": 0, "b": 1}


def test_record_update_stays_simultaneous():
    """`.update(<mapping>)` builds the whole replacement BEFORE writing any of
    it, so a field swap keeps the `{**p, ..}` spread's simultaneity."""
    source = """
type P = { x: Int, y: Int }
fn swap_n(p0: P, n: Int) -> P {
  var p = { x: p0.x, y: p0.y }
  var i = 0
  while (i < n) { p = { p | x = p.y, y = p.x } i += 1 }
  return p
}
"""
    src = emit_py(source)
    assert ".update({" in src
    ns = run(source)
    assert ns["swap_n"]({"x": 1, "y": 2}, 1) == {"x": 2, "y": 1}
    assert ns["swap_n"]({"x": 1, "y": 2}, 2) == {"x": 1, "y": 2}


def test_library_push_loops_are_linear():
    """`xs.map(f)` desugars to a `list_map` written as a push loop, so the
    quadratic was in LIBRARY code and not only in hand-written loops."""
    source = """
fn list_map(xs: List[Int], f: (Int) -> Int) -> List[Int] {
  var out: List[Int] = []
  for (x of xs) { out = out.push(f(x)) }
  return out
}
fn doubled(xs: List[Int]) -> List[Int] { return xs.map(x => x * 2) }
"""
    src = emit_py(source)
    assert "out.append(f(x))" in src
    assert "(out + [" not in src
    assert run(source)["doubled"]([1, 2, 3]) == [2, 4, 6]


def test_local_born_off_a_name_takes_one_defensive_copy():
    """`var out = m` then `out = out.remove(k)`: the caller's binding must NOT
    be written through, so the local is materialised ONCE at birth: one copy
    where the persistent form made one per removal."""
    source = """
fn without(m: Map[Str, Int], ks: List[Str]) -> Map[Str, Int] {
  var out = m
  for (k of ks) { out = out.remove(k) }
  return out
}
"""
    src = emit_py(source)
    assert "out = dict(m)" in src
    assert "out.pop(k, None)" in src
    caller = {"a": 1, "b": 2, "c": 3}
    assert run(source)["without"](caller, ["a", "c"]) == {"b": 2}
    assert caller == {"a": 1, "b": 2, "c": 3}, "the caller's map was mutated"


def test_list_born_off_a_name_copies_as_a_list():
    """The container is named by the methods that rebind the local (`push` is
    List-only), so no receiver type has to be recovered."""
    source = """
fn extend(xs: List[Int], n: Int) -> List[Int] {
  var out = xs
  var i = 0
  while (i < n) { out = out.push(i) i += 1 }
  return out
}
"""
    src = emit_py(source)
    assert "out = list(xs)" in src
    caller = [9, 9]
    assert run(source)["extend"](caller, 2) == [9, 9, 0, 1]
    assert caller == [9, 9], "the caller's list was mutated"


# ---------------------------------------------------------------------------
# the shapes that must NOT qualify
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,source,call,want", [
    (
        # a second name is bound to the accumulator, so the pre-image is live
        "aliased",
        """
fn aliased(n: Int) -> Int {
  var out: List[Int] = []
  var snap: List[Int] = []
  var i = 0
  while (i < n) {
    out = out.push(i)
    if (i == 1) { snap = out }
    i += 1
  }
  return snap.length()
}
""",
        (5,), 2,
    ),
    (
        # the accumulator is handed to a call that RETURNS it, so the caller's
        # next write would be visible through what came back
        "observed",
        """
fn echo(xs: List[Int]) -> List[Int] { return xs }
fn observed(n: Int) -> Int {
  var out: List[Int] = []
  var snap: List[Int] = []
  var i = 0
  while (i < n) { out = out.push(i) snap = echo(out) i += 1 }
  return snap.length()
}
""",
        (4,), 4,
    ),
    (
        # the accumulator is stored inside another container
        "nested",
        """
fn nested(n: Int) -> Int {
  var out: List[Int] = []
  var keep: List[List[Int]] = []
  var i = 0
  while (i < n) { out = out.push(i) keep = keep.push(out) i += 1 }
  return keep[0].length()
}
""",
        (3,), 1,
    ),
    (
        # a lambda captures by default-argument, snapshotting the OBJECT
        "captured",
        """
fn apply_all(fs: List[() -> Int]) -> Int {
  var t = 0
  for (f of fs) { t = t + f() }
  return t
}
fn captured(n: Int) -> Int {
  var out: List[Int] = []
  var fs: List[() -> Int] = []
  var i = 0
  while (i < n) {
    out = out.push(i)
    fs = fs.push(() => out.length())
    i += 1
  }
  return apply_all(fs)
}
""",
        (3,), 1 + 2 + 3,
    ),
])
def test_escaping_accumulator_keeps_the_copy(name, source, call, want):
    # `out` is the escaping accumulator in every case; a sibling accumulator in
    # the same body may legitimately still be rewritten
    src = emit_py(source)
    assert "out.append(" not in src, f"{name}: the accumulator escapes, so the copy must stay"
    assert "(out + [i])" in src
    assert run(source)[name](*call) == want


def test_a_non_retaining_call_no_longer_disqualifies_the_write():
    """Roadmap item 445 (b). `sizeof` walks its argument and answers an Int; it
    keeps nothing, so handing the accumulator to it leaves no second holder.
    An intraprocedural rule could not know that and had to assume every call
    retains — which is exactly what kept `stdlib/list.rvl`'s `list_dedup`
    quadratic, since its `if (!list_contains(out, x))` hands `out` to a call one
    statement before the write. The whole-program summary answers it."""
    source = """
fn sizeof(xs: List[Int]) -> Int {
  var t = 0
  for (x of xs) { t = t + 1 + x - x }
  if (t < 0) { return 0 }
  return t
}
fn observed(n: Int) -> Int {
  var out: List[Int] = []
  var seen = 0
  var i = 0
  while (i < n) { out = out.push(i) seen = seen + sizeof(out) i += 1 }
  return seen
}
"""
    src = emit_py(source)
    assert "out.append(i)" in src
    assert "(out + [i])" not in src
    assert run(source)["observed"](4) == 10


def test_the_retention_summary_is_conservative_about_storing():
    """A parameter put into a CONTAINER is retained by the summary even when
    that container is itself local and dies with the call. Proving otherwise is
    a second analysis; the fallback here is the copying form, which is right."""
    source = """
fn stash(xs: List[Int]) -> Int {
  var keep: List[List[Int]] = []
  keep = keep.push(xs)
  return keep.length()
}
fn observed(n: Int) -> Int {
  var out: List[Int] = []
  var seen = 0
  var i = 0
  while (i < n) { out = out.push(i) seen = seen + stash(out) i += 1 }
  return seen
}
"""
    src = emit_py(source)
    assert "out.append(i)" not in src
    assert "(out + [i])" in src
    assert run(source)["observed"](4) == 4


def test_an_extern_call_always_retains():
    """An extern has no body to summarise, so every argument to one retains and
    the write it reaches keeps the copying form."""
    source = """
extern pure fn note(xs: List[Int]) -> Int = @py {
  return len(xs)
}
fn observed(n: Int) -> Int {
  var out: List[Int] = []
  var seen = 0
  var i = 0
  while (i < n) { out = out.push(i) seen = seen + note(out) i += 1 }
  return seen
}
"""
    src = emit_py(source)
    assert "out.append(i)" not in src
    assert "(out + [i])" in src


def test_a_parameter_is_never_written_through():
    """A parameter is the CALLER's object: `out = out.push(..)` on one would
    destructively update a binding this function does not own."""
    source = """
fn grow(xs: List[Int], n: Int) -> List[Int] {
  var i = 0
  var out: List[Int] = xs
  while (i < n) { out = out.push(i) i += 1 }
  return out
}
"""
    caller = [7]
    assert run(source)["grow"](caller, 2) == [7, 0, 1]
    assert caller == [7], "the caller's list was mutated"


def test_iterating_the_accumulator_keeps_the_copy():
    """`for (x of out)` holds the object for the loop's duration, so appending
    to it INSIDE the loop would extend what the loop is walking. The copying
    form iterates the pre-image and terminates; `out.append(x)` would not."""
    source = """
fn selfiter(n: Int) -> Int {
  var out: List[Int] = []
  out = out.push(1)
  out = out.push(2)
  var seen = 0
  for (x of out) { seen = seen + x  out = out.push(x) }
  return seen
}
"""
    src = emit_py(source)
    assert "(out + [x])" in src, "the push inside the loop must keep the copy"
    assert run(source)["selfiter"](3) == 3


# ---------------------------------------------------------------------------
# flow sensitivity (roadmap item 445)
# ---------------------------------------------------------------------------
# The rule the go tier (item 434) and this one (436 F1) each derived asked
# "does this name EVER escape in this body", so one escape anywhere refused
# every write. Item 445's shared frontend analysis asks it PER WRITE, over a
# forward dataflow with a fixpoint on each loop back edge, so an escape only
# reaches the writes it can actually flow to.

def test_a_write_before_the_escape_is_still_owned():
    """The `for (x of out)` below holds the object only from the loop onward,
    so the two pushes BEFORE it are on a value nothing else can reach. The
    flow-insensitive rule refused them; the value is identical either way,
    which is the point — this is precision, not semantics."""
    source = """
fn selfiter(n: Int) -> Int {
  var out: List[Int] = []
  out = out.push(1)
  out = out.push(2)
  var seen = 0
  for (x of out) { seen = seen + x }
  return seen
}
"""
    src = emit_py(source)
    assert "out.append(1)" in src and "out.append(2)" in src
    assert run(source)["selfiter"](3) == 3


def test_an_escaped_accumulator_reborn_from_a_literal_is_owned_again():
    """`stdlib/list.rvl`'s `list_sort` in miniature, and item 445 (a): the
    inner accumulator is handed out with `out = res` at the foot of the outer
    loop, but the NEXT iteration re-declares `res` from a fresh literal, so the
    escaped object can never reach a later write. A birth KILLS the escape."""
    source = """
fn insert_all(xs: List[Int]) -> List[Int] {
  var out: List[Int] = []
  for (x of xs) {
    var res: List[Int] = []
    for (y of out) { res = res.push(y) }
    res = res.push(x)
    out = res
  }
  return out
}
"""
    src = emit_py(source)
    assert "res.append(y)" in src and "res.append(x)" in src
    assert "(res + [" not in src
    assert run(source)["insert_all"]([1, 2, 3]) == [1, 2, 3]


def test_the_copying_form_rebirths_the_local():
    """A write the analysis refuses emits `out = out + [v]`, which rebinds the
    name to a BRAND-NEW container — so the name owns that outright and the next
    write may be in place. Either form leaves the local owned afterwards, which
    is what makes the per-write question well-founded."""
    source = """
fn twice(n: Int) -> Int {
  var out: List[Int] = []
  var keep: List[List[Int]] = []
  var i = 0
  while (i < n) {
    keep = keep.push(out)
    out = out.push(i)
    out = out.push(i * 10)
    i += 1
  }
  return keep[1].length() + out.length()
}
"""
    src = emit_py(source)
    assert "(out + [i])" in src, "the write after the escape must copy"
    assert "out.append(" in src, "the write after the copy owns it"
    # keep[1] is the 2-element snapshot taken at i == 1; out ends with 6
    assert run(source)["twice"](3) == 2 + 6
