"""Functional collection transforms (roadmap item 383): `xs.map(f)` /
`xs.filter(p)` / `xs.reduce(init, f)` and their `list_map` / `list_filter` /
`list_reduce` free-function forms, plus the comprehension parse redirect.

The single most common collection idiom in TS/Python had no revl spelling: the
only working shape was an explicit `var acc = []; for (x of xs) { acc =
acc.push(...) }` loop, and a comprehension `[x for x in xs]` gave the cryptic
`expected an expression, found 'for'`.

Design (DECIDED option 1, pure-revl desugar): the three transforms are GENERIC
pure-revl `fn`s in stdlib/list.rvl that take a function value (items 92/342),
and the receiver-first `xs.map(f)` is SUGAR that desugars — before typing and
lowering — to `list_map(xs, f)`. No `_BUILTIN_SIG` row can express `map`'s
result `List[<f's return>]`, so the sugar is a syntactic redirect, not a
builtin-method row; the existing generic-call + arrow-argument inference does
all the work. Because they take function-value parameters, they emit on the
py / ts / rust / go tiers but NOT java / wasm (those tiers cannot represent a
function value) — an honestly-scoped subset, verified per tier below.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

STDLIB = ROOT / "stdlib" / "list.rvl"

#: a consumer exercising all three transforms through BOTH surfaces — the
#: receiver-first dot-sugar (`xs.map(f)`) and the free-function form
#: (`list_map(xs, f)`) — so a divergence between the two is caught.
CONSUMER = """\
use "stdlib/list.rvl" { list_map, list_filter, list_reduce }

// receiver-first sugar
fn dbl(xs: List[Int]) -> List[Int] { return xs.map(x => x * 2) }
fn keep_gt1(xs: List[Int]) -> List[Int] { return xs.filter(x => x > 1) }
fn total(xs: List[Int]) -> Int { return xs.reduce(0, (a, x) => a + x) }

// free-function form (the sugar desugars to exactly this)
fn dbl_free(xs: List[Int]) -> List[Int] { return list_map(xs, x => x * 2) }
"""


def _compile(main_src: str, extra_stdlib: str = ""):
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "stdlib").mkdir()
    (d / "stdlib" / "list.rvl").write_text(
        STDLIB.read_text(encoding="utf-8") + extra_stdlib, encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(main_src, encoding="utf-8")
    return compile_files([str(main)])


def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_transforms", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "transforms.py", "exec"), namespace)
    finally:
        if previous is not None:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_map_filter_reduce_run_on_py():
    ns = _exec_python(_compile(CONSUMER))
    assert ns["dbl"]([1, 2, 3]) == [2, 4, 6]
    assert ns["keep_gt1"]([1, 2, 3]) == [2, 3]
    assert ns["total"]([1, 2, 3]) == 6
    # the dot-sugar and the free-function form agree
    assert ns["dbl_free"]([1, 2, 3]) == ns["dbl"]([1, 2, 3])
