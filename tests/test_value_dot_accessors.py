"""Value dot-method accessor syntax (roadmap item 189, DECIDED option A).

`node.field("x").str()` is receiver-first sugar for the free-function
`value_*` accessors (item 180, stdlib/value.rvl). The evidence (item 185
dogfood): `value_str(value_field(n, k))` at ~50 sites reads inside-out and
buries the field name; a deep chain inverts worse
(`value_str(value_field(value_field(node, "callee"), "name"))` vs the
left-to-right `node.field("callee").field("name").str()`).

Decision: add the accessors through the existing `_BUILTIN_METHODS`
(lower.py) / `_BUILTIN_SIG` (typecheck.py) machinery — builtin methods like
`.len()`/`.charAt()`, NO new IR expr-kind. The accessor set is:

  * `.field(k) -> Value`   == value_field(recv, k)
  * `.str()    -> Str`     == value_str(recv)
  * `.list()   -> List[Value]` == value_list(recv)
  * `.keys()   -> List[Str]`   == value_keys(recv)

Each dot-method lowers to the SAME IR the `value_*` free-function form
produces (a plain call node), so it is PURE SUGAR: the emitted code is
byte-identical to the nested free-function spelling, on every tier value.rvl
runs on (py + ts). The proof below asserts that byte-identity directly.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "value.rvl"

_USE = (
    'use "stdlib/value.rvl" '
    "{ value_field, value_str, value_list, value_keys }\n"
)

# A deep chain in both spellings — the item-189 headline example. The bodies
# must lower to the SAME IR and therefore emit byte-identically.
DOT_FORM = _USE + """\
fn deep(n: Value) -> Str { return n.field("callee").field("name").str() }
fn kids(n: Value) -> List[Value] { return n.field("items").list() }
fn names(n: Value) -> List[Str] { return n.field("props").keys() }
"""

FREE_FORM = _USE + """\
fn deep(n: Value) -> Str { return value_str(value_field(value_field(n, "callee"), "name")) }
fn kids(n: Value) -> List[Value] { return value_list(value_field(n, "items")) }
fn names(n: Value) -> List[Str] { return value_keys(value_field(n, "props")) }
"""


def _compile(src: str, tmp_path_factory):
    d = tmp_path_factory.mktemp("dot")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "value.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                            encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(src, encoding="utf-8")
    return compile_files([str(main)])


def _emit(backend: str, ir: dict) -> str:
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def test_dot_form_parses_and_lowers(tmp_path_factory):
    ir = _compile(DOT_FORM, tmp_path_factory)
    assert {f["name"] for f in ir["functions"]} >= {"deep", "kids", "names"}


@pytest.mark.parametrize("backend", ["python", "typescript"])
def test_dot_form_byte_identical_to_free_function(backend, tmp_path_factory):
    dot = _emit(backend, _compile(DOT_FORM, tmp_path_factory))
    free = _emit(backend, _compile(FREE_FORM, tmp_path_factory))
    assert dot == free


def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "emit_python_run", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "value.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_dot_form_runs_on_py_tier(tmp_path_factory):
    ns = _exec_python(_compile(DOT_FORM, tmp_path_factory))
    doc = {"callee": {"name": "handler"},
           "items": [1, 2, 3],
           "props": {"a": 1, "b": 2}}
    assert ns["deep"](doc) == "handler"
    assert ns["kids"](doc) == [1, 2, 3]
    assert ns["names"](doc) == ["a", "b"]
    # totality carries through the sugar: a shape lacking the path never faults
    assert ns["deep"]({}) == ""
    assert ns["kids"]({}) == []
    assert ns["names"]({}) == []
