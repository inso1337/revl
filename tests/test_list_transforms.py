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
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
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


def _emit(backend: str, ir: dict) -> str:
    path = str(ROOT / "backends" / backend)
    sys.path.insert(0, path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_{backend}_383", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = module.emit(ir)
        return "\n".join(out.values()) if isinstance(out, dict) else str(out)
    finally:
        sys.path.remove(path)


def test_map_filter_reduce_run_on_py():
    ns = _exec_python(_compile(CONSUMER))
    assert ns["dbl"]([1, 2, 3]) == [2, 4, 6]
    assert ns["keep_gt1"]([1, 2, 3]) == [2, 3]
    assert ns["total"]([1, 2, 3]) == 6
    # the dot-sugar and the free-function form agree
    assert ns["dbl_free"]([1, 2, 3]) == ns["dbl"]([1, 2, 3])


# ---------------------------------------------------------------- py + ts parity

#: everything is built and rendered INSIDE revl (list literals, the transforms,
#: `.to_str()`, `.join()`), so the proof crosses no JS/py value boundary — the
#: emitted `proof()` returns one Str both tiers can print and compare verbatim.
#: `[1,2,3].map(x => x*2)` == [2,4,6]; `.filter(x => x>1)` == [2,3];
#: `.reduce(0, (a,x) => a+x)` == 6 — the exact PROVE-IT of roadmap item 383.
_PROOF = """\
use "stdlib/list.rvl" { list_map, list_filter, list_reduce }
fn show(xs: List[Int]) -> Str { return list_map(xs, n => n.to_str()).join(",") }
pub fn proof() -> Str {
  let m = [1, 2, 3].map(x => x * 2)
  let f = [1, 2, 3].filter(x => x > 1)
  let r = [1, 2, 3].reduce(0, (a, x) => a + x)
  return show(m) + "|" + show(f) + "|" + r.to_str()
}
"""


def test_proof_py():
    ns = _exec_python(_compile(_PROOF))
    assert ns["proof"]() == "2,4,6|2,3|6"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_proof_ts_matches_py():
    """The identical source runs on the ts tier (arrows compose per item 342)
    and prints the SAME string — the map/filter/reduce trio is portable across
    the py and ts tiers, byte-for-byte on the rendered result."""
    ir = _compile(_PROOF)
    py_out = _exec_python(ir)["proof"]()
    code = _emit("typescript", ir)
    d = Path(tempfile.mkdtemp())
    (d / "runtime.ts").write_text("export const host: any = {};\n", encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    (pkg / "mod.ts").write_text(code + "\nconsole.log(proof());\n", encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        ["node", str(pkg / "mod.ts")], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    ts_out = proc.stdout.strip().splitlines()[-1]
    assert ts_out == py_out == "2,4,6|2,3|6"


# ------------------------------------------------ honest per-tier emit coverage

def test_emits_on_py_ts_rust_go():
    """The pure-revl transforms lower on the four tiers that can carry a
    function value in parameter position."""
    ir = _compile(_PROOF)
    for tier in ("python", "typescript", "rust", "go"):
        assert _emit(tier, ir), f"{tier} produced no output"


@pytest.mark.parametrize("tier", ["java", "wasm"])
def test_refused_on_java_wasm(tier):
    """java and wasm cannot represent a function value, so a program that
    reaches list_map/list_filter/list_reduce refuses at EMIT (loudly, not
    silently) on those two tiers — the honestly-scoped subset."""
    ir = _compile(_PROOF)
    with pytest.raises(Exception):
        _emit(tier, ir)


# ---------------------------------------------------------- comprehension redirect

def test_comprehension_redirect_message():
    """`[x for x in xs]` had no revl spelling and gave the cryptic `expected an
    expression, found 'for'`; item 383 redirects it to a message that names the
    missing feature and points at the two spellings that DO work."""
    with pytest.raises(RevlError) as exc:
        compile_source("fn go() -> List[Int] { return [x for x in [1, 2, 3]] }")
    assert "no list comprehensions" in str(exc.value)
    assert ".map(" in (exc.value.hint or "")
    # the old cryptic message is gone
    assert "found 'for'" not in str(exc.value)
