"""#553 cluster C: emitter-injected host-call hijack across tiers.

Confirm-and-lock for the "plausible unconfirmed (C)" method-/name-hijack cases
the issue flagged. Every emitter injects host calls (a builtin, a predeclared
identifier, a context property) into the code it emits for a user program; a
user name that lands on the same host name can shadow the call the emitter is
relying on, so the reference tier answers differently from the others for a
program the checker accepts. This is the class GHSA-c3fm / GHSA-mrqv named; the
Float half of #553 was pinned by #567. This file empirically CONFIRMS each
cluster-C case and locks the resolution. Findings (measured here):

  py  : a top-level `fn` named a Python builtin the emitter injects by bare name
        (`abs`/`len`/`sorted`/`str`/`ord`/`repr`/`float`/`isinstance`/…) landed
        in module scope and SILENTLY hijacked the operation — `Int.mod`
        (`a % abs(b)`) with a user `fn abs` returned the wrong remainder, a
        `test`'s failure message with a user `fn repr` crashed. FIXED:
        `backends/python/emit.py::_EMITTED_BUILTINS` folds them into the
        injective `_mangle` A3 ladder (user `abs` runs as `abs_`).

  go  : a top-level `fn`/`type` named a Go PREDECLARED identifier (`len`,
        `error`, `make`, `append`, …) shadowed the universe-block name a runtime
        helper used, so `go build` failed. Same class as py, loud instead of
        silent. FIXED: `backends/go/emit.py::_GO_RESERVED` now carries the
        predeclared set (user `len` runs as `len_`).

  ts  : a require/provide key becomes a `ctx.<key>` PROPERTY on cordis's
        Context; a key colliding with a JS Object/Function member (`then`,
        `prototype`, `constructor`, `toString`, `__proto__`, …) or cordis's
        reserved `_`-prefixed namespace resolved to `undefined` on the consumer
        side while py/go read the same key from a string store — a silent
        consumer-side hijack. FIXED: `backends/typescript/emit.py::
        _reject_service_key` refuses such a key at emit (a key is a wire string
        and cannot be renamed), applied to BOTH require and provide keys.

  java: the frame/undo scaffolding names (`config`/`fx`/`frame`/`undos`/`ctx`/
        `root`) are ALREADY refused by `_EMITTER_RESERVED`; the flagged
        `bind`/`require` (and rust's `borrow`/`type_id`) are not scaffolding
        names and execute correctly. CONFIRMED SAFE.

  rust: a unit-service method named a smart-pointer/prelude method (`to_owned`,
        `as_ref`, `as_mut`) IS hijacked wherever the receiver is the
        `Arc<Box<dyn Svc>>` a `call`/require yields: `w.to_owned()` binds
        `Arc::clone`, `w.as_ref()` binds `Arc::as_ref` (`&Box<dyn Svc>`) — the
        wrong method, caught here as a type error against the i64 the service
        method returns. `borrow`/`type_id` need traits not in the prelude, so
        they resolve to the service method and are safe. CONFIRMED OPEN — the
        fix is depth-aware fully-qualified dispatch (`Svc::m(&**recv)`) at every
        service-call site, a rust-backend change tracked in contract-errata.md
        and by the strict-xfail below.

  wasm: the per-MODULE (not per-component) host-import subset check the issue
        lists is by-design extern trust (item 289), not a divergence.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

FAST_TIERS = ("py", "go", "ts")


def _run(tier: str, source: str):
    return RUNNERS[tier](compile_source(source, "cluster_c.rvl"))


def _emit(backend: str, source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_clusterc", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "cluster_c.rvl"))


# --------------------------------------------------------------- py / go: builtins
#
# `use_ops` forces the emitter to inject the host builtins a user fn of the same
# name could shadow: `Int.mod` -> `a % abs(b)` on py, `List.length` -> `len(...)`
# on both. Each `*_shadow` fn returns a sentinel the operation never would, so a
# hijack (the operation calling the user fn, or the user fn failing to be
# callable) changes a value the asserts pin. Before the fix, py returned the
# wrong remainder here and go failed to build.
_BUILTIN_SHADOW = """
pub fn abs(x: Int) -> Int { return 444 }
pub fn len(x: Int) -> Int { return 111 }
pub fn sorted(x: Int) -> Int { return 333 }
pub fn str(x: Int) -> Int { return 222 }
pub fn use_ops(xs: List[Int], a: Int, b: Int) -> Int {
  let count = xs.length
  let rem = a.mod(b)
  return count + rem
}
test "user builtin-named fns run as user fns" {
  assert abs(1) == 444
  assert len(1) == 111
  assert sorted(1) == 333
  assert str(1) == 222
}
test "the emitter's own builtins are not hijacked" {
  assert use_ops([9, 9, 9], 5, 3) == 5
}
"""


@pytest.mark.parametrize("tier", FAST_TIERS)
def test_builtin_named_user_fns_do_not_hijack(tier: str):
    status, message = _run(tier, _BUILTIN_SHADOW)
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} hijack on builtin-named fns: {message}"


def test_python_mangles_builtin_named_top_level_fn():
    """The cheap static guard: the user `fn abs` emits as `abs_` (so the
    module-scope name never shadows), while the injected `Int.mod` still spells
    the real builtin `abs`."""
    emitted = _emit("python", _BUILTIN_SHADOW)
    assert "def abs_(" in emitted, "user `fn abs` must be escaped off module scope"
    assert "% abs(" in emitted, "`Int.mod` must still reach the builtin `abs`"
    assert "\ndef abs(" not in emitted, "a bare `def abs` would shadow the builtin"


def test_go_reserves_predeclared_identifiers():
    """A user `fn len` emits as `len_` on go, so a runtime helper's `len(...)`
    still binds Go's predeclared `len` and the package builds."""
    emitted = _emit("go", _BUILTIN_SHADOW)
    assert "func len_(" in emitted, "user `fn len` must be escaped off package scope"
    assert "\nfunc len(" not in emitted, "a package `func len` shadows the builtin"


# --------------------------------------------------------------- ts: service keys
#
# A require/provide key is read on the consumer side as `ctx.<key>`. cordis
# resolves it off the Context, whose prototype chain and reserved `_` namespace
# swallow these keys — so the injected service is never returned. They cannot be
# renamed (a key is the provide/require wire string), so the emitter refuses
# them; the alternative was the silent `undefined` this pins.
_TS_UNSAFE_KEYS = ["then", "prototype", "constructor", "toString",
                   "valueOf", "hasOwnProperty", "__proto__", "_internal"]


def _ts_key_program(key: str) -> str:
    return f"""
service Dep {{ fn val() -> Int }}
service Main {{ fn go() -> Int }}
component Provider provides {key}: Dep {{ provide {key} {{ fn val() = 42 }} }}
component Consumer requires {key}: Dep provides main: Main {{
  provide main {{ fn go() {{ return {key}.val() }} }}
}}
lifecycle test "t" {{
  load Provider  load Consumer
  let r = call main.go()
  assert r == 42
  unload Consumer  unload Provider
  assert no_residue
}}
"""


@pytest.mark.parametrize("key", _TS_UNSAFE_KEYS)
def test_ts_refuses_host_unsafe_service_keys(key: str):
    with pytest.raises(Exception) as exc:
        _emit("typescript", _ts_key_program(key))
    msg = str(exc.value)
    assert "not host-safe on the ts tier" in msg, msg
    assert "Rename the key" in msg


def test_ts_refuses_unsafe_key_on_the_require_side_too():
    """The require side was previously unguarded: a consumer requiring `then`
    emitted `ctx.then.val()` (undefined) with no provider of `then` in sight."""
    program = """
service Dep { fn val() -> Int }
service Main { fn go() -> Int }
component Consumer requires then: Dep provides main: Main {
  provide main { fn go() { return then.val() } }
}
test "noop" { assert 1 == 1 }
"""
    with pytest.raises(Exception) as exc:
        _emit("typescript", program)
    assert "requirement key 'then'" in str(exc.value)


def test_ts_ordinary_service_key_still_resolves():
    """A key that is not a reserved property runs end to end (the fix must not
    over-refuse)."""
    status, message = _run("ts", _ts_key_program("store"))
    if status == "skip":
        pytest.skip(f"ts: {message}")
    assert status == "pass", message


# ------------------------------------------------------ java: confirmed safe / guarded

_HOST_NAMED_METHODS = """
service Widget {
  fn borrow() -> Int
  fn type_id() -> Int
  fn bind() -> Int
  fn require() -> Int
}
component W provides w: Widget {
  provide w {
    fn borrow() = 20
    fn type_id() = 30
    fn bind() = 60
    fn require() = 70
  }
}
lifecycle test "host-named service methods dispatch to the user impl" {
  load W
  let b = call w.borrow()
  let t = call w.type_id()
  let bi = call w.bind()
  let rq = call w.require()
  assert b == 20
  assert t == 30
  assert bi == 60
  assert rq == 70
  unload W
  assert no_residue
}
"""


def test_go_runs_host_named_service_methods():
    """go executes the live composition: `borrow`/`type_id`/`bind`/`require` as
    service method names all dispatch to the user impl (the safe half of the
    rust/java cluster-C list)."""
    status, message = _run("go", _HOST_NAMED_METHODS)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", message


def test_java_refuses_scaffolding_named_method():
    """java already guards its frame/undo scaffolding: a service method named
    `frame` is refused at emit (a loud portability error, not a silent
    hijack)."""
    program = """
service S { fn frame() -> Int }
component C provides s: S { provide s { fn frame() = 1 } }
test "noop" { assert 1 == 1 }
"""
    with pytest.raises(Exception) as exc:
        _emit("java", program)
    assert "frame" in str(exc.value)


def test_java_allows_non_scaffolding_host_names():
    """`bind`/`require`/`borrow`/`type_id` are not scaffolding names; they emit
    as ordinary interface methods."""
    emitted = _emit("java", _HOST_NAMED_METHODS)
    assert "long borrow();" in emitted
    assert "long bind();" in emitted
    assert "long require();" in emitted


# ------------------------------------------------------------- rust: confirmed OPEN

_RUST_HIJACK = """
service Widget {
  fn to_owned() -> Int
  fn as_ref() -> Int
  fn as_mut() -> Int
}
component W provides w: Widget {
  provide w {
    fn to_owned() = 10
    fn as_ref() = 40
    fn as_mut() = 50
  }
}
lifecycle test "smart-pointer-named service methods dispatch to the user impl" {
  load W
  let a = call w.to_owned()
  let d = call w.as_ref()
  let e = call w.as_mut()
  assert a == 10
  assert d == 40
  assert e == 50
  unload W
  assert no_residue
}
"""


@pytest.mark.skipif(not os.environ.get("REVL_CROSS_TIER_SLOW"),
                    reason="set REVL_CROSS_TIER_SLOW=1 (cargo is slow)")
@pytest.mark.xfail(strict=True, reason=(
    "#553 cluster C, CONFIRMED OPEN: on rust a service method named "
    "`to_owned`/`as_ref`/`as_mut` is hijacked by the `Arc<Box<dyn Svc>>` "
    "receiver's own pointer method. Fix: depth-aware fully-qualified dispatch "
    "at the service-call sites (backends/rust/emit.py, a digest input). Flip to "
    "a plain assert once fixed; see contract-errata.md."))
def test_rust_smart_pointer_named_methods_hijack():
    status, message = _run("rust", _RUST_HIJACK)
    if status == "skip":
        pytest.skip(f"rust: {message}")
    assert status == "pass", message
