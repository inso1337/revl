"""The stdlib RENDER module (roadmap item 195, option C, stdlib/render.rvl).

The blessed threaded-accumulator combinator a self-hosted emitter reaches for
when it must thread the document-wide `$revl_match_N` counter through a run of
pure renderers in exact evaluation order. `render_seq` names that one shape
(seed a counter, walk a list left-to-right feeding each render the counter the
previous render returned, collect the fragments) so a caller writes the
sequence as one call instead of re-rolling the loop by hand. A Path B enabler
kin to stdlib/list.rvl (194) and stdlib/value.rvl (180).

Like list.rvl these are PURE revl (built on the base List surface, no `@py`),
so there is nothing to defer per tier: the module runs on every backend. The py
tier is executed here; the crux is that `render_seq` threads the counter
STRICTLY left-to-right (item i sees the counter item i-1 returned) and hands
back the counter the LAST render left, because that evaluation order is the
whole correctness property behind the emitter's gensym numbering.

Checked here:
  * the module imports through `use` and `render_seq` reaches the IR;
  * the module file is the documented surface;
  * the py tier EXECUTES a pure-revl program using ONLY the combinator;
  * threading order: a render that stamps the counter it was given reproduces
    the seed sequence exactly (seed, seed+1, ...), and the final counter is
    seed + len;
  * a render that advances the counter by more than one per step still threads
    the running total (the counter is a general Int accumulator, not a bare
    index);
  * fragments are collected in list order; edges (empty list, single item).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "render.rvl"

#: a self-hosted-emitter-shaped consumer: thread an Int counter through a run of
#: renders in evaluation order, in PURE revl, using ONLY the combinator. Each
#: helper exposes one property of `render_seq` to the py tier.
CONSUMER = """\
use "stdlib/render.rvl" { render_seq, Rendered, Threaded }

// a render that STAMPS the counter it was given into its fragment and advances
// it by one, the emitter's `$revl_match_N` shape. If `render_seq` threads in
// order, fragment i reads `seed + i`.
fn stamp(e: Str, c: Int) -> Rendered {
  return { text: `${e}${c}`, counter: c + 1 }
}

// join the stamped fragments with "," so the test reads order as one string
fn stamp_join(nodes: List[Str], seed: Int) -> Str {
  let r = render_seq(nodes, seed, (e, c) => stamp(e, c))
  return r.texts.join(",")
}

// the counter the LAST render left (seed + len when each step advances by one)
fn stamp_final(nodes: List[Str], seed: Int) -> Int {
  let r = render_seq(nodes, seed, (e, c) => stamp(e, c))
  return r.counter
}

// a render that advances the counter by the item's OWN length, proving the
// counter is a running Int accumulator threaded onward, not a bare 0..n index
fn widen(e: Str, c: Int) -> Rendered {
  return { text: `${c}`, counter: c + e.length() }
}

fn widen_join(nodes: List[Str], seed: Int) -> Str {
  let r = render_seq(nodes, seed, (e, c) => widen(e, c))
  return r.texts.join(",")
}

fn widen_final(nodes: List[Str], seed: Int) -> Int {
  let r = render_seq(nodes, seed, (e, c) => widen(e, c))
  return r.counter
}
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface)
    d = tmp_path_factory.mktemp("render_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "render.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_function_reaches_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert "render_seq" in names
    # PURE revl: no @py externs are introduced by the kit
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert ("pub fn render_seq[E](items: List[E], counter: Int, "
            "render: (E, Int) -> Rendered) -> Threaded") in text
    assert "pub type Rendered = { text: Str, counter: Int }" in text
    assert "pub type Threaded = { texts: List[Str], counter: Int }" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_render", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "render.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---- threading order: item i sees the counter item i-1 returned -------------

def test_threads_counter_left_to_right(ns):
    # each fragment stamps the counter it was handed; in evaluation order that
    # is seed, seed+1, seed+2, ..., the emitter's `$revl_match_N` numbering
    assert ns["stamp_join"](["a", "b", "c"], 5) == "a5,b6,c7"


def test_seed_offset_carries_through(ns):
    assert ns["stamp_join"](["x", "y"], 0) == "x0,y1"
    assert ns["stamp_join"](["x", "y"], 100) == "x100,y101"


def test_final_counter_is_seed_plus_len(ns):
    assert ns["stamp_final"](["a", "b", "c"], 5) == 8
    assert ns["stamp_final"](["a", "b", "c", "d"], 0) == 4


# ---- the counter is a running accumulator, not a bare 0..n index ------------

def test_variable_step_threads_running_total(ns):
    # widen advances by each item's own length; fragment i reads the running
    # total so far: 10, 10+1, 10+1+2, ...
    assert ns["widen_join"](["a", "bb", "ccc"], 10) == "10,11,13"
    assert ns["widen_final"](["a", "bb", "ccc"], 10) == 16


# ---- order preservation + edges --------------------------------------------

def test_fragments_kept_in_list_order(ns):
    assert ns["stamp_join"](["z", "y", "x"], 0) == "z0,y1,x2"


def test_empty_list_returns_seed_unchanged(ns):
    assert ns["stamp_join"]([], 7) == ""
    assert ns["stamp_final"]([], 7) == 7


def test_single_item(ns):
    assert ns["stamp_join"](["only"], 3) == "only3"
    assert ns["stamp_final"](["only"], 3) == 4
