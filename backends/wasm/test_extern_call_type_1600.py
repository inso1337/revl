"""The type of a `@wasm` extern call is inferred like a fn call's (issue #1600).

`_V3Emitter._call_expr` resolves a callee in `fn_sigs` and then `extern_sigs`,
so a call to a `@wasm`-bodied extern lowers. `_V3Emitter._call_type`, which
answers the same call's TYPE, looked in `fn_sigs` only. So every position that
asks for the type before lowering (an index, a `let`, a builtin method's
receiver) refused a perfectly lowerable extern call with
`callee 'names' is not a lowerable function`, the sentence meant for a callee
that does not exist.

The emitted functions are run on wasmtime (the in-process python package, the
same gate the other execution tests here use), so the fix is proved by the
VALUE each body returns, not by the refusal going away.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.run_wasm import _wasm_emitter  # noqa: E402

_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")

# A three-element List[Str], every element the Str "hi" at 6100.
_NAMES = '''
extern pure fn names() -> List[Str] = @wasm {
  (i32.store (i32.const 6100) (i32.const 2))
  (i32.store8 (i32.const 6104) (i32.const 104))
  (i32.store8 (i32.const 6105) (i32.const 105))
  (i32.store (i32.const 6000) (i32.const 3))
  (i64.store (i32.const 6008) (i64.extend_i32_u (i32.const 6100)))
  (i64.store (i32.const 6016) (i64.extend_i32_u (i32.const 6100)))
  (i64.store (i32.const 6024) (i64.extend_i32_u (i32.const 6100)))
  (i32.const 6000)
}
'''

# body -> (return type, the value it must return)
_CASES = {
    "index": ("Int", "return names()[0].length()", 2),
    "let": ("Int", "let n = names()\n  return n.length()", 3),
    "let-index": ("Int", "let s = names()[1]\n  return s.length()", 2),
    "method": ("Int", "return names().length()", 3),
    "join": ("Int", 'return names().join(",").length()', 8),
}


def _module(case: str) -> str:
    ret, body, _expected = _CASES[case]
    src = _NAMES + f"\npub fn probe() -> {ret} {{\n  {body}\n}}\n"
    emit = _emitter()
    modules = emit.emit(compile_source(src, f"extern_call_type_{case}.rvl"))
    assert len(modules) == 1, sorted(modules)
    return next(iter(modules.values()))


def _emitter():
    return _wasm_emitter()


@pytest.mark.parametrize("case", sorted(_CASES))
def test_an_extern_calls_type_is_inferred(case):
    """Each spelling used to be refused by name of a callee that exists."""
    wat = _module(case)
    assert "(call $names)" in wat


def _wasmtime():
    try:
        import wasmtime  # noqa: PLC0415
    except ImportError:
        if _REQUIRE:
            pytest.fail("wasmtime is not installed and REVL_REQUIRE_WASMTIME is set")
        pytest.skip("wasmtime Python package not installed")
    return wasmtime


@pytest.mark.parametrize("case", sorted(_CASES))
def test_the_inferred_call_returns_the_right_value(case):
    wasmtime = _wasmtime()
    store = wasmtime.Store()
    module = wasmtime.Module(store.engine, _module(case))
    instance = wasmtime.Instance(store, module, [])
    assert instance.exports(store)["probe"](store) == _CASES[case][2]


def test_an_extern_without_a_wasm_body_is_still_refused_by_name():
    """The lookup widens to externs that HAVE a `@wasm` body, which is what
    `extern_sigs` holds. One carrying only another tier's body is still the
    named portability refusal, in the inferred position as in the lowered one."""
    emit = _emitter()
    src = (_NAMES.replace("extern pure fn names", "extern pure fn other_names", 1)
           .split("= @wasm")[0]
           + '= @py { return ["hi"] }\n'
           + "\npub fn probe() -> Int {\n  return other_names()[0].length()\n}\n")
    with pytest.raises(emit.EmitError) as caught:
        emit.emit(compile_source(src, "extern_call_type_bodyless.rvl"))
    assert "extern `other_names` has no @wasm body" in str(caught.value)
