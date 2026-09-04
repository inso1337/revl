"""The self-hosted ADMISSION GATE slice (selfhost/lower.rvl), compiled by revl,
emitted through the python backend, executed, and cross-checked against the
reference lowering gate (src/revl/lower.py's `check_and_lower` / `_link`, driven
through `compile_source`) on the admission VERDICT.

This is the fourth self-host differential oracle, in the exact shape of
tests/test_selfhost_{lexer,parser,checker}.py: two independent implementations
of one admission algebra — lower a checked program to the IR and enforce the
cordis guarantees over it — are forced to agree on every input. Neither is the
spec; a disagreement is a real defect in one of them.

The gate answers one question: admit, or refuse and name the guarantee. So the
oracle compares the VERDICT:
  * accepted input -> both admit (reference raises nothing; selfhost returns "");
  * refused input  -> both refuse with the SAME guarantee tag
    (G1 | G2 | G3 | G4 | A1 | PRELUDE) AND, where the tag is one this slice
    spells fully, the SAME message text.

Slice 3 extends the check-parity for the non-spawn program surface with three
more surfaces from lower.py, cross-checked here alongside slices 1+2:
  * G1 bare-value resolution — an undeclared bare `Var` used as a value inside a
    provide-method body (not only call/access heads), matching lower.py's
    `_lower_component_pure_expr`/`ExprVar` `_plain_body` refusal.
  * A1 first-class async value (`passed_async`) — an async extern/colored fn
    referenced as a VALUE in a provide method (any method, sync or async).
  * A1 setup/activation async-reach — a setup body that reaches an async
    callable (by call or value), matching `_async_reached_outside_provide`.
  * the realm PRELUDE rule — an `isolate`/`intercept` placed after an effect/
    emit/await/provide is refused (lower.py `_lower_component`'s `action_seen`).

Slice 5b adds the spawn/instance dimension (lower.py's `_check_spawn_*`),
cross-checked here alongside every earlier slice. All three surfaces reduce to
the same emission-capability machinery G4 runs, so they classify G4:
  * capability attenuation (item 66) — an activation-body spawn may only grant a
    child capabilities its spawner holds (`_check_spawn_attenuation`).
  * G4/G6 spawn-emission bounds (decision 8) — a provide-method spawn may not
    exceed the method's declared emission bound (`_check_spawn_emission_bounds`).
  * an unmarked emission reached through a spawn handle (`_instance_get_call`).
Spawn targets are runtime TEMPLATES, excluded from the static G2/G3 composition
(decision 5/6) — so two workers providing one key compose when both are spawned.

The slice mirrors three guarantees, in the reference's own checking order
(per-component G4 then A1 during lowering, then G2 at link):
  * G4 (capability containment) — a plain-declared provider that reaches an
    emission; an `emission[caps]` provider that emits outside its scope; an
    unmarked required-service emission call.
  * A1 (item 117) — a sync provide method that reaches an async callable (an
    async extern or a transitively-colored fn) or an async service operation.
  * G2 (provision disjointness) — two components providing one key (shared
    realm).

Item 186 (bounded pieces) adds the last two single-source surfaces:
  * multi-realm routing VALIDATION (item 162) — `isolate <key> in realms(...)
    [strategy(...)]` was parsed-but-unvalidated. Its component-level refusals
    (prelude placement, a routed provision, an undeclared target, a key both
    pinned and routed, a key routed twice, an unknown strategy) and the
    link-time per-realm provider check now cross-check; the four
    routing-specific ones carry the "ROUTE" tag, its prelude case is PRELUDE and
    its undeclared target is the shared G1 diagnostic.
  * the async-coloring approximation — callee collection and leak-reach now stop
    at a nested COERCED arrow (`stop_async_arrows`). Reaching it needs an
    async-typed but UNCALLED parameter (else rule 2 colors the callee and masks
    the difference), which no fixture had; the corpus and
    `test_nested_coerced_arrows_agree` now cover both sides of every switch.

Corpus discipline (as in the checker oracle): every rejected program is one
whose ONLY reference refusal is inside this slice — a T1/G1/G3/parse refusal
would be out of slice on the selfhost side, so those are excluded and the test
asserts, per case, that the reference's refusal classifies into {G2, G4, A1}.
"""

import importlib.util
import random
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_lower", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns():
    return _exec_emitted()


@pytest.fixture(scope="module")
def admit(ns):
    """The gate's verdict: "" to admit, else "<tag>|<message>"."""
    return ns["admit_src"]


@pytest.fixture(scope="module")
def admit_tag(ns):
    """The coarse verdict: "" | "G1" | "G2" | "G3" | "G4" | "A1" | "PRELUDE"
    | "BAD"."""
    return ns["admit_tag"]


def test_selfhosted_lower_in_file_tests_pass(ns):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = ns.get("REVL_TESTS")
    assert tests and len(tests) >= 12, \
        "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()


# ------------------------------------------------- reference classifier

def _classify(e: RevlError) -> str:
    """The reference refusal rendered in the gate's guarantee vocabulary. A
    RevlError.code is authoritative for A1/G4 (the checks that set one); G2 in
    `_link` sets no code, so its message marker is used. Anything else is
    OUT-OF-SLICE (the corpus must never hit it on a rejected case)."""
    if e.code in ("G4", "A1"):
        return e.code
    m = e.message
    if "provision conflict" in m and "(G2)" in m:
        return "G2"
    # G3 (dependency-cycle / self-provision) and G1 (undeclared access) set no
    # code, so their message markers classify them. G3's two shapes both end
    # "(G3)"; G1 is the reference's postfix/var head-resolution refusal.
    if "(G3)" in m:
        return "G3"
    # The realm PRELUDE rule (an `isolate`/`intercept` after an effect/emit/
    # await/provide) sets no code either; its message is unambiguous. Slice 3
    # mirrors it, so it is an in-slice tag rather than OUT.
    if "must precede every effect, emit, await, and provide statement" in m:
        return "PRELUDE"
    # ---- slice 4 -----------------------------------------------------------
    # `intercept` metadata validation (target must be a required key, no double
    # interception): the undeclared case reuses the "is not a declared
    # requirement of" G1 diagnostic below; the provision and double-intercept
    # cases carry their own text but are the same declared-wiring (G1) family.
    if "applies to required keys only" in m or "is intercepted twice in" in m:
        return "G1"
    # `await` outside a component body, and the services-2.0 async signature
    # parity mismatch — both async-color (A1) surfaces, neither carrying an
    # explicit `(A1)` marker or `code`.
    if "`await` is only allowed in a component body" in m:
        return "A1"
    if "declares it async" in m or "declares it not async" in m:
        return "A1"
    if "is not a declared requirement of" in m:
        return "G1"
    # ---- slice 6 (final): code-less spawn-form + handoff + isolate refusals ---
    # None of these carry an `e.code`; the gate tags them so the oracle compares
    # tag AND message. Spawn-form (bind-to-a-handle, unknown target) -> "SPAWN";
    # handoff target/uniqueness -> "HANDOFF"; isolate target/uniqueness -> "G1"
    # (the declared-wiring family, as `intercept` is). The handoff/isolate PRELUDE
    # cases reuse the "must precede …" wording already classified PRELUDE above.
    if "names an unknown component" in m or "must be bound to a handle" in m:
        return "SPAWN"
    if ("is not a declared provision of" in m
            or "declares more than one `handoff`" in m):
        return "HANDOFF"
    if ("is not a declared requirement or provision of" in m
            or "is isolated twice in" in m):
        return "G1"
    # ---- item 186: multi-realm routing validation (item 162) ---------------
    # `isolate … in realms(...)` — the four routing-specific component refusals
    # (lower.py's `RouteStmt` branch) plus the link-time per-realm provider
    # check. All code-less; the gate tags them "ROUTE" so the oracle compares
    # tag AND message. Two of the branch's refusals are NOT here on purpose:
    # its prelude case reuses the "must precede …" wording (PRELUDE above) and
    # its undeclared-target case IS the shared "is not a declared requirement
    # of" G1 diagnostic (above).
    if ("routes a *required* key" in m
            or "is already isolated to a single realm in" in m
            or "is routed twice in" in m
            or "unknown routing strategy" in m
            or "multi-realm bind of" in m):
        return "ROUTE"
    # ---- item 350: the environment contract -------------------------------
    # `_link`'s at-most-one-`boot`-component refusal. Code-less; the gate tags it
    # "BOOT" so the oracle compares tag AND message.
    if "at most one `boot` component" in m:
        return "BOOT"
    if ("declared plain, but this implementation reaches" in m
            or "must be marked `emit`" in m
            or "emits through" in m):
        return "G4"
    if "(A1)" in m:
        return "A1"
    return "OUT:" + m


def _ref(src: str) -> tuple[str, str]:
    """(tag, message): ("", "") if the reference admits, else its guarantee tag
    and diagnostic message."""
    try:
        compile_source(src, "diff.rvl")
        return ("", "")
    except RevlError as e:
        return (_classify(e), e.message)


def _fixture(name: str) -> str:
    return (ROOT / "examples" / "rejections" / f"{name}.rvl").read_text()


def _oneline(src: str) -> str:
    """Collapse a program onto a SINGLE source line (item 168): every run of
    whitespace — newlines included — becomes one space. The reference lexer and
    the gate both accept whitespace-separated statements, so this preserves the
    program's meaning while removing the newline the gate's statement reader used
    to lean on. Before the item-168 fix the gate's line-based reader swallowed a
    single-line body's trailing statements/declarations; the single-line fuzzer
    variants below exercise exactly that path for every mirrored check. (None of
    the generators emit comments or space-bearing string literals, so the naive
    whitespace collapse is safe.)"""
    return " ".join(src.split())


def _agree(admit, src: str) -> None:
    """Full agreement: both admit, or both refuse with the same tag AND (for a
    tag this slice spells fully) the same message."""
    ref_tag, ref_msg = _ref(src)
    got = admit(src)
    got_tag = got.split("|", 1)[0] if "|" in got else ("" if got == "" else got)
    got_msg = got.split("|", 1)[1] if "|" in got else ""
    if ref_tag == "":
        assert got == "", f"reference admits, selfhost refused: {got!r}"
    else:
        assert got_tag == ref_tag, \
            f"tag: selfhost {got_tag!r} != reference {ref_tag!r} ({got_msg!r})"
        assert got_msg == ref_msg, \
            f"msg: selfhost {got_msg!r} != reference {ref_msg!r}"


# ---------------------------------------------------------------- corpus

# Two providers of one key, isolated into realms `r1` and `r2` — the backend
# realms every multi-realm routing case below binds against (item 162). Per-realm
# G2 keeps them non-conflicting, so the routing verdict is the only one in play.
# That is load-bearing, not tidiness: a program with TWO live refusals hits the
# 419c ordering divergence (the reference picks the earlier LINE, the gate the
# earlier PHASE), so the corpus keeps every case single-refusal. See
# `test_which_refusal_wins_diverges_when_a_program_has_several` at the end of
# this file, which is where that divergence is pinned.
_ROUTE_PROVIDERS = """service Kv { fn get(k: Str) -> Str }
service Api { fn go(k: Str) -> Str }
component StoreA provides kv: Kv {
  isolate kv in realm("r1")
  provide kv { fn get(k) { return k } }
}
component StoreB provides kv: Kv {
  isolate kv in realm("r2")
  provide kv { fn get(k) { return k } }
}
"""

# Programs the reference admits — the gate must admit them too. Kept
# reference-clean (no out-of-slice defect), so "" is the only agreement.
ACCEPTED_PROGRAMS = [
    # item 350: a `boot` component — the environment contract. `boot` is a
    # contextual keyword the admission gate carries no verdict for (the contract
    # is an admission-time CONFIG concern, checked by `run.py`'s `--env`
    # preflight, not a composition-guarantee one), so both implementations must
    # step over it and admit the composition exactly as if it were plain.
    ("boot component with a bounded environment contract", """
service Env { fn data_root() -> Str }
boot component HarnessBoot provides env: Env {
  config {
    data_dir: Str under "./.harness-data",
    model: Str in ["mock", "real"] = "mock",
  }
  provide env { fn data_root() = config.data_dir }
}
"""),
    # A `compensate` reversing an emission, and a timer body doing the same.
    # Both are statement forms whose operand the gate reads; both must stay
    # ADMITTED, because reading a statement is only an improvement if it does
    # not also start refusing the ordinary shape. A compensation that undoes a
    # write is the ordinary shape — walking it as an UNMARKED emission refused
    # eighty-five programs in this repo's own bench corpus.
    ("emit with a compensation that emits", """
service Outbox { emission fn add(row: Str) }
service Db { emission fn execute(q: Str) }
component Writer requires db: Db provides outbox: Outbox {
  provide outbox {
    fn add(row) {
      emit       db.execute(row)
      compensate db.execute(row)
    }
  }
}
"""),
    ("timer body emitting through a declared key", """
service Log { emission fn write(msg: Str) -> Int }
component Beat requires log: Log {
  every 5s { emit log.write("beat") }
  after 2m { emit log.write("late") }
}
"""),
    ("honest provider", """
service Cache { fn put(key: Str, value: Str) }
component HonestCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""),
    ("declared emission covers the body", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}
"""),
    ("scoped declaration honored", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}
"""),
    ("two disjoint keys compose", """
service D { fn q(s: Str) -> Int }
service E { fn g(s: Str) -> Int }
component A provides db: D { provide db { fn q(s) { return 1 } } }
component B provides ev: E { provide ev { fn g(s) { return 1 } } }
"""),
    ("async op admits an async body", """
extern emission async fn http_post(url: Str, body: Str) -> Str
  = @py { return url }
service Http { emission async fn post(url: Str, body: Str) -> Str }
component Poster provides http: Http {
  provide http { async fn post(url, body) = http_post(url, body) }
}
"""),
    # per-realm G2: the same key provided in two DIFFERENT realms composes —
    # this is the multi-tenancy feature, not a conflict.
    ("same key in different realms composes", """
service Kv { fn get(k: Str) -> Opt[Str] }
component StoreOne provides kv: Kv {
  isolate kv in realm("tenant_a")
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(k) }
}
component StoreTwo provides kv: Kv {
  isolate kv in realm("tenant_b")
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(k) }
}
"""),
    # a dependency edge that is broken by realm separation: Beta requires `a`
    # in realm r2, but Alpha provides it in the shared realm — no edge, so the
    # would-be Alpha<->Beta cycle never forms and the composition admits.
    ("realm separation breaks a would-be cycle", """
service A { fn ping(tag: Str) -> Str }
service B { fn pong(tag: Str) -> Str }
component Alpha requires b: B provides a: A {
  provide a { fn ping(tag) = b.pong(tag) }
}
component Beta requires a: A provides b: B {
  isolate a in realm("r2")
  provide b { fn pong(tag) = a.ping(tag) }
}
"""),
    ("pure helper chain stays clean", """
fn normalize(k: Str) -> Str { return k }
service Cache { fn put(key: Str, value: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      let k = normalize(key)
      effect store.insert(k, value)
      undo   store.remove(k)
    }
  }
}
"""),
    # Int32 bitwise operators (item 366) are pure expressions with no bearing on
    # the cordis guarantees: a helper full of `& | ^ << >> ~` must admit exactly
    # as any other pure arithmetic does. This pins that the self-host gate walks
    # the bitwise `bin`/`un` nodes without choking (the same generic-walk path
    # the emitters' IR flows through).
    ("bitwise pure helper stays clean", """
fn mix(a: Int32, b: Int32, c: Int32) -> Int32 {
  let band = a & b
  let bor = a | b
  let bxor = a ^ c
  let shifted = a << b >> c
  let inv = ~a
  return band | bor & bxor ^ shifted | inv
}
service Cache { fn put(key: Str, value: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""),
    # G1 bare-value: a `config.<field>` read is a config access, not a bare
    # undeclared `config`, so the method admits.
    ("config field read is not a bare-value access", """
service S { fn go() -> Int }
component C provides s: S {
  config { timeout: Int }
  provide s { fn go() { let x = config.timeout   return x } }
}
"""),
    # G1 bare-value: a module fn named as a first-class VALUE resolves as a
    # callable (it is not an undeclared access).
    ("a callable named as a value resolves", """
fn helper(x: Int) -> Int { return x }
service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let f = helper   return 0 } }
}
"""),
    # G1 bare-value: an arrow's own parameter is a declared name in its body —
    # the bare-value check must see it, not refuse the arrow's `x`.
    ("an arrow parameter is in scope in its body", """
fn apply(f: (Int) -> Int, x: Int) -> Int { return f(x) }
service S { fn go(n: Int) -> Int }
component C provides s: S {
  provide s { fn go(n) { let r = apply(x => x, n)   return r } }
}
"""),
    # G1 bare-value: a match arm's payload binding is a declared name in the arm.
    ("a match-arm binding is in scope in the arm", """
service S { fn go(o: Opt[Str]) -> Str }
component C provides s: S {
  provide s { fn go(o) { return match o { Some(v) => v, None => "x" } } }
}
"""),
    # the PRELUDE rule admits an `isolate` that PRECEDES every action; `config`
    # is a component-level declaration, not an action, so it may sit before it.
    ("isolate before every action composes (prelude ok)", """
service Kv { fn get(k: Str) -> Opt[Str] }
component C requires kv: Kv {
  config { tenant: Str }
  isolate kv in realm("r1")
  let probe = effect kv.get("boot") undo probe.drop()
}
"""),
    # slice 4: `intercept` on a REQUIRED key is valid metadata wiring — admitted.
    ("intercept on a required key composes", """
service Kv { fn get(k: Str) -> Opt[Str] }
component C requires kv: Kv {
  intercept kv with { quota: 5 }
  let probe = effect kv.get("boot") undo probe.drop()
}
"""),
    # slice 4: `await` inside an ASYNC provide method is legal (the async op has
    # an in-flight window) — admitted, the twin of the sync-method refusal.
    ("await in an async provide method composes", """
service Cache { async fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    async fn get(key) {
      await Job.run("lookup")
      return store.get(key)
    }
  }
}
"""),
    # ---- slice 5a: the arrow-color LEAK trio + Async[T] coercion ----------
    # The ADMITTED twin of the checked-in `a1_async_arrow_sync_type` rejection:
    # the callback param is declared `(Str) -> Async[Str]`, so the arrow lands in
    # an `Async[T]` slot and is coerced async (`_coerce_async_args`) — the caller
    # awaits the suspension through the colored type, so it is NOT a leak. This
    # is the whole point of the coercion model: the same arrow, sync slot vs
    # async slot, refuses vs admits.
    ("arrow coerced into an Async[T] parameter admits", """
service Model { emission async fn complete(msgs: Str) -> Str }
service Runner { emission async fn run(prompt: Str) -> Str }
fn agent_loop(current: Str, complete: (Str) -> Async[Str]) -> Str {
  let resp = complete(current)
  return resp
}
component Agent requires model: Model provides runner: Runner {
  provide runner {
    async fn run(prompt) = agent_loop(prompt, msgs => emit model.complete(msgs))
  }
}
"""),
    # The module-fn twin of the coercion admit: an arrow passed into a pure fn's
    # `Async[T]` parameter is coerced, so `_refuse_leaky_pure_arrow` skips it.
    ("module-fn arrow into an Async[T] slot admits", """
extern emission async fn tick() -> Int = @py { return 1 }
fn apply_cb(cb: () -> Async[Int]) -> Int { return cb() }
fn holder() -> Int { return apply_cb(() => tick()) }
service S { fn go() -> Int }
component C provides s: S { provide s { fn go() { return 0 } } }
"""),
    # ---- slice 5b: the spawn/instance dimension ---------------------------
    # Capability attenuation NARROWING: the canonical multi-tenant router — each
    # per-tenant worker reaches only its own store (kv_a ⊆ {kv_a,kv_b}), so every
    # spawn narrows. Both workers provide `worker`, but as spawn templates neither
    # enters the static G2 table, so there is no provision conflict.
    ("per-tenant spawn narrowing composes",
     (ROOT / "examples" / "tenant_attenuation.rvl").read_text()),
    # G4/G6 spawn-emission bounds ADMIT: an `emission[kv]` method spawning a
    # target that emits only `kv` is within bound.
    ("scoped method whose spawn target stays in bound admits", """
service Store { emission[kv] fn write(row: Str) -> Int }
service Task { emission[kv] fn go() -> Int }
service Sup { emission[kv] fn run() -> Int }
component Worker requires kv: Store provides task: Task {
  provide task { fn go() { emit kv.write("x")  return 0 } }
}
component Supervisor requires kv: Store provides sup: Sup {
  provide sup { fn run() { let w = effect spawn Worker with { } undo w.dispose()  return 0 } }
}
"""),
    # The marked twin of the unmarked-handle rejection: an emission through a
    # spawn handle, correctly `emit`-marked from an `emission`-declared method —
    # the boundary is marked one level up, so it is admitted.
    ("marked emission through a spawn handle admits", """
service Net { emission[net] fn send(msg: Str) -> Int }
service Task { emission[net] fn run(prompt: Str) -> Int  fn status() -> Int }
component Worker requires net: Net provides task: Task {
  provide task { fn run(prompt: Str) { emit net.send(prompt)  return 1 }  fn status() = 0 }
}
service Sup { emission fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup { fn go(prompt: Str) { let w = effect spawn Worker with { } undo w.dispose()  emit w.task.run(prompt)  return 0 } }
}
"""),
    # ---- slice 6 (final) --------------------------------------------------
    # rule-2 param coloring: a fn that calls its async-typed parameter is
    # colored, but reaching it from an ASYNC method is legal (an async op has an
    # in-flight window) — the admitted twin of the sync-method rejection below.
    ("async method reaching a rule-2-colored fn admits", """
extern emission async fn tick() -> Int = @py { return 1 }
fn caller(cb: () -> Async[Int]) -> Int { return cb() }
service S { emission async fn go() -> Int }
component C provides s: S {
  provide s { async fn go() { let r = caller(() => tick())   return 0 } }
}
"""),
    # item 53: a `handoff` on a PROVIDED key is valid state-handoff wiring —
    # admitted (the twin of the not-a-provision / twice / prelude rejections).
    ("handoff on a provided key composes", """
service Kv { fn get(k: Str) -> Str }
component C provides kv: Kv {
  handoff kv: Str
  provide kv { fn get(k) { return k } }
}
"""),
    # a valid bound spawn to a KNOWN component with an in-bound emission handle
    # admits — the twin of the unknown-target / bind-to-a-handle rejections.
    ("a bound spawn of a known component admits", """
service Task { emission[net] fn run(prompt: Str) -> Int  fn status() -> Int }
service Net { emission[net] fn send(msg: Str) -> Int }
component Worker requires net: Net provides task: Task {
  provide task { fn run(prompt: Str) { emit net.send(prompt)  return 1 }  fn status() = 0 }
}
service Sup { emission fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup { fn go(prompt: Str) { let w = effect spawn Worker with { } undo w.dispose()  emit w.task.run(prompt)  return 0 } }
}
"""),
    # ---- item 186: multi-realm routing (item 162) --------------------------
    # A route whose every named realm has its own provider composes, with and
    # without a strategy; pinning a key AFTER routing it is NOT a refusal (the
    # reference's IsolateStmt branch does not consult `routes`, and the routed
    # key resolves per-realm at link, shadowing the single-realm table).
    ("a multi-realm bind with a provider per realm admits",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1", "r2") strategy(round_robin)
  provide api { fn go(k) { return kv.get(k) } }
}
"""),
    ("a multi-realm bind without a strategy admits",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1", "r2")
  provide api { fn go(k) { return kv.get(k) } }
}
"""),
    ("a routed key pinned afterwards still admits",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1", "r2")
  isolate kv in realm("r1")
  provide api { fn go(k) { return kv.get(k) } }
}
"""),
    # ---- item 186: the closed coloring approximation -----------------------
    # Callee collection and leak-reach STOP at a nested COERCED arrow, exactly
    # as `stop_async_arrows` does. `wrap`'s async-typed parameter is never
    # CALLED, so rule 2 does not color `wrap` and mask the difference — these
    # are the inputs that reach the approximation the gate used to carry.
    ("a coerced arrow nested in a sync arrow does not leak", """
extern emission async fn tick(n: Str) -> Str = @py { return n }
fn wrap(cb: (Str) -> Async[Str], y: Str) -> Str { return y }
fn plain(f: (Str) -> Str) -> Str { return f("a") }
service S { emission async fn go(y: Str) -> Str }
component C provides s: S {
  provide s { async fn go(y) { let r = plain(w => wrap(z => tick(z), w))   return r } }
}
"""),
    ("a fn whose only async reach is a coerced arrow stays sync", """
extern emission async fn tick(n: Str) -> Str = @py { return n }
fn wrap(cb: (Str) -> Async[Str], y: Str) -> Str { return y }
fn h(y: Str) -> Str { return wrap(z => tick(z), y) }
service S { emission fn go(y: Str) -> Str }
component C provides s: S {
  provide s { fn go(y) { let r = emit h(y)   return r } }
}
"""),
    # The other side of the bare-Upper-cased-call-head fix below: the call heads
    # that legitimately ARE Upper-cased must still resolve, or the fix would buy
    # its bypass back with a false rejection.
    ("a declared ADT case is a callable head", """
type Found = Hit(Str) | Missing
service Kv { fn get(k: Str) -> Found }
component Store provides kv: Kv {
  provide kv { fn get(k) { return Hit(k) } }
}
"""),
    ("a built-in Result constructor is a callable head", """
service Kv { fn get(k: Str) -> Result[Str, Str] }
component Store provides kv: Kv {
  provide kv { fn get(k) { return Ok(k) } }
}
"""),
    ("an Upper-cased host acquisition is a callable head", """
service Kv { fn get(k: Str) -> Str }
component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv { fn get(k) { return k } }
}
"""),

    # The ACCEPTING TWINS of the G4 cluster below. Refusing more is trivially
    # "correct" and is the failure mode a bypass test cannot see, so each new
    # refusal ships with the legitimate near-twin it must not touch.
    ("an aliased spawn-handle provision, correctly marked `emit`", """
service Task { emission[net] fn run(p: Str) -> Int }
component Worker provides task: Task {
  provide task { fn run(p) { return 1 } }
}
service Sup { emission fn go(p: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(p: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let t = w.task
      let r = emit t.run(p)
      return r
    }
  }
}
"""),
    ("a host acquisition in its bracket, the whole point of the rule", """
service S { fn go(u: Str) -> Int }
component C provides s: S {
  let m = effect Map.new() undo m.drop()
  provide s { fn go(u) = 1 }
}
"""),
    ("a host acquisition in a fn no component reaches", """
fn helper(u: Str) -> Int { let p = Map.new()   return 1 }
service S { fn go(u: Str) -> Int }
component C provides s: S { provide s { fn go(u) = 1 } }
"""),
]


# (name, source, expected tag). The message is compared too, via _agree; the
# reference's own text is the ground truth. Several are the documented
# `expected error` of a checked-in rejection fixture.
REJECTED_PROGRAMS = [
    # item 350: two environment contracts cannot both be the exhaustive list of
    # what the host must inject, and an admission check against "the" contract
    # would silently check only one of them — so the link refuses the second.
    ("two boot components", """
service Env { fn a() -> Str }
service Env2 { fn b() -> Str }
boot component B1 provides e1: Env {
  config { x: Str }
  provide e1 { fn a() = config.x }
}
boot component B2 provides e2: Env2 {
  config { y: Str }
  provide e2 { fn b() = config.y }
}
""", "BOOT"),
    ("g4 emission not declared", _fixture("g4_emission_not_declared"), "G4"),
    ("g4 capability not declared", _fixture("g4_capability_not_declared"), "G4"),
    ("g4 unmarked emission", _fixture("g4_unmarked_emission"), "G4"),
    ("a1 async extern in sync method",
     _fixture("a1_async_extern_sync_method"), "A1"),
    ("a1 async op via sync ternary",
     _fixture("a1_async_op_sync_ternary"), "A1"),
    ("g2 provision conflict", _fixture("g2_provision_conflict"), "G2"),
    ("g4 multi-hop named-call chain", """extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }
fn audit_log(msg: Str) -> Int { return audit_write(msg) }
fn write_through(key: Str) -> Int { return audit_log(key) }
service Cache { fn put(key: Str, value: Str) }
component LyingCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      let n = write_through(key)
    }
  }
}
""", "G4"),
    ("a1 transitively-colored fn in sync method", """extern emission async fn http_post(url: Str, body: Str) -> Str = @py { return url }
fn helper(u: Str) -> Str { return http_post(u, u) }
service Http { emission fn post(url: Str, body: Str) -> Str }
component Poster provides http: Http {
  provide http { fn post(url, body) = helper(url) }
}
""", "A1"),
    # G1: `db` is read but never declared in the requires row (fixture).
    ("g1 undeclared access", _fixture("g1_undeclared_access"), "G1"),
    # G1 in a provide-method body (not just setup): the receiver head resolves
    # to nothing declared, so the reference refuses it before the op.
    ("g1 undeclared receiver in method", """service Log { fn write(msg: Str) }
component Logger provides log: Log {
  provide log { fn write(msg) { emit db.execute(msg) } }
}
""", "G1"),
    # G3: Alpha and Beta each require what the other provides (fixture).
    ("g3 dependency cycle", _fixture("g3_dependency_cycle"), "G3"),
    # per-realm G2: same key, SAME realm — a conflict, and the realm is named
    # (fixture).
    ("g2 same-realm conflict", _fixture("v2_same_realm_conflict"), "G2"),
    # ---- slice 3 ---------------------------------------------------------
    # G1 bare-value: an undeclared bare `Var` used as a value in a provide
    # method body (not a call/access head) — the reference's `_plain_body`
    # var-resolution refusal.
    ("g1 undeclared bare value in method", """service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let x = nope   return 0 } }
}
""", "G1"),
    # G1 bare-value: a required key is the service path, never a bare value —
    # using it bare is the same undeclared-access refusal.
    ("g1 required key used as a bare value", """service D { fn q(s: Str) -> Int }
service S { fn go() -> Int }
component C requires d: D provides s: S {
  provide s { fn go() { let x = d   return 0 } }
}
""", "G1"),
    # G1 bare-value in a SETUP body. The reference resolves a bare head the
    # same way wherever it stands — a setup statement's operand is not a
    # weaker position than a provide method's — so each of these is the one
    # `is not a declared requirement of` diagnostic, reached through a
    # different statement form. Every statement head that takes an expression
    # gets an entry, because the operand is walked per statement KIND and a
    # form nobody listed is a form nobody checks.
    ("g1 undeclared bare value in setup effect", """service S { fn go() -> Int }
component C requires s: S { effect nope }
""", "G1"),
    ("g1 undeclared bare value in setup emit", """service S { emission fn go() -> Int }
component C requires s: S { emit nope }
""", "G1"),
    ("g1 undeclared bare value in setup await", """service S { fn go() -> Int }
component C requires s: S { await nope }
""", "G1"),
    ("g1 undeclared bare value in a setup let", """service S { fn go() -> Int }
component C requires s: S { let x = effect nope }
""", "G1"),
    ("g1 undeclared bare value in a setup undo", """service S { fn go() -> Int }
component C requires s: S { effect s.go() undo nope }
""", "G1"),
    ("g1 undeclared bare value in a setup compensate", """service S { emission fn go() -> Int }
component C requires s: S { emit s.go() compensate nope }
""", "G1"),
    # G1 inside a TIMER body. `every`/`after` open a block of ordinary
    # statements, and the reference resolves their heads exactly as it does the
    # statements around them. A reader that steps over the block steps over
    # everything in it.
    ("g1 undeclared bare value in an every body", """service S { emission fn go() -> Int }
component C requires s: S { every 5s { emit nope } }
""", "G1"),
    ("g1 undeclared bare value in an after body", """service S { emission fn go() -> Int }
component C requires s: S { after 2m { emit nope } }
""", "G1"),
    # G1 under an `await` OPERAND. `await` is a keyword, so an operand that
    # opens with one stopped the expression grammar dead; the statement became
    # a skip and its heads were never resolved. A suspension is where a
    # component reaches outside itself, which makes it the last operand a gate
    # should decline to read.
    ("g1 undeclared head awaited in a let-effect", """service S { async fn go() -> Int }
component C requires s: S { let h = effect await nope() }
""", "G1"),
    ("g1 undeclared head awaited in an effect", """service S { async fn go() -> Int }
component C requires s: S { effect await nope() }
""", "G1"),
    # A1 passed_async: an async extern referenced as a function value in a
    # provide method — refused even when the method's op is declared `async`.
    ("a1 async extern used as a value (async method)",
     """extern emission async fn tick() -> Int = @py { return 1 }
service S { emission async fn go() -> Int }
component C provides s: S {
  provide s { async fn go() { let f = tick   return 0 } }
}
""", "A1"),
    # A1 passed_async: a transitively-colored fn referenced as a value.
    ("a1 colored fn used as a value", """extern emission async fn tick() -> Int = @py { return 1 }
fn helper() -> Int { return tick() }
service S { emission fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let f = helper   return 0 } }
}
""", "A1"),
    # A1 passed_async: an async callable handed in as a call ARGUMENT is a value
    # use too.
    ("a1 async extern passed as an argument", """extern emission async fn tick(n: Int) -> Int = @py { return n }
service Db { emission fn exec(n: Int) -> Int }
service S { emission fn go() -> Int }
component C requires db: Db provides s: S {
  provide s { fn go() { emit db.exec(tick)   return 0 } }
}
""", "A1"),
    # A1 setup/activation async-reach: a setup body that reaches an async extern
    # cannot suspend a fiber.
    ("a1 setup body reaches an async extern", """extern emission async fn tick() -> Int = @py { return 1 }
service S { fn go() -> Int }
component C provides s: S {
  emit tick()
  provide s { fn go() { return 0 } }
}
""", "A1"),
    # G4 first-class emission value: a plain provider that hands an emission
    # callable off as a value reaches an emission (the value-use G4 evidence).
    ("g4 emission callable passed as a value", """extern emission async fn tick() -> Int = @py { return 1 }
service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let f = tick   return 0 } }
}
""", "G4"),
    # PRELUDE: an `isolate` after a setup effect is refused (fixture).
    ("prelude isolate after effect", _fixture("v2_isolate_after_effect"),
     "PRELUDE"),
    # PRELUDE: an `intercept` after a setup effect is refused.
    ("prelude intercept after effect", """service D { fn q(s: Str) -> Int }
service S { fn go() -> Int }
component C requires d: D provides s: S {
  let store = effect Map.new() undo store.drop()
  intercept d with { retries: 3 }
  provide s { fn go() { return 0 } }
}
""", "PRELUDE"),
    # PRELUDE: an `isolate` after the `provide` block is refused too.
    ("prelude isolate after provide", """service D { fn q(s: Str) -> Int }
service S { fn go() -> Int }
component C requires d: D provides s: S {
  provide s { fn go() { return 0 } }
  isolate d in realm("r1")
}
""", "PRELUDE"),
    # ---- slice 4 ---------------------------------------------------------
    # `intercept` metadata validation: an intercept on a key that is not a
    # declared requirement is the reference's G1 head-resolution refusal
    # (fixture — the exact message names the undeclared key and the component).
    ("intercept on an undeclared key (G1)",
     _fixture("v2_intercept_undeclared"), "G1"),
    # `intercept` on a PROVIDED key: a provision has nothing to intercept — the
    # reference refuses with its provision-specific wording (fixture).
    ("intercept on a provision (G1)",
     _fixture("v2_intercept_on_provision"), "G1"),
    # `intercept` uniqueness: the same key intercepted twice in one component.
    ("intercept twice on one key (G1)", """service D { fn q(s: Str) -> Int }
service S { fn go() -> Int }
component C requires d: D provides s: S {
  intercept d with { retries: 3 }
  intercept d with { retries: 4 }
  provide s { fn go() { return 0 } }
}
""", "G1"),
    # A1 await-outside-body: a SYNC provide method containing an `await` has no
    # in-flight window to suspend — refused (fixture).
    ("await in a sync provide method (A1)",
     _fixture("a1_await_in_method"), "A1"),
    # services-2.0 signature parity: a sync `fn` implementing an `async fn`
    # service op is refused — the async marker is part of the interface (fixture).
    ("async signature mismatch (A1)",
     _fixture("v2_async_signature_mismatch"), "A1"),
    # item 168 regression: the WHOLE component body on ONE source line. Before
    # the statement-reader fix the gate's line-based reader let the `let` line
    # swallow the trailing `isolate`/`provide`, so it ADMITTED while the
    # reference refuses PRELUDE. The reader now ends a run at a structural
    # boundary, so both refuse.
    ("single-line body: isolate after effect (PRELUDE)",
     'service D { fn q(s: Str) -> Int } service S { fn go() -> Int } '
     'component C requires d: D provides s: S { '
     'let store = effect Map.new() undo store.drop() '
     'isolate d in realm("r1") provide s { fn go() { return 0 } } }',
     "PRELUDE"),
    # ---- slice 5a: the arrow-color LEAK trio -----------------------------
    # `_refuse_leaky_arrow`: a sync-typed callback arrow that reaches an async
    # service operation carries no async color — the caller would receive an
    # unawaited suspension (the checked-in item-92 / finding-#21 fixture).
    ("a1 leaky arrow (sync-typed callback)",
     _fixture("a1_async_arrow_sync_type"), "A1"),
    # The same arrow passed into a SYNC parameter slot leaks, while its Async[T]
    # twin (in ACCEPTED_PROGRAMS) admits — the coercion model's two halves.
    ("a1 leaky arrow in a sync fn-parameter slot",
     """extern emission async fn tick(n: Str) -> Str = @py { return n }
fn apply(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service S { emission async fn go() -> Str }
component C provides s: S {
  provide s { async fn go() { let r = apply(msgs => tick(msgs), "x")   return r } }
}
""", "A1"),
    # `_refuse_leaky_pure_arrow`: the module-fn twin — a sync arrow in a pure fn
    # body reaching an async callable (named, since pure fns have no req keys).
    ("a1 leaky arrow in a module fn (pure twin)",
     """extern emission async fn tick() -> Int = @py { return 1 }
fn holder(g: (Int) -> Int) -> Int { let h = x => tick()   return 0 }
service S { fn go() -> Int }
component C provides s: S { provide s { fn go() { return 0 } } }
""", "A1"),
    # module-fn first-class async value: an async callable referenced as a VALUE
    # in a module fn body — an arrow type carries no async color.
    ("a1 module fn uses an async callable as a value",
     """extern emission async fn tick() -> Int = @py { return 1 }
fn holder() -> Int { let f = tick   return 0 }
service S { fn go() -> Int }
component C provides s: S { provide s { fn go() { return 0 } } }
""", "A1"),
    # ---- slice 5b: the spawn/instance dimension --------------------------
    # capability attenuation (item 66): an activation-body spawn that grants a
    # child a boundary its spawner does not hold is widening — refused (the
    # checked-in fixture; `code="G4"`).
    ("g4 spawn widens a child's capability",
     _fixture("g4_spawn_widens_capability"), "G4"),
    # an unmarked emission reached THROUGH a spawn handle (`w.task.run`) must
    # still be `emit`-marked, exactly as a required-service emission (fixture).
    ("g4 unmarked emission through a spawn handle",
     _fixture("g4_unmarked_handle_emission"), "G4"),
    # G4/G6 spawn-emission bounds (decision 8): a PLAIN provide method spawning
    # an emitting target — the emission cannot escape the (absent) bound by
    # moving into a child.
    ("g4 plain method spawns an emitting target", """service Store { emission[kv] fn write(row: Str) -> Int }
service Task { emission[kv] fn go() -> Int }
service Sup { fn run() -> Int }
component Worker requires kv: Store provides task: Task {
  provide task { fn go() { emit kv.write("x")  return 0 } }
}
component Supervisor provides sup: Sup {
  provide sup { fn run() { let w = effect spawn Worker with { } undo w.dispose()  return 0 } }
}
""", "G4"),
    # G4/G6 spawn-emission bounds: an `emission[other]` method spawning a target
    # that emits outside its scope (`kv` ∉ {other}).
    ("g4 scoped method spawns a target that widens its caps", """service Store { emission[kv] fn write(row: Str) -> Int }
service Task { emission[kv] fn go() -> Int }
service Sup { emission[other] fn run() -> Int }
component Worker requires kv: Store provides task: Task {
  provide task { fn go() { emit kv.write("x")  return 0 } }
}
component Supervisor requires other: Store provides sup: Sup {
  provide sup { fn run() { let w = effect spawn Worker with { } undo w.dispose()  return 0 } }
}
""", "G4"),
    # ---- slice 6 (final) --------------------------------------------------
    # async coloring rule 2 (item 92 §3): a fn that CALLS its async-typed
    # parameter is colored; a SYNC method reaching it has no in-flight window.
    # Both `caller` (colored by rule 2) and `tick` (reached through the arrow)
    # are named, sorted — the message the reference computes.
    ("a1 rule-2 param-colored fn in a sync method", """extern emission async fn tick() -> Int = @py { return 1 }
fn caller(cb: () -> Async[Int]) -> Int { return cb() }
service S { emission fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let r = caller(() => tick())   return 0 } }
}
""", "A1"),
    # code-less spawn-form: a spawn naming a component not in this composition
    # (`_lower_spawn`'s unknown-target refusal).
    ("spawn names an unknown component", """service Sup { fn run() -> Int }
component Supervisor provides sup: Sup {
  provide sup { fn run() { let w = effect spawn Nope with { } undo w.dispose()  return 0 } }
}
""", "SPAWN"),
    # code-less spawn-form: an UNBOUND `effect spawn` in a provide method — a
    # spawn's teardown needs a handle to name (decision 2, bind-to-a-handle).
    ("unbound spawn in a method is refused", """service Task { fn go() -> Int }
component Worker provides task: Task { provide task { fn go() { return 0 } } }
service Sup { fn run() -> Int }
component Supervisor provides sup: Sup {
  provide sup { fn run() { effect spawn Worker with { } undo dispose()  return 0 } }
}
""", "SPAWN"),
    # code-less spawn-form: an UNBOUND `effect spawn` in an activation body.
    ("unbound spawn in a setup body is refused", """service Task { fn go() -> Int }
component Worker provides task: Task { provide task { fn go() { return 0 } } }
component Supervisor requires t: Task {
  effect spawn Worker with { } undo dispose()
}
""", "SPAWN"),
    # item 53: a `handoff` after an action is out of prelude order — the same
    # prelude rule `isolate`/`intercept` obey (classified PRELUDE).
    ("handoff after an effect is refused (prelude)", """service Kv { fn get(k: Str) -> Str }
component C provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  handoff kv: Str
  provide kv { fn get(k) { return k } }
}
""", "PRELUDE"),
    # item 53: a `handoff` targets a key this component PROVIDES; a required
    # (non-provided) key has no state to hand off (code-less, classified HANDOFF).
    ("handoff on a non-provided key is refused", """service Kv { fn get(k: Str) -> Str }
component C requires kv: Kv {
  handoff kv: Str
  let v = effect kv.get("x") undo kv.get("x")
}
""", "HANDOFF"),
    # item 53: at most one `handoff` per component (one activation frame, one
    # state shape) — code-less, classified HANDOFF.
    ("two handoffs in one component are refused", """service Kv { fn get(k: Str) -> Str }
component C provides kv: Kv {
  handoff kv: Str
  handoff kv: Int
  provide kv { fn get(k) { return k } }
}
""", "HANDOFF"),
    # isolate target validation: `isolate` names a key from the component header
    # (a requirement or a provision); an undeclared key is refused (code-less,
    # the declared-wiring G1 family).
    ("isolate on an undeclared key is refused (G1)", """service Kv { fn get(k: Str) -> Str }
component C requires kv: Kv {
  isolate nope in realm("r1")
  let v = effect kv.get("x") undo kv.get("x")
}
""", "G1"),
    # isolate uniqueness: a key is pinned to one realm at most once (code-less,
    # classified G1).
    ("a key isolated twice is refused (G1)", """service Kv { fn get(k: Str) -> Str }
component C requires kv: Kv {
  isolate kv in realm("r1")
  isolate kv in realm("r2")
  let v = effect kv.get("x") undo kv.get("x")
}
""", "G1"),
    # ---- item 186: multi-realm routing validation (item 162) ---------------
    # The routing form's four own refusals, its undeclared-target G1, and the
    # link-time per-realm provider check. Each program is otherwise clean, so
    # the routing verdict is the only one the reference can reach.
    ("routing a provision is refused (ROUTE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate api in realms("r1")
  provide api { fn go(k) { return kv.get(k) } }
}
""", "ROUTE"),
    ("routing an undeclared key is refused (G1)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate nope in realms("r1")
  provide api { fn go(k) { return kv.get(k) } }
}
""", "G1"),
    ("a pinned key cannot also be routed (ROUTE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realm("r1")
  isolate kv in realms("r1", "r2")
  provide api { fn go(k) { return kv.get(k) } }
}
""", "ROUTE"),
    ("a key routed twice is refused (ROUTE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1")
  isolate kv in realms("r2")
  provide api { fn go(k) { return kv.get(k) } }
}
""", "ROUTE"),
    ("an unknown routing strategy is refused (ROUTE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1", "r2") strategy(round_robbin)
  provide api { fn go(k) { return kv.get(k) } }
}
""", "ROUTE"),
    ("a routed realm with no provider is refused (ROUTE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  isolate kv in realms("r1", "r9")
  provide api { fn go(k) { return kv.get(k) } }
}
""", "ROUTE"),
    ("a route after a provide block is refused (PRELUDE)",
     _ROUTE_PROVIDERS + """component Router requires kv: Kv provides api: Api {
  provide api { fn go(k) { return kv.get(k) } }
  isolate kv in realms("r1", "r2")
}
""", "PRELUDE"),

    # The G4 cluster PR #331 taught the reference to refuse. `crates/revl-gate`
    # is generated from `selfhost/lower.rvl`, so until these agreed the SHIPPED
    # gate admitted four programs the reference refuses — the fail-open
    # direction. The message is compared too (`_agree`), which is the point:
    # the crate promises its refusals are the reference's verbatim.
    ("g4 unmarked emission through an aliased spawn-handle provision",
     _fixture("g4_unmarked_alias_emission"), "G4"),
    ("g4 host acquire in a provide-method let",
     _fixture("g4_method_host_acquire"), "G4"),
    ("g4 host acquire in a teardown slot",
     _fixture("g4_undo_host_acquire"), "G4"),
    ("g4 host acquire in a component-reachable fn body",
     _fixture("g4_fn_body_host_acquire"), "G4"),
    # the same rule at the two positions no checked-in fixture occupies
    ("g4 host acquire in an emit expression", """
service S { fn go(u: Str) -> Int }
component C provides s: S {
  let m = effect Map.new() undo m.drop()
  provide s {
    fn go(u) {
      emit Pool.open(u, 1)
      return 1
    }
  }
}
""", "G4"),
    ("g4 host acquire wrapped inside an effect bracket's acquisition", """
fn wrap(x: Int) -> Int { return x }
service S { fn go(u: Str) -> Int }
component C provides s: S {
  let m = effect wrap(Pool.open("a", 1)) undo m.drop()
  provide s { fn go(u) = 1 }
}
""", "G4"),
    ("g4 host acquire in a compensation", """
service Out { emission fn add(u: Str) -> Int }
service S { emission fn go(u: Str) -> Int }
component C requires o: Out provides s: S {
  provide s {
    fn go(u) {
      emit o.add(u) compensate Pool.open(u, 1)
      return 1
    }
  }
}
""", "G4"),
    # A bare Upper-cased CALL head is not a host acquisition. The reference's
    # host branch is `head[:1].isupper() and ops and ops[0].args is not None`
    # (lower.py `_lower_postfix`) — it needs a `.method(...)` after the head, so
    # `Map.new()` takes it and `Row(k)` does not. The gate resolved every
    # Upper-cased head and admitted the whole family; both positions are pinned
    # here, a provide method and an activation body, because the two reach the
    # check down different paths.
    ("a bare Upper-cased call head in a method is undeclared (G1)", """
service Cache { fn put(k: Str, v: Str) }
component MemCache provides cache: Cache {
  provide cache {
    fn put(k, v) {
      let row = Row(k)
    }
  }
}
""", "G1"),
    ("a bare Upper-cased call head in an activation body is undeclared (G1)", """
service Log { fn note(m: Str) }
component Chatty requires log: Log provides out: Log {
  effect Audit()
  provide out { fn note(m) { let x = m } }
}
""", "G1"),
    # The G4 evidence list is DEDUPED, first-seen order — the reference collects
    # it through `_method_emissions`'s `note`, which carries a `seen` set. A
    # body crossing the same seam twice used to draw
    # "reaches `db.run`, `db.run`" from the gate and "reaches `db.run`" from the
    # reference: the same refusal, spelled differently, which is exactly what
    # `crates/revl-gate`'s byte-agreement promise forbids.
    ("G4 evidence names a repeated emission once", """
service Db { emission fn run(sql: Str) -> Int }
service Cache { fn put(k: Str) }
component Twice requires db: Db provides cache: Cache {
  provide cache {
    fn put(k) {
      emit db.run(k)
      emit db.run(k)
    }
  }
}
""", "G4"),
    ("g4 host acquire two fn hops from a component body", """
fn inner(u: Str) -> Int { let p = Pool.open(u, 1)   return 1 }
fn outer(u: Str) -> Int { return inner(u) }
service S { fn go(u: Str) -> Int }
component C provides s: S { provide s { fn go(u) = outer(u) } }
""", "G4"),
]


@pytest.mark.parametrize("name_src", ACCEPTED_PROGRAMS,
                         ids=[n for n, _ in ACCEPTED_PROGRAMS])
def test_accepted_programs_agree(admit, name_src):
    name, src = name_src
    assert _ref(src) == ("", ""), f"corpus bug: reference refuses {name}"
    _agree(admit, src)


@pytest.mark.parametrize("case", REJECTED_PROGRAMS,
                         ids=[n for n, _, _ in REJECTED_PROGRAMS])
def test_rejected_programs_agree(admit, case):
    name, src, tag = case
    ref_tag, _ = _ref(src)
    assert ref_tag == tag, \
        f"corpus bug: reference tag for {name} is {ref_tag!r}, expected {tag!r}"
    _agree(admit, src)


# ---------------------------------------------------------------- G2 fuzz

# A real differential over G2: random compositions of clean single-key
# providers over a small key pool. The only possible refusal is a provision
# conflict (or none), so the two linkers are each other's oracle on both the
# verdict AND the "provided by both X and Y" wording — including the entry
# order that decides which two components the message names.
_KEYS = ["a", "b", "c"]


def _compose(rng: random.Random) -> str:
    n = rng.randint(2, 5)
    lines = ["service S { fn op(x: Str) -> Str }"]
    for i in range(n):
        key = rng.choice(_KEYS)
        lines.append(
            f"component C{i} provides {key}: S {{ "
            f"provide {key} {{ fn op(x) {{ return x }} }} }}")
    return "\n".join(lines)


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_compositions_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _compose(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # the generator can only produce an admit or a G2 conflict
        assert ref_tag in ("", "G2"), f"corpus bug: reference tag {ref_tag!r}"
        _agree(admit, src)


# --------------------------------------------------------- per-realm G2 fuzz

# The multi-tenancy differential: random single-key providers over a small key
# pool, each key optionally isolated into one of a few realms (the empty realm
# = no isolate = the shared realm). Provision disjointness is now per-(key,
# realm), so the two linkers must agree on the verdict AND — for a conflict —
# the "in realm `<r>`" wording and which two components the message names. No
# requires, so the only possible refusal is a per-realm G2 conflict.
_REALMS = ["", "r1", "r2"]


def _compose_realms(rng: random.Random) -> str:
    n = rng.randint(2, 5)
    lines = ["service S { fn op(x: Str) -> Str }"]
    for i in range(n):
        key = rng.choice(_KEYS)
        realm = rng.choice(_REALMS)
        iso = f"  isolate {key} in realm(\"{realm}\")\n" if realm else ""
        lines.append(
            f"component C{i} provides {key}: S {{\n{iso}"
            f"  provide {key} {{ fn op(x) {{ return x }} }}\n}}")
    return "\n".join(lines)


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_realm_compositions_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _compose_realms(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # per-realm composition admits or conflicts under G2, nothing else
        assert ref_tag in ("", "G2"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# ---------------------------------------------------------------- G4 fuzz

# Random provider bodies over one required emission op, marked or not: the two
# gates must agree on verdict AND message for the plain-provider G4 check and
# the unmarked-emission G4 check, including the (frequent) clean case.
def _provider(rng: random.Random) -> str:
    declared_emission = rng.random() < 0.5
    marked = rng.random() < 0.5
    call = ("emit db.execute(key)" if marked else "let r = db.execute(key)")
    decl = "emission fn put(key: Str)" if declared_emission else "fn put(key: Str)"
    body_reaches = rng.random() < 0.7
    inner = call if body_reaches else "let k = key"
    return (
        "service Database { emission fn execute(sql: Str) -> Int }\n"
        f"service Cache {{ {decl} }}\n"
        "component C requires db: Database provides cache: Cache {\n"
        "  provide cache {\n"
        f"    fn put(key) {{ {inner} }}\n"
        "  }\n"
        "}\n")


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_providers_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _provider(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # a provider over one emission op is admitted or refused by G4 only
        assert ref_tag in ("", "G4"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# ------------------------------------------------------------- prelude fuzz

# A real differential over the realm PRELUDE rule: one component with a setup
# effect and a single realm/metadata declaration (`isolate` or `intercept`)
# placed either BEFORE the effect (admits) or AFTER it (refused). `config` is a
# declaration, not an action, so it never shifts the boundary. The only two
# outcomes are an admit and a PRELUDE refusal, so the two gates are each other's
# oracle on the verdict AND the `<kw>`-specific wording.
def _prelude(rng: random.Random) -> str:
    after = rng.random() < 0.5
    kw = rng.choice(["isolate", "intercept"])
    decl = ('isolate d in realm("r1")' if kw == "isolate"
            else "intercept d with { retries: 3 }")
    effect = "let store = effect Map.new() undo store.drop()"
    lines = ["service D { fn q(s: Str) -> Int }", "service S { fn go() -> Int }",
             "component C requires d: D provides s: S {"]
    lines += (["  " + effect, "  " + decl] if after
              else ["  " + decl, "  " + effect])
    lines += ["  provide s { fn go() { return 0 } }", "}"]
    return "\n".join(lines)


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_preludes_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _prelude(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # the generator can only produce an admit or a PRELUDE refusal
        assert ref_tag in ("", "PRELUDE"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# ---------------------------------------------------------- G1 bare-value fuzz

# The bare-value name-resolution differential: a provide method whose body reads
# a single bare name in value position — either the method's own parameter (a
# declared name, admits) or an undeclared identifier (the G1 access refusal).
# The only two outcomes are an admit and a G1 refusal naming the culprit, so the
# two gates agree on the verdict AND the "`<x>` is not a declared requirement"
# wording.
def _bare_value(rng: random.Random) -> str:
    name = "p" if rng.random() < 0.5 else "ghost"
    return ("service S { fn go(p: Str) -> Str }\n"
            "component C provides s: S {\n"
            f"  provide s {{ fn go(p) {{ let x = {name}   return p }} }}\n}}\n")


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_bare_values_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _bare_value(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # a lone bare-value read is admitted or refused by G1 only
        assert ref_tag in ("", "G1"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# ------------------------------------------------------- leaky-arrow fuzz

# The Async[T]-coercion differential (slice 5a): the finding-#21 `agent_loop`
# shape, with the callback parameter's declared type flipped between a sync
# `(Str) -> Str` and an async `(Str) -> Async[Str]`. The arrow argument reaches
# an async service op either way; only the async-typed slot coerces it (admits),
# the sync slot leaks (A1). The body of the callback is also varied to a pure
# arrow (reaches nothing async) so the sync slot can also admit — the two gates
# are each other's oracle on the coercion decision AND the leak wording.
def _leaky_arrow(rng: random.Random) -> str:
    async_slot = rng.random() < 0.5
    reaches = rng.random() < 0.5
    cb = "(Str) -> Async[Str]" if async_slot else "(Str) -> Str"
    arrow = "msgs => emit model.complete(msgs)" if reaches else 'msgs => "x"'
    return (
        "service Model { emission async fn complete(msgs: Str) -> Str }\n"
        "service Runner { emission async fn run(prompt: Str) -> Str }\n"
        f"fn agent_loop(current: Str, complete: {cb}) -> Str {{\n"
        "  let resp = complete(current)\n"
        "  return resp\n"
        "}\n"
        "component Agent requires model: Model provides runner: Runner {\n"
        f"  provide runner {{ async fn run(prompt) = agent_loop(prompt, {arrow}) }}\n"
        "}\n")


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_leaky_arrows_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _leaky_arrow(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # the callback either coerces (admit) or leaks (A1) — nothing else
        assert ref_tag in ("", "A1"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# ---------------------------------------------- spawn attenuation fuzz

# The capability-attenuation differential (slice 5b): a supervisor that holds a
# random subset of {kv_a, kv_b} (its `requires`) spawns, in its activation body,
# a leaker that emits exactly one of them. The spawn narrows (admits) when the
# leaked key is held, and widens (G4) when it is not — the two gates are each
# other's oracle on the verdict AND the "granting it … holds only …" wording,
# including which held set the message names.
_STORE = {"kv_a": ("StoreA", "write_a"), "kv_b": ("StoreB", "write_b")}


def _spawn_atten(rng: random.Random) -> str:
    held = rng.choice([["kv_a"], ["kv_b"], ["kv_a", "kv_b"]])
    reach = rng.choice(["kv_a", "kv_b"])
    svc_a, op_a = _STORE["kv_a"]
    svc_b, op_b = _STORE["kv_b"]
    rsvc, rop = _STORE[reach]
    reqs = " ".join(f"requires {k}: {_STORE[k][0]}" for k in held)
    return (
        f"service {svc_a} {{ emission[kv_a] fn {op_a}(r: Str) -> Int }}\n"
        f"service {svc_b} {{ emission[kv_b] fn {op_b}(r: Str) -> Int }}\n"
        "service Task { emission fn go() -> Int }\n"
        f"component Leaker requires {reach}: {rsvc} provides task: Task {{\n"
        f"  provide task {{ fn go() {{ emit {reach}.{rop}(\"x\")  return 0 }} }}\n"
        "}\n"
        f"component Supervisor {reqs} {{\n"
        "  let l = effect spawn Leaker with { } undo l.dispose()\n"
        "}\n")


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_spawn_attenuation_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _spawn_atten(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        # a narrowing spawn admits; a widening spawn is refused by G4 — nothing else
        assert ref_tag in ("", "G4"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# --------------------------------------------------- in-file test audit

# The 169 lesson, systematized: every program a `test` block in lower.rvl
# hand-asserts is routed through the differential oracle here, so the .rvl's own
# eyeballed literals cannot silently drift from the reference (the ground truth).
# The programs are made reference-clean (real `@backend` bodies, returns, valid
# stdlib) precisely so their ONLY reference verdict is the in-slice one the block
# asserts.
def _infile_programs() -> list[str]:
    text = (ROOT / "selfhost" / "lower.rvl").read_text()
    section = text.split("======= tests")[-1]
    progs: list[str] = []
    for head in ("admit_src(", "admit_tag("):
        idx = 0
        while True:
            hit = section.find(head, idx)
            if hit == -1:
                break
            j = hit + len(head)
            if section[j:j + 3] == '"""':
                end = section.index('"""', j + 3)
                progs.append(section[j + 3:end])
                idx = end + 3
            elif section[j] == '"':
                end = section.index('"', j + 1)
                progs.append(section[j + 1:end])
                idx = end + 1
            else:
                idx = j
    return progs


def test_in_file_test_programs_agree(admit):
    progs = _infile_programs()
    assert len(progs) >= 25, f"expected the in-file programs, found {len(progs)}"
    for src in progs:
        _agree(admit, src)


# ------------------------------------------------------ multi-realm route fuzz

# Random routed compositions (item 162, mirrored under roadmap item 186): a few
# single-key providers scattered over three realms, and one consumer routing the
# key across a random realm subset with a random (sometimes misspelled)
# strategy. Providers take DISTINCT realms, so per-realm G2 never fires and the
# only reachable verdicts are the routing ones: a realm with no provider is the
# link-time refusal, an unknown strategy is refused earlier, while the component
# is read. (A same-realm provider pair is deliberately not generated: the
# reference's collect-all sink orders its diagnostics by LINE, so an
# earlier-line G2 outranks a later-line component refusal, while this gate
# reports the first refusal it reaches. That order gap is pre-existing — it
# already separates the two on `isolate … twice` — and is not a routing
# property.)
_ROUTE_REALMS = ["r1", "r2", "r3"]
_ROUTE_STRATEGIES = [None, "round_robin", "least_loaded", "random", "sticky",
                     "round_robbin", "roundrobin"]


def _compose_routes(rng: random.Random) -> str:
    lines = ["service Kv { fn get(k: Str) -> Str }",
             "service Api { fn go(k: Str) -> Str }"]
    for i, realm in enumerate(rng.sample(_ROUTE_REALMS, rng.randint(1, 3))):
        lines.append(
            f"component Store{i} provides kv: Kv {{\n"
            f'  isolate kv in realm("{realm}")\n'
            f"  provide kv {{ fn get(k) {{ return k }} }}\n}}")
    picked = rng.sample(_ROUTE_REALMS, rng.randint(1, 3))
    labels = ", ".join('"%s"' % r for r in picked)
    strategy = rng.choice(_ROUTE_STRATEGIES)
    clause = f"  isolate kv in realms({labels})"
    if strategy is not None:
        clause += f" strategy({strategy})"
    lines.append("component Router requires kv: Kv provides api: Api {\n"
                 + clause
                 + "\n  provide api { fn go(k) { return kv.get(k) } }\n}")
    return "\n".join(lines)


@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("seed", range(24))
def test_generated_routes_agree(admit, seed, oneline):
    rng = random.Random(seed)
    for _ in range(20):
        src = _compose_routes(rng)
        if oneline:
            src = _oneline(src)
        ref_tag, _ = _ref(src)
        assert ref_tag in ("", "ROUTE"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)


# --------------------------------------------------- nested coerced-arrow fuzz

# The inputs that reach roadmap item 186's residual coloring approximation, and
# the neighbours that do not. Three independent switches:
#   * the inner callee's callback parameter is `Async[T]`-typed or plain — the
#     first coerces (and async-stamps) the arrow handed to it, the second does
#     not, so the arrow leaks;
#   * that callee CALLS its parameter or ignores it — calling it is async
#     coloring rule 2, which colors the callee and MASKS the pruning (this is
#     why the approximation needed an async-typed-but-uncalled parameter to be
#     reachable at all);
#   * the arrow is nested one level down inside a plain sync arrow, or passed
#     directly — nesting is what makes the outer arrow's own reach the question.
# The gate must agree on every combination, which pins both the prune and the
# absence of a prune.
@pytest.mark.parametrize("oneline", [False, True])
@pytest.mark.parametrize("async_slot", [False, True])
@pytest.mark.parametrize("calls_param", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("async_method", [False, True])
def test_nested_coerced_arrows_agree(admit, oneline, async_slot, calls_param,
                                     nested, async_method):
    cb_type = "(Str) -> Async[Str]" if async_slot else "(Str) -> Str"
    wrap_body = "return cb(y)" if calls_param else "return y"
    call = ("plain(w => wrap(z => tick(z), w))" if nested
            else "wrap(z => tick(z), y)")
    decl = "emission async fn go(y: Str) -> Str" if async_method \
        else "emission fn go(y: Str) -> Str"
    method = "async fn go(y)" if async_method else "fn go(y)"
    src = f"""extern emission async fn tick(n: Str) -> Str = @py {{ return n }}
fn wrap(cb: {cb_type}, y: Str) -> Str {{ {wrap_body} }}
fn plain(f: (Str) -> Str) -> Str {{ return f("a") }}
service S {{ {decl} }}
component C provides s: S {{
  provide s {{ {method} {{ let r = {call}   return r }} }}
}}
"""
    if oneline:
        src = _oneline(src)
    ref_tag, _ = _ref(src)
    assert ref_tag in ("", "A1", "G4"), \
        f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
    _agree(admit, src)


# ---------------------------------------------------------------------------
# A KNOWN, DELIBERATE divergence, pinned rather than left as folklore
# ---------------------------------------------------------------------------

_MULTI_REFUSAL = """service D { fn q(s: Str) -> Int }
service Bus { emission fn publish(topic: Str) }
component A provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component B provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component Z requires bus: Bus { effect bus.publish("x") undo bus.publish("y") }
"""


def test_which_refusal_wins_diverges_when_a_program_has_several(admit):
    """Roadmap 419c, pinned as a documented limitation, NOT as agreement.

    `admit_src` returns the FIRST refusal it reaches, in its own fixed phase
    order (module fns, then per component, then spawn bounds, then the G2/G3
    link). The reference collects diagnostics and orders them by LINE (item
    386's sink). So on a program carrying BOTH an earlier-line link refusal and
    a later-line component refusal the two disagree: genuinely different
    refusals, both true of the program, chosen by different rules.

    Closing it means a line number on every gate refusal plus a collecting sink
    in `selfhost/lower.rvl`, which is item 186's own deferred work; `admit_src`
    is also what `tools/build_gate_crate.py` digests, so its phase order is not
    a local edit. Until then the corpus above deliberately never builds a
    multi-refusal program (see `_ROUTE_PROVIDERS`, which isolates its two
    providers into distinct realms so the routing verdict is the only one in
    play), and this test is the only place the divergence is written down.

    When this test reds because the two now agree, delete it and tick 419c."""
    ref_tag, _ = _ref(_MULTI_REFUSAL)
    got = admit(_MULTI_REFUSAL)
    got_tag = got.split("|", 1)[0]
    # both refuse, and both refusals are true of this program
    assert ref_tag == "G2" and got_tag == "G4", (ref_tag, got_tag)
    # the reference picks the earlier LINE (the duplicate provision, line 4);
    # the gate picks the earlier PHASE (the component loop precedes the link).
    assert _ref(_MULTI_REFUSAL)[1].startswith("provision conflict")
    assert "must be marked `emit`" in got
