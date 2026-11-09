"""One regression test per issue in the cross-tier host-name reservation fix.

An emitted module is one namespace shared by the user's declarations and the
names the emitter itself spells bare (JS globals, Python builtins, Rust
prelude constructors, Go predeclared identifiers, and each backend's own
`revl*` helpers). A user declaration that lands on one of those names used to
make the emitted code read the user binding instead of the host name, so a
program the checker accepts ran different code. The issues:

  #3127  ts   a module fn/extern/component named like a JS global (or helper)
  #3129  py   a fn/type named like a builtin the helpers call
  #3159  rust a fn named Ok/Err/Some/None shadows the constructors
  #3160  java a fn named revlEq/revlConcat wins overload resolution
  #3171  java a fn named revlMarkSecret/revlSecretResult skips the registry
  #3161  go   a fn named recover replaces the panic guard
  #3168  go   a local/param named revlSub replaces checked arithmetic

Each test emits the program and either runs it on the tier or pins the emitted
spelling. The runtime tests are the ones that catch a rename applied at the
declaration but not at the use sites.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_BACKENDS = ROOT / "backends"


def _load(backend: str):
    spec = importlib.util.spec_from_file_location(
        f"host_name_{backend}", _BACKENDS / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(backend: str, source: str) -> str:
    return _load(backend).emit(compile_source(source, "host_names.rvl"))


def _run(tier: str, source: str) -> None:
    status, message = RUNNERS[tier](compile_source(source, "host_names.rvl"))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier}: {message}"


# --------------------------------------------------------------- #3127 (ts)
#
# `fn undefined` made every `match` on an Opt compare against the user fn
# (`x !== undefined` always true, so the None arm never fired), and `fn BigInt`
# made every `.length()` return the user fn's value. A pub fn and a component
# are resolved by the host under their source spelling, so those are refused
# rather than renamed.
_TS_MODULE_GLOBALS = """\
fn undefined(x: Int) -> Int = x
fn BigInt(x: Int) -> Int = 999
fn revlI64(x: Int) -> Int = 7
pub fn pick(o: Opt[Str]) -> Str = match o {
    Some(v) => v,
    None => "denied"
}
pub fn count(xs: List[Int]) -> Int = xs.length()
pub fn add(a: Int, b: Int) -> Int = a + b
test "globals stay globals" {
  assert pick(None) == "denied"
  assert pick(Some("A")) == "A"
  assert count([1, 2, 3]) == 3
  assert add(2, 3) == 5
}
"""


def test_3127_ts_module_names_do_not_shadow_js_globals_or_helpers():
    emitted = _emit("typescript", _TS_MODULE_GLOBALS)
    assert "export function undefined_(" in emitted
    assert "export function BigInt_(" in emitted
    assert "export function revlI64_(" in emitted
    assert "export function undefined(" not in emitted
    # the injected helpers still spell the host names they need
    assert "!== undefined)" in emitted
    assert "BigInt(" in emitted
    _run("ts", _TS_MODULE_GLOBALS)


def test_3127_ts_host_visible_names_are_refused_and_externs_rename():
    with pytest.raises(Exception, match="pub fn 'undefined'"):
        _emit("typescript", "pub fn undefined(x: Int) -> Int = x\n")
    with pytest.raises(Exception, match="component 'undefined'"):
        _emit("typescript", "component undefined {\n}\n")
    # an extern is bound inside this module, so it is renamed instead
    emitted = _emit("typescript",
                    "extern pure fn fetch() -> Int = @ts { return 1 }\n")
    assert "export function fetch_(" in emitted
    assert "export function fetch(" not in emitted


# --------------------------------------------------------------- #3129 (py)
#
# The emitted helpers reach `chr`, the index guards' exception classes and the
# router's `setattr`/`next` through module globals. A user declaration of the
# same name replaced them; `chr` was the silent wrong value.
_PY_BUILTINS = """\
fn chr(n: Int) -> Str = "HIJACKED"
fn setattr(a: Int) -> Int = a
fn next() -> Int = 1
fn realm_label() -> Int = 1
type IndexError = { x: Int }
pub fn letter(n: Int) -> Str = n.to_char()
pub fn at(xs: List[Int]) -> Int = xs[5]
test "to_char is the builtin" {
  assert letter(65) == "A"
}
"""


def test_3129_python_declarations_do_not_shadow_emitted_builtins():
    emitted = _emit("python", _PY_BUILTINS)
    for name in ("chr", "setattr", "next", "realm_label"):
        assert f"def {name}_(" in emitted, name
        assert f"\ndef {name}(" not in emitted, name
    assert "class IndexError_:" in emitted
    assert "return chr(" in emitted        # the to_char helper
    assert "raise IndexError(" in emitted  # the list index guard
    namespace: dict = {}
    exec(emitted, namespace)  # noqa: S102 — executing the backend's own output
    assert namespace["letter"](65) == "A"
    with pytest.raises(IndexError):
        namespace["at"]([1])


def test_3129_python_family_covers_every_reported_name():
    emit = _load("python")
    reported = {"chr", "IndexError", "KeyError", "AssertionError", "setattr",
                "next", "realm_label"}
    assert reported <= emit._EMITTED_BUILTINS


# --------------------------------------------------------------- #3159 (rust)
#
# The emitted Result body spells `Ok(..)`/`Err(..)` bare; a module `fn Err`
# shadowed the constructor, so `Err("denied")` called the user fn and a denial
# returned Ok.
_RUST_CONSTRUCTORS = """\
fn Err(e: Str) -> Result[Int, Str] = Ok(1)
fn Ok(x: Int) -> Result[Int, Str] = Err("shadowed")
pub fn check(allowed: Bool) -> Result[Int, Str] = allowed ? Ok(7) : Err("denied")
test "a denial is still a denial" {
  let r = check(false)
  let v = match r { Ok(x) => x, Err(e) => 0 }
  assert v == 0
  let s = check(true)
  let w = match s { Ok(x) => x, Err(e) => 0 }
  assert w == 7
}
"""


def test_3159_rust_prelude_constructors_are_reserved():
    emitted = _emit("rust", _RUST_CONSTRUCTORS)
    assert "fn Err_(" in emitted
    assert "fn Ok_(" in emitted
    assert "Err(String::from(\"denied\"))" in emitted
    _run("rust", _RUST_CONSTRUCTORS)


# --------------------------------------------------------------- #3160 (java)
#
# A user `revlEq(String, String)` is more specific than the helper
# `revlEq(Object, Object)`, so every Str `==`/`!=` in the module called it.
_JAVA_HELPERS = """\
fn revlConcat(a: List[Int], b: List[Int]) -> List[Int] = a
fn revlEq(a: Str, b: Str) -> Bool = true
pub fn join(a: List[Int], b: List[Int]) -> List[Int] = a.concat(b)
pub fn same(a: Str, b: Str) -> Bool = a == b
pub fn differ(a: Str, b: Str) -> Bool = a != b
"""


def test_3160_java_helper_overload_names_are_escaped():
    emitted = _emit("java", _JAVA_HELPERS)
    assert "public static boolean revlEq_(String a, String b)" in emitted
    assert "public static java.util.List<java.lang.Long> revlConcat_(" in emitted
    # the lowered equality and concatenation still call the helpers
    assert "return revlEq(a, b)" in emitted
    assert "return !revlEq(a, b)" in emitted
    assert "return revlConcat(a, b)" in emitted
    assert "public static boolean revlEq(String a" not in emitted


def test_3160_java_local_helper_names_are_escaped():
    emitted = _emit("java", """\
pub fn diff(a: Int, b: Int) -> Int {
  let revlSub = (x: Int, y: Int) => 0
  let used = revlSub
  return a - b + used(0, 0)
}
""")
    assert "revlSub_" in emitted
    assert "revlSub(a, b)" in emitted


# --------------------------------------------------------------- #3171 (java)
#
# A user `revlMarkSecret(String)` is a strict-invocation match where the
# varargs helper is variable-arity, so the secret registration ran the user fn
# and the credential was never registered for redaction.
_JAVA_SECRET = """\
extern emission[vault.connect] fn connect(key: Secret[Str], url: Str, user: Str) -> Unit
  = @java { throw new RuntimeException("vault refused key " + key + " at " + url + " for " + user); }
service Vault { emission fn open(user: Str) -> Str }
component Keeper provides vault: Vault {
  config { url: Str = "pg://main", api_key: Secret[Str] = "k" }
  provide vault {
    fn open(user) {
      emit connect(config.api_key, config.url, user)
      return "opened"
    }
  }
}
fn revlMarkSecret(v: Str) -> Str = v
fn revlSecretResult(v: Str) -> Str = v
"""


def test_3171_java_secret_registry_helpers_are_escaped():
    emitted = _emit("java", _JAVA_SECRET)
    assert "public static String revlMarkSecret_(String v)" in emitted
    assert "public static String revlSecretResult_(String v)" in emitted
    assert "revlMarkSecret(this.api_key);" in emitted
    assert "public static String revlMarkSecret(String v)" not in emitted


# --------------------------------------------------------------- #3161 (go)
#
# The deferred `if r := recover(); r != nil` guard called a zero-argument user
# `fn recover`, so every lifecycle test failed at the end and a real panic was
# no longer recovered.
_GO_RECOVER = """\
fn recover() -> List[Int] = []
service S { fn ping() -> Int }
component C provides s: S {
  provide s {
    fn ping() = 1
  }
}
lifecycle test "the panic guard stays the builtin" {
  load C
  let n = call s.ping()
  assert n == 1
  unload C
  assert no_residue
}
"""


def test_3161_go_predeclared_recover_is_reserved():
    emitted = _emit("go", _GO_RECOVER)
    assert "func recover_(" in emitted
    assert "\nfunc recover(" not in emitted
    assert "defer func() { _ = recover() }()" in emitted  # still the builtin
    _run("go", _GO_RECOVER)


# --------------------------------------------------------------- #3168 (go)
#
# A local or parameter named like a checked-arithmetic helper shadowed the
# helper inside its function: `a - b` called the user closure and returned 0.
_GO_HELPER_LOCAL = """\
pub fn diff(a: Int, b: Int) -> Int {
  let revlSub = (x: Int, y: Int) => 0
  return a - b
}
pub fn via_param(revlAdd: (Int, Int) -> Int, a: Int, b: Int) -> Int = a + b
test "checked arithmetic" {
  assert diff(5, 3) == 2
  assert via_param((x: Int, y: Int) => 0, 2, 3) == 5
}
"""


def test_3168_go_helper_named_bindings_are_escaped():
    emitted = _emit("go", _GO_HELPER_LOCAL)
    assert "revlSub_ :=" in emitted
    assert "revlAdd_ func(a int64" in emitted or "revlAdd_ " in emitted
    assert "return revlSub(a, b)" in emitted
    assert "return revlAdd(a, b)" in emitted
    _run("go", _GO_HELPER_LOCAL)
