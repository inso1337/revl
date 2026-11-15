"""issue #2975: a lifecycle test binding named like the driver's own local.

The emitted lifecycle driver binds names of its own: the locals `root`,
`events` and `baseline` on the python tier, and the `Context` import that
`_run` reads on its first line. A `let` of any of those spellings was emitted
bare, so the driver and the binding were one name:

  * python silently rebound the driver's context and the NEXT step died inside
    the runtime (`'str' object has no attribute 'get'`), naming a runtime member
    rather than the binding. A `let Context` was assigned inside `_run`, so it
    shadowed the enclosing import and the first line raised UnboundLocalError;
  * typescript emitted a second `const root`, the generated suite did not parse,
    and `revl test` reported `Tests no tests`, the shape a reader is most
    likely to read as "nothing to run".

Every driver name now lives in a namespace a revl binding cannot enter:
python's `_revl*` (which `_ident` refuses by name) and typescript's `$revl*`
(`$` is not in revl's identifier alphabet, the same collision-proof convention
as the assert temporaries `$revl_l` / `$revl_match_N`). This file pins the two
halves the issue asks for:

  * the measured names emit, with the author's own spelling untouched;
  * the driver's bound names are DERIVED from the emitted source (locals,
    imports and function definitions) and asserted to stay in that namespace,
    so a name added to a preamble tomorrow fails here instead of a consumer.

The real-runtime proof lives beside the other tier runners:
``test_lifecycle_exec.py`` (python) and ``test_lifecycle_cross_tier.py`` (ts).
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

EMIT_PY = backend_emitter("python")
EMIT_TS = backend_emitter("typescript")

# The names the issue measured, one binding per program. `Context` is the
# python driver's import; typescript already renames a user `Context`
# (`EMITTER_RESERVED`), so only the python tier enumerates it.
LOCAL_NAMES = ("root", "baseline", "events")
PY_LOCAL_NAMES = LOCAL_NAMES + ("Context",)

BASE = """service Box { fn label() -> Str }

component BoxC provides box: Box {
  provide box {
    fn label() = "b"
  }
}
"""


def _program(name: str) -> str:
    return BASE + f"""
lifecycle test "bind {name}" {{
  load BoxC
  let {name} = call box.label()
  assert {name} == "b"
  let again = call box.label()
  assert again == "b"
  unload BoxC
  assert no_residue
}}
"""


def _body_program(body: str) -> str:
    """A lifecycle test with no user `let`, so every name the emitted test
    function binds is the driver's own."""
    return BASE + f"""
lifecycle test "preamble" {{
  {body}
}}
"""


def _emitted(backend: str, source: str) -> str:
    module = EMIT_PY if backend == "python" else EMIT_TS
    return module.emit(compile_source(source))


# ----------------------------------------------------- the measured programs

@pytest.mark.parametrize("name", PY_LOCAL_NAMES)
def test_python_keeps_the_user_binding_and_reserves_the_driver(name: str):
    """Before the fix the driver's names shadowed these bindings; now the
    author's spelling is emitted untouched and every driver reference reads
    its own `_revl_` name."""
    source = _emitted("python", _program(name))
    assert f"{name} = await _revl_call(_revl_root," in source
    assert "again = await _revl_call(_revl_root," in source
    assert "from cordis import Context as _revl_Context" in source
    assert "_revl_root = _revl_Context()" in source
    assert (
        "_revl_no_residue(_revl_root, _revl_baseline, _revl_events,"
        in source
    )
    compile(source, f"<2975:{name}>", "exec")


@pytest.mark.parametrize("name", LOCAL_NAMES)
def test_typescript_keeps_the_user_binding_and_reserves_the_driver(name: str):
    """Before the fix `const root = new Context()` and the user's `const root`
    were two declarations, so the file did not parse."""
    source = _emitted("typescript", _program(name))
    assert f"const {name} = $revl_root.box.label()" in source
    assert "const $revl_root = new Context()" in source
    assert "$revl_assertNoResidue($revl_root, $revl_baseline)" in source


# ------------------------------------------- derived: a new local must show here

def _python_test_function(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    (fn,) = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    return fn


#: Function definitions the driver emits for itself. `_run` is in
#: `_EMITTER_TEMPS`, so `_ident` renames a user binding or function of that
#: spelling; it is the one def this scan sees and the one allowed bare name.
_DRIVER_DEFS = frozenset({"_run"})


def _python_bound_names(fn: ast.FunctionDef) -> set[str]:
    """Every name the emitted test function binds: locals, `import`/`from`
    aliases and nested function definitions. Import bindings were the gap that
    let the bare `Context` import through this scan."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if node is fn:
            # the emitted test entry point is not a driver binding
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _typescript_it_body(source: str) -> str:
    start = source.index('it("')
    end = source.index("\n})", start)
    return source[start:end]


#: A declaration at the it-block's OWN indentation: the scope a lifecycle `let`
#: and the driver's `const` locals share. The body sits one level inside
#: `_revl_test_context(async () => {`, so its top-level lines carry four spaces;
#: a local declared inside a nested load block carries six and is block-scoped,
#: so it cannot collide with a binding of the test body. `var`, a declaration
#: without an initializer, and a function/class declaration are matched too, so
#: a future driver local in any of those forms is caught rather than skipped.
_TS_TOP_DECL = re.compile(
    r"^    (?:const|let|var|function|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
    re.MULTILINE)

#: A destructured declaration at the same scope binds names the pattern above
#: cannot see; the driver has no reason to introduce one, so its presence is a
#: failure rather than something to interpret.
_TS_TOP_DESTRUCTURE = re.compile(
    r"^    (?:const|let|var)\s*[\[{]", re.MULTILINE)


@pytest.mark.parametrize("body", [
    "load BoxC unload BoxC assert no_residue",
    "load BoxC abort assert no_residue",
])
def test_python_driver_binds_only_the_reserved_namespace(body: str):
    """Derived from the EMITTED driver, not from a hand-kept list: every name
    it binds (including a step's own loop targets) must be `_revl`-prefixed, so
    a future preamble local cannot reopen the collision."""
    fn = _python_test_function(EMIT_PY.emit(compile_source(_body_program(body))))
    bound = _python_bound_names(fn)
    assert bound, "the lifecycle driver binds nothing? the scan is wrong"
    bare = sorted(
        name for name in bound
        if name not in _DRIVER_DEFS and not name.startswith("_revl"))
    assert not bare, (
        f"lifecycle driver binds user-spellable name(s) {bare}; prefix each "
        "with `_revl` so a revl `let` cannot reach it (issue #2975)")


@pytest.mark.parametrize("body", [
    "load BoxC unload BoxC assert no_residue",
    "load BoxC abort assert no_residue",
])
def test_typescript_driver_binds_only_unspellable_names(body: str):
    """Same derivation on the ts tier: a top-level declaration in the test body
    must carry the `$revl_` sigil, which revl's identifier alphabet cannot
    spell."""
    source = EMIT_TS.emit(compile_source(_body_program(body)))
    body_source = _typescript_it_body(source)
    assert not _TS_TOP_DESTRUCTURE.search(body_source), (
        "the lifecycle driver destructures at the test-body scope; the "
        "derived scan cannot read those names (issue #2975)")
    declared = set(_TS_TOP_DECL.findall(body_source))
    assert declared, "the lifecycle driver declares nothing? the scan is wrong"
    bare = sorted(name for name in declared if not name.startswith("$revl_"))
    assert not bare, (
        f"lifecycle driver declares user-spellable name(s) {bare}; use the "
        "`$revl_` sigil (issue #2975)")


# ------------------------------------------------------- the python refusal

def test_python_refuses_a_binding_that_enters_the_reserved_namespace():
    """A `_revl*` binding cannot be renamed into safety on this tier (the
    rung would still be inside the reserved namespace), so it is refused by
    name, a clean diagnostic instead of a runtime AttributeError."""
    with pytest.raises(EMIT_PY.EmitError) as excinfo:
        EMIT_PY.emit(compile_source(_program("_revl_root")))
    assert "_revl_root" in str(excinfo.value)
    assert "lifecycle binding" in str(excinfo.value)
