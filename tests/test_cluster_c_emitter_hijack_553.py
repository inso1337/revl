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
        predeclared set (user `len` runs as `len_`) AND the type names the
        prelude declares (`RevlResult`, `RevlOpt`, `RevlFrame`, `Stream`, …),
        which had the same effect one name over — see the type-name section at
        the end of this file.

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
        names and execute correctly. CONFIRMED SAFE. `_EMITTER_RESERVED` now
        also carries the prelude TYPE names (`RevlSecretShape`, `RevlResult`,
        `RevlActivation`); the `RevlSecretShape` one is the case whose output no
        longer says what it means — see the type-name section at the end of
        this file.

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
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
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


# ------------------------------------------------- prelude TYPE names, not just fns
#
# The class above is about injected *callables*; the emitters also inject TYPE
# declarations into the module they emit, and that half was never reserved. A
# user type declared under one of those names lands in the same scope as the
# prelude's, so the emitted module carries the declaration twice: the checker
# accepts a program whose output does not compile. On go the user type is
# escaped off the runtime name by the same `_GO_RESERVED` ladder that already
# escapes user fns; on java the prelude nests its types inside `Components`, so
# a user type of the same simple name is a javac duplicate — refused loudly by
# `_EMITTER_RESERVED`, exactly like the `Components` case next to it.
#
# The java `RevlSecretShape` half is the one whose output no longer says what it
# means: a user type of that name in a secret-mode program is emitted
# `implements RevlSecretShape`, which now resolves to the user's own class, and
# the interface the redaction path probes with `instanceof` is that class rather
# than the prelude's. Still a compile failure of the emitted program — no
# runtime impact — but worth a clean refusal. Refused now.
_PRELUDE_TYPE_USE = """
pub fn ok(x: Int) -> Result[Int, Str] { if (x == 0) { return Err("z") } return Ok(x) }
pub fn maybe(x: Int) -> Opt[Int] { if (x > 0) { return Some(x) } return None }
test "t" { assert ok(1) == Ok(1) }
"""


def _shadow_prelude_type(name: str) -> str:
    return (_PRELUDE_TYPE_USE
            + f"\npub type {name} = {{ a: Int }}\n"
            + f"pub fn use_{name}(v: {name}) -> Int {{ return 1 }}\n")


def _shadow_generic_type(name: str) -> str:
    """`Map` is a revl builtin GENERIC, so a bare `Map` is not a usable type
    annotation — `use_Map(v: Map)` is refused by the checker ("takes 2 type
    argument(s), got 0") before any emitter runs. The declaration alone still
    reaches the collision (every declared type gets a declaration emitted), so
    that is all this builds."""
    return _PRELUDE_TYPE_USE + f"\npub type {name} = {{ a: Int }}\n"


_GO_PRELUDE_TYPES = ["RevlResult", "RevlOpt", "RevlOk", "RevlErr", "RevlFrame",
                     "RevlTimer", "RevlTeardownRecord", "Stream", "Subscription",
                     "EventContract"]


@pytest.mark.parametrize("name", _GO_PRELUDE_TYPES)
def test_go_escapes_injected_type_names(name: str):
    """A user `type RevlResult` emits as `RevlResult_`, so the runtime type the
    go prelude declares keeps its name and the package declares it once."""
    emitted = _emit("go", _shadow_prelude_type(name))
    assert f"type {name}_ struct" in emitted, f"user `type {name}` must be escaped"
    assert f"type {name} struct" not in emitted, f"bare `type {name}` shadows the prelude"
    assert len(re.findall(rf"^type {name}(?:\[| struct)", emitted, re.M)) <= 1


@pytest.mark.parametrize("name", ["RevlResult", "RevlSecretShape", "RevlActivation"])
def test_java_reserves_injected_type_names(name: str):
    """java's prelude types are nested in `Components`; a user type of the same
    simple name is a duplicate declaration, so the emitter refuses it."""
    with pytest.raises(Exception) as exc:
        _emit("java", _shadow_prelude_type(name))
    assert name in str(exc.value)


# ------------------------------------- the injected type names, tier by tier
#
# The block above closed the type-name half for the THREE java names and the TEN
# go names #936 reserved. A corrected probe (a declaration-aware base filter, so
# a fixture that merely *mentions* the name is no longer mistaken for one that
# already declares it) showed the same class was still open on four more tiers,
# and that on python it is not a build failure at all but a silent runtime one.
# Each case below is pinned on the tier it belongs to.
#
#   py   : `_emit_builtin_result` injects `class Ok:` / `class Err:` at module
#          scope, guarded only by the set of user VARIANT CASE names — a user
#          `type Ok` is not in it. Python class redefinition is silent and the
#          later class wins, so the user's `Ok` replaced the runtime's
#          constructor and the emitted test crashed with
#          `TypeError: Ok() takes no arguments`. Also `_RevlNoLiveWorker` /
#          `_RevlRouter` (module-scope classes in the router scaffolding), which
#          slipped through because the scaffolding guard is case-sensitive on
#          the lowercase `_revl*` spelling. FIXED: `_TYPE_RESERVED`, applied at
#          the type-name position through the same injective ladder.
#   rust : 25 scaffolding structs/enums the emitter writes itself (`Map`,
#          `Pool`/`PoolState`, the `Job*` family, `Stream*`, `Subscription*`,
#          `EventContract`, the `Revl*` operation structs) were never reserved,
#          so a user type of the same name emitted a SECOND declaration and
#          rustc rejected the crate. Escaped, not refused: `Map`/`Pool`/`Job`
#          are also live host roots spelled as bare tokens at call sites.
#   java : `RevlFrame` / `RevlSpawnHandle` are nested in `Components`, so a user
#          type of the same simple name is a javac duplicate — refused like the
#          other emitter-scaffolding names. `Map`/`Pool`/`Job` are ALSO live
#          host roots, so a role-agnostic refusal would make `effect Pool.open()`
#          unportable; they are escaped at the type-name position only.
#   go   : `RevlSpawnHandle` is declared on the live (`stc-go`) path only, which
#          is why the earlier go sweep missed it — the pure typed-core path
#          drops components, so a component-only fixture never reaches the
#          declaration. The base below therefore carries a `lifecycle test` to
#          hold the emission on the path that declares it.
#
# NOT fixed here, and pinned as an open gap below: the type names the emitter
# DERIVES from a user-chosen name (`<Comp>Config`, `<Svc>Proxy`,
# `<Comp><Provision>`, `<Comp><Sink>Intercept<N>`, py's `_<Provision>`) are not
# a static set — the colliding spelling depends on the document — so they need
# a derivation-aware reservation rather than another reserved-set entry. See
# contract-errata.md.

_PY_INJECTED_TYPES = ["Ok", "Err", "_RevlNoLiveWorker", "_RevlRouter"]

_RUST_INJECTED_TYPES = [
    "Pool", "PoolState", "Job", "JobToken", "JobHandle",
    "StreamNext", "StreamState", "StreamInner", "Stream", "StreamRegistry",
    "SubscriptionState", "SubscriptionInner", "Subscription", "EventContract",
    "RevlStrOps", "RevlStrListOps", "RevlListOps", "RevlListSearchOps",
    "RevlTimer", "RevlClock", "RevlSpawnHandle", "RevlTeardown",
    "RevlPendingCompensation", "RevlWal",
]

_JAVA_INJECTED_TYPE_SCAFFOLDING = ["RevlFrame", "RevlSpawnHandle"]
_JAVA_INJECTED_TYPE_HOST_ROOTS = ["Pool", "Job"]

# The go emitter only declares `RevlSpawnHandle` on the live (`stc-go`) path; a
# component-only document without a lifecycle test routes onto the pure
# typed-core path, which drops the components and never reaches the
# declaration. So the base is the real accessor scenario plus the `lifecycle
# test` that holds the emission on the live path.
_ACCESSOR_LIFECYCLE = (
    (ROOT / "backends" / "go" / "scenarios" / "accessor.rvl").read_text()
    + """
lifecycle test "accessor" {
  load App
  let x = call reader.read_a()
  assert x == 1
}
""")

# The ts base whose component name makes the emitter derive `WorkerConfig`
# (`backends/typescript/emit.py`: `export interface {name}Config`).
_INSTANCE_GET = """
component Worker {
  provide store {
    fn get() -> Int { return 1 }
  }
}
service Store { fn get() -> Int }
test "t" { assert 1 == 1 }
"""


@pytest.mark.parametrize("name", _PY_INJECTED_TYPES)
def test_python_escapes_injected_type_names(name: str):
    """A user `type Ok` emits as `Ok_`, so the runtime's own `Ok` keeps the
    module-scope name its constructor is called by."""
    emitted = _emit("python", _shadow_prelude_type(name))
    assert f"class {name}_:" in emitted, f"user `type {name}` must be escaped"
    assert emitted.count(f"class {name}:") <= 1, f"bare `class {name}` twice"


def test_python_result_type_name_does_not_break_the_program():
    """The runtime half of the finding: with a user `type Ok`, the pre-fix
    emitter replaced the runtime's `Ok` constructor and the emitted test died
    with `TypeError: Ok() takes no arguments` — a program the checker accepts
    that passes on every other tier. It must now run."""
    status, message = _run("py", _shadow_prelude_type("Ok"))
    assert status == "pass", message


@pytest.mark.parametrize("name", _RUST_INJECTED_TYPES)
def test_rust_escapes_injected_type_names(name: str):
    """A user `type Map` emits as `Map_`, so the scaffolding struct the emitter
    writes keeps its name and the crate declares it once."""
    emitted = _emit("rust", _shadow_prelude_type(name))
    assert f"struct {name}_ {{" in emitted, f"user `type {name}` must be escaped"
    assert f"struct {name} {{" not in emitted, f"bare `struct {name}` collides"


def test_rust_escapes_injected_generic_type_name():
    """`Map` is the one injected name that is also a revl builtin generic, so
    the shadow is a bare declaration — see `_shadow_generic_type`."""
    emitted = _emit("rust", _shadow_generic_type("Map"))
    assert "struct Map_ {" in emitted, "user `type Map` must be escaped"
    assert "struct Map {" not in emitted, "bare `struct Map` collides"


@pytest.mark.parametrize("name", _JAVA_INJECTED_TYPE_SCAFFOLDING)
def test_java_reserves_injected_type_scaffolding(name: str):
    """`RevlFrame`/`RevlSpawnHandle` live inside `Components`, so a user type of
    the same simple name is a javac duplicate declaration — refused loudly."""
    with pytest.raises(Exception) as exc:
        _emit("java", _shadow_prelude_type(name))
    assert name in str(exc.value)


@pytest.mark.parametrize("name", _JAVA_INJECTED_TYPE_HOST_ROOTS)
def test_java_escapes_injected_host_root_type_names(name: str):
    """`Map`/`Pool`/`Job` are both scaffolding type names and live host roots
    (`effect Pool.open(...)`), so they are escaped at the type-name position
    instead of refused — a refusal would make those programs unportable."""
    emitted = _emit("java", _shadow_prelude_type(name))
    assert f"class {name}_ {{" in emitted, f"user `type {name}` must be escaped"
    assert f"class {name} {{" not in emitted, f"bare `class {name}` collides"


def test_java_escapes_injected_generic_host_root_type_name():
    """`Map` is both a scaffolding type name and a revl builtin generic; see
    `_shadow_generic_type` for why the shadow is a bare declaration."""
    emitted = _emit("java", _shadow_generic_type("Map"))
    assert "class Map_ {" in emitted, "user `type Map` must be escaped"
    assert "class Map {" not in emitted, "bare `class Map` collides"


def test_go_escapes_spawn_handle_type_name():
    """`RevlSpawnHandle` is declared on the live path only, so the base must
    carry a `lifecycle test` — a bare top-level `type` routes the emitter onto
    the pure typed-core path, which drops components and never declares it."""
    source = (_ACCESSOR_LIFECYCLE
              + "\npub type RevlSpawnHandle = { a: Int }\n")
    emitted = _emit("go", source)
    assert "type RevlSpawnHandle_ struct" in emitted, "user type must be escaped"
    assert len(re.findall(r"^type RevlSpawnHandle(?![A-Za-z0-9_])", emitted,
                          re.M)) == 1, "the runtime's handle must survive once"


@pytest.mark.xfail(strict=True, reason=(
    "#553 cluster C, CONFIRMED OPEN: the type names the emitter DERIVES from a "
    "user-chosen name are not reserved. ts emits `export interface "
    "<Comp>Config`, rust `<Comp>Config`/`<Svc>Proxy`/`<Comp><Provision>`, java "
    "`<Comp>Plugin`/`<Comp><Provision>`, go `<Comp>Config`, py `_<Provision>`, "
    "so a user type spelled after the component/service it derives from is a "
    "duplicate declaration. Unlike the literal injected names these cannot be a "
    "static reserved set — the colliding spelling depends on the document — so "
    "they need a derivation-aware reservation. Flip to a plain assert once "
    "fixed; see contract-errata.md."))
def test_derived_component_config_type_name_is_reserved():
    source = _INSTANCE_GET + "\npub type WorkerConfig = { a: Int }\n"
    emitted = _emit("typescript", source)
    assert emitted.count("export interface WorkerConfig {") == 1, (
        "the user's `WorkerConfig` and the emitter's derived `<Comp>Config` "
        "interface are declared twice in one module")
