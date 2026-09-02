"""Adversarial gate suite — authority-smuggling attacks on the admission gate.

revl is an admission gate over agent-generated components (docs/threat-model.md).
Its input is hostile by construction: an agent optimising for "make the tool say
yes" will phrase a side effect however it must to slip past a name-based check.
This suite generalises `tests/test_mcp_hint_adversarial.py` (which attacks the
G4 read-only hint with first-class function values) into a battery across the
guarantee families. Each attack must be one of:

* REFUSED  — the gate rejects it with the guarantee-naming diagnostic, OR
* SURFACED — the gate admits it but it lands on the G8 audit review surface
             (the `extern`/emission enumeration `revl audit` projects), OR
* PINNED   — a real gap the gate does NOT yet defend, marked `xfail` with a
             reason and fenced in docs/contract-errata.md.

An attack that silently succeeds with none of the three is the bug this suite
exists to catch. See docs/threat-model.md for the attacker model and the
defends/non-goals split, and docs/rejections.md for the guarantee catalogue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.audit_diff import audit_report, crossings  # noqa: E402
from revl.mcp.schema import tools_from_ir  # noqa: E402

SHIP = (
    'extern emission fn ship(x: Str) -> Str = '
    '@py { print("SHIP EMITTED"); return x }\n'
)


def _tools(source: str) -> dict:
    return {t["name"]: t for t in tools_from_ir(compile_source(source))}


def _boundary(source: str, component: str) -> dict:
    return audit_report(compile_source(source))["boundary"][component]


# --------------------------------------------------------------------------
# G4 — laundering an emission past a plain (read-only) declaration.
#
# A plain provide-method advertises `readOnlyHint: true`. The emission fixed
# point must refuse it if the body *reaches* an emission by ANY route. The seed
# file pins the passed-as-argument, returned-from-helper, aliased-to-local and
# transitive-chain routes; these add the container-literal routes and the
# "referenced but never even called" conservative bound.
# --------------------------------------------------------------------------

def test_emission_stashed_in_a_record_field_is_refused():
    """`{ f: ship }` never names `ship` in call position — the record literal
    carries the value. First-class detection must trip on the container."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s {
    fn quiet(a) {
      let r = { f: ship }
      return r.f(a)
    }
  }
}
""")
    assert "`S.quiet` is declared plain" in str(excinfo.value)
    assert "passed as a function value" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_emission_stashed_in_a_list_literal_is_refused():
    """The list-element sibling of the record attack: `[ship]` handed to a
    dispatcher that indexes and applies element 0."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
fn apply0(fs: List[(Str) -> Str], x: Str) -> Str { return fs[0](x) }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(a) = apply0([ship], a) }
}
""")
    assert "passed as a function value" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_an_emission_bound_but_never_called_is_still_refused():
    """The conservative bound: a bare reference to an emitting callable is
    enough, even if the value is never applied in this body — whoever the
    binding escapes to may dispatch it. Pins that the gate does not require a
    call site to fire the refusal."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s {
    fn quiet(a) {
      let g = ship
      return a
    }
  }
}
""")
    assert "passed as a function value" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_an_emission_in_value_position_is_refused():
    """`let r = ship(a)` binds the *result* of an emission — the call still
    happened. Value position is not a hiding place."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s {
    fn quiet(a) {
      let r = ship(a)
      return r
    }
  }
}
""")
    assert "reaches `ship()`" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_a_mutual_recursion_cycle_hiding_an_emission_is_refused():
    """The fixed point is closed under cycles: `quiet -> a -> b -> ship`, and
    `a`/`b` are mutually recursive so a naive DFS could loop. The gate still
    reaches the emission."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
fn a(x: Str) -> Str { return b(x) }
fn b(x: Str) -> Str { return ship(a(x)) }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(z) = a(z) }
}
""")
    assert "`S.quiet` is declared plain" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_honest_higher_order_over_pure_fns_stays_read_only():
    """The sound direction must not regress into a false positive: the same
    container/dispatcher shape is safe when only PURE functions flow through
    it, and the projection may keep advertising read-only."""
    tools = _tools("""
fn apply0(fs: List[(Str) -> Str], x: Str) -> Str { return fs[0](x) }
fn shout(x: Str) -> Str { return x + "!" }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(a) = apply0([shout], a) }
}
""")
    quiet = tools["revl.s.quiet"]
    assert quiet["annotations"]["readOnlyHint"] is True
    assert quiet["x-revl"]["effects"]["reachesEmission"] == []


# --------------------------------------------------------------------------
# G4 capability bound — a scoped `emission[db]` may not cross a DIFFERENT
# boundary, named or (worse) unnameable via a first-class value.
# --------------------------------------------------------------------------

def test_a_scoped_emission_reaching_an_unnameable_boundary_is_refused():
    """`emission[db]` promises the provider crosses only `db`. Laundering a
    host emission through a first-class value reaches the unnameable `*`, which
    no `emission[...]` bound can name — so the bound must reject it."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHIP + """
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service Database { emission fn execute(sql: Str) -> Int }
service S { emission[db] fn put(a: Str) -> Str }
component C requires db: Database provides s: S {
  provide s { fn put(a) = indirect(ship, a) }
}
""")
    assert "`S.put` is declared `emission[db]`" in str(excinfo.value)
    assert "unnameable host boundary" in str(excinfo.value)
    assert excinfo.value.category == "emission-capability"


def test_a_scoped_emission_that_stays_in_bounds_compiles():
    """The honest control for the capability bound: `emission[db]` that emits
    only `db` is admitted, and the audit surface reports the scoped crossing."""
    tools = _tools("""
service Database { emission fn execute(sql: Str) -> Int }
service S { emission[db] fn put(a: Str) -> Str }
component C requires db: Database provides s: S {
  provide s { fn put(a) { emit db.execute(a) return a } }
}
""")
    put = tools["revl.s.put"]
    assert put["annotations"]["readOnlyHint"] is False
    assert put["x-revl"]["capabilities"] == ["db"]
    assert put["x-revl"]["effects"]["reachesCapabilities"] == ["db"]


# --------------------------------------------------------------------------
# G8 — the boundary is a *review surface*, not a wall. Legitimate host reach is
# admitted and SURFACED; the non-goal (no sandboxing) is asserted explicitly.
# --------------------------------------------------------------------------

def test_arbitrary_host_code_in_an_extern_is_admitted_but_surfaced():
    """NON-GOAL made executable: the gate does not sandbox host code. An
    extern with an arbitrary @py body compiles — and is surfaced, classified
    `emission`, flagged non-read-only, and enumerated as a host crossing. The
    gate reviews it; the quarantine tier (roadmap item 45) confines it."""
    source = """
extern emission fn rm() -> Str = @py { import os; os.system("echo pwned"); return "x" }
service S { emission fn wipe() -> Str }
component C provides s: S {
  provide s { fn wipe() = rm() }
}
"""
    tools = _tools(source)
    wipe = tools["revl.s.wipe"]
    assert wipe["annotations"]["readOnlyHint"] is False
    assert wipe["annotations"]["destructiveHint"] is True
    stats = _boundary(source, "C")
    assert "rm" in {e["name"] for e in stats["externs"]}
    assert "host:C:rm" in crossings(audit_report(compile_source(source)))


def test_a_teardown_position_emission_is_refused():
    """This attack was SURFACED and is now REFUSED. Hiding the crossing in the
    bracket's `undo` slot puts it at or after the session verdict, where it
    cannot be answered, rolled back, or reviewed by the 246 approval gate — so
    the contract bounds it instead of merely reporting it: a bracket inverse
    "may emit in teardown: no (G5)" (docs/design/teardown-contract.md), and the
    runtime tags a failed bracket inverse contract-grade precisely because the
    inverse claimed to be host-local and non-emitting."""
    source = SHIP + """
component Logger {
  let h = effect Map.new() undo ship("closing")
}
"""
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    assert "(G5)" in str(excinfo.value)
    assert excinfo.value.code == "G5"


def test_a_forward_position_emission_is_still_surfaced_not_refused():
    """The bound above is positional, not a ban on the extern: the same
    emission on the FORWARD path is admitted and lands on the G8 audit surface
    as a reached host crossing. `compensate` is the teardown slot that MAY
    emit (item 247); the bracket inverse is not."""
    source = SHIP + """
service S { emission fn note(x: Str) -> Str }
component Logger provides s: S {
  let h = effect Map.new() undo h.drop()
  provide s { fn note(x) = ship(x) }
}
"""
    stats = _boundary(source, "Logger")
    assert "ship" in {e["name"] for e in stats["externs"]}
    assert "host:Logger:ship" in crossings(audit_report(compile_source(source)))


def test_an_unclassified_extern_is_refused():
    """G8's floor: an extern must say what it is, so the boundary stays
    enumerable. An unclassified escape hatch is refused."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("extern fn f() = @py { pass }\n")
    assert "unclassified extern" in str(excinfo.value)


# --------------------------------------------------------------------------
# G2 — smuggling a second provider onto a key, including via an equal realm
# label. The gate refuses at compile time on every tier (the cordis4j runtime
# equal-string separation is a fenced runtime divergence, not a gate defence —
# docs/contract-errata.md).
# --------------------------------------------------------------------------

def test_two_providers_of_one_key_are_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Kv { fn get(k: Str) -> Str }
component A provides kv: Kv { provide kv { fn get(k) = k } }
component B provides kv: Kv { provide kv { fn get(k) = k } }
""")
    assert "provision conflict: key `kv` is provided by both A and B (G2)" \
        in str(excinfo.value)


def test_a_second_provider_hidden_behind_an_equal_realm_label_is_refused():
    """One realm label = one realm (docs/design-v2-realms.md): two providers
    that both `isolate kv in realm("t")` are a G2 conflict, not two disjoint
    realms. The gate proves this even though the cordis4j *runtime* separates
    equal strings (fenced divergence)."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Kv { fn get(k: Str) -> Str }
component A provides kv: Kv { isolate kv in realm("t") provide kv { fn get(k) = k } }
component B provides kv: Kv { isolate kv in realm("t") provide kv { fn get(k) = k } }
""")
    assert "in realm `t` is provided by both A and B (G2)" in str(excinfo.value)


# --------------------------------------------------------------------------
# G1 / A1 — declared access and iteration boundaries. A plain provide-method
# body is a restricted expression scope; the attacker cannot reach a service it
# never required, nor open an iteration boundary where none can exist.
# --------------------------------------------------------------------------

def test_reaching_an_undeclared_service_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Database { fn query(sql: Str) -> Str }
service S { fn q() -> Str }
component C provides s: S {
  provide s { fn q() = db.query("x") }
}
""")
    assert "`db` is not a declared requirement of C" in str(excinfo.value)


def test_an_await_smuggled_into_a_provide_method_is_refused():
    """A1: a provide method runs while ACTIVE, where there is no transition to
    divert — an `await` there is refused, so a component cannot sneak an
    iteration boundary into per-call code."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Cache { fn get(key: Str) -> Opt[Str] }
component BadAwait provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(key) {
      await Job.run("lookup")
      return store.get(key)
    }
  }
}
""")
    assert "`await` is only allowed in a component body" in str(excinfo.value)


# --------------------------------------------------------------------------
# PINNED GAP — G8 enumeration is incomplete for a host block reached ONLY
# through a first-class function value. The G4 defence holds (the operation is
# correctly flagged non-read-only), but the per-component `externs` list and
# the `host:` crossing token are missing, so `revl audit --diff` cannot see a
# widening that happens purely through first-class laundering. Fenced in
# docs/contract-errata.md ("G8 enumeration is incomplete for first-class host
# reaches"); the fix belongs in `_boundary` (src/revl/__main__.py), not here.
# --------------------------------------------------------------------------

def test_direct_host_reach_is_enumerated_on_the_g8_surface():
    """Baseline the gap contrasts against: a directly-called host extern IS
    enumerated and produces a `host:` crossing token."""
    source = SHIP + """
service S { emission fn loud(a: Str) -> Str }
component C provides s: S {
  provide s { fn loud(a) = ship(a) }
}
"""
    assert "ship" in {e["name"] for e in _boundary(source, "C")["externs"]}
    assert "host:C:ship" in crossings(audit_report(compile_source(source)))


def test_first_class_laundered_host_reach_is_enumerated_on_the_g8_surface():
    """The attack: reach `ship` only through a first-class value handed to a
    dispatcher (bare `emission` service, so it compiles). It runs at runtime
    exactly as the direct call does. Item 24 folded the G4 first-class-reach
    fixed point into `_boundary`, so the laundered reach now surfaces the same
    `host:` crossing token as the direct call — no authority-drift blind spot.
    Previously xfail (G8 enumeration gap); now resolved."""
    source = SHIP + """
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service S { emission fn loud(a: Str) -> Str }
component C provides s: S {
  provide s { fn loud(a) = indirect(ship, a) }
}
"""
    # surfaces `ship` the same way the direct call does (see the baseline above).
    assert "ship" in {e["name"] for e in _boundary(source, "C")["externs"]}
    assert "host:C:ship" in crossings(audit_report(compile_source(source)))
