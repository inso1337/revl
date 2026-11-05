"""issue #3053: lifecycle driver names a user binding could still shadow.

Follow-up to #2975 (python and typescript driver locals, PR #3037). Driver
names were still emitted bare at a scope a lifecycle `let` shares, so a binding
of that spelling broke the generated driver instead of naming the binding:

  * typescript: the driver called the runtime helpers `plug`, `snapshotRuntime`,
    `assertNoResidue` and `frameForCtx` by their bare import names, and the load
    block's fiber handle was a bare `_f`. A lifecycle `let snapshotRuntime =
    call ...` emitted `const snapshotRuntime` in the driver's scope, so the
    driver's earlier call read the binding's temporal dead zone; a `let _f` read
    inside a later load step's config was in the same dead zone;
  * go: the driver binds `revlT` (the `*testing.T` receiver), `root`, `_fibers`,
    `_revlLoadOrder` and `_revlAt` at the test function's scope, and its call
    steps write the user's binding through block locals `_svc`, `_err`, `_r` and
    `_ok`. A user `let root` emitted `var root ...` beside `root := stc.New()`
    and the package did not build; a user binding of a call-step helper was
    captured by the step's own block.

The ts driver now spells its own local `$revl_f` and imports the four runtime
helpers under the `$revl_` sigil the other driver locals already carry (#2975;
`$` is not in revl's identifier alphabet), in the reference and the self-hosted
emitter alike. Go has no such sigil (a revl identifier may start with `_`, so
`_revl*` is spellable), so the go emitter follows rust's `root` posture: the
USER name moves one rung of the injective `_safe_local` ladder and the driver
keeps the bare token. The self-hosted go emitter mirrors the reserved set.

This file pins the reference emitters and the runnable result. The self-hosted
halves live beside the other self-host oracles: ``test_selfhost_emit_ts.py``
(the ts driver's aliased imports, byte for byte) and ``test_selfhost_emit_go.py``
(the component tier's `safe_local`, byte for byte). The go function-scope names
are DERIVED from a driver-only document, so a driver local added tomorrow fails
here instead of a consumer.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

EMIT_GO = backend_emitter("go")
EMIT_TS = backend_emitter("typescript")

BASE = """service Box { fn label() -> Str }

component BoxC provides box: Box {
  provide box {
    fn label() = "b"
  }
}

service Store { fn get(key: Str) -> Opt[Str] }

component StoreC provides store: Store {
  provide store {
    fn get(key) = Some("v")
  }
}
"""

SINK = """service Sink { fn ping() -> Str }

component SinkC provides sink: Sink {
  config { msg: Str }
  provide sink {
    fn ping() = config.msg
  }
}
"""

TS_RUNTIME_NAMES = ("plug", "snapshotRuntime", "assertNoResidue", "frameForCtx")

#: The go driver's own declarations at the lifecycle test function's scope:
#: the receiver parameter and the four bare `:=` locals. Derived in
#: ``test_go_driver_scope_names_all_escape_the_user_binding`` and pinned so a
#: driver local that stops being emitted also fails, not only a new one.
GO_DRIVER_NAMES = ("revlT", "root", "_fibers", "_revlAt", "_revlLoadOrder")

#: The call step's block-scoped locals. They do not share the function's scope
#: (a user binding there is shadowed, not redeclared), but the driver writes the
#: user's binding through them (`<bind> = (_svc).Label()`), so a user binding of
#: one of these spellings made the assignment target the wrong variable.
GO_CALL_STEP_NAMES = ("_svc", "_err", "_r", "_ok")


def _ts_test(name: str) -> str:
    """A lifecycle test binding ``name`` against Box. `frameForCtx` is only
    called by the driver's `abort` step, so that program drives one."""
    steps = ("  load BoxC\n"
             f"  let {name} = call box.label()\n"
             f"  assert {name} == \"b\"\n")
    if name == "frameForCtx":
        steps += "  abort\n"
    else:
        steps += "  unload BoxC\n"
    return f"""
lifecycle test "bind {name}" {{
{steps}  assert no_residue
}}
"""


def _ts_config_test() -> str:
    """A load step whose config reads a prior binding named `_f`, the load
    block's own fiber handle before issue #3053."""
    return """
lifecycle test "bind _f from a load config" {
  load BoxC
  let _f = call box.label()
  load SinkC with { msg: _f }
  unload SinkC
  unload BoxC
  assert no_residue
}
"""


def _go_test(name: str, *, opt: bool = False) -> str:
    """A lifecycle test binding ``name``. An Opt-returning method drives the
    call step's `_r`/`_ok` arm."""
    if opt:
        return f"""
lifecycle test "bind {name}" {{
  load StoreC
  let {name} = call store.get("k")
  assert {name} == Some("v")
  unload StoreC
  assert no_residue
}}
"""
    return f"""
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


def _ts_source(source: str) -> str:
    return EMIT_TS.emit(compile_source(source))


def _go_source(source: str) -> str:
    return EMIT_GO.emit(compile_source(source))


def _driver_only_program(body: str) -> str:
    """A lifecycle test with no user `let`, so every name the emitted driver
    binds or calls is the driver's own."""
    return BASE + f"""
lifecycle test "driver only" {{
  {body}
}}
"""


# ------------------------------------------------------------ typescript

def test_ts_runtime_helpers_are_imported_under_the_unspellable_sigil():
    """Every runtime helper the driver calls is imported as `$revl_<name>` and
    the driver body never spells the bare name: `$` keeps a lifecycle `let`
    out of the driver's references (issue #3053)."""
    source = _ts_source(_driver_only_program(
        "load BoxC call box.label() abort assert no_residue"))
    imports = re.search(r"^import \{ (.*) \} from '[^']*runtime[^']*'",
                        source, re.MULTILINE)
    assert imports, "no runtime import line in the emitted module"
    imported = imports.group(1)
    for name in TS_RUNTIME_NAMES:
        assert f"{name} as $revl_{name}" in imported, (
            f"runtime helper {name!r} is not imported under the `$revl_` "
            f"sigil (issue #3053); import line: {imported}")
    # The driver body (imports excluded) must reference only the aliases.
    body = "\n".join(line for line in source.splitlines()
                     if not line.startswith("import "))
    for name in TS_RUNTIME_NAMES:
        bare = re.search(rf"(?<![\w$]){re.escape(name)}\s*\(", body)
        assert bare is None, (
            f"the lifecycle driver calls bare {name!r}; a user `let {name}` "
            f"would shadow it (issue #3053)")


def test_ts_load_config_can_read_a_binding_named_like_the_loads_fiber():
    """issue #3053: the load block's fiber handle is `$revl_f`, so the config
    expression keeps the author's `_f` and does not read its own TDZ."""
    source = _ts_source(BASE + SINK + _ts_config_test())
    assert "const _f = $revl_root.box.label()" in source
    assert "const $revl_f = $revl_plug($revl_root, SinkC, {msg: _f})" in source


def test_ts_runs_a_lifecycle_binding_named_like_each_driver_name(tmp_path):
    """The measured programs in one module: every name binds and runs. The
    author's spelling is untouched and the driver reads its `$revl_` aliases."""
    source = BASE + SINK
    for name in TS_RUNTIME_NAMES:
        source += _ts_test(name)
        assert f"const {name} = $revl_root.box.label()" in _ts_source(
            BASE + _ts_test(name))
        assert f"$revl_{name}(" in _ts_source(BASE + _ts_test(name))
    source += _ts_config_test()
    if shutil.which("node") is None or not (
            ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest"
    ).exists():
        pytest.skip("vitest is not installed in backends/typescript")
    path = tmp_path / "bind_driver_names.rvl"
    path.write_text(source, encoding="utf-8")
    result = _revl_test("--backend", "ts", str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ts] pass:" in result.stdout
    assert "vitest: 5 test(s) passed" in result.stdout


# -------------------------------------------------------------------- go

_GO_FUNC_SCOPE_SHORT = re.compile(
    r"^\t([A-Za-z_][A-Za-z0-9_]*)(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*\s*:=",
    re.MULTILINE)
_GO_FUNC_SCOPE_VAR = re.compile(
    r"^\tvar\s+([A-Za-z_][A-Za-z0-9_]*)\s", re.MULTILINE)
_GO_RECEIVER_PARAM = re.compile(
    r"^func Test\w+\(([A-Za-z_][A-Za-z0-9_]*) ", re.MULTILINE)


def _go_driver_scope_names(source: str) -> set[str]:
    """The names a driver-only lifecycle test binds at its function's own
    scope: the `*testing.T` receiver and every one-tab short/`var`
    declaration. A block-scoped driver local is covered separately by
    ``GO_CALL_STEP_NAMES``."""
    start = source.index("func Test")
    end = source.index("\n}\n", start)
    function = source[start:end]
    receiver = _GO_RECEIVER_PARAM.search(function)
    names = set(_GO_FUNC_SCOPE_SHORT.findall(function))
    names |= set(_GO_FUNC_SCOPE_VAR.findall(function))
    if receiver:
        names.add(receiver.group(1))
    return names


def test_go_driver_scope_names_all_escape_the_user_binding():
    """Derived from the EMITTED driver, not from a hand-kept list: every name
    the driver binds at the shared scope must move a user binding of that
    spelling one rung clear (`root` -> `root_`, the rust `_EMITTER_RESERVED`
    posture), so a name added to the driver tomorrow cannot reopen the
    collision."""
    driver = _go_source(_driver_only_program(
        "load BoxC call box.label() unload BoxC assert no_residue"))
    derived = _go_driver_scope_names(driver)
    assert derived, "the lifecycle driver binds nothing at its own scope? the scan is wrong"
    assert derived == set(GO_DRIVER_NAMES), (
        f"the emitted driver's function-scope names moved: derived {sorted(derived)}, "
        f"pinned {sorted(GO_DRIVER_NAMES)}; reserve the new spelling in "
        f"backends/go/emit.py::_EMITTER_RESERVED and the self-hosted "
        f"selfhost/emit_go.rvl::safe_local (issue #3053)")
    for name in derived:
        user = _go_source(BASE + _go_test(name))
        assert f"var {name}_ string" in user, (
            f"a lifecycle `let {name}` still emits the driver's bare name "
            f"(issue #3053)")
        if name == "revlT":
            assert "(revlT *testing.T)" in user
        else:
            assert f"\t{name} :=" in user


@pytest.mark.parametrize("name", GO_CALL_STEP_NAMES)
def test_go_call_step_helpers_cannot_capture_the_user_binding(name):
    """issue #3053: the call step assigns the result to the user's binding. Its
    own `_svc`/`_err`/`_r`/`_ok` block locals must not be the ones that
    assignment reaches."""
    user = _go_source(BASE + _go_test(name, opt=name in ("_r", "_ok")))
    assert f"var {name}_ string" in user or f"var {name}_ revlOptPair" in user
    # the driver keeps its own block locals, independent of the user's name
    assert "_svc, _err := stc.Service" in user


def test_go_reserved_names_stay_distinct_from_their_escaped_twins():
    """The reserved set moves `root` to `root_`; the equally legal revl name
    `root_` must move one more rung, not collide with the escaped `root`."""
    source = _go_source(BASE + """
lifecycle test "root and root_" {
  load BoxC
  let root = call box.label()
  let root_ = call box.label()
  assert root == "b"
  assert root_ == "b"
  unload BoxC
  assert no_residue
}
""")
    assert "var root_ string" in source
    assert "var root__ string" in source


def test_go_runs_a_lifecycle_binding_named_like_each_driver_name(tmp_path):
    """The measured programs in one module: every function-scope name and every
    call-step helper binds and runs on the live go runner. (The go self-hosted
    emitter does not carry lifecycle tests, a documented boundary; see
    ``test_selfhost_emit_go.py`` for its reserved-set parity.)"""
    if shutil.which("go") is None:
        pytest.skip("go toolchain not installed")
    source = BASE
    for name in GO_DRIVER_NAMES + ("_svc", "_err"):
        source += _go_test(name)
    for name in ("_r", "_ok"):
        source += _go_test(name, opt=True)
    path = tmp_path / "bind_driver_names.rvl"
    path.write_text(source, encoding="utf-8")
    result = _revl_test("--backend", "go", str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[go] pass:" in result.stdout


# ---------------------------------------------------------------- plumbing

def _revl_test(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=900)
