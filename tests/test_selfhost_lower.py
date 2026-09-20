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

import contextlib
import importlib.util
import random
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402


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


@pytest.fixture(scope="module")
def admit_all(ns):
    """Item 419c: the WHOLE collected sink, newline-joined "<line>|<tag>|<msg>"
    in pipeline order — the test-only twin of the reference's `RevlErrors`
    carrier. `admit_src` returns only its minimum by `(line, seq)`."""
    return ns["admit_all"]


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
    `_link` sets no code, so its message marker is used. The TYPE-LAYER tail
    (docs/design/457 §4.3, item 429) names the type checker's refusals
    (T1/T2/G1/G6/A6/HOST-METHOD and the code-less `TYPE` remainder) so the
    census can see the measured false-admit gap; see the block at the end of
    this function. Anything still unrecognised is OUT-OF-SLICE."""
    if e.code in ("G4", "A1"):
        return e.code
    m = e.message
    if "provision conflict" in m and "(G2)" in m:
        return "G2"
    # item 186, the replacement wave: the unmet-consumer refusal of
    # `admission._admit_provision_withdrawal`. It DOES set `code="G2"`, but the
    # code arm above is deliberately limited to G4/A1, so the marker classifies
    # it like every other G2 in this file.
    if "withdraws the running provider of" in m:
        return "G2"
    # item 186, the replacement wave part 2: the state hand-off drift of
    # `admission._admit_handoff_replacement` (item 53). Code-less, and the
    # phrase "differs from the running manifest" is what routes it through
    # `diagnostics.classify` to the same (G2, "admission") bucket every other
    # admission rejection carries, so it classifies G2 here too. The marker is
    # the hand-off HALF of that phrase and not the phrase itself: the §5 SERVICE
    # drift of `_admit_service_replacement` spells it as well and the gate has
    # no phase for that one, so naming it here would claim an agreement that
    # does not exist.
    if "state hand-off on `" in m:
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
    # ---- item 391 / issue #106: extern declarations and inverse slots -------
    # `_lower_externs`'s declaration-line class rules plus `_check_extern_undo`'s
    # slot judgment (`undo`/`compensate`). Both surfaces are code-less in the
    # reference, so before this slice every one of them fell through to "OUT:"
    # and parked in `no-objection-out-of-slice` — the bucket `--check` cannot
    # see. The gate now mirrors them itself (selfhost/lower.rvl's explicit
    # extern section), so naming them here is what turns a parked divergence
    # into a measured agreement.
    #
    # The markers are POSITIVE: each is a substring the gate now spells
    # byte-for-byte. That deliberately excludes the shapes still left out of the
    # gate — `_check_witnessed_inverse`'s "witnessed extern … must declare
    # `undo`" (code G5/G4, a real refusal the gate is silent on) and
    # `_check_deferred_not_in_teardown`'s "calls deferred emission …" (G5), and
    # `_check_inverse_args`' T1 arm — all of which stay "OUT:" so a future
    # no-objection case stays visible in the census rather than being mislabelled
    # as an agreement.
    if "unclassified extern" in m:
        return "G8"
    if ("slot of extern" in m
            or "declares no return type, so there is no acquired value to bind" in m
            or "cannot call the extern itself" in m
            or ("acquire extern `" in m and "must declare `undo`" in m)
            or "cannot declare `undo`" in m):
        return "G4"
    # ---- the self-host TYPE LAYER (docs/design/457, slice T0; item 429) -----
    # The reference's type layer refuses a program the gate has no phase for, so
    # `admit_src` waves it through. Every such refusal used to fall here to
    # "OUT:", which parks it in the census bucket `no-objection-out-of-slice`
    # that the baseline tolerates. Naming it instead moves the measured
    # false-admits (section 1 of the design) into `false-admit/<tag>`, where
    # `tests/test_gate_reference_census.py`'s KNOWN_BYPASSES caps them by name
    # and the `TYPE_LAYER_GAP` pin below records the divergence per family. No
    # gate behaviour changes: the gate still admits these, they are just no
    # longer invisible. Each later type-layer slice deletes lines from both
    # lists as the gate starts refusing its family for real.
    #
    # The vocabulary is design §4.3: `e.code` for the checks that set T1/T2,
    # `HOST-METHOD` straight through (it is already a reference code), and the
    # message-shape families for the code-less remainder. It is append-only and
    # deliberately narrow: it must not name a refusal outside the type layer.
    # The extern-slot G4 family has since landed for real (item 391, above) and
    # was struck from this list, as has the component header's service-existence
    # rule (docs/design/457 §2.4, the `in `requires`/`provides` of` arm below);
    # `a2`/`a9`, `use`, the parser-form fixtures and the extern G5s (`deferred`
    # in teardown, a witnessed inverse that reaches an emission) stay "OUT:"
    # until their own slice lands and can refuse them natively.
    # `HOST-ARITY` joins the pass-through set with the call-and-signature slice
    # (docs/design/457 T2b): the gate now counts a host stub verb's arguments
    # itself, and the code is the reference's own.
    if e.code in ("T1", "T2", "HOST-METHOD", "HOST-ARITY"):
        return e.code
    if "is not declared in this function" in m:
        return "G1"
    # the component header's service-existence rule (`Env.__init__` over
    # `comp.requires`, `_lower_component`'s `comp.provides` loop). The GATE now
    # spells both byte for byte, so naming them here is what turns a parked
    # divergence into a measured agreement. The marker carries the clause on
    # purpose: lower.py's third `unknown service `S`` — the one a `provide`
    # STATEMENT draws for a service the component never declared — is a
    # different site the gate does not decide, and stays "OUT:".
    if ("unknown service `" in m
            and ("in `requires` of" in m or "in `provides` of" in m)):
        return "G1"
    if ("cannot reassign" in m
            or "is already declared in this function" in m
            or "is bound here and called in this body" in m
            or "is already bound in" in m
            or "(G6)" in m):
        return "G6"
    if "is not a method of service" in m:
        return "A6"
    if ("no builtin method" in m
            or "non-exhaustive match" in m
            or "type argument(s), got" in m
            # the unknown-field read, structural (item 71) and nominal alike.
            # Code-less in the reference, but `revl.diagnostics.classify`
            # already files it as a type mismatch, so it carries the T1 the
            # design's §4.3 vocabulary gives it (slice T2a).
            or "has no field" in m):
        return "T1"
    if ("is not a case of" in m
            or "record update names" in m
            or "record destructuring requires a record" in m
            or "type alias cycle" in m
            # all four of `_DIVIDES_BY`, not only `mod`: `div_trunc`,
            # `div_floor` and `div_euclid` draw the identical sentence and were
            # filed "OUT:" while their sibling was named.
            or " by a literal zero is undefined" in m
            or "Float literal is infinite" in m
            # docs/design/457 T2b: `builtin_check`'s code-less receiver-family
            # refusals and the lowering-time builtin arity count, plus the
            # `Map.empty()` arity. Their CODED siblings (the `has no form for`
            # row miss, the `join` element constraint and every `argument
            # expects` mismatch) carry T1 and are named by the code arm above;
            # these three shapes carry none, and the gate spells each one byte
            # for byte, so leaving them "OUT:" would file an agreement as a tag
            # mismatch.
            or ("builtin `" in m and "` needs a " in m
                and " receiver, got " in m)
            or ("builtin `" in m and "` takes " in m
                and " argument(s), " in m)
            or "takes no arguments, " in m
            # slice T2a's three remaining code-less expression refusals, named
            # in the design's §4.3 TYPE list. Each is a zero-hit marker over the
            # whole census corpus today (no program in the tree draws one), so
            # naming them moves no document between buckets; they exist so the
            # checker oracle can compare a TAG as well as a message when the
            # statement layer (T3a) starts carrying these to `admit_src`.
            or "cannot order" in m
            or "ternary branches disagree" in m
            or "record update requires" in m
            or "record literal for `" in m
            or "but the record has " in m):
        return "TYPE"
    return "OUT:" + m


def _ref(src: str) -> tuple[str, str]:
    """(tag, message): ("", "") if the reference admits, else its guarantee tag
    and diagnostic message."""
    try:
        compile_source(src, "diff.rvl")
        return ("", "")
    except RevlError as e:
        return (_classify(e), e.message)


def _ref_all(src: str) -> list[tuple[int, str]]:
    """The reference's full ORDERED refusal list as (line, tag) pairs (item
    419c). The `RevlErrors` carrier already dedups on `(code, filename, line,
    message)` and sorts by `(file rank, line)` with a stable tie-break; a lone
    refusal arrives as a plain `RevlError`. `[]` when the reference admits."""
    try:
        compile_source(src, "diff.rvl")
        return []
    except RevlErrors as e:
        return [(x.line, _classify(x)) for x in e.errors]
    except RevlError as e:
        return [(e.line, _classify(e))]


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
# That keeps every `_agree` case SINGLE-refusal, so the message check is exact
# regardless of ordering. Multi-refusal ordering itself (item 419c) is exercised
# separately by `test_multi_refusal_programs_agree` at the end of this file.
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

# The qualified-test fixtures below share a shape: a declaration the gate reads
# (`_QT_HEAD`), then the test block under test, then a declaration whose verdict
# the gate must still reach (`_QT_TAIL` — it emits through a required key, so a
# tail the walk never parsed is silently admitted and the pairing in
# REJECTED_PROGRAMS catches it).
_QT_HEAD = """service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}
component PgDatabase provides db: Database {
  config { url: Str, pool_size: Int = 10 }
  let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
"""

_QT_TAIL = """component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute("INSERT INTO cache_log VALUES (1)")
    }
  }
}
"""


# Programs the reference admits — the gate must admit them too. Kept
# reference-clean (no out-of-slice defect), so "" is the only agreement.
# ---- item 391 / issue #106: the service-OPERATION head ----------------------
#
# `p_methods` read only `emission` / `async` / `idempotent`, so a declaration
# carrying any other part of the reference's modifier slot (`parser.py::service`)
# failed the WHOLE document with `BAD|bad method signature in service <S>` — and
# a parse-stage refusal is the worst kind, because a verdict-direction comparison
# reads it as agreement while no guarantee was reached at all.
#
# Every fixture below shares one shape, the pairing PR #1085 established. The
# clause under test sits on the FIRST operation of `Cache`; a SECOND operation
# (`put`) follows it, and the program's verdict depends on how `put` was
# declared. So a step that overshoots and swallows `put` cannot pass: the
# accepted half declares `put` an `emission` and must stay silent, the refused
# half declares it plain and must still draw the reference's own G4
# (`examples/rejections/g4_emission_not_declared.rvl`, verbatim).
_SOP_HEAD = """service Database {
  emission fn execute(sql: Str) -> Int
}
service Cache {
"""


def _sop(clause: str, put: str, impl: str) -> str:
    """A `Cache` whose first operation carries `clause` and whose second is
    declared `put`. `impl` is the provide method for the first operation."""
    return _SOP_HEAD + "  " + clause + """
  fn get(key: Str) -> Opt[Str]
  """ + put + """
}
component LyingCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
""" + impl + """
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute("INSERT INTO log VALUES (1)")
    }
  }
}
"""


_SOP_EMISSION_PUT = "emission fn put(key: Str, value: Str)"
_SOP_PLAIN_PUT = "fn put(key: Str, value: Str)"

# (label, the declaration carrying the clause, the provide method for it).
_SOP_CLAUSES = [
    # item 457: the `route <verb> "<path>"` clause HEADS the operation. This is
    # the one the census corpus actually holds — `examples/app/notes.rvl`.
    ("a route clause heading the operation",
     'route get "/cache/{key}"\n  fn find(key: Str) -> Str',
     '    fn find(key) = "x"'),
    # Def. 39: order-independence, declared per operation.
    ("a commutative operation",
     "commutative fn touch(key: Str) -> Int",
     "    fn touch(key) = 1"),
    # item 343 + the endorsement slot: an origin set on an emission.
    ("an endorse slot on an emission",
     "emission endorse[trusted] fn audit(key: Str) -> Int",
     '    fn audit(key) { emit db.execute("a")   return 1 }'),
    # item 257: `validated`, and its `retry N` sibling.
    ("a validated emission",
     "emission validated fn audit(key: Str) -> Int",
     '    fn audit(key) { emit db.execute("a")   return 1 }'),
    ("a validated emission with a retry budget",
     "emission validated retry 3 fn audit(key: Str) -> Int",
     '    fn audit(key) { emit db.execute("a")   return 1 }'),
    # item 310: the `cache` trailing clause, AFTER the return type.
    ("a cache pure trailing clause",
     "fn touch(key: Str) -> Int cache pure",
     "    fn touch(key) = 1"),
    ("a cache external clause with a ttl bound",
     "emission fn audit(key: Str) -> Int cache external ttl 5m",
     '    fn audit(key) { emit db.execute("a")   return 1 }'),
    # the CONTROL: `emission[caps]` was already read, so this pair passes
    # against the unported gate too. That is what it is for — it pins that the
    # one bracket this slice reads rather than steps is still read.
    ("an emission capability bound (control)",
     "emission[db] fn audit(key: Str) -> Int",
     '    fn audit(key) { emit db.execute("a")   return 1 }'),
]


ACCEPTED_PROGRAMS = [
    # ---- the qualified test heads (item 391) -------------------------------
    # `lifecycle` (syntax-2.0 §7.1), `fault` (docs/fault-tests.md) and `prop`
    # (roadmap item 37) are CONTEXTUAL keywords qualifying `test`. They lex as
    # plain identifiers, and `p_top` refused every non-keyword head, so a
    # document holding one failed WHOLE with `BAD|unexpected token at top
    # level`: 20 of them in the census corpus, every `examples/lifecycle_*.rvl`
    # among them. The refusal also took the declarations AFTER the block with
    # it, which is what the `_QT_TAIL` pairing pins.
    ("lifecycle test between two components",
     _QT_HEAD + '''
lifecycle test "cache reverts cleanly" {
  load PgDatabase with { url: "postgres://primary:5432/app" }
  unload PgDatabase
  assert no_residue
}
''' + _QT_TAIL),
    ("prop test between two components",
     _QT_HEAD + '''
prop test "addition commutes" (a: Int, b: Int) {
  assert a + b == b + a
}
''' + _QT_TAIL),
    ("fault test between two components",
     _QT_HEAD + '''
fault test "the pool closes" for PgDatabase {
  fail at step 1
  assert no residue
}
''' + _QT_TAIL),
    # the `with { ... }` config slot is the one arm whose body brace is NOT the
    # first `{` after the header, so an end-scan that took the first brace would
    # stop inside the config block and resume mid-test
    ("fault test with a with-block between two components",
     _QT_HEAD + '''
fault test "the pool closes" for PgDatabase with { url: "postgres://p:1/a" } {
  fail at step 1
  assert no residue
}
''' + _QT_TAIL),
    # the contextual-keyword control: none of the three words is a lexer
    # keyword, so all three stay usable as ordinary module-fn names. This one
    # passes against the unported gate too — that is what it is for.
    ("lifecycle, prop and fault stay ordinary identifiers", '''
fn lifecycle(x: Int) -> Int { return x }
fn prop(x: Int) -> Int { return x }
fn fault(x: Int) -> Int { return x }
service Kv { fn get(k: Str) -> Str }
component Store provides kv: Kv {
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(lifecycle(prop(fault(1)))) }
}
'''),
    # ---- the timer body's own in-flight window (item 170) ------------------
    # A timer body reaching an async op is ADMITTED and coloured async: the
    # firing opens an `Async[T]` window the runtime awaits on the tick and
    # cancels on unload. The reference expresses that by keeping the body in a
    # `timer` step and pruning that step from both A1 surfaces; this gate reads
    # a timer body out INLINE, so without the `inTimer` mark it applied rule 1
    # and refused `examples/async_timer.rvl`, which the reference admits.
    ("timer body reaching an async operation", '''
service Counter {
  fn count() -> Int
  emission async fn tick()
}
component AsyncCounter provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
    async fn tick() {
      let key = "tick"
      effect store.insert(key, "fired")
      undo   store.remove(key)
    }
  }
}
component Heartbeat requires counter: Counter {
  every 10s { emit counter.tick() }
}
'''),
    # ---- the provide-method return annotation (item 391) -------------------
    # A provide method may RESTATE the return type its service already declares.
    # The reference parses it and leaves any mismatch to the type layer
    # (`t7_provide_param_annotation_mismatch.rvl` is the parameter twin, a T1).
    # `p_prov_methods` read the `{`/`=` straight off the end of the parameter
    # list, so an annotated method failed the whole component with `BAD|bad
    # provide block in component <C>` — a false rejection of legal programs AND,
    # on a program the reference refuses, a parse-stage BAD standing where the
    # real verdict should be (the census filed three documents as
    # `tag-mismatch/G4->BAD` for exactly this reason). The step is conditional,
    # so both unannotated forms are pinned beside the annotated ones: an
    # unconditional step would eat the `{` or `=` of a bare method instead.
    ("provide method restates its return type", """service Counter {
  fn size() -> Int
  fn bump(n: Int) -> Int
  fn label(n: Int) -> Str
  fn flag() -> Bool
}
component Tally provides counter: Counter {
  provide counter {
    fn size() -> Int { return 0 }
    fn bump(n) { return n + 1 }
    fn label(n) -> Str = n.to_str()
    fn flag() = true
  }
}
"""),
    ("async provide method restates its return type", """service Feed {
  async fn head() -> Int
}
component Reader provides feed: Feed {
  provide feed {
    async fn head() -> Int { return 1 }
  }
}
"""),
    # The accepting twin of the single-case-alias G1 rejections: a MULTI-case
    # variant registers each case name as a constructor, builtin-spelled names
    # included (`type T = Foo | Str` makes `Str(...)` a real case), so both
    # implementations admit the call. Pinned so the `type_ctors` alias/variant
    # split cannot regress into refusing a genuine constructor.
    ("bare call of a builtin-named case in a multi-case variant", """type T = Foo | Str
service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let x = Str("a")   return 0 } }
}
"""),
    # ---- item 391: the accepting twins of the fn-body binding refusals -----
    # A `var` is exactly what the reassignment guard lets through.
    ("var reassignment", """fn bump() -> Int {
  var n = 1
  n = 2
  n += 1
  return n
}
"""),
    # Two DISJOINT sibling blocks may reuse a name: only one arm is ever live,
    # so neither `let` is redeclaring something already bound (item 155).
    ("disjoint sibling blocks reuse a name", """fn pick(c: Bool) -> Int {
  if (c) { let y = 1  return y } else { let y = 2  return y }
}
"""),
    # A loop binding lives in the body's scope only, so a second loop over the
    # same spelling is not a redeclaration.
    ("two for loops binding one name", """fn total(xs: List[Int]) -> Int {
  var t = 0
  for (x of xs) { t += x }
  for (x of xs) { t += x }
  return t
}
"""),
    # THE HOST PROVENANCE. A non-`var` `let` bound to a host acquisition is
    # recorded `"host"`, and the reference's reassign guard tests falsiness — so
    # this IS admitted, and a gate that refused it would be a false rejection.
    ("reassigning a host-provenance let", """fn f() -> Int {
  let m = Map.new()
  m = m
  return 1
}
"""),
    # A statement-block MATCH arm spells `Pat => { … }` with the same two tokens
    # an arrow does, and revl does allow a `let` there — so the arrow-body write
    # scan must not fire on it.
    ("a let inside a statement-block match arm", """type S = A(Str) | B(Str)
fn f(s: S) -> Int {
  return match s {
    A(x) => { let y = 1  y },
    B(x) => 2,
  }
}
"""),
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
    ("a provision through a service-typed arrow parameter, correctly marked", """
service Task { emission[net] fn run(p: Str) -> Int }
component Worker provides task: Task {
  provide task { fn run(p) { return 1 } }
}
service Sup { emission fn go(p: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(p: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let f = (t: Task, s: Str) => emit t.run(s)
      let r = f(w.task, p)
      return r
    }
  }
}
"""),
    ("a service-typed arrow parameter not applied to a provision", """
service Task { emission[net] fn run(p: Str) -> Int }
component Worker provides task: Task {
  provide task { fn run(p) { return 1 } }
}
service Sup { emission fn go(p: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(p: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let f = (t: Task, s: Str) => t.run(s)
      return 0
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

    # ---- item 391 / issue #106: extern declarations and inverse slots ------
    # The accepting twins of the extern refusals pinned in REJECTED_PROGRAMS.
    # Each is a legal extern whose inverse slot must keep admitting: an
    # `emission` may declare `compensate`, an `acquire` must declare `undo`
    # (and that `undo` may name a declared `pure` extern or a builtin
    # constructor). `extern` declarations do not need a calling component, so
    # these are declaration-only programs — the narrowest input that reaches the
    # rule and nothing else.
    ("an emission extern declaring its optional compensate slot", """
extern pure fn log_unsent(n: Int) = @py { return None }
extern emission fn send(data: Str) -> Int compensate log_unsent(1) = @py { return 1 }
"""),
    ("an acquire extern whose undo is a declared pure extern", """
extern pure fn close_it(s: Socket) = @py { return None }
extern acquire fn listen(port: Int) -> Socket undo close_it(result) = @py { return port }
"""),
    ("an acquire extern whose undo is a builtin constructor", """
extern acquire fn listen(port: Int) -> Socket undo Ok(result) = @py { return port }
"""),
    ("an acquire extern with no return type and a legal inverse", """
extern pure fn g(h: Int) = @py { return None }
extern acquire fn f() undo g(1) = @py { return 1 }
"""),
    # ---- item 391 / issue #106: the service-operation head -----------------
    # The accepting half of each `_SOP_CLAUSES` pair: the clause in front of a
    # `Cache` whose `put` is declared `emission`, which the reference admits.
    # Every one of these drew `BAD|bad method signature in service Cache` from
    # the gate before this slice.
    *[(f"{label} is read", _sop(clause, _SOP_EMISSION_PUT, impl))
      for label, clause, impl in _SOP_CLAUSES],

    # ---- item 391 / issue #106: the shadowed module callable ---------------
    # The ACCEPTING twins of the callable-shadowing refusals below, and the
    # whole reason that scan is scoped to the CALL position. Every one of these
    # would be refused by a reader that fired on the mere coincidence of a name,
    # which is the false-rejection direction the gate may not err in.
    ("a binding sharing a callable's name that never calls it", """
fn make_row(id: Int, name: Str) -> Str { return name }
fn f(row: Str) -> Str {
  let name = row
  return make_row(1, name)
}
"""),
    ("a parameter sharing a callable's name that never calls it", """
fn name(row: Str) -> Str { return row }
fn f(name: Str) -> Str { return name }
"""),
    ("a bound name called only as a METHOD of a receiver", """
fn get(k: Str) -> Str { return k }
fn f(k: Str) -> Str {
  let m = Map.new()
  let get = k
  return m.get(get)
}
"""),
    ("a callable called in a fn that binds nothing of that name", """
fn helper(xs: List[Int]) -> Int { return xs.length() }
fn f() -> Int { return helper([1, 2, 3]) }
"""),
    # the `=>` fence: a match ARM head is an identifier followed by `(` and must
    # not read as a call, or a bound name colliding with a constructor-shaped
    # module fn would be refused here and admitted by the reference.
    ("a match arm head that shares a module callable's name", """
fn Wrap(n: Int) -> Int { return n }
fn f(o: Opt[Int]) -> Int {
  let Wrap = 1
  return match o { Some(v) => v, None => Wrap }
}
"""),
    # ---- item 391 / issue #106: the provide-method rebind ------------------
    # The accepting twins of the method-local collision: a fresh method-local
    # name, a method PARAMETER that only coincides with another method's local,
    # and a component local read (never rebound) by the method that closes over
    # it. Refusing any of these would break the ordinary component shape.
    ("a provide method binding a name the component does not hold", """
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let slot = key
      effect store.insert(slot, "v")
      undo   store.remove(slot)
    }
  }
}
"""),
    ("two provide methods binding the same fresh local", """
service Cache { fn set(key: Str)  fn put(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let slot = key
      effect store.insert(slot, "v")
      undo   store.remove(slot)
    }
    fn put(key) {
      let slot = key
      effect store.insert(slot, "w")
      undo   store.remove(slot)
    }
  }
}
"""),
    # ---- the transparent alias, across every declaration site the
    # provide-method type layer reads (docs/design/457) ----------------------
    # The corpus had no document that spelled `type X = <scalar>` and then used
    # X in a signature, so nothing held the gate to ERASING it. The reference
    # does erase it, which makes every position below an ordinary `Int`/`Str`
    # and the whole component legal; a gate that reads the alias as a type of
    # its own refuses all of them at once. This is an ACCEPTED program for that
    # reason — it is the false rejection, written down.
    ("a transparent alias through a service, a config field and a body", """
type Slot = Int
type Label = Str

service Shelf {
  fn at(i: Slot) -> Label
  fn width() -> Slot
}

component Rack provides shelf: Shelf {
  config { size: Slot = 3 }

  let rows = effect Map.new() undo rows.drop()

  provide shelf {
    fn at(i: Slot) -> Label {
      let names: List[Str] = ["a", "b", "c"]
      return names[i]
    }
    fn width() -> Slot { return 3 }
  }
}
"""),
    # ---- docs/design/457 T3b: returns on every path, the ADMITTING side ----
    # The direction this rule may not err in is the false alarm, so each shape
    # the reference ACCEPTS is here beside the refusal it neighbours. The
    # termination rule is conservative (JLS 14.21 / rust E0308), and each of
    # these is a body it must judge terminating.
    ("a fn with no declared return type need not return",
     "fn f(n: Int) {\n  let doubled = n * 2\n}\n"),
    ("an if/else whose arms both return", """
fn f(c: Bool) -> Int {
  if (c) { return 1 } else { return 2 }
}
"""),
    # `while (true)` with no `break` targeting it diverges, so the path never
    # reaches the end of the body (item 379).
    ("a while(true) with no targeting break", """
fn f() -> Int {
  while (true) {
    return 1
  }
}
"""),
    # a `break` in a NESTED loop belongs to that loop, so the outer
    # `while (true)` still diverges.
    ("a while(true) whose only break targets a nested loop", """
fn f() -> Int {
  while (true) {
    while (true) { break }
    return 1
  }
}
"""),
    ("a loop that may run zero times, followed by a return", """
fn f(xs: List[Int]) -> Int {
  for (x of xs) {
    if (x > 0) { return x }
  }
  return 0
}
"""),
    # an `else if` chain: `fb_scan` cannot model one, so the whole `fn` is
    # withheld rather than judged on a truncated body. The reference admits it,
    # and this is the case that proves the withholding is real.
    ("an else-if chain whose every arm returns", """
fn f(c: Bool) -> Int {
  if (c) { return 1 } else if (!c) { return 2 } else { return 3 }
}
"""),
    # a destructuring binder is the other `fb_scan` bail, and it sits BEFORE
    # the return here so a reader that dropped the tail would refuse.
    ("a destructuring binder ahead of the return", """
type R = { a: Int, b: Int }
fn f(r: R) -> Int {
  let { a, b } = r
  return a + b
}
"""),
    # ---- docs/design/457: what the name-RESOLUTION rule must NOT refuse -----
    # The accepting twins of the G1 read rows in REJECTED_PROGRAMS. Each names
    # one member of `callables` or one binder, and a rule that missed any of
    # them would refuse a program the reference admits.
    ("every callable universe member read by name", """
type Shape = Circle | Square
extern pure fn ext(n: Int) -> Int = @py { return n }
fn helper(n: Int) -> Int { return n }
fn f(p: Int) -> Int {
  let m = Map.new()
  let s = Some(p)
  let o = Ok(p)
  let e = Err("x")
  let n = None
  let sh = Circle
  let q = Square
  let h = helper(p)
  let x = ext(p)
  var t = 0
  for (v of [1, 2]) { t += v }
  return h + x + t
}
"""),
    # the reference writes `scope[name]` BEFORE it lowers the initialiser, so a
    # `let` may mention its own name and still resolve.
    ("a let whose initialiser mentions its own name", """
fn f(n: Int) -> Int {
  let n2 = n
  return n2
}
"""),
    # the host constructor roots, read through their own verbs.
    ("a host root used as a constructor receiver", """
fn f() -> Int {
  let p = Pool.open("u", 2)
  let j = Job.new()
  return 1
}
"""),
    # The iteration HEAD binds its item; it does not TYPE it. `every o in sub`
    # reads the head as a statement whose expression is a placeholder, and a
    # placeholder that IS a value (`IntLit("0")`) infers `Int`: the type walker
    # binds an untyped statement's name to its expression's type, so the item
    # became `Int` and `emit sink.write(o)` was refused T1 (`argument `v`
    # expects `Str`, got `Int`) — a refusal the reference does not draw, since
    # it leaves a plain `every`'s item deliberately untyped (lower.py
    # `_lower_stream_iter_step`). Nothing else in this program refuses, so the
    # pair is the pin; the item's type is the only thing in question.
    ("a plain every iteration's item stays untyped", """
service Sink { emission fn write(v: Str) -> Int }
component C requires sink: Sink {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every o in sub { emit sink.write(o) }
}
"""),
]


# (name, source, expected tag). The message is compared too, via _agree; the
# reference's own text is the ground truth. Several are the documented
# `expected error` of a checked-in rejection fixture.
REJECTED_PROGRAMS = [
    # ---- the qualified test heads, negative controls (item 391) ------------
    # Stepping over a test block must land on the NEXT declaration, not past
    # it: each of these puts a real refusal in the tail, so a step that
    # overshoots turns the program silently clean.
    ("an undeclared requirement after a lifecycle test", _QT_HEAD + '''
lifecycle test "cache reverts cleanly" {
  load PgDatabase with { url: "postgres://primary:5432/app" }
  unload PgDatabase
  assert no_residue
}
component UserCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute("INSERT INTO cache_log VALUES (1)")
    }
  }
}
''', "G1"),
    ("an unmarked emission after a prop test", _QT_HEAD + '''
prop test "addition commutes" (a: Int, b: Int) {
  assert a + b == b + a
}
component Auditor requires db: Database {
  effect db.execute("CREATE TABLE audit (id INT)")
  undo   db.execute("DROP TABLE audit")
}
''', "G4"),
    # ---- the timer A1 prune, negative controls (item 170) ------------------
    # The prune is per-STATEMENT and only over the A1 pairing. An ordinary
    # activation-body `emit` reaching the same async op keeps its rule-1
    # refusal, and a timer body is still walked for every other verdict.
    ("a non-timer emit reaching an async op is still A1", '''
service Counter {
  fn count() -> Int
  emission async fn tick()
}
component AsyncCounter provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
    async fn tick() {
      let key = "tick"
      effect store.insert(key, "fired")
      undo   store.remove(key)
    }
  }
}
component Eager requires counter: Counter {
  emit counter.tick()
}
''', "A1"),
    ("a timer body reaching an undeclared key is still G1", '''
service Counter {
  fn count() -> Int
  emission fn tick()
}
component SyncCounter provides counter: Counter {
  let store = effect Map.new() undo store.drop()
  provide counter {
    fn count() = store.size()
    fn tick() {
      let key = "tick"
      effect store.insert(key, "fired")
      undo   store.remove(key)
    }
  }
}
component Heartbeat requires counter: Counter {
  every 10s { emit ghost.tick() }
}
''', "G1"),
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
    # ---- the fn-body TYPE layer (docs/design/457 T3a) ----------------------
    # The statement walk over a module `fn` body now carries a type environment
    # beside its binding scope: parameters at their declared types over the
    # program-wide declaration table, the written annotation at a `let`, and
    # the declared return as every `return`'s checking position. Each of these
    # was a `false-admit` in `tools/gate_reference_census.py` until it was, and
    # each is compared on TAG and MESSAGE here.
    ("t2 null in an expression", _fixture("t2_null_in_expression"), "T2"),
    ("t11 field read through an optional",
     _fixture("t11_field_through_opt"), "T1"),
    ("t12 index on a Str", _fixture("t12_str_index"), "T1"),
    ("t21 implicit Int -> Int32 narrowing at a return",
     _fixture("t21_int32_narrow_implicit"), "T1"),
    ("t22 mixed-width arithmetic", _fixture("t22_int32_width_mix"), "T1"),
    ("t23 remainder on Int32 operands", _fixture("t23_int32_remainder"), "T1"),
    ("t26 record update with a wrong field type",
     _fixture("t26_anon_record_update_wrong_type"), "T1"),
    ("t27 record update naming a field the literal has not",
     _fixture("t27_anon_record_update_undeclared_field"), "TYPE"),
    ("t28 bitwise on non-Int32 operands",
     _fixture("t28_bitwise_non_int32"), "T1"),
    ("t29 field read on an erased Any", _fixture("t29_field_read_on_any"), "T1"),
    ("t36 a Float literal outside binary64",
     _fixture("t36_float_literal_range"), "TYPE"),
    # the same erased-`Any` field read reached through a backend fixture rather
    # than a rejection fixture — the one census entry of this family with no
    # `examples/rejections/` name.
    ("dynamic reserved key: a field read on a json_parse result",
     (ROOT / "backends" / "typescript" / "tests" / "fixtures"
      / "dynamic_reserved_key.rvl").read_text(), "T1"),
    # ---- calls and signatures (docs/design/457 T2b) ------------------------
    # The same walk resolves a CALL against a declaration: the signature table's
    # arity window and per-argument check (monomorphic by `compatible`, generic
    # by `unify` + `substitute`), the host stub surface, `_BUILTIN_SIG`, and the
    # four refusals the reference makes while LOWERING a method call rather than
    # while typing it.
    ("t10 a call at the wrong arity", _fixture("t10_call_arity"), "T1"),
    ("t15 a generic fn's result at a call site",
     _fixture("t15_generic_call_site"), "T1"),
    ("t25 an explicit [T] list turns the implicit heuristic off",
     _fixture("t25_explicit_tparam_heuristic_off"), "T1"),
    ("v2 a Map value's `set` against the map's V",
     _fixture("v2_map_set_value_mismatch"), "T1"),
    ("v2 a method a Map value's stdlib surface does not name",
     _fixture("v2_map_value_unknown_method"), "T1"),
    ("a literal zero divisor", _fixture("arith_zero_divisor"), "TYPE"),
    ("t24 a stdlib method on a receiver no constructor pins",
     _fixture("t24_opaque_receiver_builtin"), "HOST-METHOD"),
    ("a method the host stub surface does not name",
     _fixture("host_method_not_on_surface"), "HOST-METHOD"),
    ("g4 an extern undo slot's argument type",
     _fixture("g4_extern_undo_wrong_arg_type"), "T1"),
    ("g4 emission not declared", _fixture("g4_emission_not_declared"), "G4"),
    ("g4 capability not declared", _fixture("g4_capability_not_declared"), "G4"),
    ("g4 unmarked emission", _fixture("g4_unmarked_emission"), "G4"),
    ("a1 async extern in sync method",
     _fixture("a1_async_extern_sync_method"), "A1"),
    ("a1 async op via sync ternary",
     _fixture("a1_async_op_sync_ternary"), "A1"),
    # item 131 §3, the per-STATEMENT effect-composition rules the gate's
    # name-based async fence could not express (GHSA-p5c5-fhrh-6mqp): the exact
    # `await`/async pairing on a forward `effect`/`emit`, and a suspending
    # `undo`/`compensate` teardown. Each was a gate false-admit until the
    # statement carried its own `await` marker.
    ("a1 async effect not awaited",
     _fixture("a1_async_effect_not_awaited"), "A1"),
    ("a1 async emit step not awaited",
     _fixture("a1_async_emit_step_not_awaited"), "A1"),
    ("a1 await emit on sync emission",
     _fixture("a1_await_emit_sync"), "A1"),
    ("a1 effect await on sync acquisition",
     _fixture("a1_effect_await_sync"), "A1"),
    ("a1 async undo suspends",
     _fixture("a1_async_undo_suspends"), "A1"),
    ("a1 async compensate suspends",
     _fixture("a1_async_compensate_suspends"), "A1"),
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
    # G1 bare CALL head: a single-case type declaration aliasing a builtin type
    # (`type Alias = Int`) binds a type, not a constructor — the reference's
    # `_case_table` never registers `Int` as a case (typecheck.py
    # `_is_type_expression`), so `Int("1")` draws the same "not a declared
    # requirement" G1 refusal a bare `nope()` does. The gate's `type_ctors` used
    # to collect every Upper-cased name a `type` declaration mentioned, admitting
    # this whole family; it now follows the same alias/variant split.
    ("g1 bare call of a builtin type aliased single-case",
     """type Alias = Int
service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let x = Int("1")   return 0 } }
}
""", "G1"),
    # G1 bare CALL head: a single-case type application (`type Rows = List[Row]`)
    # is an alias RHS too, so its head `List` is not a constructor.
    ("g1 bare call of a type-application alias head",
     """type Rows = List[Row]
service S { fn go() -> Int }
component C provides s: S {
  provide s { fn go() { let x = List(1)   return 0 } }
}
""", "G1"),
    # The accepting twin: in a MULTI-case variant the same builtin name IS a
    # registered case, so `Str("a")` resolves and both admit. (Held in the
    # ACCEPTED corpus below so a future over-eager fix cannot silently start
    # refusing it.)
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
    # the sibling one indirection further (GHSA-wg4v-r47x-52p2 residual): the
    # provision flows into a service-typed arrow PARAMETER at the application.
    # Until the self-host followed it across the parameter binding the shipped
    # gate admitted this too — the fail-open direction, same as the alias was.
    ("g4 unmarked emission through a service-typed arrow parameter",
     _fixture("g4_arrow_param_emission"), "G4"),
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
    # issue #285: FIRST refusal wins, inside one expression.
    #
    # `walk_expr`'s composite arms thread the accumulator through more than one
    # sub-walk, and `ac_refuse` overwrites `msg`/`tag` unconditionally, so a
    # refusal from the LEFT side was replaced by one from the RIGHT. The
    # reference raises on the first thing it reaches, and `crates/revl-gate`
    # promises its refusals are the reference's verbatim.
    #
    # The census surfaced this as a diagnostic naming a subject that reads like
    # a truncated identifier: a source holding `st|ore` has two undeclared
    # names, `st` and `ore`; the reference named `st` and the gate named `ore`,
    # so the reader searched for a symbol their program does not contain. One
    # case per unguarded arm — `Bin`, `Index`, `If` — because each threads the
    # accumulator differently and a guard on one is not a guard on the others.
    ("both operands of a binary op are undeclared: the LEFT one is named", """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha | beta.drop()
    }
  }
}
""", "G1"),
    ("an index whose target and subscript are both undeclared names the target",
     """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha[beta]
    }
  }
}
""", "G1"),
    ("a ternary with three undeclared arms names the condition", """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha ? beta : gamma
    }
  }
}
""", "G1"),
    # The half of #285 that is not cosmetic. Here the clobbered refusal carries
    # a different TAG: the reference refuses the unmarked emission receiver
    # (G4), and the gate replaced it with the G1 from the right operand — so a
    # consumer switching on the refusal code was handed the wrong guarantee.
    # Both operand orders are pinned; only one of them was ever wrong, which is
    # precisely why the wrong one went unnoticed.
    ("an unmarked emission left of an undeclared name is still G4", """
service Kv { emission fn put(key: Str) -> Int }
service Api { fn go() -> Int }
component C requires kv: Kv provides api: Api {
  provide api {
    fn go() { return kv.put("a") + beta }
  }
}
""", "G4"),
    ("an undeclared name left of an unmarked emission is still G1", """
service Kv { emission fn put(key: Str) -> Int }
service Api { fn go() -> Int }
component C requires kv: Kv provides api: Api {
  provide api {
    fn go() { return beta + kv.put("a") }
  }
}
""", "G1"),
    # The clobber does not need two different tags to be wrong. Two unmarked
    # emissions in one expression are both G4, and the gate named the SECOND
    # one — the same verdict pointing at the wrong call. This is the census's
    # `msg-mismatch/G4` in a form that does not need a fuzzer to reach.
    ("two unmarked emissions in one expression name the first (G4)", """
service Db { emission fn run(sql: Str) -> Int }
service Bus { emission fn send(m: Str) -> Int }
service Api { fn go() -> Int }
component C requires db: Db, bus: Bus provides api: Api {
  provide api {
    fn go() { return db.run("a") + bus.send("b") }
  }
}
""", "G4"),
    # issue #285: FIRST refusal wins, inside one expression.
    #
    # `walk_expr`'s composite arms thread the accumulator through more than one
    # sub-walk, and `ac_refuse` overwrites `msg`/`tag` unconditionally, so a
    # refusal from the LEFT side was replaced by one from the RIGHT. The
    # reference raises on the first thing it reaches, and `crates/revl-gate`
    # promises its refusals are the reference's verbatim.
    #
    # The census surfaced this as a diagnostic naming a subject that reads like
    # a truncated identifier: a source holding `st|ore` has two undeclared
    # names, `st` and `ore`; the reference named `st` and the gate named `ore`,
    # so the reader searched for a symbol their program does not contain. One
    # case per unguarded arm — `Bin`, `Index`, `If` — because each threads the
    # accumulator differently and a guard on one is not a guard on the others.
    ("both operands of a binary op are undeclared: the LEFT one is named", """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha | beta.drop()
    }
  }
}
""", "G1"),
    ("an index whose target and subscript are both undeclared names the target",
     """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha[beta]
    }
  }
}
""", "G1"),
    ("a ternary with three undeclared arms names the condition", """
service Kv { fn put(key: Str, value: Str) }
component C provides kv: Kv {
  provide kv {
    fn put(key, value) {
      let n = alpha ? beta : gamma
    }
  }
}
""", "G1"),
    # The half of #285 that is not cosmetic. Here the clobbered refusal carries
    # a different TAG: the reference refuses the unmarked emission receiver
    # (G4), and the gate replaced it with the G1 from the right operand — so a
    # consumer switching on the refusal code was handed the wrong guarantee.
    # Both operand orders are pinned; only one of them was ever wrong, which is
    # precisely why the wrong one went unnoticed.
    ("an unmarked emission left of an undeclared name is still G4", """
service Kv { emission fn put(key: Str) -> Int }
service Api { fn go() -> Int }
component C requires kv: Kv provides api: Api {
  provide api {
    fn go() { return kv.put("a") + beta }
  }
}
""", "G4"),
    ("an undeclared name left of an unmarked emission is still G1", """
service Kv { emission fn put(key: Str) -> Int }
service Api { fn go() -> Int }
component C requires kv: Kv provides api: Api {
  provide api {
    fn go() { return beta + kv.put("a") }
  }
}
""", "G1"),
    # The clobber does not need two different tags to be wrong. Two unmarked
    # emissions in one expression are both G4, and the gate named the SECOND
    # one — the same verdict pointing at the wrong call. This is the census's
    # `msg-mismatch/G4` in a form that does not need a fuzzer to reach.
    ("two unmarked emissions in one expression name the first (G4)", """
service Db { emission fn run(sql: Str) -> Int }
service Bus { emission fn send(m: Str) -> Int }
service Api { fn go() -> Int }
component C requires db: Db, bus: Bus provides api: Api {
  provide api {
    fn go() { return db.run("a") + bus.send("b") }
  }
}
""", "G4"),

    # ---- item 391 / issue #106: extern declarations and inverse slots ------
    # Roadmap item 391's "remaining family" for the boundary surface. The
    # reference refuses each of these in `_lower_externs` (the `extern` line's
    # CLASS rules) or `_check_extern_undo` (the `undo`/`compensate` slot walk),
    # both code-less, so until this slice the gate raised no objection at all
    # and the census parked them in `no-objection-out-of-slice`. The first six
    # are the checked-in rejection fixtures; the rest are the family's message
    # shapes, kept inline because the corpus carries one fixture per shape.
    ("extern with no classification is G8", _fixture("v2_extern_unclassified"),
     "G8"),
    ("acquire extern with no undo", _fixture("v2_extern_acquire_no_undo"),
     "G4"),
    ("compensate slot binding an unbound result",
     _fixture("g4_extern_compensate_result"), "G4"),
    ("undo slot sees only the implicit result binding",
     _fixture("g4_extern_undo_param_not_in_scope"), "G4"),
    ("undo slot cannot call the extern itself",
     _fixture("g4_extern_undo_self_call"), "G4"),
    ("undo slot may only call a declared fn, extern, or host builtin",
     _fixture("g4_extern_undo_undeclared_fn"), "G4"),
    # a `pure` extern has no observable effect, so it has no inverse to declare;
    # `compensate` alone is already a refusal (the slot rule is per-class).
    ("pure extern cannot declare compensate", """
extern pure fn g(h: Int) = @py { return None }
extern pure fn f(x: Str) compensate g(1) = @py { return x }
""", "G4"),
    # an `emission` may declare `compensate` but never `undo` (its inverse would
    # be a second boundary crossing, which is what G5's teardown rule forbids).
    ("emission extern cannot declare undo", """
extern pure fn g(h: Int) = @py { return None }
extern emission fn f(x: Str) -> Int undo g(1) = @py { return 1 }
""", "G4"),
    # `compensate` is not a substitute for `acquire`'s mandatory `undo`: the
    # class rule fires before the slot rules are reached.
    ("acquire extern with compensate but no undo", """
extern pure fn g(h: Int) = @py { return None }
extern acquire fn f() -> H compensate g(1) = @py { return 1 }
""", "G4"),
    # the three slot shapes that need the DECLARATION read, not just the slot:
    # a `result` in a slot with no acquired value, an undeclared callee, and a
    # bare name with no binding at all.
    ("no-return extern refuses a `result` binding", """
extern pure fn g(h: Int) = @py { return None }
extern acquire fn f(x: Str) undo g(result) = @py { return 1 }
""", "G4"),
    ("a no-return extern's slot runs with no variables in scope", """
extern pure fn g(h: Int) = @py { return None }
extern acquire fn f(x: Str) undo g(q) = @py { return 1 }
""", "G4"),
    ("an extern slot must be a plain call", """
extern acquire fn f(x: Str) -> H undo obj.close(x) = @py { return 1 }
""", "G4"),
    # the walk is generic, so a refusal nested inside the slot expression's own
    # `match`/list sub-expressions is reached exactly as the reference reaches
    # it — and `path` is out of scope at either depth.
    ("an extern slot refusal inside a match arm", """
extern pure fn close(h: Int) = @py { return None }
extern acquire fn open_(p: Str) -> H undo close(match result { _ => p })
  = @py { return 1 }
""", "G4"),
    ("an extern slot refusal inside a list literal", """
extern pure fn close(h: Int) = @py { return None }
extern acquire fn open_(p: Str) -> H undo close([p][0]) = @py { return 1 }
""", "G4"),
    # ORDERING. `_lower_externs` finishes ONE declaration — its class rules, then
    # both slots — before it starts the next, so the winner is the first
    # declaration in SOURCE order, not the lowest line: here the first extern's
    # `undo` sits on line 4 while the second extern's class rule is on line 6.
    ("the first malformed extern in source order wins", """
extern acquire fn a() -> H
  undo ghost(1)
  = @py { return 1 }
extern pure fn p() -> Str undo ghost2(1) = @py { return "x" }
""", "G4"),
    # ...and a valid declaration before a malformed one does not mask it.
    ("a valid extern before a malformed one", """
extern pure fn g(h: Int) = @py { return None }
extern acquire fn good() -> H undo g(1) = @py { return 1 }
extern acquire fn bad() -> H = @py { return 1 }
""", "G4"),

    # ---- item 391 / issue #106: the fn-body BINDING DISCIPLINE (G1/G6) -------
    # `_lower_pure_stmt`'s scope rules over a module `fn` body, plus the
    # arrow-body write form the reference's PARSER refuses. All four are
    # code-less in the reference, so before this slice the gate raised no
    # objection and the census filed them as false-admits; they were pinned in
    # TYPE_LAYER_GAP below and have been struck from it. The first four are the
    # checked-in rejection fixtures; the rest are the family's remaining shapes.
    ("let reassignment", _fixture("v2_let_reassignment"), "G6"),
    ("compound assignment on a let",
     _fixture("v2_compound_assign_on_let"), "G6"),
    ("duplicate let in one straight-line scope",
     _fixture("v2_duplicate_let_block_scope"), "G6"),
    ("a closure assigning to a capture",
     _fixture("g6_closure_mutates_capture"), "G6"),
    # a PARAMETER is recorded not-mutable, so writing one is a `let`
    # reassignment and not an undeclared name.
    ("assignment to a parameter", """
fn f(p: Int) -> Int {
  p = 2
  return p
}
""", "G6"),
    # the same guard inside a nested block: the arm snapshots the enclosing
    # scope, so the outer `let` is still what the write lands on.
    ("let reassignment inside an if arm", """
fn f(c: Bool) -> Int {
  let n = 1
  if (c) { n = 2 }
  return n
}
""", "G6"),
    # a redeclaration INSIDE one arm collides, unlike two disjoint arms.
    ("duplicate let inside one if arm", """
fn f(c: Bool) -> Int {
  if (c) { let y = 1  let y = 2  return y }
  return 0
}
""", "G6"),
    # a loop binding shadows nothing already live, and draws the same
    # already-declared diagnostic a second `let` would.
    ("a for binding over a live name", """
fn f(xs: List[Int]) -> Int {
  let x = 1
  for (x of xs) { }
  return x
}
""", "G6"),
    # the assignment position also decides UNDECLARED: nothing bound `z`, so the
    # write is the reference's G1 fn-scope refusal (not the G6 half).
    ("assignment to a name nothing bound", """
fn f() -> Int {
  z = 2
  return 1
}
""", "G1"),
    # ---- docs/design/457: the name RESOLUTION half of G1 --------------------
    # `_lower_pure_expr`'s `ExprVar` arm: a READ must land in the fn's `scope`
    # or in `callables`. Both fixtures were pinned in TYPE_LAYER_GAP below and
    # have been struck from it.
    ("an undeclared name read in a return",
     _fixture("v2_undeclared_fn_var"), "G1"),
    ("an undeclared name read inside a template",
     _fixture("g1_template_undeclared"), "G1"),
    # the callee position is a name read like any other.
    ("a call to a name nothing declares", """
fn f() -> Int {
  return g()
}
""", "G1"),
    # an argument is walked after the callee, which is the reference's order.
    ("an undeclared name in an argument", """
fn g(n: Int) -> Int { return n }
fn f() -> Int {
  return g(missing)
}
""", "G1"),
    # a `var` binds the name for the rest of its block; the read AFTER the block
    # is the undeclared one, since a block never leaks a binding outward.
    ("a read of a name bound only inside a sibling block", """
fn f(c: Bool) -> Int {
  if (c) { let inner = 1 }
  return inner
}
""", "G1"),
    # an ADT case with no payload is a VALUE, not an undeclared name: the
    # reference's `_tagged_case` arm returns ahead of the resolver.
    # (the accepting twin lives in ACCEPTED_PROGRAMS.)
    #
    # a receiver-first list transform is SUGAR for its free function, so the
    # next name the reference resolves is that free function's — undeclared
    # here, exactly as the reference has it.
    ("a list transform whose free function nothing declares", """
fn f(xs: List[Int]) -> Int {
  let ys = xs.map(3)
  return 1
}
""", "G1"),
    # ---- item 391 / issue #106: the shadowed module callable (G6) ----------
    # `_refuse_callable_shadowing`. A body that BINDS a name and CALLS it while
    # a module `fn` or `extern` of that name is in scope has two readings, and
    # the tiers do not pick the same one. Code-less in the reference, so before
    # this slice the gate raised no objection and the census filed
    # `shadowed_module_fn_call` as a false-admit; it was pinned in
    # TYPE_LAYER_GAP below and has been struck from it.
    ("a binding that shadows a module fn it calls",
     _fixture("shadowed_module_fn_call"), "G6"),
    # the extern half: the diagnostic spells the kind, so a shadowed `extern`
    # must draw "module extern" and not "module function".
    ("a binding that shadows a module extern it calls", """
extern pure fn helper(n: Int) -> Int = @py { return n }
fn f(g: (Int) -> Int) -> Int {
  let helper = g
  return helper(1)
}
""", "G6"),
    # a PARAMETER is a binder too, and it is reported ahead of any later `let`.
    ("a parameter that shadows a module fn the body calls", """
fn helper(n: Int) -> Int { return n }
fn f(helper: (Int) -> Int) -> Int {
  return helper(1)
}
""", "G6"),
    # whole-body granularity, not per-block: the bind in one arm and the call
    # outside it is the same ambiguity.
    ("a bind in an if arm with the call outside it", """
fn helper(n: Int) -> Int { return n }
fn f(c: Bool, g: (Int) -> Int) -> Int {
  if (c) { let helper = g }
  return helper(1)
}
""", "G6"),
    # ---- item 391 / issue #106: the provide-method rebind (G6) -------------
    # `_check_rebind`'s component-scope arm: a method-local `let` reusing an
    # activation-body name is emitted with the SAME host-safe name as the
    # component local, so the local's declared inverse runs against the shadow
    # and the acquisition it was meant to release leaks. Also code-less, also
    # struck from TYPE_LAYER_GAP below.
    ("a method local shadowing a component local",
     _fixture("g6_method_local_shadows_component"), "G6"),
    # the earlier-METHOD-LOCAL arm: the scope grows as the body walk goes.
    ("a method local bound twice in one method body", """
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let slot = key
      let slot = key.concat("!")
      effect store.insert(slot, "v")
      undo   store.remove(slot)
    }
  }
}
""", "G6"),
    # the method PARAMETER arm of the same rule.
    ("a method local shadowing the method's own parameter", """
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      effect store.insert(key, "v")
      undo   store.remove(key)
      let key = "k"
    }
  }
}
""", "G6"),
    # ---- item 391 / issue #106: the service-operation head -----------------
    # The refusing half: the same clause, same position, in front of a `Cache`
    # whose `put` is declared PLAIN. The reference's G4 names `put`, not the
    # clause, so a step that swallowed the `put` declaration would turn this
    # refusal into silence and the pair would red.
    *[(f"{label} does not swallow the next operation",
       _sop(clause, _SOP_PLAIN_PUT, impl), "G4")
      for label, clause, impl in _SOP_CLAUSES],
    # ---- docs/design/457 slice T1: the DECLARED-TYPE validation ------------
    # `lower.py::_validate_declared_types` -> `typecheck.check_type_wellformed`,
    # now decided natively: `selfhost/lower.rvl` `use`s the shared spelling
    # algebra in `selfhost/types.rvl` and runs it over every module `fn` and
    # `extern` signature and every config field, at the phase position the
    # reference gives it (the head of the same function that closes with the
    # config-is-data walk). `t6_bare_generic` was pinned in TYPE_LAYER_GAP
    # below and has been struck from it.
    ("a bare builtin generic as a fn return",
     _fixture("t6_bare_generic"), "T1"),
    ("a bare builtin generic as a fn parameter", """
fn f(x: List) -> Int {
  return 1
}
""", "T1"),
    # the walk recurses into type ARGUMENTS, so a well-formed head does not
    # excuse a malformed argument.
    ("a bare builtin generic nested in a type argument", """
fn f(x: Map[Str, Opt]) -> Int {
  return 1
}
""", "T1"),
    # the arity is checked in both directions, not just against zero.
    ("a builtin generic given too few arguments", """
fn f() -> Map[Str] {
  return Map.empty()
}
""", "T1"),
    ("a bare builtin generic as an extern parameter",
     'extern pure fn e(x: Result) -> Int = @py { return 1 }\n', "T1"),
    ("a bare builtin generic as an extern return",
     'extern pure fn e(x: Int) -> List = @py { return [] }\n', "T1"),
    # the POSITION-restricted heads. `Async[T]` is a value type nowhere, and an
    # async function type is a module `fn` parameter only (item 92) — so the
    # same spelling is refused on an extern and admitted on a module fn, which
    # is the pair that proves the flag is threaded and not hard-coded.
    ("a bare Async as a value type", """
fn f(x: Async[Str]) -> Int {
  return 1
}
""", "A1"),
    ("an async function type outside a module fn parameter",
     'extern pure fn e(cb: (Str) -> Async[Str]) -> Int = @py { return 1 }\n',
     "A1"),
    # a config field asks the WELLFORMED question before the is-data one.
    ("a bare builtin generic as a config field", """service S { fn q(a: Str) -> Int }
component C provides s: S {
  config { n: Opt }
  provide s { fn q(a) { return 0 } }
}
""", "T1"),
    # ---- docs/design/457 §2.4: the component header's service-existence rule -
    # The reference resolves every `requires`/`provides` annotation against its
    # service table (`Env.__init__` for the requirements, `_lower_component`'s
    # `comp.provides` loop for the provisions) and refuses an unresolved one by
    # name. Both are here, plus the ORDER between them: the reference decides
    # every requirement before it reaches the provisions, so a component whose
    # two clauses both dangle is refused for its requirement.
    ("an undeclared service in requires",
     "component C requires s: S { }", "G1"),
    ("an undeclared service in provides",
     "component C provides s: S { }", "G1"),
    ("requires is decided before provides",
     "component C requires a: A provides b: B { }", "G1"),
    # the issue-346 harness candidate, verbatim: the STANDALONE question about a
    # component that requires a service the running composition provides. The
    # manifest arm of the same bytes is `test_the_manifest_gap_is_priced_not_hidden`.
    ("a candidate requiring an ambient-only service, standalone", """
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
""", "G1"),
    # ---- docs/design/457 T4b: the required-service member rule (A6) -------
    # `_component_req_call` / `_lower_postfix`'s `req` branch look the operation
    # up in the service the requirement resolves to and refuse an absent one by
    # name, before they count arguments or judge the emit marking. Both body
    # positions are here — a setup `effect` bracket (the `a6_method_not_in_service`
    # fixture's own shape) and a provide-method call — plus the ORDER against
    # G4: an absent operation cannot be an unmarked emission, so the A6 refusal
    # is what an `emission`-less name draws even under `emit`.
    ("an absent service operation in a setup effect bracket", """
service Database { fn query(sql: Str) -> Int }
component P requires db: Database {
  let n = effect db.execute("x") undo db.query("y")
}
""", "A6"),
    ("an absent service operation in a provide method", """
service Store { fn get(key: Str) -> Str }
service Cache { fn lookup(key: Str) -> Str }
component C requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.nonexistent(key) }
}
""", "A6"),
    ("an absent service operation under `emit` is A6, not G4", """
service Bus { emission fn publish(topic: Str) }
service Cache { fn put(key: Str) }
component C requires bus: Bus provides cache: Cache {
  provide cache { fn put(key) { emit bus.broadcast(key) } }
}
""", "A6"),
    # ---- docs/design/457 T3b: returns on every path (T1) -------------------
    # `_check_returns_on_every_path`, the last obligation `_lower_fns` runs for
    # a `fn`. Two messages, and which one fires is decided by whether the body
    # contains a `return` AT ALL — the fixtures are the reference's own
    # documented pair, and the shapes below are the rest of the rule.
    ("a declared return whose body never returns",
     _fixture("t8_missing_return"), "T1"),
    ("a declared return the trailing bare if can fall past",
     _fixture("t9_return_path_incomplete"), "T1"),
    # a bare `if` with no `else` may be skipped, so the path falls through.
    ("an if with no else", """
fn f(c: Bool) -> Int {
  if (c) { return 1 }
}
""", "T1"),
    # a `for` may run zero times and so terminates nothing.
    ("a for loop as the only returning path", """
fn f(xs: List[Int]) -> Int {
  for (x of xs) { return x }
}
""", "T1"),
    # a `while` whose condition is not the literal `true` may run zero times.
    ("a conditional while as the only returning path", """
fn f(c: Bool) -> Int {
  while (c) { return 1 }
}
""", "T1"),
    # `while (true)` with a `break` that TARGETS it may leave the loop and fall
    # through, so it does not terminate the path (item 379).
    ("a while(true) with a break targeting it", """
fn f() -> Int {
  while (true) {
    if (true) { break }
    return 1
  }
}
""", "T1"),
    # an `if`/`else` where only ONE arm returns.
    ("an if/else with only one returning arm", """
fn f(c: Bool) -> Int {
  if (c) { return 1 } else { let n = 2 }
}
""", "T1"),
    # the message quotes the DECLARED return spelling verbatim, arguments and
    # fn types included, so a renderer that normalised it would show here.
    ("the refusal quotes a generic return spelling", """
fn f(n: Int) -> Map[Str, Int] {
  let doubled = n * 2
}
""", "T1"),
    ("the refusal quotes a fn-type return spelling", """
fn f(n: Int) -> (Int) -> Int {
  let doubled = n * 2
}
""", "T1"),
    # the rule is per `fn` in DECLARATION order, and a clean `fn` ahead of the
    # refusing one must not move the anchor.
    ("the second fn is the one refused", """
fn g() -> Int {
  return 1
}
fn f() -> Int {
  let n = 2
}
""", "T1"),
    # The same iteration head, now with a real refusal in the program: the
    # unmarked emission the G4 walk must still reach (the loop body is read out
    # inline, so a reader that stepped over it would lose this too). The
    # reference draws ONE refusal here — the G4 — and naming the item `Int`
    # added a T1 that outranked it, which is the whole shape of the defect.
    ("an iteration body is still walked for its own refusal", """
service Sink { emission fn write(v: Str) -> Int }
service Api { fn go() -> Int }
component C requires sink: Sink provides api: Api {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo sub.close()
  every o in sub { emit sink.write(o) }
  provide api { fn go() { return sink.write("x") } }
}
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


@pytest.mark.parametrize("binding", ["let", "var"])
@pytest.mark.parametrize("annotation", ["", ": Str"])
def test_method_local_binding_captured_by_undo(admit, binding, annotation):
    assignment = 'key = "later"' if binding == "var" else ""
    source = f'''service CasOps {{ fn run() -> Int }}
component CasProvider provides ops: CasOps {{
  let ledger = effect Map.new() undo ledger.drop()
  let fresh = effect ledger.insert_if_absent("boot", 1) undo ledger.remove("boot")
  provide ops {{
    fn run() {{
      {binding} key{annotation} = "method"
      let token = effect ledger.insert_if_absent(key, 2) undo ledger.remove(key)
      {assignment}
      return 2
    }}
  }}
}}'''
    assert _ref(source) == ("", "")
    _agree(admit, source)


@pytest.mark.parametrize("binding", ["let", "var"])
@pytest.mark.parametrize("annotation", ["", ": Int"])
def test_local_initializer_emission_is_not_skipped(admit, binding, annotation):
    source = f'''service Db {{ emission fn write() -> Int }}
service Api {{ fn run() -> Int }}
component C requires db: Db provides api: Api {{
  provide api {{
    fn run() {{
      {binding} value{annotation} = db.write()
      return 0
    }}
  }}
}}'''
    assert _ref(source)[0] == "G4"
    _agree(admit, source)


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
                # A `"`-delimited rvl string literal: `\"` and `\\` are the only
                # escapes (src/revl/lexer.py `_lex_string`), so scan honouring
                # them to find the true closing quote and decode the content the
                # way the compiler does. A naive `.index('"')` truncates the
                # program at the first `\"`, leaving a trailing `\` that neither
                # side was meant to see.
                k = j + 1
                buf: list[str] = []
                while k < len(section):
                    c = section[k]
                    if c == "\\" and k + 1 < len(section) and section[k + 1] in ('"', "\\"):
                        buf.append(section[k + 1])
                        k += 2
                        continue
                    if c == '"':
                        break
                    buf.append(c)
                    k += 1
                progs.append("".join(buf))
                idx = k + 1
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
# item 419c: refusal ordering by line, cross-checked against the reference's
# collect-all sink (item 386). `admit_src` now collects every recoverable
# refusal, anchors each at the line the reference's diagnostic carries, and
# reports the minimum by (line, seq) — the reference's `diagnostics[0]`. This is
# where `test_which_refusal_wins_diverges_when_a_program_has_several` used to
# pin the divergence; it is gone because the two now AGREE.
# ---------------------------------------------------------------------------

# Each program carries two or three TRUE refusals of DISTINCT families on
# distinct lines, so the winner is decided by LINE and the full list exercises
# the collecting sink. Messages are kept distinct so the reference's dedup (on
# `(code, filename, line, message)`) never collapses an entry — which would
# desync the `admit_all` count on the one-line variant, where every line is 1.
_MULTI_REFUSAL_PROGRAMS = [
    # an earlier-LINE link (G2, the second provider's line 4) beats a later-line
    # component body (G4, line 5). Multiline: G2 wins. One-line: every line is 1,
    # so the tie-break is pipeline order — the component body is collected before
    # the link, so G4 wins on BOTH sides.
    ("link vs later body", """service D { fn q(s: Str) -> Int }
service Bus { emission fn publish(topic: Str) }
component A provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component B provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component Z requires bus: Bus { effect bus.publish("x") undo bus.publish("y") }
"""),
    # two components each refused (G4 on DIFFERENT keys, lines 3 and 4); the
    # earlier line wins and the full list carries both. Distinct keys keep the
    # messages distinct, so the one-line variant is two entries, not one.
    ("two components each refused", """service Bus { emission fn publish(topic: Str) }
service Log { emission fn write(m: Str) }
component P requires bus: Bus { effect bus.publish("x") undo bus.publish("y") }
component Q requires log: Log { effect log.write("a") undo log.write("b") }
"""),
    # a module-fn A1 twin (line 2) beside a component body (G4, line 4). The
    # reference RAISES the module-fn twin before its collect-all sink exists, so
    # its diagnostic list is JUST that one A1 — the gate mirrors the FAIL-FAST
    # (it returns the module-fn refusal alone). The winner is A1 on both
    # variants; the full list is a singleton on both sides.
    ("module fn plus a component", """extern emission async fn tick() -> Int = @py { return 1 }
fn holder() -> Int { let f = tick   return 0 }
service Bus { emission fn publish(topic: Str) }
component Z requires bus: Bus { effect bus.publish("x") undo bus.publish("y") }
"""),
    # docs/design/457 slice T1: the DECLARED-TYPE validation is FAIL-FAST on
    # both sides — the reference raises it inside `_validate_declared_types`,
    # before its collect-all sink exists — so it beats an EARLIER-LINE link
    # refusal and the full list is a singleton. This is the case a phase order
    # that merely appended the new refusal into the collected sink would get
    # wrong: the sink orders by line, and the bare generic is on line 4 while
    # the duplicate provider is on line 3.
    ("bare generic after an earlier-line duplicate provider",
     """service D { fn q(s: Str) -> Int }
component A provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component B provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
fn late() -> Opt { return Some(1) }
"""),
    # the same two refusals with the declared-type one FIRST in the file, so the
    # winner is not evidence of the phase order on its own. Both variants must
    # name the bare generic, and both lists must be singletons.
    ("bare generic before the duplicate provider",
     """fn early() -> Opt { return Some(1) }
service D { fn q(s: Str) -> Int }
component A provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
component B provides db: D { provide db { fn q(s) { let x = s   return 0 } } }
"""),
    # the declared-type question and the config-is-data question live in the
    # SAME reference function, per field, wellformed first: the component's
    # config declares an arrow field (is-data, G4) and a bare generic
    # (wellformed, T1), and the wellformed one wins on both sides.
    ("bare generic beside an arrow config field",
     """service S { fn q(a: Str) -> Int }
component C provides s: S {
  config { h: (Str) -> Str, n: Opt }
  provide s { fn q(a) { return 0 } }
}
"""),
]


def _parse_admit_all(blob: str) -> list[tuple[int, str]]:
    r"""Parse `admit_all`'s row-joined "<line>|<tag>|<msg>" into (line, tag).

    The gate joins rows with the two characters ``\n`` — a revl PLAIN string
    preserves ``\n`` verbatim rather than as a newline (selfhost/lexer.rvl: the
    only escapes are ``\"`` and ``\\``), so the emitted separator is a literal
    backslash-n, not U+000A. No refusal message contains that sequence, so the
    split is unambiguous."""
    out: list[tuple[int, str]] = []
    for row in blob.split("\\n"):
        if not row:
            continue
        line, tag, _ = row.split("|", 2)
        out.append((int(line), tag))
    return out


@pytest.mark.parametrize("oneline", [False, True], ids=["multiline", "oneline"])
@pytest.mark.parametrize("name,src", _MULTI_REFUSAL_PROGRAMS,
                         ids=[n for n, _ in _MULTI_REFUSAL_PROGRAMS])
def test_multi_refusal_programs_agree(admit, admit_all, name, src, oneline):
    """Item 419c. On a multi-refusal program the gate's `admit_src` reports the
    reference's `diagnostics[0]` — the minimum by (line, seq). The one-line
    variant collapses every line to 1, so the tie-break is pipeline order, which
    the gate's `seq` (its append order) must reproduce.

    Two assertions per program:

    * WINNER — `admit_src` equals the reference's first diagnostic, tag AND
      message (`_ref`).
    * FULL LIST — `admit_all` equals the reference's `RevlErrors` carrier
      (`_ref_all`), compared as sorted (line, tag) lists. This is restricted to
      the recovery granularity the gate mirrors: the component loop, the
      post-passes, and the link COLLECT (so their programs carry the full list),
      while the module-fn twin is FAIL-FAST on BOTH sides (the reference raises
      before its sink; the gate returns the one refusal), a singleton list —
      which is exactly why the corpus keeps every OTHER family in the collected
      regime. Statement-level (Stage-2) recovery inside a body is out of scope.
    """
    if oneline:
        src = _oneline(src)

    ref = _ref_all(src)
    assert ref, "the reference must refuse every corpus program"
    assert all(not t.startswith("OUT:") for _, t in ref), ref  # in-slice only

    # WINNER: tag AND message equal the reference's diagnostics[0].
    ref_tag, ref_msg = _ref(src)
    got = admit(src)
    got_tag = got.split("|", 1)[0] if "|" in got else got
    got_msg = got.split("|", 1)[1] if "|" in got else ""
    assert got_tag == ref_tag, (name, oneline, got, ref)
    assert got_msg == ref_msg, (name, oneline, got_msg, ref_msg)

    # FULL LIST: sorted (line, tag) equality against the carrier.
    got_all = _parse_admit_all(admit_all(src))
    assert sorted(got_all) == sorted(ref), (name, oneline, got_all, ref)


# ============================================================ nesting bound
#
# The emitted front end is recursive twice over: once descending to parse an
# expression (one nested level costs a full pass down the precedence ladder)
# and again in every walk over the tree that comes back. Neither descent had a
# bound, so a SHORT source could drive both arbitrarily deep. Here that is a
# `RecursionError`; behind `crates/revl-gate` it is a rust stack overflow,
# which ABORTS — `catch_unwind` cannot turn an abort back into a verdict, so
# an embedder of the gate lost the process instead of getting an answer.
#
# `selfhost/parser.rvl` now carries a depth through its ladder and refuses past
# `nesting_limit()`, and `admit_src` measures the same bound over the token
# stream ahead of any descent, which is where it can be an actual verdict.

def _nesting_limit() -> int:
    """Read out of `selfhost/parser.rvl` rather than restated, so a change to
    the bound cannot leave this file asserting the old number."""
    text = (ROOT / "selfhost" / "parser.rvl").read_text(encoding="utf-8")
    match = re.search(r"fn nesting_limit\(\) -> Int \{ return (\d+) \}", text)
    assert match, "selfhost/parser.rvl no longer states a nesting_limit()"
    return int(match.group(1))


NESTING_LIMIT = _nesting_limit()


def test_the_self_host_bound_equals_the_reference_compilers():
    """The two bounds are one bound, asserted rather than assumed.

    `selfhost/parser.rvl` states 200 because `revl.parser.NESTING_LIMIT` does.
    Nothing held them together: the self-host side is read out of the file (so
    this module cannot assert a stale number) and the reference side was never
    consulted at all, so moving either one alone would have left the gate and
    the reference refusing at different depths, silently disagreeing over a
    whole band of inputs. Whichever side moves, the other has to move with it.
    """
    from revl import parser as reference_parser
    assert NESTING_LIMIT == reference_parser.NESTING_LIMIT


_TOO_DEEP = (f"BAD|expression nesting is deeper than the parser's limit of "
             f"{NESTING_LIMIT} levels")

# Every shape that makes the front end go deeper — the ones that nest through
# a bracket, the ones that recurse without one (a prefix run, a `??` chain, a
# right-associative conditional), and the ones the parser reads with a loop but
# that still build a tree one level taller per operator.
_NESTED = {
    "groups":      lambda n: _fn("(" * n + "1" + ")" * n),
    "calls":       lambda n: _fn("g(" * n + "1" + ")" * n),
    "lists":       lambda n: _fn("[" * n + "1" + "]" * n),
    "records":     lambda n: _fn("{ a: " * n + "1" + " }" * n),
    "indexes":     lambda n: _fn("a" + "[0]" * n),
    "matches":     lambda n: _fn("match x { A => " * n + "1" + " }" * n),
    "arrows":      lambda n: _fn("() => " * n + "1"),
    "templates":   lambda n: _fn("`" + "${`" * n + "x" + "`}" * n + "`"),
    "not":         lambda n: _fn("!" * n + "true"),
    "negate":      lambda n: _fn("-" * n + "1"),
    "complement":  lambda n: _fn("~" * n + "1"),
    "conditional": lambda n: _fn("true ? 1 : " * n + "1"),
    "nullish":     lambda n: _fn("a ?? " * n + "1"),
    "blocks":      lambda n: "fn f() -> Int { " + "{ " * n + " " + "} " * n + " return 1 }",
    "ifs":         lambda n: "fn f() -> Int { " + "if (true) { " * n + " " + "} " * n + " return 1 }",
    "types":       lambda n: "fn f() -> " + "List[" * n + "Int" + "]" * n + " { return 1 }",
    "add spine":   lambda n: _fn("1 + " * n + "1"),
    "and spine":   lambda n: _fn("a && " * n + "b"),
    "field spine": lambda n: _fn("a" + ".b" * n),
    "call spine":  lambda n: _fn("a" + "()" * n),
    "method spine": lambda n: _fn('"a"' + '.concat("b")' * n),
}


def _fn(expr: str) -> str:
    return "fn f() -> Int { return " + expr + " }"


@contextlib.contextmanager
def _frames(budget: int):
    """Run with a stated frame budget, restoring the interpreter's own.

    The number is the interesting part: `crates/revl-gate` was measured to
    abort at roughly seven thousand frames of the emitted parser, so a descent
    that stays inside this budget here is one the crate has the stack for.
    """
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(budget)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


_FRAME_BUDGET = 6000


@pytest.mark.parametrize("shape", sorted(_NESTED))
def test_a_deeply_nested_program_is_refused_by_the_bound(admit, shape):
    """5000 levels of every construct that nests, refused by name.

    Not "does not crash": the gate has to SAY the bound. A resource failure an
    embedder cannot distinguish from a verdict is the thing being fixed.
    """
    with _frames(_FRAME_BUDGET):
        assert admit(_NESTED[shape](5000)) == _TOO_DEEP


@pytest.mark.parametrize("shape", sorted(_NESTED))
def test_ordinary_nesting_is_not_refused_by_the_bound(admit, shape):
    """The other half: the bound sits above anything a program means. The
    deepest program in the whole census corpus measures 74 and the deepest one
    the gate will decide at all measures 42."""
    with _frames(_FRAME_BUDGET):
        for depth in (1, 2, 5, 20, 40):
            assert admit(_NESTED[shape](depth)) != _TOO_DEEP, (shape, depth)


def test_no_nesting_under_the_size_bound_exhausts_the_descent(admit):
    """The fuzz the size bound was standing in for.

    `MAX_SOURCE_BYTES` is a byte count and nesting is not: 1 KB of parentheses
    was enough to abort the crate, at 0.4% of that bound. So the descent is
    fuzzed on its own terms — random compositions of every nesting shape, grown
    to the size bound — and the property is that NONE of them exhausts the
    stack. A `RecursionError` here is an abort there.
    """
    max_bytes = int(re.search(
        r"MAX_SOURCE_BYTES: usize = (\d+);",
        (ROOT / "crates" / "revl-gate" / "src" / "frontier.rs")
        .read_text(encoding="utf-8")).group(1))
    rng = random.Random(20260905)
    shapes = sorted(_NESTED)
    refused = 0
    with _frames(_FRAME_BUDGET):
        for _ in range(300):
            shape = rng.choice(shapes)
            # from just under the bound to a source that fills the size bound
            depth = rng.choice([
                rng.randint(1, NESTING_LIMIT),
                rng.randint(NESTING_LIMIT, 4 * NESTING_LIMIT),
                rng.randint(1000, 20000),
            ])
            src = _NESTED[shape](depth)
            if len(src) > max_bytes:
                continue
            verdict = admit(src)           # must not raise
            if verdict == _TOO_DEEP:
                refused += 1
    assert refused > 0, "the fuzz never reached the bound"


# =============================================================== type layer
#
# The self-host TYPE-LAYER parity gap, named executably (docs/design/457,
# slice T0). This is the same device `test_selfhost_ownership_gap_308.py` uses
# for O1/B1: a KNOWN, DESIGNED divergence recorded here so no later oracle
# discovers it by going red on a surprise.
#
# The reference types a function body, its declarations and its provide-method
# bodies and refuses when they do not check. `selfhost/lower.rvl`'s `admit_src`
# has no phase for any of that yet (`lir_*` threads a typed environment through
# the IR but never refuses), so it ADMITS every one of these programs. The two
# therefore DIVERGE: the reference refuses with a type-layer tag, the gate
# returns "". Design section 1 measured that gap at 46 fixtures over
# `examples/rejections/`; it now stands at 9. The self-declared async-colour
# arrow (rule C1) and then the four fn-body BINDING fixtures (item 391's
# binding-discipline slice) moved OUT of the gap into gate/reference agreement,
# and two slices have moved fixtures IN by making the gate READ a body it used
# to stop short of: the `pub` prefix slice brought `t29`/`t30` (hidden behind
# the `pub extern` parse refusal), and the type-parameter-list slice brought
# `t25` (hidden behind a parser that did not spell `fn name[T](…)`). All three
# were type-layer false-admits all along. Grouped below by the
# reference check that refuses
# them (the family each self-host slice T1..T4 will move from "pinned gap" to
# "agrees").
#
# This test asserts the divergence is still there. When a later slice ports a
# family, its fixtures start refusing on the gate side and the matching rows
# here go red. That is the signal to: (1) delete those rows, (2) remove the same
# fixtures from KNOWN_BYPASSES in tests/test_gate_reference_census.py and
# re-record the census baseline, and (3) fold the fixtures into the slice's own
# agreement corpus. Until then this file plus KNOWN_BYPASSES are the whole
# record that the remaining gap is intended, not a latent gate bypass.
#
# The tag beside each fixture is what `_classify` (above) derives from the
# reference refusal, i.e. the bucket the census now files the false-admit under.
TYPE_LAYER_GAP: dict[str, list[tuple[str, str]]] = {
    # fn-body binding rules (G1/G6): LANDED WHOLE. The ASSIGNMENT half went
    # first (item 391's binding-discipline slice), then the callable-shadowing
    # slice, and the name-RESOLUTION half (docs/design/457, the G1 read position)
    # closed the rest: `g1_template_undeclared` and `v2_undeclared_fn_var` are
    # now in REJECTED_PROGRAMS above, where tag AND message are compared, so
    # this family has no row left here.
    # expression typing (T1/T2): the operator/field/index/record algebra and the
    # literal-range and `null` refusals. The fn-body STATEMENT layer
    # (docs/design/457 T3a) landed this family: the `lir_*` walk now carries a
    # `TEnv` beside its binding scope and consults the algebra at each position
    # `_lower_pure_stmt` does, so `t2`, `t11`, `t12`, `t21`, `t22`, `t23`,
    # `t26`, `t27`, `t28`, `t29`, `t36` and the backend fixture
    # `dynamic_reserved_key` moved into REJECTED_PROGRAMS above, where tag AND
    # message are compared, and left this list.
    #
    # What stays needs the optional-chaining rules the expression slice did not
    # build: `?.` on a non-optional is decided from the target's type at the
    # CHAIN, which is T2d's.
    "expression typing (T1/T2)": [
        ("t14_optional_chain_on_nonoptional", "T1"),
    ],
    # calls and signatures: LANDED whole (docs/design/457 T2b). The signature
    # table, `unify`/`substitute`, the host stub surface, `_BUILTIN_SIG` and the
    # four lowering-time method refusals moved all nine of this family's
    # fixtures into REJECTED_PROGRAMS above, where tag AND message are compared,
    # so the family has no row left here.
    # arrows and function values: arrow-body checking, function-value flow and
    # arity, arrow annotations. (The self-declared async colour,
    # t34_arrow_self_declared_async, was in this family until the gate learned
    # to parse an arrow's written return annotation and refuse a self-declared
    # `Async[…]` colour — rule C1 — so it now AGREES with the reference and has
    # left this gap; see agree-refuse/A1 in the census.)
    "arrows and function values": [
        ("t17_arrow_body_unchecked", "T1"),
        ("t32_arrow_value_result_flows", "T1"),
        ("t33_arrow_value_arity", "T1"),
        ("t35_arrow_annotation_not_quantified", "T1"),
    ],
    # return paths and match: unknown/missing match cases. The RETURN-PATH half
    # has LANDED (docs/design/457 T3b): `fb_function` runs
    # `_check_returns_on_every_path` over the statement tree `fb_scan` already
    # builds, so `t8_missing_return` and `t9_return_path_incomplete` moved into
    # REJECTED_PROGRAMS above, where tag AND message are compared. What stays
    # here needs the variant table and the arm algebra, which is T2d's.
    "return paths and match": [
        ("t13_unknown_match_case", "TYPE"),
        ("v2_match_nonexhaustive", "T1"),
    ],
    # declarations: alias cycles and non-record destructuring. The DECLARED-TYPE
    # half of this family has LANDED (slice T1): `selfhost/lower.rvl` `use`s the
    # spelling algebra in `selfhost/types.rvl` and runs `check_type_wellformed`
    # over every module `fn`/`extern` signature and every config field, so
    # `t6_bare_generic` now refuses with the reference's tag and message and has
    # moved into REJECTED_PROGRAMS above. What stays pinned here is decided
    # elsewhere: the alias cycle in `_resolve_type_aliases` and the destructuring
    # rule in `_lower_let_pattern_stmt`, neither of which is a declared-type
    # question.
    "declarations": [
        ("t18_type_alias_cycle", "TYPE"),
        ("t5_destructure_nonrecord", "TYPE"),
    ],
    # provide-method and component bodies: EMPTY. This family has landed
    # (docs/design/457, the provide-method slice). All seven of its documents —
    # `t1_service_arg_type`, `t4_field_arg_type`,
    # `t7_provide_param_annotation_mismatch`, `t16_provide_method_missing_return`,
    # `t30_field_read_on_any_provide_method`, `t31_index_non_int_provide_method`
    # and `t3_config_default_type` — moved into
    # `_PROVIDE_FIXTURES` below, where tag AND message are compared. The key is
    # kept rather than deleted so the family's name stays attached to the slice
    # that closed it.
    "provide-method and component bodies": [],
}

_TYPE_LAYER_CASES = [
    (family, name, tag)
    for family, rows in TYPE_LAYER_GAP.items()
    for name, tag in rows
]


def test_the_service_existence_rule_stops_at_a_use_declaration(admit):
    """The declared frontier of the component header's service-existence rule
    (docs/design/457 §2.4), pinned as a divergence rather than left to be
    discovered.

    A `use` declaration can IMPORT a service — the reference admits
    `use "./svc.rvl" { Store }` followed by `requires store: Store` when the
    module is supplied — and `p_top` steps over `use` without reading the module,
    so a text carrying one has no knowable service set. The gate therefore
    decides nothing there. That is the UNDER-refusing direction, which is the one
    this gate is allowed to err in: refusing a program the reference admits is
    the false alarm it may not produce.

    The reference's own verdict on such a single source is the missing-`modules=`
    refusal, which is out of this gate's slice for the same reason the three
    `v2_use_*` fixtures are: the crate cannot supply `modules=` either. When a
    later slice teaches the gate to read modules, this test flips to an
    agreement — it is not a waiver on the rule, it is the rule's boundary."""
    src = 'use "./svc.rvl" { S }\ncomponent C requires s: S { }\n'
    ref_tag, ref_msg = _ref(src)
    assert ref_tag.startswith("OUT:") and "`modules=`" in ref_msg, (
        f"the reference's single-source `use` refusal changed: {ref_msg!r}")
    assert admit(src) == "", (
        "a text whose services may come from a module must draw no "
        "service-existence verdict")
    # ... and the SAME text without the `use` is refused, so the exemption is
    # doing the work rather than the program being harmless.
    assert admit("component C requires s: S { }\n") == (
        "G1|unknown service `S` in `requires` of C")


# ---- the A6 member rule against item 391's G6 shadowing discipline ----------
#
# Two slices landed a refusal into the SAME walk: the A6 member rule
# (docs/design/457 T4b) refuses inside `req_call`, and `mth_rebind`
# (`_check_rebind`, item 391) refuses a provide-method binding that collides
# with a name already in scope. Both are raised by the reference DURING the body
# lowering, so on a program carrying both the EARLIER SOURCE LINE wins - and the
# gate has to pick the same one. That exact class of disagreement is item 419c,
# and it is not something a per-slice corpus can see, because every corpus
# program carries exactly one refusal.
#
# `csh_refusal` (`_refuse_callable_shadowing`, item 391's other half) is the
# contrast: the reference runs it in `check_and_lower` BEFORE it lowers a single
# component, so it beats a body refusal regardless of line order.

_A6_G6_SVCS = ("service Store { fn get(key: Str) -> Str }\n"
               "service Cache { fn lookup(key: Str) -> Str }\n")

@pytest.mark.parametrize("name,src", [
    # the method-PARAMETER arm of `_check_rebind`, each order
    ("A6 on the earlier line", _A6_G6_SVCS + """component C requires store: Store provides cache: Cache {
  provide cache {
    fn lookup(key) {
      let a = store.nope(key)
      let key = "x"
      return "y"
    }
  }
}
"""),
    ("the rebind on the earlier line", _A6_G6_SVCS + """component C requires store: Store provides cache: Cache {
  provide cache {
    fn lookup(key) {
      let key = "x"
      let a = store.nope(key)
      return "y"
    }
  }
}
"""),
    # the COMPONENT-LOCAL arm, in the shape of the corpus fixture
    # `g6_method_local_shadows_component.rvl`, each order
    ("A6 before a component-local rebind",
     "service Store { fn get(key: Str) -> Str }\n"
     "service Cache { fn set(key: Str) }\n" + """component C requires svc: Store provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let a = svc.nope(key)
      let store = key
    }
  }
}
"""),
    ("a component-local rebind before the A6",
     "service Store { fn get(key: Str) -> Str }\n"
     "service Cache { fn set(key: Str) }\n" + """component C requires svc: Store provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let store = key
      let a = svc.nope(key)
    }
  }
}
"""),
    # the fail-fast phase, with the A6 deliberately on the EARLIER line
    ("callable shadowing fails fast ahead of an earlier-line A6",
     _A6_G6_SVCS + """component C requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.nope(key) }
}
fn helper(xs: List[Int]) -> Int { return xs.length() }
fn shadowed(g: (List[Int]) -> Int) -> Int {
  let helper = g
  return helper([1, 2, 3])
}
"""),
])
def test_the_member_rule_and_the_shadowing_rules_agree_on_which_refusal_wins(
        admit, name, src):
    """Both refusals are true of the program; the gate must name the one
    `check_and_lower` names, not merely refuse something."""
    ref_tag, ref_msg = _ref(src)
    assert ref_tag in ("A6", "G6"), (
        f"probe bug: the reference answers {ref_tag!r} ({ref_msg!r}), so this "
        f"program does not pit the two rules against each other")
    assert admit(src) == f"{ref_tag}|{ref_msg}"


def test_the_type_layer_gap_is_exactly_9_fixtures():
    """Section 1's measured gap, held as a count so a fixture cannot quietly
    leave or join the pinned set without this number moving in the diff. It was
    42 until the returns-on-every-path rule (docs/design/457 T3b(returns)) took
    two of them, the fn-body STATEMENT layer (T3a) eleven more of the expression
    rows for the module-`fn` surface, the call-and-signature layer (T2b) nine
    more, the declared-type slice (T1) `t6_bare_generic`, and the provide-method
    / component slice the whole `provide-method and component bodies` family,
    all seven of it; on top of those, the name-RESOLUTION half of the G1/G6
    family (docs/design/457, the G1 read position) took the last two. The
    twelfth document that moved with T3a, `dynamic_reserved_key`, never had a
    row here because this pin addresses its fixtures by bare name under
    `examples/rejections/`."""
    assert len(_TYPE_LAYER_CASES) == 9, len(_TYPE_LAYER_CASES)
    names = [name for _, name, _ in _TYPE_LAYER_CASES]
    assert len(set(names)) == 9, "a fixture is listed twice"


@pytest.mark.parametrize("family,name,tag", _TYPE_LAYER_CASES,
                         ids=[f"{n}" for _, n, _ in _TYPE_LAYER_CASES])
def test_type_layer_gap_is_a_named_selfhost_divergence(admit, family, name, tag):
    """Reference refuses this program in its type layer; the gate does not run
    that layer yet, so it admits. Pinned per fixture so the divergence is owned,
    not discovered by a red oracle.

    When a row here reds because the gate began refusing its fixture, the type
    layer for that family has landed: delete the row, drop the same fixture from
    KNOWN_BYPASSES in tests/test_gate_reference_census.py, re-record the census
    baseline, and move the fixture into the slice's agreement corpus."""
    src = _fixture(name)
    ref_tag, ref_msg = _ref(src)
    assert ref_tag == tag and ref_msg != "", (
        f"reference no longer refuses {name} with tag {tag!r}: "
        f"got tag {ref_tag!r}, message {ref_msg!r}. If the reference message "
        f"shape changed, update _classify and this row together.")
    got = admit(src)
    assert got == "", (
        f"the gate now refuses {name} ({got!r}): the {family!r} type-layer "
        f"slice appears to have landed. Flip this fixture — delete its row "
        f"here, delete it from KNOWN_BYPASSES, re-record the census baseline, "
        f"and fold it into the slice's agreement corpus.")


# ------------------------------------------ returns on every path: the anchor
#
# `_agree` compares the tag and the message; it does not compare the LINE, and
# the two messages of this rule are anchored at DIFFERENT statements — the
# never-returns one at the `fn` declaration, the falls-through one at the last
# statement of the body. A port that spelled both sentences correctly off one
# anchor would pass every row in REJECTED_PROGRAMS and still report the wrong
# place, so the anchor is asserted here, against the reference's own line.

_RETURN_PATH_ANCHORS = [
    ("never returns, anchored at the declaration", _fixture("t8_missing_return")),
    ("falls through, anchored at the last statement",
     _fixture("t9_return_path_incomplete")),
    # the two anchors pull APART here: the declaration is line 2 and the
    # trailing `if` that falls through is line 5, so an anchor that had
    # collapsed onto the declaration would show.
    ("falls through several lines below the declaration", """
fn f(c: Bool) -> Int {
  let a = 1
  let b = 2
  if (c) { return a + b }
}
"""),
    # and here the last statement is a `while`, not an `if`.
    ("falls through at a trailing while", """
fn f(c: Bool) -> Int {
  let a = 1
  while (c) { return a }
}
"""),
]


@pytest.mark.parametrize("name,src", _RETURN_PATH_ANCHORS,
                         ids=[n for n, _ in _RETURN_PATH_ANCHORS])
def test_the_return_path_refusal_is_anchored_where_the_reference_anchors_it(
        admit_all, name, src):
    try:
        compile_source(src, "diff.rvl")
        pytest.fail(f"corpus bug: the reference admits {name}")
    except RevlError as error:
        rows = [row for row in admit_all(src).split("\n") if row]
        assert len(rows) == 1, f"{name}: expected one refusal, got {rows!r}"
        line, tag, message = rows[0].split("|", 2)
        assert tag == "T1"
        assert message == error.message
        assert int(line) == error.line, (
            f"{name}: gate anchored the refusal at line {line}, the reference "
            f"at line {error.line}")


def test_an_unresolved_name_read_outranks_the_return_path_on_the_reference(
        admit):
    """The PRECEDENCE divergence the returns-on-every-path slice introduced, now
    CLOSED, kept here as the non-vacuity witness.

    The reference lowers a fn body statement by statement and only then asks
    whether the fn returns on every path, so a body that BOTH reads an
    undeclared name and never returns draws the name refusal. Until the name
    READ position was built, this gate decided only the ASSIGNMENT half of the
    binding discipline and drew the return-path refusal instead — true, but the
    other of the two guarantees the program breaks. The read position now runs
    inside `fb_walk`, ahead of `rp_refusal`, which is the reference's own
    order."""
    src = "fn f() -> Int {\n  nobody\n}\n"
    ref_tag, ref_msg = _ref(src)
    assert (ref_tag, ref_msg) == ("G1", "`nobody` is not declared in this function")
    assert admit(src) == "G1|`nobody` is not declared in this function"


def test_the_name_resolution_rule_stops_at_a_use_declaration(admit):
    """The declared frontier of the G1 read rule (docs/design/457 §2.3), pinned
    as a divergence rather than left to be discovered.

    A `use` declaration puts the imported module's `pub fn`s into `callables`
    (`program.fn_scopes`) and makes an aliased `alias.f(..)` call resolve before
    the name resolver runs at all. This gate does not read modules, so the
    callable universe stops being knowable from the text alone and the rule is
    switched off for the WHOLE text. That under-refuses, which is the direction
    the gate is allowed to err in.

    The reference cannot be driven on these texts either (it wants `modules=`),
    so the assertion is on the GATE alone: the withholding is deliberate."""
    withheld = """use "./other.rvl" as other

fn f() -> Int {
  return nothing_declares_this
}
"""
    assert admit(withheld) == ""
    # the same body WITHOUT the `use` is refused, so the row above is the `use`
    # doing the withholding and not the reader failing to reach the statement.
    assert admit("fn f() -> Int {\n  return nothing_declares_this\n}\n") == \
        "G1|`nothing_declares_this` is not declared in this function"


# ---------------------------------------------------------------- ambient / #86
#
# Item 186 / issue #86, SLICE 1: ambient (running-manifest) composition
# admission. `admit_ambient(src, manifest)` admits a component against an
# ALREADY RUNNING manifest, not a single self-contained text — the reference's
# `check_and_lower` `ambient` input, which `admit_src` has no analogue for.
#
# The manifest is the linker's live provision table, serialised as rows
# "<component>/<key>/<realm>" joined by ";" (realm "" = shared). Slice 1 covers
# provision disjointness: a `(key, realm)` the manifest already holds conflicts.
#
# There is no reference `admit_ambient` to drive directly, but slice 1 has an
# exact single-source equivalent that IS the oracle: admitting `X` against a
# manifest holding `M`'s provisions equals single-source-admitting the text
# `M ++ X` for the provision-disjointness guarantee, so
#
#     admit_ambient(X, manifest_of(M)) == admit_src(M ++ X) == reference(M ++ X)
#
# and all three legs are pinned below. This equivalence is exactly what the
# later handoff/replacement wave breaks (a hot-swap overrides rather than
# conflicts), which is why replacement is a wave and disjointness is a slice.


@pytest.fixture(scope="module")
def admit_ambient(ns):
    """The ambient gate: `admit_ambient(src, manifest) -> "" | "<tag>|<msg>"`."""
    return ns["admit_ambient"]


_SVC_D = "service D { fn q(s: Str) -> Int } "
_PROV_DB = "provides db: D { provide db { fn q(s) { let x = s   return 0 } } }"


@pytest.mark.parametrize("name,src", ACCEPTED_PROGRAMS,
                         ids=[n for n, _ in ACCEPTED_PROGRAMS])
def test_empty_manifest_is_single_source(admit, admit_ambient, name, src):
    """The base invariant: an empty manifest is the empty provision table, so
    `admit_ambient(src, "")` is `admit_src(src)` byte-for-byte, across the whole
    accepted corpus."""
    assert admit_ambient(src, "") == admit(src)


@pytest.mark.parametrize("name,src,tag", REJECTED_PROGRAMS,
                         ids=[n for n, _, _ in REJECTED_PROGRAMS])
def test_empty_manifest_is_single_source_when_refused(admit, admit_ambient,
                                                       name, src, tag):
    """Same invariant on every refused program: a refusal reached with no
    manifest is forwarded exactly."""
    assert admit_ambient(src, "") == admit(src)


def test_ambient_provision_conflict_shared_realm(admit_ambient):
    v = admit_ambient(
        _SVC_D + "component NewStore " + _PROV_DB, "OldStore/db/")
    assert v == ("G2|provision conflict: key `db` is provided by both "
                 "OldStore and NewStore (G2)")


def test_ambient_provision_conflict_names_the_realm(admit_ambient):
    src = ('service Kv { fn get(k: Str) -> Str } '
           'component StoreB provides kv: Kv { isolate kv in realm("tenant_a") '
           'provide kv { fn get(k) { return k } } }')
    v = admit_ambient(src, "StoreA/kv/tenant_a")
    assert v == ("G2|provision conflict: key `kv` in realm `tenant_a` is "
                 "provided by both StoreA and StoreB (G2)")


def test_ambient_provision_in_a_different_realm_composes(admit_ambient):
    src = ('service Kv { fn get(k: Str) -> Str } '
           'component StoreB provides kv: Kv { isolate kv in realm("tenant_b") '
           'provide kv { fn get(k) { return k } } }')
    assert admit_ambient(src, "StoreA/kv/tenant_a") == ""


def test_ambient_disjoint_manifest_key_does_not_conflict(admit_ambient):
    v = admit_ambient(_SVC_D + "component NewStore " + _PROV_DB,
                      "OldCache/cache/;OldBus/bus/")
    assert v == ""


def test_ambient_forwards_an_internally_refused_component(admit, admit_ambient):
    src = ('service Database { emission fn execute(sql: Str) -> Int } '
           'component P requires db: Database { effect db.execute("x") '
           'undo db.execute("y") }')
    # a manifest never launders an internally refused component
    assert admit_ambient(src, "Other/svc/") == admit(src)
    assert admit(src).startswith("G4|")


@pytest.mark.parametrize("realm", ["", "tenant_a"])
def test_ambient_equals_single_source_composition(admit, admit_ambient, realm):
    """The load-bearing differential: ambient admission of `X` against a
    manifest holding `M` equals single-source admission of `M ++ X`, and the
    reference agrees with that composed text. `realm=""` exercises the shared
    realm (conflict), a named realm the per-(key,realm) path."""
    iso = f' isolate db in realm("{realm}")' if realm else ""
    prov = ("provides db: D {" + iso +
            " provide db { fn q(s) { let x = s   return 0 } } }")
    old = "component OldStore " + prov
    new = "component NewStore " + prov
    manifest = f"OldStore/db/{realm}"

    ambient_src = _SVC_D + new
    composed = _SVC_D + old + " " + new

    got = admit_ambient(ambient_src, manifest)
    # leg 1: ambient == selfhost single-source of the composed text
    assert got == admit(composed)
    # leg 2: the composed text's G2 verdict is what the reference reports too
    ref_tag, _ = _ref(composed)
    assert ref_tag == "G2"
    assert got.startswith("G2|")


def test_ambient_manifest_wire_tolerates_trailing_and_empty_rows(admit_ambient):
    """A trailing or doubled ";" yields no phantom provision — the wire parser
    skips empty rows, so these all read as the single OldStore/db provision."""
    src = _SVC_D + "component NewStore " + _PROV_DB
    for manifest in ("OldStore/db/;", ";OldStore/db/", "OldStore/db/;;"):
        v = admit_ambient(src, manifest)
        assert v == ("G2|provision conflict: key `db` is provided by both "
                     "OldStore and NewStore (G2)"), manifest


# ------------------------------------------------------------ item 186 SLICE 2
#
# Multi-realm ROUTE validation (item 162) against AMBIENT realms. A `realms(...)`
# route in the incoming component targets realms whose providers live only in
# the RUNNING manifest, not in the incoming text. Before slice 2 the self-check
# refused such a route — its legs dangle in the text alone — and forwarded that
# refusal; a route refusal is composition-dependent, not internal, so slice 2
# recomputes the whole G2/ROUTE/G3 link seeded with the manifest's provisions.
#
# The oracle is the same M ++ X equivalence slice 1 uses: admitting a router `X`
# against a manifest holding the store components' provisions equals
# single-source admitting the store text ++ `X`, and the reference agrees:
#
#     admit_ambient(Router, manifest_of(StoreA, StoreB))
#         == admit_src(StoreA ++ StoreB ++ Router) == reference(...)

_ROUTE_SVCS = (
    "service Kv { fn get(k: Str) -> Str }\n"
    "service Api { fn go(k: Str) -> Str }\n"
)
_STORES = (
    'component StoreA provides kv: Kv {\n'
    '  isolate kv in realm("r1")\n'
    '  provide kv { fn get(k) { return k } }\n'
    '}\n'
    'component StoreB provides kv: Kv {\n'
    '  isolate kv in realm("r2")\n'
    '  provide kv { fn get(k) { return k } }\n'
    '}\n'
)
_ROUTER = (
    'component Router requires kv: Kv provides api: Api {\n'
    '  isolate kv in realms("r1", "r2") strategy(round_robin)\n'
    '  provide api { fn go(k) { return kv.get(k) } }\n'
    '}'
)


def test_ambient_route_realms_all_provided_by_manifest_admits(admit_ambient):
    """Router routes kv across r1+r2; NEITHER realm has a provider in the
    incoming text — both are held by the running manifest. The seeded link
    resolves each leg instead of forwarding a dangling-leg ROUTE refusal."""
    assert admit_ambient(_ROUTE_SVCS + _ROUTER, "StoreA/kv/r1;StoreB/kv/r2") == ""


def test_ambient_route_equals_single_source_and_reference(admit, admit_ambient):
    ambient_src = _ROUTE_SVCS + _ROUTER
    composed = _ROUTE_SVCS + _STORES + _ROUTER
    got = admit_ambient(ambient_src, "StoreA/kv/r1;StoreB/kv/r2")
    # leg 1: ambient == selfhost single-source of the composed text
    assert got == admit(composed) == ""
    # leg 2: the reference agrees the composed text admits
    assert _ref(composed) == ("", "")


def test_ambient_route_dangling_realm_still_refuses(admit, admit_ambient):
    """r1 is in the manifest, r9 is nowhere: the surviving leg still dangles, so
    the ROUTE refusal stands — byte-identical to the composed single source."""
    router = (
        'component Router requires kv: Kv provides api: Api {\n'
        '  isolate kv in realms("r1", "r9")\n'
        '  provide api { fn go(k) { return kv.get(k) } }\n'
        '}'
    )
    store_a = (
        'component StoreA provides kv: Kv {\n'
        '  isolate kv in realm("r1")\n'
        '  provide kv { fn get(k) { return k } }\n'
        '}\n'
    )
    ambient_src = _ROUTE_SVCS + router
    composed = _ROUTE_SVCS + store_a + router
    got = admit_ambient(ambient_src, "StoreA/kv/r1")
    assert got == (
        "ROUTE|multi-realm bind of `kv` in Router names realm `r9`, but no "
        "component provides `kv` in realm `r9` (item 162: every routed realm "
        "needs a provider)")
    assert got == admit(composed)
    ref_tag, _ = _ref(composed)
    assert ref_tag == "ROUTE"


def test_ambient_link_refusal_ordered_against_internal_refusal(admit,
                                                               admit_ambient):
    """Diagnostic ordering: an ambient G2 conflict at an EARLIER line outranks a
    LATER internal refusal. The old code forwarded the internal refusal and never
    computed the link; slice 2 merges the seeded link with the internal refusals
    and reports the minimum by (line, seq) — exactly admit_src(manifest ++ src)."""
    early = ("component Early provides db: D "
             "{ provide db { fn q(s) { let x = s   return 0 } } }")
    late = ("component Late provides ap: D "
            "{ provide ap { fn q(s) { let x = nope   return 0 } } }")
    src = "service D { fn q(s: Str) -> Int }\n" + early + "\n" + late
    composed = ("service D { fn q(s: Str) -> Int }\n"
                "component OldStore provides db: D "
                "{ provide db { fn q(s) { let x = s   return 0 } } }\n"
                + early + "\n" + late)
    got = admit_ambient(src, "OldStore/db/")
    assert got == ("G2|provision conflict: key `db` is provided by both "
                   "OldStore and Early (G2)")
    assert got == admit(composed)
    ref_tag, _ = _ref(composed)
    assert ref_tag == "G2"


# ------------------------------------------------ item 186 slice 3: G3 across
# the manifest, the `!halted` header, manifest_wire(ir), and oracle A extended
# to a cross-manifest dependency cycle.

# A minimal running composition M and an incoming X that closes a cycle THROUGH
# M: M's `A` requires `b` (provided by X's `B`) and X's `B` requires `a`
# (provided by M's `A`). The cycle A -> B -> A spans the manifest boundary.
_G3_SVC = "service A { fn pa() -> Int } service B { fn pb() -> Int } "
_G3_M = ("component A requires b: B provides a: A "
         "{ provide a { fn pa() { return 0 } } } ")
_G3_X = ("component B requires a: A provides b: B "
         "{ provide b { fn pb() { return 0 } } }")


def test_manifest_wire_projects_provisions_and_requirements():
    """manifest_wire(IR(M)) renders both the provision row (`A/a/`) and the
    requirement row (`A<b`) of the running component, so the wire carries the
    edges the G3 union graph needs."""
    from revl import manifest_wire
    ir = compile_source(_G3_SVC + _G3_M, "m.rvl")
    wire = manifest_wire(ir)
    rows = set(wire.split(";"))
    assert "A/a/" in rows, wire
    assert "A<b" in rows, wire


def test_ambient_g3_cycle_through_manifest_oracle_A(admit, admit_ambient):
    """Oracle A extended to G3: a cycle that closes through the running manifest
    is refused, byte-identical on admit_ambient(X, wire(M)), admit_src(M ++ X),
    and the reference compile of M ++ X."""
    from revl import manifest_wire
    ir = compile_source(_G3_SVC + _G3_M, "m.rvl")
    wire = manifest_wire(ir)
    composed = _G3_SVC + _G3_M + _G3_X
    got = admit_ambient(_G3_SVC + _G3_X, wire)
    assert got == "G3|dependency cycle: A -> B -> A (G3)", got
    assert got == admit(composed)
    ref_tag, ref_msg = _ref(composed)
    assert ref_tag == "G3"
    assert ref_msg == got.split("|", 1)[1]


def test_ambient_g3_requirement_row_is_load_bearing(admit_ambient):
    """Without the requirement row (`A<b`) — the pre-slice-3 provisions-only
    wire — the edge back into the manifest is invisible and the same admission
    WRONGLY admits. This pins that the requirement row closed the hole."""
    assert admit_ambient(_G3_SVC + _G3_X, "A/a/") == ""


def test_ambient_halted_header_refuses_every_incoming(admit_ambient):
    """A `!halted` manifest refuses admission of every X, naming the halt —
    even a clean component that would otherwise admit (item 443)."""
    clean = ("service D { fn q(s: Str) -> Int } component NewStore provides "
             "db: D { provide db { fn q(s) { let x = s   return 0 } } }")
    assert admit_ambient(clean, "!halted;A/a/") == (
        "HALTED|admission against a halted composition is refused (item 443)")
    # the base invariant is unmoved: the same clean component admits against an
    # empty (non-halted) manifest.
    assert admit_ambient(clean, "") == ""


def test_ambient_unknown_and_garbled_row_kinds_refuse(admit_ambient):
    """An unknown row kind refuses naming the row, and a row of a kind the gate
    DOES know that does not carry that kind's fields refuses as a garbled wire —
    a withdrawal row naming no bare component, a handoff row naming no component,
    key and state type. A wire the gate cannot read fails closed."""
    clean = ("service D { fn q(s: Str) -> Int } component NewStore provides "
             "db: D { provide db { fn q(s) { let x = s   return 0 } } }")
    assert admit_ambient(clean, "?A/a/") == (
        "MANIFEST|unrecognized manifest row `?A/a/`")
    for row in ("-", "-OldStore/db/", "-Old<db"):
        assert admit_ambient(clean, "OldStore/db/;" + row) == (
            "MANIFEST|manifest replacement row `" + row
            + "` does not name a component"), row
    for row in ("OldStore=db", "OldStore=db:", "OldStore=:D",
                "OldStore=d/b:D"):
        assert admit_ambient(clean, row) == (
            "MANIFEST|manifest handoff row `" + row
            + "` does not name a component, a key and its state type"), row
    # a row that does not even START with an identifier is not a handoff row
    # missing its component; it is a row of no kind at all, and says so.
    assert admit_ambient(clean, "=db:D") == (
        "MANIFEST|unrecognized manifest row `=db:D`")


# ---------------------------------------- item 186, the REPLACEMENT wave, part 1
#
# `-C` withdrawal rows, G2/ROUTE/G3 against `M \\ R`, the unmet-consumer refusal,
# and ORACLE B — the differential that builds the running manifest on BOTH
# sides, which is what the roadmap asked item 186 for and what slices 1-3 could
# not have: once a replacement is in play the `admit_ambient(X, wire(M)) ==
# admit_src(M ++ X)` equivalence of oracle A breaks by construction (a hot-swap
# OVERRIDES what the manifest holds, where the composed text CONFLICTS with it).
#
# Oracle B therefore derives both sides from ONE artifact: the reference
# compiles `M` to `IR(M)`; `revl.manifest_wire(ir, replacing=R)` renders `IR(M)`
# onto the gate's wire; and the first `TAG|message` of
# `compile_source(X, manifest=IR(M), replacing=R)` is compared byte-for-byte
# against `admit_ambient(X, wire)`.
#
# FAILURE DIRECTION of the one refusal this adds (`withdraws the running
# provider of ...`): fail-CLOSED. The running composition keeps running and the
# admission is what does not happen. Its scope is exactly the transition met ->
# unmet: a requirement already unmet before the admission stays admissible, a
# routed key is left to the item-162 per-realm check, and the match is
# per-(key, realm) so re-providing a withdrawn key in another realm does not
# satisfy a shared-realm consumer. The controls below pin all three, so the
# refusal cannot pass by refusing everything.

_W_SVCS = ("service D { fn q(s: Str) -> Int }\n"
           "service C { fn g(k: Str) -> Str }\n")
_W_DB = ('component Db provides db: D {\n'
         '  provide db { fn q(s) { let x = s   return 0 } }\n'
         '}\n')
_W_STORE = ('component Store requires db: D provides cache: C {\n'
            '  provide cache { fn g(k) { return k } }\n'
            '}\n')
#: The running composition M: one provider, one retained consumer of it. The
#: smallest shape in which a withdrawal can strand something.
_W_M = _W_SVCS + _W_DB + _W_STORE

#: `Db` redeclared WITHOUT its `db` provision — the implicit same-name
#: replacement `compile_files` performs, and the common hot-swap shape.
_W_X_DROPS = (_W_SVCS + 'component Db provides other: C {\n'
              '  provide other { fn g(k) { return k } }\n'
              '}')
#: The legitimate replacement: same name, key re-provided in the same realm.
_W_X_KEEPS = (_W_SVCS + 'component Db provides db: D {\n'
              '  provide db { fn q(s) { let x = s   return 1 } }\n'
              '}')

_W_LOST_DB = (
    "G2|this admission withdraws the running provider of `db` (`Db`) and "
    "nothing provides it again, but the running component `Store` still "
    "requires it (G2)")


def _ref_ambient(src: str, running_src: str,
                 replacing: tuple[str, ...] = ()) -> str:
    """ORACLE B, reference leg: the FIRST verdict of
    `compile_source(X, manifest=IR(M), replacing=R)`, on the gate's
    "<TAG>|<message>" wire ("" when the reference admits)."""
    ir = compile_source(running_src, "running.rvl")
    try:
        compile_source(src, "diff.rvl", manifest=ir, replacing=replacing)
        return ""
    except RevlErrors as error:
        first = error.errors[0]
        return _classify(first) + "|" + first.message
    except RevlError as error:
        return _classify(error) + "|" + error.message


def _gate_ambient(admit_ambient, src: str, running_src: str,
                  replacing: tuple[str, ...] = ()) -> str:
    """ORACLE B, gate leg: `admit_ambient(X, manifest_wire(IR(M), R))`.

    An empty `R` renders the plain slice-1-3 wire (no withdrawal row), which is
    what keeps the two controls below runnable against a gate that has no
    replacement support at all."""
    from revl import manifest_wire
    ir = compile_source(running_src, "running.rvl")
    wire = manifest_wire(ir, replacing=replacing) if replacing else manifest_wire(ir)
    return admit_ambient(src, wire)


def test_manifest_wire_renders_the_withdrawal_rows():
    """`replacing=R` renders one `-C` row per withdrawn component, after the
    composition rows it acts on — the wire's carrier for `compile_files`'
    `replacing=`, so `R` is a property of the admission CALL and never
    something the incoming text can assert about itself."""
    from revl import manifest_wire
    ir = compile_source(_W_M, "running.rvl")
    rows = manifest_wire(ir, replacing=("Db",)).split(";")
    assert rows[-1] == "-Db"
    assert "Db/db/" in rows and "Store<db" in rows
    # no `replacing` renders no withdrawal row: slices 1-3 are byte-identical
    assert manifest_wire(ir) == ";".join(rows[:-1])


def test_manifest_wire_realm_qualifies_a_requirement_row():
    """A running consumer that resolves its required key in a named realm
    renders `C<k/r`, so the gate's per-(key, realm) reasoning matches `_link`'s
    instead of assuming the shared realm."""
    from revl import manifest_wire
    src = (_W_SVCS
           + 'component Db provides db: D {\n'
             '  isolate db in realm("t1")\n'
             '  provide db { fn q(s) { let x = s   return 0 } }\n'
             '}\n'
             'component Store requires db: D provides cache: C {\n'
             '  isolate db in realm("t1")\n'
             '  provide cache { fn g(k) { return k } }\n'
             '}\n')
    rows = set(manifest_wire(compile_source(src, "running.rvl")).split(";"))
    assert "Db/db/t1" in rows, rows
    assert "Store<db/t1" in rows, rows


def test_oracle_b_implicit_replacement_that_strands_a_consumer(admit,
                                                               admit_ambient):
    """ORACLE B, the refusal. `Db` is redeclared without `db`; `Store` is
    RETAINED and still requires it. Both sides refuse with the same string.

    Fails on main in the ADMISSION direction: the gate saw a key leave the
    provider table and a retained consumer with no edge, so it had nothing to
    report and wrongly ADMITTED."""
    got = _gate_ambient(admit_ambient, _W_X_DROPS, _W_M)
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(_W_X_DROPS, _W_M)


def test_oracle_b_explicit_withdrawal_row_strands_a_consumer(admit_ambient):
    """The same loss reached through an explicit `-Db` row rather than a
    redeclaration: the incoming text mentions `Db` nowhere. Both sides refuse
    identically, and the gate anchors the refusal at line 0 because the
    withdrawn provider has no declaration in this text."""
    fresh = (_W_SVCS + 'component Fresh provides other: C {\n'
             '  provide other { fn g(k) { return k } }\n'
             '}')
    got = _gate_ambient(admit_ambient, fresh, _W_M, replacing=("Db",))
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(fresh, _W_M, replacing=("Db",))


def test_oracle_b_re_providing_in_another_realm_does_not_satisfy_the_consumer(
        admit_ambient):
    """The case a realm-blind check would have wrongly admitted: the
    replacement re-provides `db`, but isolated into realm `t1`, while `Store`
    resolves `db` in the SHARED realm. The key is still lost to that consumer,
    so both sides refuse."""
    realmed = (_W_SVCS + 'component Db provides db: D {\n'
               '  isolate db in realm("t1")\n'
               '  provide db { fn q(s) { let x = s   return 0 } }\n'
               '}')
    got = _gate_ambient(admit_ambient, realmed, _W_M)
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(realmed, _W_M)


def test_oracle_b_a_legitimate_replacement_still_admits(admit_ambient):
    """NON-VACUITY. The ordinary hot-swap — same name, same key, same realm —
    ADMITS on both sides. On main the gate refused it with a spurious G2
    provision conflict, because the link ran against `M` rather than `M \\ R`
    and the withdrawn provider was still in the seeded table."""
    got = _gate_ambient(admit_ambient, _W_X_KEEPS, _W_M)
    assert got == "", got
    assert got == _ref_ambient(_W_X_KEEPS, _W_M) == ""


def test_oracle_b_an_already_unmet_requirement_stays_admissible(admit_ambient):
    """NON-VACUITY, scope. `Store` requires `db` and NOTHING ever provided it;
    the admission withdraws `Aux`, which no one consumes. Only the transition
    met -> unmet is a refusal — an incremental composition legitimately admits
    a consumer before its provider — so both sides admit."""
    running = (_W_SVCS + _W_STORE
               + 'component Aux provides aux: C {\n'
                 '  provide aux { fn g(k) { return k } }\n'
                 '}\n')
    drops_aux = (_W_SVCS + 'component Aux provides other: C {\n'
                 '  provide other { fn g(k) { return k } }\n'
                 '}')
    got = _gate_ambient(admit_ambient, drops_aux, running)
    assert got == "", got
    assert got == _ref_ambient(drops_aux, running) == ""


def test_ambient_admission_without_a_withdrawal_is_unchanged(admit_ambient):
    """NON-VACUITY, the other side of the gate: with nothing withdrawn, a
    retained running consumer is no reason to refuse anything. Passes on main
    too — the withdrawal check is inert unless this admission drops something."""
    fresh = (_W_SVCS + 'component Fresh provides other: C {\n'
             '  provide other { fn g(k) { return k } }\n'
             '}')
    got = _gate_ambient(admit_ambient, fresh, _W_M)
    assert got == "", got
    assert got == _ref_ambient(fresh, _W_M) == ""


def test_withdrawing_the_consumer_itself_releases_the_key(admit_ambient):
    """The composition's own escape hatch, as the design note words it: "a
    composition that wants a key gone unloads the consumer first". With both
    `Db` and `Store` withdrawn there is no retained consumer left, so the same
    drop admits."""
    fresh = (_W_SVCS + 'component Fresh provides other: C {\n'
             '  provide other { fn g(k) { return k } }\n'
             '}')
    got = _gate_ambient(admit_ambient, fresh, _W_M, replacing=("Db", "Store"))
    assert got == "", got
    assert got == _ref_ambient(fresh, _W_M, replacing=("Db", "Store")) == ""


def test_withdrawal_row_for_a_component_that_is_not_running_withdraws_nothing(
        admit_ambient):
    """A `-C` naming a component the manifest does not hold is a no-op, exactly
    as `replacing=` is on the reference side: it matches no row, so nothing is
    lost and the admission is decided on its own merits."""
    fresh = (_W_SVCS + 'component Fresh provides other: C {\n'
             '  provide other { fn g(k) { return k } }\n'
             '}')
    got = _gate_ambient(admit_ambient, fresh, _W_M, replacing=("Nowhere",))
    assert got == "", got
    assert got == _ref_ambient(fresh, _W_M, replacing=("Nowhere",)) == ""


def test_a_replacement_no_longer_conflicts_with_what_it_withdraws(admit_ambient):
    """G2 runs against `M \\ R`. A key the admission WITHDRAWS is out of the
    seeded table, so taking it over under a new component name is not a
    conflict — which is what makes a replacement expressible at all. A key a
    RETAINED component provides is untouched, and re-providing it still
    conflicts, on both sides."""
    running = (_W_M + 'component Aux provides aux: C {\n'
               '  provide aux { fn g(k) { return k } }\n'
               '}\n')
    takeover = (_W_SVCS + 'component NewAux provides aux: C {\n'
                '  provide aux { fn g(k) { return k } }\n'
                '}')
    got = _gate_ambient(admit_ambient, takeover, running, replacing=("Aux",))
    assert got == "", got
    assert got == _ref_ambient(takeover, running, replacing=("Aux",)) == ""

    clash = (_W_SVCS + 'component NewCache provides cache: C {\n'
             '  provide cache { fn g(k) { return k } }\n'
             '}')
    got = _gate_ambient(admit_ambient, clash, running, replacing=("Aux",))
    assert got == ("G2|provision conflict: key `cache` is provided by both "
                   "Store and NewCache (G2)"), got
    assert got == _ref_ambient(clash, running, replacing=("Aux",))


def test_ambient_g3_cycle_survives_a_withdrawal_of_an_unrelated_component(
        admit_ambient):
    """The G3 union graph is rebuilt over `M \\ R`: a cycle that closes through
    a RETAINED manifest component is still seen when the admission withdraws a
    different one."""
    running = (_G3_SVC + _G3_M
               + 'component Spare provides s: B { provide s { fn pb() { return 0 } } }')
    got = _gate_ambient(admit_ambient, _G3_SVC + _G3_X, running,
                        replacing=("Spare",))
    assert got == "G3|dependency cycle: A -> B -> A (G3)", got


# ------------------------------------------- the service block (issue #346)
#
# `manifest_wire` grew a SERVICE BLOCK: a `!services` header asserting the list
# is exhaustive, then one `:S` row per service the running composition declares.
# The consumer is the rust crate's admission certifier, which needs the running
# NAMES to tell a fresh interface from a redeclaration. This fold has nothing to
# compute from them - a service declaration is no provision, no requirement, no
# graph node and no withdrawable component - so the property held here is that it
# changes NO verdict.
#
# It is held by ORACLE B rather than by a suite of its own: the block rides on
# every wire `manifest_wire` renders, so every oracle-B case above already runs
# against it, and the two below name that explicitly (one over the replacement
# wave's own rows, so the two additions are checked by one differential).


def test_manifest_wire_projects_the_service_block():
    """The projection renders the header and one row per declared service, in
    declaration order, between the composition rows and the withdrawal rows: a
    service declaration describes the composition, and a withdrawal acts on what
    precedes it, so `-C` stays last.

    Each row carries the service's OPERATION names after its name (T4b), which
    is what lets a candidate's call through a required key be resolved against
    the RUNNING declaration and not only against its name. The comma is the
    claim: `:A,pa` says the surface is exactly `pa`, and a bare `:A` — every
    wire a producer with no operation table renders — says nothing about it.

    Each operation name carries its own declared PARAMETER LIST behind it
    (issue #346), one level of the same claim down: `pa()` says `pa` takes no
    parameter, while a bare `pa` says nothing about its arguments. That is what
    lets the call be TYPED against the running declaration and not only
    resolved against it."""
    from revl import manifest_wire

    ir = compile_source(_G3_SVC + _G3_M, "m.rvl")
    assert manifest_wire(ir).endswith(";!services;:A,pa();:B,pb()"), manifest_wire(ir)
    rows = manifest_wire(ir, replacing=("A",)).split(";")
    assert rows[-1] == "-A"
    assert rows[-4:-1] == ["!services", ":A,pa()", ":B,pb()"]
    # the composition rows keep their exact positions and order, so the G3 DFS
    # seed order cannot have moved
    assert rows[:rows.index("!services")] == manifest_wire(ir).split(
        ";")[:rows.index("!services")]


# ---- docs/design/457 T4b: the requirement resolved against the RUNNING service

#: The issue-346 harness scenario verbatim (bench/admission_latency.py): a
#: running `Store` provided by `Kv` and consumed by `App`. The candidates below
#: are the two questions a drafting agent asks about it.
_T4B_RUNNING = """
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
service AppSvc { fn ping() -> Str }

component Kv provides store: Store {
  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(key) = key
    fn bump(n) = n
    fn put(key, value) = value
  }
}
component App requires store: Store provides app: AppSvc {
  provide app { fn ping() = store.get("boot") }
}
"""

#: Calls an operation the running `Store` declares: the reference admits it
#: INTO the composition, and refuses it standalone for the service's absence.
_T4B_CANDIDATE = """
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"""

#: The same shape calling an operation the running `Store` does NOT declare.
#: Only a reader that resolved the requirement against the running declaration
#: can tell the two apart.
_T4B_MISSING = """
service Cache { fn lookup(key: Str) -> Str }
component CacheMiss requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.nonexistent(key) }
}
"""


def test_a_required_running_service_resolves_its_operations(admit_ambient):
    """ORACLE B over the A6 member rule: a candidate whose `requires store:
    Store` is satisfied by the RUNNING composition is checked against the
    running service's declared operations, and agrees with the reference on
    both answers — admitted for an operation `Store` declares, refused in the
    reference's own words for one it does not.

    This is the half `test_the_manifest_gap_is_priced_not_hidden` calls
    requirement RESOLUTION: before it, the wire carried service NAMES and the
    gate could say only that `Store` exists."""
    assert _gate_ambient(admit_ambient, _T4B_CANDIDATE, _T4B_RUNNING) == ""
    assert _ref_ambient(_T4B_CANDIDATE, _T4B_RUNNING) == ""

    got = _gate_ambient(admit_ambient, _T4B_MISSING, _T4B_RUNNING)
    assert got == "A6|`store.nonexistent` is not a method of service Store", got
    assert got == _ref_ambient(_T4B_MISSING, _T4B_RUNNING)


def test_a_wire_that_makes_no_operation_claim_decides_no_member(admit_ambient):
    """The frontier of the same rule, pinned rather than left to be discovered.

    A `:S` row with no comma names a running service and says NOTHING about its
    surface — that is every wire a producer without an operation table renders.
    Reading it as the EMPTY surface would refuse every call through that
    requirement, which is the false-alarm direction this gate may not err in, so
    it decides no member at all. The NAME still resolves, so the rule the
    previous slice landed is untouched."""
    named_only = "Kv/store/;App/app/;App<store;!services;:Store;:AppSvc"
    assert admit_ambient(_T4B_MISSING, named_only) == ""
    assert admit_ambient(_T4B_CANDIDATE, named_only) == ""
    # ... and the SAME wire carrying the claim refuses, so the silence is what
    # is doing the work rather than the program being harmless
    claimed = "Kv/store/;App/app/;App<store;!services;:Store,get,bump,put;:AppSvc,ping"
    assert admit_ambient(_T4B_MISSING, claimed) == (
        "A6|`store.nonexistent` is not a method of service Store")
    assert admit_ambient(_T4B_CANDIDATE, claimed) == ""


def test_the_empty_operation_claim_is_a_claim(admit_ambient):
    """`:S,` is the running service that declares NO operation — a claim, and a
    different one from `:S`. Every call through a requirement bound to it is
    refused, which is what makes the trailing comma load-bearing rather than
    cosmetic."""
    empty_claim = "Kv/store/;App/app/;App<store;!services;:Store,;:AppSvc,ping"
    assert admit_ambient(_T4B_CANDIDATE, empty_claim) == (
        "A6|`store.get` is not a method of service Store")


@pytest.mark.parametrize("row", [":Store,get,", ":Store,,get", ":Store,ge t",
                                 ":Store,get/put"])
def test_a_garbled_operation_list_refuses_the_wire(admit_ambient, row):
    """A malformed operation list fails the WIRE by name. Reading it as a
    shorter surface would refuse calls the reference admits, and skipping it
    would leave a claim half-read: a wire the gate cannot read decides
    nothing at all."""
    got = admit_ambient(_T4B_CANDIDATE, f"Kv/store/;!services;{row}")
    assert got.startswith("MANIFEST|"), got
    assert "does not name an operation" in got or "does not name a service" in got


def test_manifest_wire_makes_no_service_claim_for_a_manifest_dict():
    """A manifest DICT carries components and no service table, and an absent
    table is not an empty one. The projection emits no block for it, so a reader
    is told nothing rather than told the composition declares nothing."""
    from revl import manifest_wire

    ir = compile_source(_G3_SVC + _G3_M, "m.rvl")
    assert "!services" not in manifest_wire(ir["manifest"])


def test_oracle_b_the_service_block_moves_no_verdict(admit, admit_ambient):
    """ORACLE B over the block: both legs derived from one artifact, with the
    block on the wire the projection renders. The cross-manifest G3 cycle is
    still named identically by the gate, by the single-source composition, and by
    the reference."""
    from revl import manifest_wire

    ir = compile_source(_G3_SVC + _G3_M, "m.rvl")
    wire = manifest_wire(ir)
    assert "!services" in wire.split(";"), wire
    got = admit_ambient(_G3_SVC + _G3_X, wire)
    assert got == "G3|dependency cycle: A -> B -> A (G3)", got
    assert got == admit(_G3_SVC + _G3_M + _G3_X)
    assert got == _ref_ambient(_G3_SVC + _G3_X, _G3_SVC + _G3_M)


def test_oracle_b_the_service_block_beside_a_withdrawal(admit_ambient):
    """ORACLE B where the two item-186 additions MEET: a wire carrying both the
    replacement wave's withdrawal row and the service block. The unmet-consumer
    refusal is unmoved, in the reference's own words, and the block is not what
    decides it."""
    fresh = _W_SVCS + ('component Fresh provides other: C {\n'
                       '  provide other { fn g(k) { return k } }\n'
                       '}')
    got = _gate_ambient(admit_ambient, fresh, _W_M, replacing=("Db",))
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(fresh, _W_M, replacing=("Db",))
    # and the same wire with the block STRIPPED answers identically, which is
    # what makes "the block decides nothing here" a measurement
    from revl import manifest_wire

    ir = compile_source(_W_M, "running.rvl")
    wire = manifest_wire(ir, replacing=("Db",))
    stripped = ";".join(r for r in wire.split(";")
                        if r != "!services" and not r.startswith(":"))
    assert stripped != wire
    assert admit_ambient(fresh, stripped) == got


def test_ambient_malformed_service_row_refuses_naming_the_row(admit_ambient):
    """A row whose marker is recognized and whose name is not still refuses BY
    NAME - the block is parsed, not skipped - and it is held to the same bare
    identifier rule the wave's withdrawal name is."""
    clean = ("service D { fn q(s: Str) -> Int } component NewStore provides "
             "db: D { provide db { fn q(s) { let x = s   return 0 } } }")
    assert admit_ambient(clean, ":") == (
        "MANIFEST|manifest service row `:` does not name a service")
    assert admit_ambient(clean, ":9bad") == (
        "MANIFEST|manifest service row `:9bad` does not name a service")
    assert admit_ambient(clean, ":A/b") == (
        "MANIFEST|manifest service row `:A/b` does not name a service")
    assert admit_ambient(clean, "!service") == (
        "MANIFEST|unrecognized manifest header row `!service`")
# ---------------------------------------------- item 186 / issue #1036: ROUTE
# ROWS, and the per-realm loss of a ROUTED RUNNING consumer.
#
# Wave part 1 left one live reference/gate divergence in the FALSE-ADMIT
# direction, and named it rather than reporting it under a tag that does not
# describe it. Route rows were absent from the wire, so when a routed running
# consumer's realm lost its provider the reference refused it (item 162's
# link-time per-realm check, which walks the ambient entries too) and the gate
# ADMITTED: the running consumer stranded, with nothing said anywhere.
#
# The wire grows one row kind, `C>k/r1,r2` — the realms a running component
# binds a key across, in declaration order. The requirement row's `*` marker
# said a key was routed; this says WHERE, which is the half the per-realm check
# needs. `admit_ambient` then runs that check over the running composition's
# routes, under ITEM 162's OWN TAG AND MESSAGE: the withdrawal check answers
# "this key became unmet", this one answers "this realm has no provider", and
# reporting the second under the first's name would teach the reader the wrong
# rule.
#
# FAILURE DIRECTION: fail-CLOSED. Every refusal here is an admission that does
# not happen; the running composition keeps running, its routed consumer still
# bound to the providers it has. The scope is exactly the reference's — a realm
# named by a route with no provider in the resulting per-(key, realm) table —
# and the two non-vacuity controls below pin that a legitimate routed
# replacement and an untouched routed key both still admit, on BOTH sides.

_R_SVCS = ("service Kv { fn get(k: Str) -> Str }\n"
           "service Api { fn go(k: Str) -> Str }\n")
_R_STORE_A = ('component StoreA provides kv: Kv {\n'
              '  isolate kv in realm("r1")\n'
              '  provide kv { fn get(k) { return k } }\n'
              '}\n')
_R_STORE_B = ('component StoreB provides kv: Kv {\n'
              '  isolate kv in realm("r2")\n'
              '  provide kv { fn get(k) { return k } }\n'
              '}\n')
_R_ROUTER = ('component Router requires kv: Kv provides api: Api {\n'
             '  isolate kv in realms("r1", "r2") strategy(round_robin)\n'
             '  provide api { fn go(k) { return kv.get(k) } }\n'
             '}\n')
#: The running composition: two per-realm providers and a router bound across
#: both. The smallest shape in which a realm can lose its provider.
_R_M = _R_SVCS + _R_STORE_A + _R_STORE_B + _R_ROUTER

#: `StoreB` redeclared WITHOUT its `kv` provision: realm `r2` loses its only
#: provider while `Router` keeps routing across it.
_R_X_DROPS = (_R_SVCS + 'component StoreB provides other: Api {\n'
              '  provide other { fn go(k) { return k } }\n'
              '}')
#: The legitimate routed replacement: same name, same key, same realm.
_R_X_KEEPS = (_R_SVCS + 'component StoreB provides kv: Kv {\n'
              '  isolate kv in realm("r2")\n'
              '  provide kv { fn get(k) { return k } }\n'
              '}')
#: An unrelated arrival: nothing the route depends on moves.
_R_X_FRESH = (_R_SVCS + 'component Fresh provides other: Api {\n'
              '  provide other { fn go(k) { return k } }\n'
              '}')

_R_LOST_R2 = (
    "ROUTE|multi-realm bind of `kv` in Router names realm `r2`, but no "
    "component provides `kv` in realm `r2` (item 162: every routed realm "
    "needs a provider)")


def test_manifest_wire_renders_the_route_rows():
    """`manifest_wire` renders a running component's multi-realm bind as
    `C>k/r1,r2`, in the realm order the route declared — the order the per-realm
    check walks, so which realm a refusal names first is the reference's choice,
    not an accident.

    A route row is a COMPOSITION row: it says what one running component binds
    across which realms, so it sits WITH its component, after that component's
    requirement rows and ahead of the service block, which describes the whole
    composition. The withdrawal rows stay last, because a withdrawal acts on
    everything that precedes it."""
    from revl import manifest_wire
    ir = compile_source(_R_M, "running.rvl")
    rows = manifest_wire(ir).split(";")
    assert "Router>kv/r1,r2" in rows, rows
    # the routed marker stays: it is what keeps the routed key out of the
    # single-realm table and out of the withdrawal check
    assert rows.index("Router<*kv") < rows.index("Router>kv/r1,r2"), rows
    # ... and the whole ordering of the combined wire, in one line
    assert rows == ["StoreA/kv/r1", "StoreB/kv/r2", "Router/api/", "Router<*kv",
                    "Router>kv/r1,r2", "!services", ":Kv,get(k:Str)",
                    ":Api,go(k:Str)"], rows
    # the withdrawal row stays last, after the service block
    assert manifest_wire(ir, replacing=("StoreB",)).split(";")[-1] == "-StoreB"
    # a composition with no route renders no route row (the earlier slices are
    # byte-identical)
    assert ">" not in manifest_wire(compile_source(_W_M, "running.rvl"))


def test_a_wire_carrying_both_a_service_block_and_a_route_row(admit_ambient):
    """The two row kinds that landed together, on one wire. The service block is
    inert for the fold and the route row is not, so the interaction to pin is
    that neither disturbs the other: the block moves no verdict, the route legs
    still refuse the realm that lost its provider, and a `:S` row sitting after
    the route rows does not swallow them."""
    from revl import manifest_wire
    wire = manifest_wire(compile_source(_R_M, "running.rvl"))
    assert "!services" in wire.split(";") and "Router>kv/r1,r2" in wire.split(";")
    assert admit_ambient(_R_X_DROPS, wire) == _R_LOST_R2
    assert admit_ambient(_R_X_DROPS, wire) == _ref_ambient(_R_X_DROPS, _R_M)
    # the block really is inert here: stripping it changes no verdict, in both
    # the refusing and the admitting direction
    blockless = ";".join(r for r in wire.split(";")
                         if r != "!services" and not r.startswith(":"))
    assert admit_ambient(_R_X_DROPS, blockless) == _R_LOST_R2
    assert admit_ambient(_R_X_KEEPS, wire) == admit_ambient(_R_X_KEEPS, blockless) == ""
    # and the combined wire is nowhere near the fold's row bound
    assert len(wire.split(";")) < 512


def test_oracle_b_a_routed_running_consumer_loses_a_realms_provider(
        admit_ambient):
    """ORACLE B, the refusal this closes. `StoreB` is redeclared without `kv`,
    so realm `r2` has no provider while the RUNNING `Router` still routes
    across it. Both sides refuse with the same string, under item 162's tag.

    Fails on main in the ADMISSION direction: the wire carried no route legs,
    so the gate could not ask which realms `Router` routed and admitted."""
    got = _gate_ambient(admit_ambient, _R_X_DROPS, _R_M)
    assert got == _R_LOST_R2, got
    assert got == _ref_ambient(_R_X_DROPS, _R_M)


def test_oracle_b_routed_realm_loss_through_an_explicit_withdrawal_row(
        admit_ambient):
    """The same loss reached through an explicit `-StoreB` row: the incoming
    text mentions `StoreB` nowhere. Both sides refuse identically."""
    got = _gate_ambient(admit_ambient, _R_X_FRESH, _R_M, replacing=("StoreB",))
    assert got == _R_LOST_R2, got
    assert got == _ref_ambient(_R_X_FRESH, _R_M, replacing=("StoreB",))


def test_the_route_row_is_what_closes_the_routed_realm_loss(admit_ambient):
    """The row is LOAD-BEARING. The SAME admission against the pre-#1036 wire —
    routed marker, no legs — is invisible to the gate and admits, while the
    reference refuses it. This is the divergence, pinned to the row that
    removes it, so a projection that stopped rendering route rows would red
    here rather than quietly reopening the false admit."""
    from revl import manifest_wire
    ir = compile_source(_R_M, "running.rvl")
    legless = ";".join(row for row in manifest_wire(ir).split(";")
                       if ">" not in row)
    assert admit_ambient(_R_X_DROPS, legless) == ""
    assert _ref_ambient(_R_X_DROPS, _R_M) == _R_LOST_R2


def test_oracle_b_a_legitimate_routed_replacement_still_admits(admit_ambient):
    """NON-VACUITY. The ordinary routed hot-swap — same name, same key, same
    realm — ADMITS on both sides. The check refuses a realm with no provider,
    not a realm whose provider changed hands."""
    got = _gate_ambient(admit_ambient, _R_X_KEEPS, _R_M)
    assert got == "", got
    assert got == _ref_ambient(_R_X_KEEPS, _R_M) == ""


def test_oracle_b_a_routed_key_that_stays_provided_is_untouched(admit_ambient):
    """NON-VACUITY, the other side. Nothing the route depends on is withdrawn —
    an unrelated component arrives — so the routed running consumer is not
    reasoned about at all and both sides admit. Passes on main too."""
    got = _gate_ambient(admit_ambient, _R_X_FRESH, _R_M)
    assert got == "", got
    assert got == _ref_ambient(_R_X_FRESH, _R_M) == ""


def test_oracle_b_a_routed_running_consumer_still_carries_its_g3_edges(
        admit_ambient):
    """The legs are EDGES as well as an existence check: the reference builds
    one provider -> consumer edge per routed realm for an ambient entry too. A
    cycle that closes through a ROUTED running consumer is therefore seen,
    where before the route row the wire contributed no edge at all and the
    cycle was missed."""
    cyclic = (_R_SVCS + 'component StoreB requires api: Api provides kv: Kv {\n'
              '  isolate kv in realm("r2")\n'
              '  provide kv { fn get(k) { return api.go(k) } }\n'
              '}')
    got = _gate_ambient(admit_ambient, cyclic, _R_M)
    assert got == "G3|dependency cycle: Router -> StoreB -> Router (G3)", got
    assert got == _ref_ambient(cyclic, _R_M)


def test_a_malformed_route_row_refuses_rather_than_dropping_the_legs(
        admit_ambient):
    """A route row the gate cannot read is a garbled wire and refuses by name,
    exactly as a garbled withdrawal row does. Fail-closed: a row that parses
    into a route with no legs is the blindness this row kind removes."""
    clean = (_W_SVCS + 'component Fresh provides other: C {\n'
             '  provide other { fn g(k) { return k } }\n'
             '}')
    assert admit_ambient(clean, "Router/api/;Router>kv") == (
        "MANIFEST|manifest route row `Router>kv` does not name a component, "
        "a key and its realms")
    assert admit_ambient(clean, "Router/api/;>kv/r1") == (
        "MANIFEST|unrecognized manifest row `>kv/r1`")


# ------------------------------- item 186, the REPLACEMENT wave, part 3 (419c)
#
# REFUSAL ORDERING when an ambient admission produces SEVERAL true refusals.
# Item 419c's single-source half landed with the collecting sink: `admit_src`
# reports the minimum by `(line, seq)`, the reference's `diagnostics[0]`. The
# ambient half is the same question with one more producer in the sink — the
# unmet-consumer WITHDRAWAL, which is neither an internal component refusal nor
# a link refusal — and `seq` is what decides a LINE TIE.
#
# The reference's phase order (`check_and_lower`) is:
#
#     component loop -> _admit_provision_withdrawal -> _check_spawn_emission_bounds
#                    -> _check_spawn_attenuation -> _link (BOOT, G2, ROUTE, G3)
#
# The gate appended the withdrawal verdicts AFTER its whole non-link sink, which
# put them behind the spawn bounds and the BOOT count. On distinct lines that is
# unobservable (the line decides), so the part-1 corpus — one refusal per
# program — could not see it. On a TIE it flips the winner: measured over 660
# generated multi-refusal admissions, 12 diverged, in exactly two families
# (a withdrawal tying with a G4 spawn-emission bound, and a withdrawal tying
# with the BOOT count). `collect_nonlink` now takes the ambient verdicts and
# splices them at the reference's own phase position, so `seq` agrees too.
#
# A tie is not only the `_oneline` degenerate case: two components written on
# ONE source line tie as well, which is the `glued` layout below.
#
# FAILURE DIRECTION: unchanged, fail-CLOSED. Nothing here changes WHICH
# admissions are refused — every program in this corpus is refused by both sides
# before and after — only WHICH of its true refusals is named. The standard
# #1034 set is the one being kept: a refusal must carry the tag that describes
# the finding, so a withdrawal must not be reported under a spawn bound's name
# (or the reverse) merely because the two landed on one line.

_MO_SVCS = (_W_SVCS
            + "service Bus { emission fn publish(topic: Str) }\n"
            + "service Kv2 { emission[kv2] fn write(row: Str) -> Int }\n"
            + "service Task { emission[kv2] fn go() -> Int }\n"
            + "service Sup { fn run() -> Int }\n"
            + "service Env { fn a() -> Str }\n"
            + "service Env2 { fn b() -> Str }\n")

#: the implicit withdrawal: `Db` redeclared without `db`, stranding `Store`.
_MO_WITHDRAW = ('component Db provides other: C '
                '{ provide other { fn g(k) { return k } } }')
#: a G4/G6 spawn-emission bound (`_check_spawn_emission_bounds`) — the reference
#: collects this AFTER the withdrawal.
_MO_SPAWN = (
    'component Worker requires kv2: Kv2 provides task: Task '
    '{ provide task { fn go() { emit kv2.write("x")  return 0 } } }\n'
    'component Supervisor provides sup: Sup '
    '{ provide sup { fn run() { let w = effect spawn Worker with { } '
    'undo w.dispose()  return 0 } } }')
#: the BOOT count (item 350), decided at the head of `_link` — also after the
#: withdrawal on the reference.
_MO_BOOT = ('boot component B1 provides e1: Env '
            '{ config { x: Str } provide e1 { fn a() = config.x } }\n'
            'boot component B2 provides e2: Env2 '
            '{ config { y: Str } provide e2 { fn b() = config.y } }')
#: an ordinary component-loop G4 — collected BEFORE the withdrawal on both
#: sides, so this family never moved.
_MO_G4 = ('component Zed requires bus: Bus '
          '{ effect bus.publish("x") undo bus.publish("y") }')

_MO_SPAWN_BOUND = ("G4|`Sup.run` is declared plain, but it spawns `Worker`, "
                   "which emits through `kv2`")
_MO_TWO_BOOTS = ("BOOT|a composition declares at most one `boot` component, "
                 "found B1, B2")


def _mo_src(fragments: list[str], layout: str) -> str:
    """One admission text from the fragments, in one of three LINE LAYOUTS.

    `separate` gives every component its own line (no tie); `glued` puts the
    component fragments on ONE source line while the service preamble keeps its
    own (the realistic tie); `oneline` collapses the whole program (every line
    is 1, the `_oneline` degenerate tie the single-source corpus already uses).
    """
    body = "\n".join(fragments)
    if layout == "separate":
        return _MO_SVCS + body + "\n"
    if layout == "glued":
        return _MO_SVCS + _oneline(body) + "\n"
    return _oneline(_MO_SVCS + body)


_MO_PAIRS = [
    ("withdrawal then spawn bound", [_MO_WITHDRAW, _MO_SPAWN]),
    ("spawn bound then withdrawal", [_MO_SPAWN, _MO_WITHDRAW]),
    ("withdrawal then two boots", [_MO_WITHDRAW, _MO_BOOT]),
    ("two boots then withdrawal", [_MO_BOOT, _MO_WITHDRAW]),
    ("withdrawal then a component G4", [_MO_WITHDRAW, _MO_G4]),
    ("a component G4 then withdrawal", [_MO_G4, _MO_WITHDRAW]),
    ("withdrawal, spawn bound and two boots",
     [_MO_WITHDRAW, _MO_SPAWN, _MO_BOOT]),
    ("two boots, a component G4 and a withdrawal",
     [_MO_BOOT, _MO_G4, _MO_WITHDRAW]),
]


@pytest.mark.parametrize("layout", ["separate", "glued", "oneline"])
@pytest.mark.parametrize("name,fragments", _MO_PAIRS,
                         ids=[n for n, _ in _MO_PAIRS])
def test_oracle_b_multi_refusal_ordering_agrees(admit_ambient, name,
                                                fragments, layout):
    """ORACLE B over MULTI-REFUSAL admissions (item 419c, the ambient half).

    Every program here carries a withdrawal refusal AND at least one internal
    one, all true of the admission. The reference's `diagnostics[0]` and
    `admit_ambient`'s `pick_min` must name the same one, byte for byte, in all
    three line layouts.

    Fails on main in the `glued` and `oneline` layouts of the spawn-bound and
    two-boot rows: the tie fell to the internal refusal on the gate and to the
    withdrawal on the reference."""
    src = _mo_src(fragments, layout)
    ref = _ref_ambient(src, _W_M)
    assert ref, f"the corpus must refuse: {name}/{layout}"
    got = _gate_ambient(admit_ambient, src, _W_M)
    assert got == ref, (name, layout, got, ref)


# ---- a CLOSED ordering divergence: the handoff verdict vs a body refusal ----
#
# `_admit_handoff_replacement` runs over `live_components`, which
# `src/revl/lower.py` builds by DROPPING every component whose body lowering
# raised (it appends a `poisoned` header stub instead). So on the reference a
# component whose body refuses contributes NO handoff verdict at all — the body
# refusal is the whole answer, whatever line either sits on.
#
# `selfhost/lower.rvl`'s `handoff_refusals` used to walk every component in the
# text, poisoned or not, with its verdict anchored at the COMPONENT declaration
# line. `pick_min` orders by `(line, seq)`, so it beat any INLINE body refusal,
# which `body_line` anchors at the offending STATEMENT — a strictly later line.
#
# This predates the A6 member rule and was not caused by it: the reproducer below
# uses the G1 undeclared-access refusal, which has been inline-anchored since
# long before docs/design/457. A whole-component AGGREGATE verdict (the G4
# emission-reach one) is anchored at the component line, ties, and is saved by
# `seq` — which is why no corpus program caught this.
#
# Both refusals are TRUE of the program, so this was a 419c naming divergence and
# never a false admission. The handoff slice (issue #1127) threaded the poisoned
# set through `collect_nonlink`, so the pin below is now an AGREEMENT, kept as
# the regression witness this comment asked the closing slice to leave behind.

_HO_RUNNING = """service D { fn q(s: Str) -> Int }
service Extra { fn e(s: Str) -> Str }
component OldStore provides db: D {
  handoff db: Str
  provide db { fn q(s) { let x = s   return 0 } }
}
"""

#: (label, source) — each carries a handoff drift AND one body refusal.
_HO_PAIRS = [
    # the PRE-A6 member: an undeclared access, inline-anchored for many slices.
    ("a G1 undeclared access", """service D { fn q(s: Str) -> Int }
component NewStore provides db: D {
  handoff db: Int
  provide db { fn q(s) { emit nope.execute(s)   return 0 } }
}
"""),
    # the A6 member of the same family (docs/design/457 T4b), which is how this
    # divergence was found.
    ("an A6 member refusal", """service D { fn q(s: Str) -> Int }
service Extra { fn e(s: Str) -> Str }
component NewStore provides db: D requires ex: Extra {
  handoff db: Int
  provide db { fn q(s) { let y = ex.nope(s)   return 0 } }
}
"""),
]


@pytest.mark.parametrize("label,src", _HO_PAIRS, ids=[n for n, _ in _HO_PAIRS])
def test_a_handoff_drift_outranks_an_inline_body_refusal_on_the_gate(
        admit_ambient, label, src):
    """The former divergence, measured in both directions so it cannot return.

    The reference names the BODY refusal, because the component never reaches
    the handoff pass. The gate named the handoff drift until the poisoned set
    was threaded through `collect_nonlink`; it now names the body refusal too."""
    ref = _ref_ambient(src, _HO_RUNNING, replacing=("OldStore",))
    got = _gate_ambient(admit_ambient, src, _HO_RUNNING, replacing=("OldStore",))
    assert ref.startswith(("G1|", "A6|")), (
        f"the reference must name the body refusal for {label}: {ref!r}")
    assert got == ref, (label, got, ref)
    # NON-VACUITY: with the handoff made compatible, the two agree on the body
    # refusal, so the divergence is the handoff verdict's ranking and nothing
    # else about these programs.
    compatible = src.replace("handoff db: Int", "handoff db: Str")
    assert _gate_ambient(admit_ambient, compatible, _HO_RUNNING,
                         replacing=("OldStore",)) == _ref_ambient(
        compatible, _HO_RUNNING, replacing=("OldStore",))


def test_oracle_b_a_withdrawal_tying_with_a_spawn_bound_names_the_withdrawal(
        admit_ambient):
    """The first closed family, with its bytes written out. The withdrawal and
    the spawn-emission bound land on ONE source line, so `seq` decides, and the
    reference collects `_admit_provision_withdrawal` ahead of
    `_check_spawn_emission_bounds`.

    Main answers `G4|...spawns `Worker`, which emits through `kv2`` here — a
    true refusal under the wrong name for what the gate was asked: the
    admission's effect on the RUNNING composition."""
    src = _mo_src([_MO_WITHDRAW, _MO_SPAWN], "glued")
    got = _gate_ambient(admit_ambient, src, _W_M)
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(src, _W_M)
    # ... and the spawn bound is genuinely there to lose the tie: on its own it
    # is what both sides report.
    alone = _mo_src([_MO_SPAWN], "glued")
    assert _gate_ambient(admit_ambient, alone, _W_M) == _MO_SPAWN_BOUND
    assert _ref_ambient(alone, _W_M) == _MO_SPAWN_BOUND


def test_oracle_b_a_withdrawal_tying_with_the_boot_count_names_the_withdrawal(
        admit_ambient):
    """The second closed family. The BOOT count is decided at the head of
    `_link`, which the reference runs after the withdrawal; the gate collected
    it in `collect_nonlink`, ahead of one. Main answers `BOOT|...found B1, B2`
    on the tie."""
    src = _mo_src([_MO_WITHDRAW, _MO_BOOT], "glued")
    got = _gate_ambient(admit_ambient, src, _W_M)
    assert got == _W_LOST_DB, got
    assert got == _ref_ambient(src, _W_M)
    alone = _mo_src([_MO_BOOT], "glued")
    assert _gate_ambient(admit_ambient, alone, _W_M) == _MO_TWO_BOOTS
    assert _ref_ambient(alone, _W_M) == _MO_TWO_BOOTS


def test_the_withdrawal_does_not_simply_win_every_ambient_admission(
        admit_ambient):
    """NON-VACUITY, and the control that separates "ordered correctly" from
    "moved to the front". Passes on main AND on the branch.

    LINE still decides: with the fragments on their own lines the earlier one
    wins whichever it is. A spawn bound declared ABOVE the withdrawal is
    reported; the same pair with the withdrawal above reports the withdrawal.
    Only the tie changed."""
    spawn_first = _mo_src([_MO_SPAWN, _MO_WITHDRAW], "separate")
    assert _gate_ambient(admit_ambient, spawn_first, _W_M) == _MO_SPAWN_BOUND
    assert _ref_ambient(spawn_first, _W_M) == _MO_SPAWN_BOUND

    withdraw_first = _mo_src([_MO_WITHDRAW, _MO_SPAWN], "separate")
    assert _gate_ambient(admit_ambient, withdraw_first, _W_M) == _W_LOST_DB
    assert _ref_ambient(withdraw_first, _W_M) == _W_LOST_DB


def test_an_earlier_component_refusal_still_outranks_a_tying_withdrawal(
        admit_ambient):
    """NON-VACUITY, the other direction. The COMPONENT LOOP runs ahead of the
    withdrawal on both sides, so a component's own G4 wins a tie with it — the
    withdrawal did not move to the head of the sink, it moved to its own place
    in it. Passes on main AND on the branch."""
    src = _mo_src([_MO_WITHDRAW, _MO_G4], "glued")
    expected = "G4|call to emission `bus.publish` must be marked `emit` (G4)"
    assert _gate_ambient(admit_ambient, src, _W_M) == expected
    assert _ref_ambient(src, _W_M) == expected


def test_the_single_source_sink_is_unchanged_by_the_ambient_splice(admit):
    """The splice is inert for `admit_src`: it passes an EMPTY ambient list, so
    every single-source program — the whole `_MULTI_REFUSAL_PROGRAMS` corpus
    included — reports exactly what it reported before. Passes on main too."""
    for _, src in _MULTI_REFUSAL_PROGRAMS:
        for text in (src, _oneline(src)):
            ref_tag, ref_msg = _ref(text)
            assert admit(text) == ref_tag + "|" + ref_msg


# ---------------------------------------- item 186, the REPLACEMENT wave, part 2
#
# `handoff` STATE compatibility (item 53), the last surface the wave held back.
# A stateful provider declares the shape of the live state it EXPORTS when it is
# replaced (`handoff <key>: <Type>`); its replacement declares the shape it
# ACCEPTS. The value flows predecessor -> successor, so the accepted shape must
# admit everything the exported one produces — the same covariant §5 relation an
# interface return position uses, pointed at state. A swap whose successor
# cannot hold the predecessor's state is REFUSED rather than admitted into a
# runtime that silently drops it.
#
# The wire grows one kind, `C=k:T`, rendered by `manifest_wire` off the whole IR
# document's `components` (whose `handoff` survives lowering) — the same place
# `compiler._running_handoffs` reads, so the gate sees exactly the ambient table
# the reference's own check sees.
#
# Why it needed no expression type layer, which is what deferred it: both shapes
# reach the two gates as declared SPELLINGS, and `_handoff_compatible` calls
# `compatible(accepted, exported)` with NO declared-type table. So the port is
# the type-STRING algebra alone, and the structural-record branch that would
# need a table is unreachable — `handoff st: {a: Int}` is a parse error on the
# reference (`Parser.type_` refuses `{` at an annotation), never a comparison.
#
# FAILURE DIRECTION: fail-CLOSED. Every refusal here is an admission that does
# not happen; the running composition keeps running with its state where it is.
# The controls below pin that it does not refuse everything: an identical shape,
# a WIDENED accepted shape, a cold key and a successor that declares no hand-off
# at all all still admit.

_H_SVC = "service D { fn q(s: Str) -> Int }\n"


def _h_comp(name: str, accepts: str | None, key: str = "db",
            extra: str = "") -> str:
    """A component providing `key`, optionally declaring a `handoff` on it."""
    line = f"  handoff {key}: {accepts}\n" if accepts else ""
    return (_H_SVC + extra + f"component {name} provides {key}: D {{\n"
            + line
            + f"  provide {key} {{ fn q(s) {{ let x = s   return 0 }} }}\n}}\n")


#: The running composition: one stateful provider exporting `Str` at `db`.
_H_M = _h_comp("Store", "Str")


def _h_drift(accepts: str, exports: str, new: str = "Store",
             old: str = "Store", key: str = "db") -> str:
    return ("G2|state hand-off on `" + key + "` differs from the running "
            "manifest: `" + new + "` accepts `" + accepts + "`, but `" + old
            + "` exports `" + exports + "` — the successor cannot hold the "
            "predecessor's state, and dropping it on the swap would be residue")


#: (name, running M, incoming X, replacing R, expected verdict). Every row is
#: checked on BOTH legs of oracle B, so the expectation is a third witness and
#: not the definition.
_HANDOFF_CORPUS = (
    ("a narrower accepted shape is refused",
     _H_M, _h_comp("Store", "Int"), ("Store",), _h_drift("Int", "Str")),
    ("an identical shape admits",
     _H_M, _h_comp("Store", "Str"), ("Store",), ""),
    ("a WIDENED accepted shape admits (T injects into Opt[T])",
     _H_M, _h_comp("Store", "Opt[Str]"), ("Store",), ""),
    ("the reverse narrowing is refused",
     _h_comp("Store", "Opt[Str]"), _h_comp("Store", "Str"), ("Store",),
     _h_drift("Str", "Opt[Str]")),
    ("numeric widening: Float accepts an exported Int",
     _h_comp("Store", "Int"), _h_comp("Store", "Float"), ("Store",), ""),
    ("and the reverse does not",
     _h_comp("Store", "Float"), _h_comp("Store", "Int"), ("Store",),
     _h_drift("Int", "Float")),
    ("containers meet elementwise",
     _h_comp("Store", "List[Int]"), _h_comp("Store", "List[Float]"),
     ("Store",), ""),
    ("and elementwise the other way is refused",
     _h_comp("Store", "List[Float]"), _h_comp("Store", "List[Int]"),
     ("Store",), _h_drift("List[Int]", "List[Float]")),
    ("a COLD key — nothing running exported it — is no conflict",
     _h_comp("Store", None), _h_comp("Store", "Int"), ("Store",), ""),
    ("a successor that declares no hand-off opts out, lossily but legally",
     _H_M, _h_comp("Store", None), ("Store",), ""),
    ("the table is keyed by KEY, so another component inherits the state too",
     _H_M, _h_comp("Fresh", "Int"), ("Store",), _h_drift("Int", "Str", "Fresh")),
    ("an ALIAS is not erased on either side, so the spellings differ",
     _h_comp("Store", "S", extra="type S = Str\n"), _h_comp("Store", "Str"),
     ("Store",), _h_drift("Str", "S")),
    ("the implicit same-name replacement reaches it with no `replacing=`",
     _H_M, _h_comp("Store", "Int"), (), _h_drift("Int", "Str")),
)


@pytest.mark.parametrize("name,running,incoming,replacing,expected",
                         _HANDOFF_CORPUS,
                         ids=[c[0] for c in _HANDOFF_CORPUS])
def test_oracle_b_handoff_state_compatibility(admit_ambient, name, running,
                                              incoming, replacing, expected):
    """ORACLE B over the hand-off row: both legs derived from one artifact, and
    the verdict string compared byte for byte."""
    got = _gate_ambient(admit_ambient, incoming, running, replacing=replacing)
    assert got == expected, name
    assert got == _ref_ambient(incoming, running, replacing=replacing), name


def test_manifest_wire_renders_the_handoff_row():
    """`C=k:T` rides with its component, after that component's requirement and
    route rows and ahead of the service block: it is a per-component COMPOSITION
    row. The provision and requirement positions are untouched, so the `mnames`
    DFS seed order cannot have moved, and a composition declaring no `handoff`
    renders byte-identically to before."""
    from revl import manifest_wire

    rows = manifest_wire(compile_source(_H_M, "running.rvl")).split(";")
    assert rows[0] == "Store/db/"
    assert rows[1] == "Store=db:Str"
    assert rows.index("!services") == 2
    # a composition with no hand-off renders no such row at all
    bare = manifest_wire(compile_source(_h_comp("Store", None), "running.rvl"))
    assert "=" not in bare, bare


def test_manifest_wire_renders_no_handoff_row_for_a_manifest_dict():
    """The manifest PROJECTION drops `handoff`, so a bare manifest dict cannot
    say what any provider exports — and the wire must not invent it. It renders
    no row, which is exactly the empty hand-off table `_running_handoffs` builds
    from the same input, so the two sides stay in step."""
    from revl import manifest_wire

    ir = compile_source(_H_M, "running.rvl")
    assert "=" not in manifest_wire(ir["manifest"])


def test_the_handoff_row_is_what_closed_it(admit_ambient):
    """NON-VACUITY, the load-bearing half. Strip the `C=k:T` row from the wire
    and the same admission goes back to the answer the gate gave before part 2:
    ADMITTED, with the running state dropped on the swap and nothing said
    anywhere. This is what pins that the row — not something already on the wire
    — is what closed the divergence."""
    from revl import manifest_wire

    ir = compile_source(_H_M, "running.rvl")
    wire = manifest_wire(ir, replacing=("Store",))
    incoming = _h_comp("Store", "Int")
    assert admit_ambient(incoming, wire) == _h_drift("Int", "Str")
    stripped = ";".join(r for r in wire.split(";") if "=" not in r)
    assert stripped != wire
    assert admit_ambient(incoming, stripped) == ""
    # ... while the reference refuses it either way: the divergence was real.
    assert _ref_ambient(incoming, _H_M, replacing=("Store",)) == \
        _h_drift("Int", "Str")


# -- where the hand-off sits in the sink (part 2 against part 3's ordering) ----
#
# `_admit_handoff_replacement` is collected immediately BEFORE
# `_admit_provision_withdrawal`, and both sit after the component loop and ahead
# of the spawn bounds, the attenuation walk and `_link`'s BOOT count. So the
# hand-off drift takes exactly one position in the `(line, seq)` sink, and the
# corpus below measures it from every side: against the withdrawal it is spliced
# beside, against the two families part 3 had to move, and against a component
# refusal that still outranks it on a tie.

#: the running composition of the ordering corpus: the part-1 withdrawal shape
#: (a provider and a retained consumer) plus a STATEFUL provider exporting `Str`.
_H_MO_CACHE = ('component Cache provides c: C {\n'
               '  handoff c: Str\n'
               '  provide c { fn g(k) { return k } }\n'
               '}\n')
_H_MO_M = _W_SVCS + _W_DB + _W_STORE + _H_MO_CACHE

#: `Cache` redeclared accepting a shape the running export does not fit.
_MO_HANDOFF = ('component Cache provides c: C '
               '{ handoff c: Int   provide c { fn g(k) { return k } } }')

_MO_HANDOFF_DRIFT = (
    "G2|state hand-off on `c` differs from the running manifest: `Cache` "
    "accepts `Int`, but `Cache` exports `Str` — the successor cannot hold the "
    "predecessor's state, and dropping it on the swap would be residue")

_H_MO_PAIRS = [
    ("hand-off then withdrawal", [_MO_HANDOFF, _MO_WITHDRAW]),
    ("withdrawal then hand-off", [_MO_WITHDRAW, _MO_HANDOFF]),
    ("hand-off then spawn bound", [_MO_HANDOFF, _MO_SPAWN]),
    ("spawn bound then hand-off", [_MO_SPAWN, _MO_HANDOFF]),
    ("hand-off then two boots", [_MO_HANDOFF, _MO_BOOT]),
    ("two boots then hand-off", [_MO_BOOT, _MO_HANDOFF]),
    ("hand-off then a component G4", [_MO_HANDOFF, _MO_G4]),
    ("a component G4 then hand-off", [_MO_G4, _MO_HANDOFF]),
    ("hand-off, withdrawal and two boots",
     [_MO_HANDOFF, _MO_WITHDRAW, _MO_BOOT]),
    ("a component G4, a spawn bound and a hand-off",
     [_MO_G4, _MO_SPAWN, _MO_HANDOFF]),
]


@pytest.mark.parametrize("layout", ["separate", "glued", "oneline"])
@pytest.mark.parametrize("name,fragments", _H_MO_PAIRS,
                         ids=[n for n, _ in _H_MO_PAIRS])
def test_oracle_b_handoff_refusal_ordering_agrees(admit_ambient, name,
                                                  fragments, layout):
    """ORACLE B over multi-refusal admissions that include a hand-off drift, in
    all three line layouts. Every one of these programs is refused by BOTH sides;
    what is compared is which of its true refusals gets named."""
    src = _mo_src(fragments, layout)
    got = _gate_ambient(admit_ambient, src, _H_MO_M)
    reference = _ref_ambient(src, _H_MO_M)
    assert reference != "", (name, layout)
    assert got == reference, (name, layout)


def test_the_handoff_is_collected_just_ahead_of_the_withdrawal(admit_ambient):
    """NON-VACUITY for the splice position, and the tie that fixes it. On ONE
    line the hand-off wins, because the reference collects it immediately ahead
    of the withdrawal; on separate lines the LINE still decides, whichever way
    round they are written. Both refusals are genuinely present in all three."""
    pair = [_MO_HANDOFF, _MO_WITHDRAW]
    glued = _mo_src(pair, "glued")
    assert _gate_ambient(admit_ambient, glued, _H_MO_M) == _MO_HANDOFF_DRIFT
    assert _ref_ambient(glued, _H_MO_M) == _MO_HANDOFF_DRIFT

    # line order decides when there is no tie, in both directions
    handoff_first = _mo_src([_MO_HANDOFF, _MO_WITHDRAW], "separate")
    assert _gate_ambient(admit_ambient, handoff_first,
                         _H_MO_M) == _MO_HANDOFF_DRIFT
    assert _ref_ambient(handoff_first, _H_MO_M) == _MO_HANDOFF_DRIFT

    withdraw_first = _mo_src([_MO_WITHDRAW, _MO_HANDOFF], "separate")
    assert _gate_ambient(admit_ambient, withdraw_first, _H_MO_M) == _W_LOST_DB
    assert _ref_ambient(withdraw_first, _H_MO_M) == _W_LOST_DB

    # ... and each fragment on its own is what both sides report, so neither
    # expectation above is the vacuous "only one refusal was ever there".
    alone = _mo_src([_MO_HANDOFF], "glued")
    assert _gate_ambient(admit_ambient, alone, _H_MO_M) == _MO_HANDOFF_DRIFT
    assert _ref_ambient(alone, _H_MO_M) == _MO_HANDOFF_DRIFT


def test_a_component_refusal_still_outranks_a_tying_handoff(admit_ambient):
    """NON-VACUITY, the other direction. The COMPONENT LOOP runs ahead of the
    hand-off on both sides, so a component's own G4 wins a tie with one: the
    hand-off did not move to the head of the sink, it took its own place in
    it."""
    src = _mo_src([_MO_HANDOFF, _MO_G4], "glued")
    expected = "G4|call to emission `bus.publish` must be marked `emit` (G4)"
    assert _gate_ambient(admit_ambient, src, _H_MO_M) == expected
    assert _ref_ambient(src, _H_MO_M) == expected


# -- a poisoned component contributes no hand-off verdict (issue #1127) -------
#
# `_admit_handoff_replacement` runs over `live_components`, which
# `src/revl/lower.py` builds by DROPPING every component whose body lowering
# raised (it appends a `poisoned` header stub instead). So on the reference a
# component whose body refuses contributes NO hand-off verdict at all: the body
# refusal is the whole answer, whatever line either sits on.
#
# `handoff_refusals` used to walk every component in the text, poisoned or not,
# and anchored its verdict at the COMPONENT declaration line. `pick_min` orders
# by `(line, seq)`, so it beat any INLINE body refusal, which `body_line`
# anchors at the offending STATEMENT — a strictly later line. Both refusals are
# true of the program, so the divergence was a 419c NAMING one and never a false
# admission; the fix narrows which of the two true refusals gets named and can
# only ever remove a hand-off verdict the component loop has already refused
# over, so it cannot turn a refusal into an admission.
#
# A whole-component AGGREGATE verdict (the G4 emission-reach one) is anchored at
# the component line, tied, and was saved by `seq` — which is why no corpus
# program caught this. The G1 undeclared access below is inline-anchored and has
# been since long before the type layer, so the reproducer predates every recent
# member of the family.

_PC_RUNNING = """service D { fn q(s: Str) -> Int }
component OldStore provides db: D {
  handoff db: Str
  provide db { fn q(s) { let x = s   return 0 } }
}
"""

#: The incoming text: `NewStore` accepts `Int` where the running `OldStore`
#: exports `Str` (a hand-off drift, anchored at the component line) AND refuses
#: inline on an undeclared access (anchored at the `provide` statement, later).
_PC_BOTH = """service D { fn q(s: Str) -> Int }
component NewStore provides db: D {
  handoff db: Int
  provide db { fn q(s) { emit nope.execute(s)   return 0 } }
}
"""

_PC_BODY_REFUSAL = "G1|`nope` is not a declared requirement of NewStore"
_PC_DRIFT = ("G2|state hand-off on `db` differs from the running manifest: "
             "`NewStore` accepts `Int`, but `OldStore` exports `Str` — the "
             "successor cannot hold the predecessor's state, and dropping it "
             "on the swap would be residue")


def _pc_ambient(admit_ambient, src: str) -> tuple[str, str]:
    kw = {"replacing": ("OldStore",)}
    return (_gate_ambient(admit_ambient, src, _PC_RUNNING, **kw),
            _ref_ambient(src, _PC_RUNNING, **kw))


def test_a_poisoned_component_contributes_no_handoff_verdict(admit_ambient):
    """Two TRUE refusals in one program, and the gate must name the one
    `check_and_lower` names: the body refusal, because the reference never
    reaches the hand-off pass for a component it poisoned.

    Before the poisoned set was threaded into `collect_nonlink` the gate
    answered the `G2` hand-off drift here, which is the issue-#1127
    divergence."""
    got, ref = _pc_ambient(admit_ambient, _PC_BOTH)
    assert ref == _PC_BODY_REFUSAL, ref
    assert got == ref, (got, ref)


def test_the_handoff_verdict_is_genuinely_there_to_be_skipped(admit_ambient):
    """NON-VACUITY, both halves.

    Make the hand-off COMPATIBLE and the answer does not move: the body refusal
    was always what both sides name. Repair the BODY instead and the hand-off
    drift is what both sides name, so the verdict the test above suppresses is
    a real one the gate still reports when nothing poisons its component."""
    compatible = _PC_BOTH.replace("handoff db: Int", "handoff db: Str")
    got, ref = _pc_ambient(admit_ambient, compatible)
    assert ref == _PC_BODY_REFUSAL, ref
    assert got == ref, (got, ref)

    sound_body = _PC_BOTH.replace("emit nope.execute(s)   ", "")
    got, ref = _pc_ambient(admit_ambient, sound_body)
    assert ref == _PC_DRIFT, ref
    assert got == ref, (got, ref)


def test_a_sibling_component_still_carries_its_own_handoff_verdict(
        admit_ambient):
    """The skip is PER COMPONENT, not per admission — exactly `live_components`,
    which drops the poisoned entry and keeps the rest.

    `NewStore` drifts on its own key and lowers cleanly, so its hand-off verdict
    survives; `Bad` refuses in its body, later in the file, and is poisoned. The
    drift is what both sides name. A skip that fired for the whole admission
    because SOME component was poisoned would name `Bad`'s `G1` here."""
    src = """service D { fn q(s: Str) -> Int }
service C { fn g(k: Str) -> Str }
component NewStore provides db: D {
  handoff db: Int
  provide db { fn q(s) { let x = s   return 0 } }
}
component Bad provides other: C {
  provide other { fn g(k) { emit nope.execute(k)   return k } }
}
"""
    bad_refusal = "G1|`nope` is not a declared requirement of Bad"
    got, ref = _pc_ambient(admit_ambient, src)
    assert ref == _PC_DRIFT, ref
    assert got == ref, (got, ref)

    # ... and `Bad` is genuinely refused, so the program really does carry two
    # true refusals and the drift won on `(line, seq)` rather than alone.
    assert _ref_ambient(src, _PC_RUNNING,
                        replacing=("OldStore",)) != bad_refusal
    compatible = src.replace("handoff db: Int", "handoff db: Str")
    got, ref = _pc_ambient(admit_ambient, compatible)
    assert ref == bad_refusal, ref
    assert got == ref, (got, ref)


# ======================= calls and signatures (docs/design/457 T2b) ==========
#
# `_tsb_program` draws a module `fn` whose body CALLS things: module `fn`s at
# every arity and both genericities, an `extern` that declares no return, the
# stdlib method table over every receiver family the walk can prove, the host
# stub surface through its constructor roots and through a bound host value, and
# `Map.empty()`. It is a DIFFERENTIAL draw, not an expectation table, compared
# on TAG and MESSAGE.
#
# One divergence CLASS survives, pinned below rather than described in a
# comment: the reference desugars a receiver-first list transform to its free
# function and then refuses that undeclared NAME, which is the G1 name-read
# family no slice has built. The gate refuses later, or not at all — an
# under-refusal. The bound the draw actually holds is absolute.

_TSB_HEAD = """type TsbRow = { h: Str }

fn mono(a: Int, b: Str) -> Int {
  return a
}

fn nores(a: Int) {
  let z = a
}

fn ident(x: T) -> T {
  return x
}

fn pick[T](xs: List[T]) -> T {
  return xs[0]
}

extern pure fn opaque(s: Str) = @py { return None }

"""

_TSB_ARGS = ["1", '"s"', "true", "3.5", "[]", "[1]", '["a"]', "None", "a", "b",
             "c", '{ h: "x" }', "Map.empty()", "0"]
# never a bare `[` at a receiver head: a statement STARTING with one is a parser
# refusal in the reference and a "bail" in this reader, which is the parser's
# surface and not this slice's.
_TSB_RECV = ["a", "b", "c", '"str"', "Map.empty()", "m", "p", "1", "3.5"]
_TSB_METH = ["length", "push", "slice", "charAt", "concat", "indexOf", "split",
             "join", "repeat", "startsWith", "to_int", "to_int32", "to_str",
             "keys", "set", "lookup", "has", "size", "remove", "mod",
             "div_trunc", "checked_mod", "putt", "fetch", "map", "field", "str"]
_TSB_HOSTV = ["new", "drop", "insert", "insert_if_absent", "remove", "get",
              "open", "close", "query", "run", "nope"]
_TSB_TYPES = ["Int", "Int32", "Float", "Str", "Bool", "Opt[Int]", "List[Int]",
              "List[Str]", "Any", "TsbRow", "Map[Str, Int]", "Map[Str, Str]"]


def _tsb_args(rng, n):
    return ", ".join(rng.choice(_TSB_ARGS) for _ in range(n))


def _tsb_call(rng, depth=1):
    k = rng.randrange(8)
    n = rng.randrange(0, 3)
    if k == 0:
        return f"mono({_tsb_args(rng, rng.randrange(0, 4))})"
    if k == 1:
        return f"ident({_tsb_args(rng, 1)})"
    if k == 2:
        return f"pick({_tsb_args(rng, 1)})"
    if k == 3:
        return f"nores({_tsb_args(rng, n)})"
    if k == 4:
        return (f"{rng.choice(_TSB_RECV)}.{rng.choice(_TSB_METH)}"
                f"({_tsb_args(rng, n)})")
    if k == 5:
        root = rng.choice(["Map", "Pool", "Job"])
        return f"{root}.{rng.choice(_TSB_HOSTV)}({_tsb_args(rng, n)})"
    if k == 6:
        return f"m.{rng.choice(_TSB_HOSTV)}({_tsb_args(rng, n)})"
    inner = (_tsb_call(rng, 0) if depth and rng.randrange(3) == 0
             else _tsb_args(rng, 1))
    return f"mono({inner}, {_tsb_args(rng, 1)})"


def _tsb_stmt(rng):
    e = _tsb_call(rng)
    k = rng.randrange(6)
    tag = rng.randrange(99)
    if k == 0:
        return f"let z{tag} = {e}"
    if k == 1:
        return f"let z{tag}: {rng.choice(_TSB_TYPES)} = {e}"
    if k == 2:
        return f"var w{tag} = {e}"
    if k == 3:
        return e
    if k == 4:
        return f"if ({e}) {{ let q{tag} = 1 }}"
    return f"assert {e}"


def _tsb_program(rng) -> str:
    body = "\n  ".join(_tsb_stmt(rng) for _ in range(rng.randrange(1, 4)))
    return (f"{_TSB_HEAD}fn f(a: Int, b: Str, c: List[Int]) -> "
            f"{rng.choice(_TSB_TYPES)} {{\n"
            f'  let m = Map.new()\n  let p = opaque("x")\n'
            f"  {body}\n  return {rng.choice(_TSB_ARGS)}\n}}\n")


# The family this draw once excluded — a receiver-first list transform desugars
# to a free function and the reference refuses that undeclared NAME — is now
# BUILT (the G1 read position, docs/design/457). The exclusion list is empty and
# stays here so the next slice that needs one has the shape to hand.
_TSB_LATER_SLICES: tuple[str, ...] = ()

_TSB_TAGS = ("T1", "T2", "TYPE", "HOST-METHOD", "HOST-ARITY", "G1")


@pytest.mark.parametrize("seed", [5, 17, 31])
def test_call_and_signature_fuzz_never_refuses_what_the_reference_admits(
        admit, seed):
    """THE BOUND, over 400 drawn call-carrying fn bodies per seed.

      * the gate NEVER refuses a program the reference admits — absolute, with
        no allowance;
      * where the reference's own refusal is in this slice's vocabulary and
        outside the family a later slice owns, the gate's verdict is the
        reference's TAG AND SENTENCE, byte for byte.
    """
    rng = random.Random(seed)
    drawn = 0
    compared = 0
    for _ in range(400):
        src = _tsb_program(rng)
        try:
            ref_tag, ref_msg = _ref(src)
        except RecursionError:  # pragma: no cover - a deep draw, not a verdict
            continue
        got = admit(src)
        drawn += 1
        assert not (ref_tag == "" and got != ""), \
            f"the reference ADMITS this and the gate refused {got!r}:\n{src}"
        if got == "" or ref_tag not in _TSB_TAGS:
            continue
        if any(m in ref_msg for m in _TSB_LATER_SLICES):
            continue
        compared += 1
        assert got == f"{ref_tag}|{ref_msg}", \
            f"verdict differs from the reference:\n{src}"
    assert drawn >= 350, drawn
    assert compared >= 150, compared


def test_the_stdlib_surface_the_gate_lists_is_the_references_own(ns):
    """`no builtin method` names the WHOLE stdlib surface, sorted, and the two
    tables are edited in different files. Held byte-exact so a method added to
    `_BUILTIN_METHODS` reds here rather than inside a message diff."""
    from revl.lower import _BUILTIN_METHODS
    assert ns["tk_stdlib_surface"]() == ", ".join(sorted(_BUILTIN_METHODS))


def test_the_host_stub_surface_the_gate_lists_is_the_references_own(ns):
    """The same, per host family: the verb list the `has no method` refusal
    quotes, and the declared argument types the arity and argument rules use."""
    from revl.typecheck import _HOST_FAMILIES
    for family, verbs in _HOST_FAMILIES.items():
        assert ns["tk_host_verbs"](family) == sorted(verbs), family
        for verb, params in verbs.items():
            assert ns["tk_host_params"](family, verb) == list(params), \
                f"{family}.{verb}"


def test_a_fn_that_declares_no_return_types_its_call_unknown(admit):
    """A FALSE REJECTION this slice closed, kept as its own case.

    `case_binds` spells a module `fn`'s own name as a function type, and gives a
    returnless `fn` the return `Unit` — which is the IR's spelling. The
    reference's signature table records `None` there (no return type, not the
    unit type), so `assert nores(1)` reads as an UNKNOWN condition and the
    program is admitted. Reading `Unit` back off the function type refused it,
    which is the one direction this gate may not err in. The signature table's
    own `sigr` row is "" and the call types unknown."""
    src = """fn nores(a: Int) {
  let z = a
}

fn f() -> Int {
  assert nores(1)
  return 1
}
"""
    assert _ref(src) == ("", "")
    assert admit(src) == ""


def test_the_call_and_signature_layer_reaches_a_module_fn_body(admit):
    """NON-VACUITY, spelled out: each position the slice opens draws the
    reference's own sentence, and the probe asserts the reference still spells
    it that way."""
    from revl.lower import _BUILTIN_METHODS
    cases = [
        # the arity window
        ("fn g(a: Int) -> Int { return a }\n"
         "fn f() -> Int { return g(1, 2) }\n",
         "T1|`g` takes 1 argument(s), 2 given"),
        # a monomorphic argument
        ("fn g(a: Int) -> Int { return a }\n"
         'fn f() -> Int { return g("s") }\n',
         "T1|argument 1 of `g(...)` expects `Int`, got `Str`"),
        # a generic call site: unify, then the UNIFIED return
        ("fn id(x: T) -> T { return x }\n"
         'fn f() -> Int { return id("s") }\n',
         "T1|this function's return expects `Int`, got `Str`"),
        # an explicit [T] list turns the implicit heuristic off, so `U` is an
        # ordinary undeclared nominal
        ("fn g[T](xs: List[U]) -> T { return xs[0] }\n"
         "fn f() -> Int { return g([1]) }\n",
         "T1|argument 1 of `g(...)` expects `List[U]`, got `List[Int]`"),
        # the builtin argument specs, through `@elem`
        ("fn f(m: Map[Str, Int], k: Str) -> Map[Str, Int] "
         '{ return m.set(k, "one") }\n',
         "T1|builtin `set` argument expects `Int`, got `Str`"),
        # a builtin's receiver family
        ("fn f(s: Str) -> Int { return s.div_trunc(2) }\n",
         "TYPE|builtin `div_trunc` needs a Int receiver, got `Str`"),
        # the multi-family row miss
        ("fn f(b: Bool) -> Str { return b.to_str() }\n",
         "T1|builtin `to_str` has no form for a `Bool` receiver "
         "(its receiver families: Float, Int)"),
        # the host stub surface, reached through a constructor-bound value
        ('fn f() { let m = Map.new()  m.putt("k", "v") }\n',
         "HOST-METHOD|`Map` has no method `putt` (its surface: drop, get, "
         "insert, insert_if_absent, new, remove)"),
        # the host constructor's own argument count
        ('fn f() { let p = Pool.open("dsn") }\n',
         "HOST-ARITY|host builtin `Pool.open` takes 2 arguments, got 1"),
        # the LOWERING refusals: the surface, the arity, the divisor
        ("fn f(m: Map[Str, Int], k: Str) -> Int { return m.fetch(k) ?? 0 }\n",
         "T1|no builtin method `fetch` on values — the stdlib surface is "
         + ", ".join(sorted(_BUILTIN_METHODS)) + " (docs/stdlib-2.0.md)"),
        ("fn f(s: Str) -> Str { return s.charAt() }\n",
         "TYPE|builtin `charAt` takes 1 argument(s), 0 given"),
        ("fn f(n: Int) -> Int { return n.div_euclid(0) }\n",
         "TYPE|`div_euclid` by a literal zero is undefined"),
        # `Map.empty()` takes none
        ('fn f() -> Map[Str, Int] { return Map.empty("k") }\n',
         "TYPE|`Map.empty()` takes no arguments, 1 given"),
    ]
    for src, expected in cases:
        ref_tag, ref_msg = _ref(src)
        assert f"{ref_tag}|{ref_msg}" == expected, f"the probe drifted:\n{src}"
        assert admit(src) == expected, src


def test_a_defaulted_parameter_list_builds_no_signature_row(admit):
    """A signature carrying a DEFAULT (item 187) gets no row, so the arity
    window is never counted: a call omitting the default must not be refused
    short. `params_at` cannot spell such a list, and `sig_walk` gates the row on
    it spelling the whole one.

    The gate still refuses this program, and the refusal is the PARSER's, not
    this slice's — `fb_span`/`p_fn` read the same parameter list and neither
    spells a default, so `fn g(a: Int, b: Str = "s")` never reaches a body. That
    divergence is older than this slice (the reference admits the program) and
    is not closed here; what is asserted is that the CALL layer adds nothing to
    it, which is what the withheld row buys."""
    src = ('fn g(a: Int, b: Str = "s") -> Int { return a }\n'
           "fn f() -> Int { return g(1) }\n")
    assert _ref(src) == ("", "")
    got = admit(src)
    assert got.startswith("BAD|"), got
    assert "argument(s)" not in got, got


def test_the_call_layer_stays_silent_where_it_cannot_prove_the_premise(admit):
    """The companion bound: the shapes this slice must NOT decide, each with the
    reason it cannot. A refusal appearing here is a false rejection waiting to
    happen on real code."""
    quiet = [
        # a name the body REBINDS: the reference reads a local of function type
        # first, and typing a function-value call is the arrow slice's (T2c).
        "fn g(a: Int) -> Int { return a }\n"
        "fn f() -> Int { let h = g\n  return 1 }\n",
        # a receiver whose type is merely not inferred HERE — an arrow's result
        # — is not a receiver PROVABLY without one, so the unpinned-receiver
        # refusal must not reach it.
        "fn f() -> Int { let k = (x: Int) => x\n  let v = k(1)\n  return 1 }\n",
    ]
    for src in quiet:
        assert _ref(src) == ("", ""), \
            f"the probe is not admitted by the reference:\n{src}"
        assert admit(src) == "", src
    # A list transform is SUGAR for a free function, and the reference types the
    # DESUGARED call — so a builtin row must not be consulted for it. The
    # reference's own refusal here is the undeclared `list_map` NAME, which is
    # the G1 family no slice has built; what this slice owes is not to invent a
    # builtin refusal in its place.
    sugar = "fn f(c: List[Int]) -> List[Str] { return c.map(n => n.to_str()) }\n"
    assert _ref(sugar)[0] == "G1", _ref(sugar)
    got = admit(sugar)
    assert "builtin `" not in got and "stdlib method `" not in got, got


# ================================ the fn-body TYPE layer (docs/design/457 T3a)
#
# `_typed_fn_body` draws a module `fn` whose signature and body are built out of
# the spellings the statement layer decides over: every scalar and container
# type in a parameter and a return position, every operator family, literals at
# the `Float` bound, field and index reads, record literals and updates, and the
# statement forms that open a checking position (`let` with and without an
# annotation, assignment and its compound form, `return`, `if`/`while`/`assert`).
#
# It is a DIFFERENTIAL draw, not an expectation table: the reference is the
# ground truth on every input, and the two are compared on TAG and MESSAGE.
#
# Three divergence CLASSES survive by design; they are pinned below as named
# tests rather than described in a comment, so a slice that closes one has to
# come here and delete it. Each is the gate refusing LATER than the reference
# because the reference's earlier refusal belongs to a surface this slice does
# not build — an under-refusal, never a false rejection. The bound the fuzz
# actually holds is absolute: the gate never refuses a program the reference
# admits, and it never refuses with a tag or a sentence the reference does not
# have for that program.

_TFB_TYPES = ["Int", "Int32", "Float", "Str", "Bool", "Opt[Int]", "Opt[Str]",
              "List[Int]", "List[Str]", "Any", "TfbRow", "Map[Str, Int]"]
_TFB_BIN = ["+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>", "==", "!=",
            "<", "<=", ">", ">=", "&&", "||", "??"]
_TFB_UN = ["!", "-", "~"]
_TFB_LITS = ["1", "2", "0", "3.5", "1e999", "1e308", "1.8e308", "0.5", '"s"',
             "true", "false", "null", "[]", "[1]", '["a"]', "{ h: 1 }",
             '{ h: "x" }', "None"]
_TFB_NAMES = ["a", "b", "c"]
_TFB_HEAD = "type TfbRow = { h: Str, name: Str }\n\n"


def _tfb_atom(rng, depth: int) -> str:
    k = rng.randrange(10)
    if depth <= 0 or k < 4:
        return rng.choice(_TFB_LITS + _TFB_NAMES)
    if k == 4:
        field = rng.choice(["h", "name", "length", "kind", "missing"])
        return f"{rng.choice(_TFB_NAMES)}.{field}"
    if k == 5:
        return f"{_tfb_atom(rng, depth - 1)}[{_tfb_atom(rng, depth - 1)}]"
    if k == 6:
        return (f"({_tfb_atom(rng, depth - 1)} {rng.choice(_TFB_BIN)} "
                f"{_tfb_atom(rng, depth - 1)})")
    if k == 7:
        return f"{rng.choice(_TFB_UN)}{_tfb_atom(rng, depth - 1)}"
    if k == 8:
        return ("{ " + f"{rng.choice(_TFB_NAMES)} | h = "
                f"{_tfb_atom(rng, depth - 1)}" + " }")
    verb = rng.choice(["to_str", "to_int", "to_int32", "length", "push"])
    return f"{rng.choice(_TFB_NAMES)}.{verb}()"


def _tfb_stmt(rng) -> str:
    k = rng.randrange(8)
    name = rng.choice(_TFB_NAMES)
    if k == 0:
        return f"let {name} = {_tfb_atom(rng, 2)}"
    if k == 1:
        return f"let {name}: {rng.choice(_TFB_TYPES)} = {_tfb_atom(rng, 2)}"
    if k == 2:
        return f"var {name} = {_tfb_atom(rng, 2)}"
    if k == 3:
        return f"{name} = {_tfb_atom(rng, 2)}"
    if k == 4:
        return f"{name} += {_tfb_atom(rng, 2)}"
    if k == 5:
        return f"if ({_tfb_atom(rng, 2)}) {{ let z = {_tfb_atom(rng, 1)} }}"
    if k == 6:
        return f"assert {_tfb_atom(rng, 2)}"
    return _tfb_atom(rng, 2)


def _typed_fn_body(rng) -> str:
    params = ", ".join(f"{n}: {rng.choice(_TFB_TYPES)}"
                       for n in _TFB_NAMES[:rng.randrange(1, 4)])
    body = "\n  ".join(_tfb_stmt(rng) for _ in range(rng.randrange(1, 5)))
    return (f"{_TFB_HEAD}fn f({params}) -> {rng.choice(_TFB_TYPES)} {{\n"
            f"  {body}\n  return {_tfb_atom(rng, 2)}\n}}\n")


# The reference messages whose family this slice deliberately leaves to a later
# one. A program whose reference refusal starts with one of these may be refused
# LATER by the gate (or not at all); a program whose reference refusal does NOT
# is held to tag and message exactly.
_TFB_LATER_SLICES = (
    # T2b: `builtin_check`'s receiver families and the unknown-receiver
    # HOST-METHOD refusal.
    "builtin `",
    "stdlib method `",
    # item 485: the `List` index bounds pass, which runs over a body before it
    # is lowered and so precedes every verdict here.
    "is out of range for a",
    # the INFER-position record update on a base that is not a record: code-less
    # in the reference and classified OUT of the gate's vocabulary, so spelling
    # it would trade a no-objection for a tag mismatch.
    "record update requires a record type",
    # the ordering family: `<`/`>` on an unorderable operand is code-less too.
    "cannot order `",
    # the NAMED record's field rules, which need the declared field SET the
    # statement layer's environment does not enumerate: the field-existence
    # read, and the two literal/annotation completeness sentences T2a names in
    # `_classify` above. `selfhost/lower.rvl` spells none of the three, so a
    # program whose reference minimum is one of them is refused LATER by the
    # gate (an under-refusal over some other true objection in the same body).
    "has no field `",
    "record literal for `",
    ", but the record has ",
)


# The tags this slice issues. A program whose reference minimum carries a tag
# outside this set is decided by some OTHER phase of the gate, and which of the
# two refusals is the minimum is that phase's ordering question, not this one's.
_TFB_TAGS = ("T1", "T2", "TYPE", "G1")


@pytest.mark.parametrize("seed", [11, 23, 97])
def test_typed_fn_body_fuzz_never_refuses_what_the_reference_admits(admit, seed):
    """THE BOUND, over 400 drawn fn bodies per seed.

    Two claims, in the order they matter:

      * the gate NEVER refuses a program the reference admits. Absolute, with no
        allowance — a false rejection is the one direction a gate may not err in;
      * where both sides' minimum refusal is in this slice's own vocabulary
        (`_TFB_TAGS`) and outside the families a later slice owns
        (`_TFB_LATER_SLICES`), the gate's verdict is the reference's TAG AND
        SENTENCE, byte for byte.

    What is deliberately not claimed: which of several true refusals is the
    minimum when one of them belongs to a surface this slice does not build.
    That is an under-refusal — the gate reports a LATER refusal, both of them
    real — and the census tracks it as a tag or message mismatch rather than a
    bypass."""
    rng = random.Random(seed)
    drawn = 0
    compared = 0
    for _ in range(400):
        src = _typed_fn_body(rng)
        try:
            ref_tag, ref_msg = _ref(src)
        except RecursionError:  # pragma: no cover - a deep draw, not a verdict
            continue
        got = admit(src)
        drawn += 1
        assert not (ref_tag == "" and got != ""), \
            f"the reference ADMITS this and the gate refused {got!r}:\n{src}"
        if got == "" or ref_tag not in _TFB_TAGS:
            continue
        if any(m in ref_msg for m in _TFB_LATER_SLICES):
            continue
        compared += 1
        assert got == f"{ref_tag}|{ref_msg}", \
            f"verdict differs from the reference:\n{src}"
    # non-vacuity: the draw really does reach the layer under test
    assert drawn >= 350, drawn
    assert compared >= 50, compared


def test_the_type_layer_reaches_a_module_fn_body(admit):
    """NON-VACUITY, spelled out: each statement position the slice opens draws
    the reference's own sentence."""
    cases = [
        ("fn f(n: Int) -> Int32 {\n  return n\n}\n",
         "T1|this function's return expects `Int32`, got `Int`"),
        ("fn f(a: Int32, b: Int) -> Int {\n  return a + b\n}\n",
         "T1|`+` does not mix `Int32` and `Int`"),
        ("fn f(s: Str) -> Str {\n  let c = s[0]\n  return c\n}\n",
         "T1|`Str` has no index operator — `[...]` indexes a `List` only"),
        ("fn f() -> Int {\n  var n = 1\n  n += 1e999\n  return n\n}\n",
         "TYPE|Float literal is infinite: it is outside the range of a "
         "64-bit float"),
        ("fn f(s: Str) -> Int {\n  assert s\n  return 0\n}\n",
         "T1|`assert` condition expects `Bool`, got `Str`"),
        ("fn f(s: Str) -> Int {\n  while (s) { let z = 1 }\n  return 0\n}\n",
         "T1|`while` condition expects `Bool`, got `Str`"),
        ("fn f(xs: List[Int]) -> Int {\n  for (x of xs) { let z = ~x }\n"
         "  return 0\n}\n",
         "T1|`~` requires an `Int32` operand, got `Int`"),
        ("fn f() -> List[Int] {\n  return [\"a\"]\n}\n",
         "T1|element of `List[Int]` expects `Int`, got `Str`"),
    ]
    for src, expected in cases:
        assert admit(src) == expected, src
        _agree(admit, src)


def test_the_type_layer_stays_silent_where_it_cannot_decide(admit):
    """The other half of the bound, as named cases: each of these is a program
    the reference ADMITS whose shape the walk approximates, and the walk must
    say nothing about any of them."""
    admitted = [
        # a structural record literal flowing into a NOMINAL record — resolved
        # through the declared-type table this walk does not carry
        "type R = { h: Str }\n\nfn f() -> R {\n  return { h: \"x\" }\n}\n",
        # a container of them, which the bracket-only argument split used to
        # read as a two-argument `List`
        "type R = { h: Str }\n\nfn f() -> List[R] {\n"
        "  return [{ h: \"x\" }]\n}\n",
        # calling an async-typed value yields the UNWRAPPED `T` (item 92 §2)
        "fn run(start: Str, step: (Str) -> Async[Str]) -> Str {\n"
        "  let a = step(start)\n  return a\n}\n",
        # a generic callee's declared return is unified at the call site, which
        # is the signature slice's; the parameter's own name is not the answer
        "fn ident(x: T) -> T {\n  return x\n}\n"
        "fn use_it() -> Str {\n  return ident(\"hello\")\n}\n",
        # the accumulator idiom: a bottom-typed binding learns its element type
        # at the first reassignment
        "fn f() -> Map[Str, Int] {\n  var m = Map.empty()\n"
        "  m = m.set(\"k\", 1)\n  return m\n}\n",
        # a `Float` literal at the very edge of binary64 is finite
        "fn f() -> Float {\n  return 1.7976931348623157e308\n}\n",
    ]
    for src in admitted:
        assert _ref(src) == ("", ""), f"the reference refuses this now:\n{src}"
        assert admit(src) == "", src




# ==================== name resolution (docs/design/457, the G1 read position) =
#
# `_lower_pure_expr`'s `ExprVar` arm. `_nr_program` draws a module `fn` whose
# body READS names from four disjoint pools — the fn's own binders, the module's
# declarations, the callable universe (`Map`/`Pool`/`Job`/`Stream`,
# `Some`/`None`/`Ok`/`Err`, `endorse`), and names nothing declares — at every
# position the lowering walk descends through: a bare read, a call callee, a
# call argument, a field target, an index, an interpolation, a ternary arm, a
# list element, a record field and a receiver-first list transform.
#
# It is a DIFFERENTIAL draw compared on TAG and MESSAGE, not an expectation
# table, and the absolute half of the bound (never refuse what the reference
# admits) is what the mixture of declared and undeclared pools is for.

_NR_HEAD = """type NrRow = { h: Str }
type NrShape = NrCircle | NrSquare(Int)

fn nr_helper(n: Int) -> Int {
  return n
}

extern pure fn nr_ext(s: Str) -> Str = @py { return s }

"""

# Names the reference resolves: the fn's binders, the module's declarations and
# the callable universe. Nothing drawn from here may EVER be refused.
_NR_DECLARED = ["a", "b", "c", "loc", "nr_helper", "nr_ext", "Map", "Pool",
                "Job", "Stream", "Some", "None", "Ok", "Err", "endorse",
                "NrCircle", "NrSquare"]
# Names nothing declares. The item-384 redirect table is deliberately absent:
# those draw the reference's own sentence, not G1, and the token scanner that
# ports them runs ahead of this walk.
_NR_UNDECLARED = ["zz", "nobody", "missing", "nr_absent", "qqq", "list_map"]


def _nr_name(rng):
    return rng.choice(_NR_DECLARED if rng.randrange(3) else _NR_UNDECLARED)


def _nr_expr(rng, depth=1):
    n = _nr_name(rng)
    k = rng.randrange(10 if depth else 1)
    if k == 0:
        return n
    if k == 1:
        return f"{n}({_nr_expr(rng, 0)})"
    if k == 2:
        return f"nr_helper({_nr_expr(rng, 0)})"
    if k == 3:
        return f"{n}.h"
    if k == 4:
        return f"{n}[0]"
    if k == 5:
        return f"`x${{{_nr_expr(rng, 0)}}}y`"
    if k == 6:
        return f"(a > 0 ? {_nr_expr(rng, 0)} : {_nr_expr(rng, 0)})"
    if k == 7:
        return f"[{_nr_expr(rng, 0)}]"
    if k == 8:
        return "{ h: " + _nr_expr(rng, 0) + " }"
    return f"{n}.{rng.choice(['map', 'filter', 'reduce'])}(nr_helper)"


def _nr_stmt(rng):
    e = _nr_expr(rng)
    tag = rng.randrange(99)
    k = rng.randrange(6)
    if k == 0:
        return f"let s{tag} = {e}"
    if k == 1:
        return f"var v{tag} = {e}"
    if k == 2:
        return e
    if k == 3:
        return f"if (a > 0) {{ let w{tag} = {e} }}"
    if k == 4:
        return f"while (false) {{ let u{tag} = {e} }}"
    return f"for (it{tag} of c) {{ let y{tag} = {e} }}"


def _nr_program(rng) -> str:
    body = "\n  ".join(_nr_stmt(rng) for _ in range(rng.randrange(1, 4)))
    return (f"{_NR_HEAD}fn f(a: Int, b: Str, c: List[Int]) -> Int {{\n"
            f"  let loc = a\n"
            f"  {body}\n  return a\n}}\n")


# The families whose EARLIER reference refusal this slice does not build, so a
# program carrying one may be refused later by the gate, or not at all.
_NR_LATER_SLICES = (
    # T2d: a match arm's payload binding, which the lowering walk does not enter
    "is not a case of",
    # item 485: the List index bounds pass runs over a body before it is lowered
    "is out of range for a",
    # the NAMED record's field-existence rule (the declared field SET)
    "has no field `",
    # the ordering family, code-less in the reference
    "cannot order `",
    # the ADT CONSTRUCTOR's payload rule (`typecheck.py`, `_case_call`): the
    # signature layer (docs/design/457 T2b) builds module-`fn` and host
    # signatures, and a case constructor's is not one of them. The reference
    # raises it while typing the call, which is ahead of the lowering walk this
    # slice runs in, so the gate reports a LATER refusal instead.
    "payload expects",
    # a callable NAME read as a VALUE. `nr_helper` and `nr_ext` resolve here —
    # that is this slice's whole claim — but neither the signature rows nor the
    # `fn`-token scan gives such a read a function TYPE, so a refusal that
    # SPELLS one (`got `(Str) -> Str``) is one the type layer could not reach.
    # Owned by the signature slice, which decides what a name's type is; matched
    # on the arrow because that is the only thing the missing type appears as.
    ") -> ",
    # the TERNARY branch-agreement rule (`typecheck.py`): the two arms of
    # `cond ? A : B` must share a type. The draw is `_nr_expr`'s `k == 6`, and
    # against `_NR_HEAD`'s `type NrShape = NrCircle | NrSquare(Int)` an arm that
    # is the bare case `NrCircle` beside one that is `c: List[Int]` always
    # disagrees. The reference raises it while TYPING the statement, ahead of the
    # lowering walk this slice runs in, so a program carrying one is refused by
    # the gate at a LATER statement — the name read inside a following statement,
    # which is a true refusal of the same program and not a gate error. Owned by
    # the type layer, like `payload expects` above.
    "ternary branches disagree",
)

_NR_TAGS = ("G1", "T1", "T2", "TYPE", "HOST-METHOD", "HOST-ARITY")


@pytest.mark.parametrize("seed", [3, 19, 41])
def test_name_resolution_fuzz_agrees_on_tag_and_message(admit, seed):
    """THE BOUND, over 400 drawn name-reading fn bodies per seed.

      * the gate NEVER refuses a program the reference admits — absolute, with
        no allowance. Two thirds of every drawn name comes from the DECLARED
        pool, so this half of the claim is the one that carries the risk;
      * where the reference's own refusal is in this slice's vocabulary and
        outside a later slice's family, the gate's verdict is the reference's
        TAG AND SENTENCE, byte for byte.
    """
    rng = random.Random(seed)
    drawn = 0
    compared = 0
    g1 = 0
    for _ in range(400):
        src = _nr_program(rng)
        try:
            ref_tag, ref_msg = _ref(src)
        except RecursionError:  # pragma: no cover - a deep draw, not a verdict
            continue
        got = admit(src)
        drawn += 1
        assert not (ref_tag == "" and got != ""), \
            f"the reference ADMITS this and the gate refused {got!r}:\n{src}"
        if got == "" or ref_tag not in _NR_TAGS:
            continue
        if any(m in ref_msg for m in _NR_LATER_SLICES):
            continue
        compared += 1
        if ref_tag == "G1":
            g1 += 1
        assert got == f"{ref_tag}|{ref_msg}", \
            f"verdict differs from the reference:\n{src}"
    assert drawn >= 350, drawn
    assert compared >= 150, compared
    assert g1 >= 50, g1


def test_the_name_resolution_rule_reaches_every_lowered_position(admit):
    """Each position the lowering walk descends through, as a named case. A
    reader that stopped short of one of these would leave the family half
    built, and the fuzz above would only report it as a rate.

    Every row asserts the REFERENCE first, so a body whose reference refusal is
    some other rule cannot pass as a witness for this one. The interpolation row
    is a `let` and not a `return` for exactly that reason: a `Str` template in a
    `-> Int` fn draws the return-type mismatch, which the reference raises while
    TYPING the statement and so ahead of the name it would otherwise resolve."""
    head = "type NrRow = { h: Str }\n\nfn g(n: Int) -> Int { return n }\n\n"
    bodies = [
        "  return zz",
        "  return zz(1)",
        "  return g(zz)",
        "  return zz.h",
        "  return zz[0]",
        '  let t = `a${zz}b`  return 1',
        "  return (a > 0 ? zz : 1)",
        "  let xs = [zz]  return 1",
        "  let r = { h: zz }  return 1",
        "  let y = -zz  return 1",
        "  let y = zz + 1  return 1",
        "  if (a > 0) { let y = zz }  return 1",
        "  while (false) { let y = zz }  return 1",
        "  for (v of [1]) { let y = zz }  return 1",
        "  var m = 1  m = zz  return 1",
        "  assert zz  return 1",
    ]
    for body in bodies:
        src = f"{head}fn f(a: Int) -> Int {{\n{body}\n}}\n"
        assert _ref(src) == ("G1", "`zz` is not declared in this function"), src
        assert admit(src) == "G1|`zz` is not declared in this function", src
        _agree(admit, src)


def test_an_adt_case_resolves_by_its_payload_and_its_position(admit):
    """The ADT-case half of the read rule, which the differential fuzz above
    found and this pins by name.

    The reference's `ExprVar` arm lets a case name stand as a VALUE only where
    `_tagged_case` reports no payload and an ADT that is neither `Result` nor
    `Opt`; anything else falls through to `callables`, which no case name joins.
    The CALL position is looser — `_lower_pure_expr`'s `ExprCall` arm builds an
    `adt` node for any unshadowed case — so the same name is declared as a
    constructor and undeclared as a value."""
    head = "type NrShape = NrCircle | NrSquare(Int)\n\n"
    refused = [
        "  let x = NrSquare\n  return 1",
        "  let x = NrSquare.h\n  return 1",
        "  return (true ? 1 : NrSquare)",
    ]
    for body in refused:
        src = f"{head}fn f() -> Int {{\n{body}\n}}\n"
        assert _ref(src) == ("G1",
                             "`NrSquare` is not declared in this function"), src
        assert admit(src) == "G1|`NrSquare` is not declared in this function", src
    admitted = [
        # the NULLARY case as a value, and the payload-carrying one CALLED
        f"{head}fn f() -> NrShape {{\n  return NrCircle\n}}\n",
        f"{head}fn f() -> NrShape {{\n  return NrSquare(1)\n}}\n",
        # `Ok`/`Err` are `Result` cases, so neither stands bare as a value —
        # but both are in `_BUILTIN_CONSTRUCTORS`, which is why they resolve
        "fn f() -> Int {\n  let x = Ok\n  let y = Ok(1)\n  return 1\n}\n",
        # a parameter SHADOWS a case of the same spelling (issue #320)
        "type S = A | B(Int)\n\nfn f(B: Int) -> Int {\n  return B\n}\n",
    ]
    for src in admitted:
        assert _ref(src) == ("", ""), f"the reference refuses this now:\n{src}"
        assert admit(src) == "", src


def test_the_name_resolution_rule_stays_silent_where_it_cannot_decide(admit):
    """The other half of the bound as named cases: programs the reference
    ADMITS whose shape this rule approximates, and says nothing about."""
    admitted = [
        # a nullary ADT case is a VALUE, not an unresolved name
        "type Shape = Circle | Square\n\nfn f() -> Shape {\n  return Circle\n}\n",
        # a module `fn` read as a function VALUE, not called
        "fn g(n: Int) -> Int { return n }\n"
        "fn f() -> Int {\n  let h = g\n  return h(1)\n}\n",
        # an `extern`'s name, which no signature row of this reader spells in
        # full when the declaration carries a witness or a slot
        'extern pure fn e(s: Str) -> Str = @py { return s }\n'
        'fn f() -> Str {\n  return e("x")\n}\n',
        # the loop binder is live inside the body and gone after it
        "fn f(xs: List[Int]) -> Int {\n  var t = 0\n"
        "  for (x of xs) { t += x }\n  return t\n}\n",
        # a `let` may mention its own name: the reference binds it in `scope`
        # before it lowers the initialiser
        "fn f(n: Int) -> Int {\n  let n2 = n\n  return n2\n}\n",
        # `endorse` and the host roots
        'fn f() -> Int {\n  let m = Map.new()\n  let p = Map.empty()\n'
        '  return 1\n}\n',
    ]
    for src in admitted:
        assert _ref(src) == ("", ""), f"the reference refuses this now:\n{src}"
        assert admit(src) == "", src


# ============ the provide-method / component TYPE layer (docs/design/457)
#
# The half the fn-body slice above deliberately left empty: a component body and
# a provide-method body resolve their names through the component's own header —
# the requirement handles, the service signature, the config fields, the
# activation locals — and none of those was in the type environment, so every
# expression in one was answered with silence.
#
# The corpus documents this closes, each drawing the reference's own sentence:
_PROVIDE_FIXTURES = [
    # the service is the source of truth for a provider's signature (A6): a
    # restated parameter annotation is compared against the declaration
    ("t7_provide_param_annotation_mismatch",
     "T1|parameter `sql` of `query` (from service `Db`) expects `Str`, "
     "got `Int`"),
    # ... and a provider must produce what its service promises
    ("t16_provide_method_missing_return",
     "T1|`get` implements `Store.get`, which returns `Str`, but this body "
     "never returns a value"),
    # the method's own body, typed from the service signature and its
    # annotated locals
    ("t30_field_read_on_any_provide_method",
     "T1|field read `.kind` on a value of type `Any` — an erased value has "
     "no known fields"),
    ("t31_index_non_int_provide_method", "T1|index expects `Int`, got `Str`"),
    # a call through a requirement handle, held to the declared signature
    ("t1_service_arg_type",
     "T1|`db.query` argument `sql` expects `Str`, got `Int`"),
    # ... with the activation local typed from the operation it binds
    ("t4_field_arg_type",
     "T1|`s.take` argument `s` expects `Str`, got `Int`"),
    # the config block's own rule
    ("t3_config_default_type",
     "T1|config field `n` default expects `Int`, got `Str`"),
]


@pytest.mark.parametrize("name,expected", _PROVIDE_FIXTURES)
def test_the_provide_method_documents_draw_the_reference_sentence(
        admit, name, expected):
    src = _fixture(name)
    tag, msg = _ref(src)
    assert f"{tag}|{msg}" == expected, "the reference moved: " + name
    assert admit(src) == expected, name


# --------------------------------------------------- the differential fuzz
#
# `_provide_program` draws a component around ONE service: the operation's
# parameter and return types, the method's own (optional) restatement of them,
# a body of the statement forms a provide method takes, and an activation body
# that calls through a requirement handle. It is a differential draw — the
# reference is the ground truth on every input and the two are compared on TAG
# and MESSAGE.

_PV_TYPES = ["Int", "Int32", "Float", "Str", "Bool", "Opt[Int]", "List[Int]",
             "Any", "PvRow", "Map[Str, Int]"]
# `k` and `v` are the drawn method's own parameters. A config field is NOT in
# the pool: the reference spells one `config.<name>`, and a bare `cfg` is an
# undeclared name the wiring walk refuses, which would make the draw measure G1.
_PV_LITS = ["1", "0", "3.5", '"s"', "true", "[]", "[1]", '{ h: "x", n: 1 }',
            "None", "k", "v"]
_PV_CFG_DEFAULTS = ["1", '"s"', "true", "3.5"]
_PV_HEAD = ("type PvRow = { h: Str, n: Int }\n"
            "extern pure fn pv_any(s: Str) -> Any\n"
            "  = @py { return s }\n"
            "  = @ts { return s }\n"
            "extern emission fn down_log(m: Str) -> Int\n"
            "  = @py { return 0 }\n"
            "  = @ts { return 0 }\n\n")


def _pv_expr(rng) -> str:
    k = rng.randrange(8)
    if k < 3:
        return rng.choice(_PV_LITS)
    if k == 3:
        return f"{rng.choice(_PV_LITS)}.{rng.choice(['h', 'n', 'missing'])}"
    if k == 4:
        return f"{rng.choice(_PV_LITS)}[{rng.choice(_PV_LITS)}]"
    if k == 5:
        op = rng.choice(["+", "-", "*", "==", "<", "&&", "??"])
        return f"({rng.choice(_PV_LITS)} {op} {rng.choice(_PV_LITS)})"
    if k == 6:
        return f"pv_any({rng.choice(_PV_LITS)})"
    return f"{rng.choice(['!', '-', '~'])}{rng.choice(_PV_LITS)}"


def _pv_stmt(rng, n: int) -> str:
    """One statement of a provide-method body. The forms are the ones the
    reference's parser takes THERE — a bare expression and an `assert` are
    statements in a `fn` body and parse errors in a provide method, so drawing
    one would measure the parser rather than the type layer.

    Each binding gets a FRESH name. A repeated one is a `_check_rebind` G6, and
    a body carrying both that and a type refusal measures the ORDERING question
    pinned in `test_a_wiring_refusal_outranks_an_earlier_type_one` rather than
    the type layer this draw is for."""
    k = rng.randrange(4)
    if k == 0:
        return f"let w{n} = {_pv_expr(rng)}"
    if k == 1:
        return f"let w{n}: {rng.choice(_PV_TYPES)} = {_pv_expr(rng)}"
    if k == 2:
        return f"let w{n} = emit down_log({_pv_expr(rng)})"
    return f"return {_pv_expr(rng)}"


def _provide_program(rng) -> str:
    pty, vty, rty = (rng.choice(_PV_TYPES) for _ in range(3))
    ann = "" if rng.randrange(2) else f": {rng.choice(_PV_TYPES)}"
    ret = "" if rng.randrange(2) else f" -> {rng.choice(_PV_TYPES)}"
    body = "\n      ".join(_pv_stmt(rng, n)
                           for n in range(rng.randrange(1, 4)))
    setup = ""
    if rng.randrange(2):
        setup = (f"  let got = effect up.read({rng.choice(_PV_LITS)})"
                 f" undo up.read(\"x\")\n")
    return (
        f"{_PV_HEAD}"
        f"service PvUp {{ fn read(a: {rng.choice(_PV_TYPES)}) -> "
        f"{rng.choice(_PV_TYPES)} }}\n"
        f"service PvDown {{ fn put(k: {pty}, v: {vty}) -> {rty} }}\n\n"
        f"component PvC requires up: PvUp provides down: PvDown {{\n"
        f"  config {{ cfg: {rng.choice(_PV_TYPES)} = "
        f"{rng.choice(_PV_CFG_DEFAULTS)} }}\n"
        f"{setup}"
        f"  provide down {{\n"
        f"    fn put(k{ann}, v){ret} {{\n"
        f"      {body}\n"
        f"    }}\n"
        f"  }}\n}}\n")


# The reference messages whose family the provide-method slice leaves to a later
# one, exactly as `_TFB_LATER_SLICES` does for the fn-body one.
_PV_LATER_SLICES = _TFB_LATER_SLICES + (
    # T2b: the unified signature of a generic / builtin receiver, and the
    # arity and existence halves of the A6 provision rules the gate steps over
    "is not a method of service",
    "params but service",
    # the missing-return rule's sibling, which needs the control-flow shape
    # `span_may_return` deliberately refuses to guess at
    "control can reach the end",
    # G1/G6 name discipline over a component body, decided by the wiring walk
    "is not declared in",
    "is already bound in",
    # the CHECK-position record rules over a component body (T2c/T3b)
    "record update",
)

_PV_TAGS = ("T1", "T2", "TYPE")


@pytest.mark.parametrize("seed", [5, 41, 83])
def test_provide_program_fuzz_never_refuses_what_the_reference_admits(
        admit, seed):
    """THE BOUND over 300 drawn components per seed, in the order the two claims
    matter:

      * the gate NEVER refuses a program the reference admits. Absolute, no
        allowance — a false rejection is the one direction a gate may not err in;
      * where both sides' minimum refusal is in this slice's vocabulary
        (`_PV_TAGS`) and outside the families a later slice owns, the gate's
        verdict is the reference's TAG AND SENTENCE, byte for byte."""
    rng = random.Random(seed)
    drawn = 0
    compared = 0
    for _ in range(300):
        src = _provide_program(rng)
        try:
            ref_tag, ref_msg = _ref(src)
        except RecursionError:  # pragma: no cover - a deep draw, not a verdict
            continue
        got = admit(src)
        drawn += 1
        assert not (ref_tag == "" and got != ""), \
            f"the reference ADMITS this and the gate refused {got!r}:\n{src}"
        if got == "" or ref_tag not in _PV_TAGS:
            continue
        if any(m in ref_msg for m in _PV_LATER_SLICES):
            continue
        compared += 1
        assert got == f"{ref_tag}|{ref_msg}", \
            f"verdict differs from the reference:\n{src}"
    assert drawn >= 280, drawn
    assert compared >= 20, compared


def test_a_transparent_alias_is_not_a_type_of_its_own(admit):
    """THE REGRESSION THE FUZZ COULD NOT DRAW, in both bodies and at every
    declaration site the slice reads.

    `type Idx = Int` is ERASED by the reference before it compares anything, so
    `xs[i]` with `i: Idx` is an `Int` index and is admitted. A gate that reads
    the alias as a type of its own refuses all eight of these — a false
    rejection, and one that reaches every rule at once rather than a single
    position. The fuzz draws only declared spellings, so it never wrote an
    alias and never saw it; these are hand-written for that reason.

    Until alias erasure lands (it is its own slice), a spelling mentioning a
    DECLARED name whose shape this walk does not carry decides nothing. The
    `decl <Name>` row is what bounds that to the alias question — see
    `test_an_undeclared_type_name_is_still_decided` for the other side of it."""
    admitted = [
        # a service parameter, reaching the method body's index rule
        "type Idx = Int\nservice S { fn at(xs: List[Int], i: Idx) -> Int }\n"
        "component C provides s: S {\n"
        "  provide s { fn at(xs, i) { return xs[i] } }\n}\n",
        # the A6 parameter-annotation rule: the alias and its expansion are the
        # same type to the reference
        "type Idx = Int\nservice S { fn at(i: Idx) -> Int }\n"
        "component C provides s: S { provide s { fn at(i: Int) = i } }\n",
        # ... and the return-annotation twin
        "type Idx = Int\nservice S { fn at(i: Int) -> Idx }\n"
        "component C provides s: S { provide s { fn at(i: Int) -> Int = i } }\n",
        # a requirement call's argument
        "type Idx = Int\nservice S { fn at(i: Idx) -> Idx }\n"
        "component C requires s: S {\n"
        "  let n = effect s.at(3) undo s.at(4)\n}\n",
        # a config field's declared type
        "type Idx = Int\nservice S { fn ping() -> Int }\n"
        "component C provides s: S {\n  config { n: Idx = 3 }\n"
        "  provide s { fn ping() = 0 }\n}\n",
        # the module-`fn` twin, on the surface the fn-body slice reads
        "type Idx = Int\nfn f(i: Idx, xs: List[Int]) -> Int {\n"
        "  return xs[i]\n}\n",
        # a condition position, where the alias hides a `Bool`
        "type Flag = Bool\nfn f(b: Flag) -> Int {\n  assert b\n  return 0\n}\n",
        # ... and through a declared record's FIELD type
        "type Row = { id: Idx }\ntype Idx = Int\n"
        "fn f(r: Row, xs: List[Int]) -> Int {\n  return xs[r.id]\n}\n",
    ]
    for src in admitted:
        assert _ref(src) == ("", ""), f"the reference refuses this now:\n{src}"
        assert admit(src) == "", src


def test_the_provide_method_layer_reaches_each_position(admit):
    """NON-VACUITY, spelled out: each name family the environment carries draws
    the reference's own sentence, and a body that names NOTHING is untouched."""
    cases = [
        # the method parameter, at the service's declared type
        ("service S { fn put(k: Str) -> Int }\n"
         "component C provides s: S {\n"
         "  provide s { fn put(k) { return k } }\n}\n",
         "T1|`put` returns expects `Int`, got `Str`"),
        # the annotated local
        # a body binding, at the INFER position the reference runs there (an
        # ANNOTATED one is not a checking position in a provide method — the
        # reference records the annotation and compares nothing)
        ("service S { fn put(k: Str) -> Int }\n"
         "component C provides s: S {\n"
         "  provide s { fn put(k) {\n    let n = k + 1\n    return 0\n"
         "  } }\n}\n",
         "T1|operand of string `+` expects `Str`, got `Int`"),
        # the activation local, typed from the operation it binds
        ("type Row = { id: Int }\n"
         "service S { fn get() -> Row\n  fn take(s: Str) -> Int }\n"
         "component C requires s: S {\n"
         "  let r = effect s.get() undo s.get()\n"
         "  let z = effect s.take(r.id) undo s.get()\n}\n",
         "T1|`s.take` argument `s` expects `Str`, got `Int`"),
        # the config default
        ("service S { fn ping() -> Int }\n"
         "component C provides s: S {\n  config { n: Bool = 1 }\n"
         "  provide s { fn ping() = 0 } }\n",
         "T1|config field `n` default expects `Bool`, got `Int`"),
    ]
    for src, expected in cases:
        ref_tag, ref_msg = _ref(src)
        assert f"{ref_tag}|{ref_msg}" == expected, "the reference moved: " + src
        assert admit(src) == expected, src


def test_a_wiring_refusal_outranks_an_earlier_type_one(admit):
    """THE ORDERING CLASS THIS SLICE DOES NOT CLOSE, pinned rather than
    described, so a slice that closes it has to come here and delete it.

    The reference lowers a provide-method body STATEMENT BY STATEMENT and both
    rules fire during that walk — `_check_rebind` on a binding that collides
    with a name already in scope, and `_sweep`'s type oracle on the value — so
    on a body carrying both the EARLIER SOURCE LINE wins. This gate runs the
    wiring walk (G1/G4/A1/G6) over the whole body first and the type layer after
    it, the placement the fn-body statement layer chose, so a wiring refusal is
    reported even when a type refusal stands on an earlier line.

    It is an under-refusal, never a false rejection: both refusals are true of
    the program and the gate names the later one. Closing it is the ordering
    question item 419c owns — interleaving the two walks per statement rather
    than running them in sequence.
    """
    src = ("service S { fn put(k: Str) -> Int }\n"
           "component C provides s: S {\n"
           "  provide s {\n"
           "    fn put(k) {\n"
           "      let w = ~[]\n"          # the TYPE refusal, line 5
           "      let w = 2\n"            # the REBIND, line 6
           "      let z = undeclared\n"   # the G1 that makes the walk refuse
           "    }\n  }\n}\n")
    ref_tag, ref_msg = _ref(src)
    assert (ref_tag, ref_msg) == (
        "T1", "`~` requires an `Int32` operand, got `List[Never]`"), ref_msg
    assert admit(src) == "G6|`w` is already bound in `put`"
    # ... and with the wiring refusal gone the gate reports the reference's own
    # verdict, so the divergence is the ORDERING and not a missing rule.
    clean = src.replace("      let z = undeclared\n", "")
    assert _ref(clean) == (ref_tag, ref_msg)
    assert admit(clean) == f"{ref_tag}|{ref_msg}"


def test_an_undeclared_type_name_is_still_decided(admit):
    """THE OTHER SIDE of the alias launder, so the narrowing is a decision
    rather than a leftover.

    A name the document never DECLARES is not an alias: the reference has no
    type for it either and refuses the document for that. Laundering it as well
    would trade a divergent refusal for a no-objection on a program the
    reference refuses — the bypass direction — so it keeps the reading it has.
    The two sides disagree on WHICH refusal (the reference reaches the undeclared
    type first), which is a message mismatch and not a bypass; both refuse."""
    src = ("fn put(m: Map[Str, Int], k: Str) -> Ma[Str, Int] {\n"
           "  return m.set(k, \"one\")\n}\n")
    ref_tag, ref_msg = _ref(src)
    assert ref_tag == "T1" and ref_msg != ""
    assert admit(src).startswith("T1|"), "an undeclared head must still decide"


def test_a_bare_return_in_a_provide_method_is_not_typed(admit):
    """The component-body twin of the module-`fn` rule (`fb_walk`'s `bare` op):
    a `return` that writes no value is a `return` for a totality question and
    NOT an expression to type.

    The second case is the one the statement reader makes possible. Its
    expression grammar does not stop at the end of a line, so `return` alone,
    followed by a statement that begins with an identifier, would read that next
    statement as this return's operand and hold the body to ITS type — a refusal
    the reference never issues, on a program it admits.

    The reference's own bare-return refusal ("`m` returns `Int` but this
    `return` carries no value") is the totality slice's and is not spelled here;
    leaving it is a no-objection, the direction this gate may take."""
    admitted = [
        # an operation that declares no result: a bare `return` is ordinary
        "service S { fn put(k: Str) }\n"
        "component C provides s: S {\n"
        "  provide s { fn put(k) { return } }\n}\n",
        # ... and the next line is not the return's operand
        "service S { fn put(k: Str) }\n"
        "component C provides s: S {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide s {\n    fn put(k) {\n      return\n"
        "      store.insert(k, \"v\")\n    }\n  }\n}\n",
    ]
    for src in admitted:
        assert _ref(src) == ("", ""), f"the reference refuses this now:\n{src}"
        assert admit(src) == "", src
    # ... while a return that DOES carry a value is still checked against the
    # service's declared result, so the fence is not a hole.
    typed = ("service S { fn put(k: Str) -> Int }\n"
             "component C provides s: S {\n"
             "  provide s { fn put(k) { return k } }\n}\n")
    assert admit(typed) == "T1|`put` returns expects `Int`, got `Str`"
