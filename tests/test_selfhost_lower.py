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

The slice mirrors three guarantees, in the reference's own checking order
(per-component G4 then A1 during lowering, then G2 at link):
  * G4 (capability containment) — a plain-declared provider that reaches an
    emission; an `emission[caps]` provider that emits outside its scope; an
    unmarked required-service emission call.
  * A1 (item 117) — a sync provide method that reaches an async callable (an
    async extern or a transitively-colored fn) or an async service operation.
  * G2 (provision disjointness) — two components providing one key (shared
    realm).

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
    if "is not a declared requirement of" in m:
        return "G1"
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

# Programs the reference admits — the gate must admit them too. Kept
# reference-clean (no out-of-slice defect), so "" is the only agreement.
ACCEPTED_PROGRAMS = [
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
]


# (name, source, expected tag). The message is compared too, via _agree; the
# reference's own text is the ground truth. Several are the documented
# `expected error` of a checked-in rejection fixture.
REJECTED_PROGRAMS = [
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


@pytest.mark.parametrize("seed", range(24))
def test_generated_compositions_agree(admit, seed):
    rng = random.Random(seed)
    for _ in range(20):
        src = _compose(rng)
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


@pytest.mark.parametrize("seed", range(24))
def test_generated_realm_compositions_agree(admit, seed):
    rng = random.Random(seed)
    for _ in range(20):
        src = _compose_realms(rng)
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


@pytest.mark.parametrize("seed", range(24))
def test_generated_providers_agree(admit, seed):
    rng = random.Random(seed)
    for _ in range(20):
        src = _provider(rng)
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


@pytest.mark.parametrize("seed", range(24))
def test_generated_preludes_agree(admit, seed):
    rng = random.Random(seed)
    for _ in range(20):
        src = _prelude(rng)
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


@pytest.mark.parametrize("seed", range(24))
def test_generated_bare_values_agree(admit, seed):
    rng = random.Random(seed)
    for _ in range(20):
        src = _bare_value(rng)
        ref_tag, _ = _ref(src)
        # a lone bare-value read is admitted or refused by G1 only
        assert ref_tag in ("", "G1"), \
            f"corpus bug: reference tag {ref_tag!r} for:\n{src}"
        _agree(admit, src)
