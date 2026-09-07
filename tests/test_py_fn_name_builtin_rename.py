"""A top-level `fn` whose name is a Python builtin the cordis-py backend emits
BARE must not shadow the emitter's own use of that builtin.

The backend lowers a handful of language constructs to unqualified Python
builtins: `.length()` -> `len(...)`, `${x}` interpolation and `.to_str()` ->
`str(...)`, `.sort()` -> `sorted(...)`, and so on. Those calls are literal text
in the emitted module, resolved through the module globals. A user program is
free to declare `fn len(...)`, and the checker accepts it — but a plain
`def len` at module scope would REBIND the global the emitter is relying on, so
`s.length()` would quietly call the user function instead of counting code
points, with nothing failing until a downstream value came out wrong.

`backends/python/emit.py::_mangle` closes this the same injective append-`_`
way it already renames a user identifier that is a Python keyword: a name that
collides with one of the builtins the backend emits bare (`_EMITTED_BUILTINS`)
is renamed at its declaration AND at every use, so the emitter's own bare
`len(...)` keeps resolving to the builtin. A name that collides with neither a
keyword nor such a builtin is emitted byte-for-byte unchanged.

The rename is for names that become a real Python BINDING (a module-global
`def`/`class`, or a function-local). A provide-method name is dispatched by its
service-contract name and a record field's values are dict-keyed by the raw
name, so those keep their spelling (they cannot shadow a builtin) — covered by
the method/field cases below.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _emit(source: str) -> str:
    ir = compile_source(source, "t.rvl")
    spec = importlib.util.spec_from_file_location(
        "emit_builtin_rename", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.emit(ir))


def test_fn_named_len_runs_and_does_not_shadow_length():
    # `fn len` and `fn str` are legal revl. The emitter must render both the
    # user functions AND its own `.length()`/`${…}` lowerings so that each keeps
    # its own meaning at runtime.
    source = (
        "fn len(x: Int) -> Int { return x + 100 }\n"
        "fn str(x: Int) -> Int { return x + 200 }\n"
        "fn slen(s: Str) -> Int { return s.length() }\n"
        "fn interp(n: Int) -> Str { return `v=${n}` }\n"
        "fn call_len(x: Int) -> Int { return len(x) }\n"
    )
    code = _emit(source)

    # the colliding user fns are lifted off the builtin names ...
    assert "def len_(" in code
    assert "def str_(" in code
    assert "def len(" not in code and "def str(" not in code
    # ... while the emitter's own bare lowerings stay on the builtins
    assert "return len(s)" in code          # `.length()`
    assert "str(n)" in code                 # `${n}` interpolation of an Int

    ns: dict = {}
    exec(code, ns)  # noqa: S102 — executing the backend's own output under test
    assert ns["len_"](5) == 105             # the user fn
    assert ns["str_"](5) == 205             # the user fn
    assert ns["slen"]("hello") == 5         # builtin len, NOT the user fn (was 105)
    assert ns["interp"](7) == "v=7"         # builtin str, NOT the user fn
    assert ns["call_len"](5) == 105         # a call to the user fn still reaches it


def test_a_provide_method_named_after_a_builtin_keeps_its_contract_name():
    # A method is a class attribute dispatched by its service-contract name, so
    # it cannot shadow a module-global builtin and must NOT be renamed — the
    # emitted `def` has to match the SERVICES table key the runtime calls.
    source = (
        "service Store { fn set(k: Str, v: Int) -> Int  fn list() -> Int }\n"
        "component C provides s: Store {\n"
        "  provide s {\n"
        "    fn set(k, v) = v\n"
        "    fn list() = 0\n"
        "  }\n"
        "}\n"
    )
    code = _emit(source)
    assert "def set(self" in code and "def list(self" in code
    assert "def set_(self" not in code and "def list_(self" not in code
    # the runtime dispatch table keeps the contract spelling too
    assert "'set': {" in code and "'list': {" in code


def test_a_non_colliding_fn_name_is_left_untouched():
    # The guard is narrow: a name that is neither a keyword nor a bare-emitted
    # builtin is emitted verbatim, so an ordinary program is unaffected.
    code = _emit("fn tally(x: Int) -> Int { return x + 1 }\n")
    assert "def tally(" in code
    assert "def tally_(" not in code
