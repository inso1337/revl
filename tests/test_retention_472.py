"""Typed data retention: `Retained[T, P]` and the signed erasure receipt
(roadmap item 472, issue #824).

The item has two clauses and this file pins both halves of the retention one,
plus the receipt shape the retention half authorises:

  * a `Retained[T, P]` value past P's retention deadline is REFUSED at a
    persistence sink, and the refusal names the sink;
  * a declared legal hold OVERRIDES the deadline, because that is what a legal
    hold is;
  * an erasure request emits a SIGNED receipt naming each covered replica and
    each covered derivative;
  * a derivative the declaration does NOT cover is reported as `not-covered`
    and carries, inside the signed body, the statement that it is not claimed
    erased — never silence, and never a claim the policy cannot support.

The honest-scope tests at the bottom are as load-bearing as the refusals: the
receipt is an ENUMERATION plus a signature, not a proof of destruction, and the
tests that pin the envelope are what stop a later revision from letting a
document say two things about one row.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl import retention as R  # noqa: E402
from revl import erasure_receipt  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.parser import Parser  # noqa: E402

KEY = b"retention-test-key-0123456789abc"
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

PAST = '''retention customer_pii {
  until: "2020-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo, subject
  derivatives: summary, index
}
'''
FUTURE = PAST.replace("2020-01-01", "2099-01-01")
HELD = PAST.replace('  residence: "eu"',
                    '  residence: "eu"\n  hold: "litigation-2025-07"')

# a persistence sink whose OWN parameter declares the retention policy
DECLARED_SINK = '''extern emission[db.insert] fn db_put(row: {t}) -> Int = @py {{ return 0 }}
service Ops {{ emission fn go(r: Str) -> Int }}
component Store provides ops: Ops {{
  provide ops {{
    fn go(r) {{
      let n = emit db_put(r)
      return n
    }}
  }}
}}
'''

# a retained SOURCE flowing into a persistence sink whose parameter carries no
# qualifier at all: the refusal follows the value, not the spelling at the sink
FLOW = '''extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py {{ return k }}
extern emission[{cap}] fn sink(row: Str) -> Int = @py {{ return 0 }}
service Ops {{ emission fn go(k: Str) -> Int }}
component Store provides ops: Ops {{
  provide ops {{
    fn go(k) {{
      let row = load(k)
      let n = emit sink(row)
      return n
    }}
  }}
}}
'''


def _policy(source: str = PAST, name: str = "customer_pii") -> R.RetentionPolicy:
    return R.policies(Parser(source, "p.rvl").parse())[name]


def _refuses(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "retain.rvl")
    return excinfo.value


# ===========================================================================
# 1. The exit criterion: refused at a persistence sink, BY NAME.
# ===========================================================================

def test_expired_retained_value_is_refused_at_a_persistence_sink_by_name():
    """The flow half: the value carries the policy, the sink declares durable
    storage, and the refusal names the sink and the scope."""
    err = _refuses(PAST + FLOW.format(cap="db.insert"))
    assert err.code == "G-RETAIN"
    assert "`sink`" in err.message, err.message
    assert "`db` crossing" in err.message
    assert "may not be written to durable storage" in err.message


@pytest.mark.parametrize("cap", ["db(calls=3)", "fs(calls=1)", "kv(size=10)"])
def test_a_resource_ceiling_does_not_hide_a_bare_persistence_sink_head(cap):
    """A resource ceiling is a budget on the crossing, not a different scope
    (#3194). `db(calls=3)` still declares a `db` sink, so the past-deadline
    value is refused exactly as it is for the unceilinged spelling."""
    err = _refuses(PAST + FLOW.format(cap=cap))
    assert err.code == "G-RETAIN"
    assert "`sink`" in err.message, err.message
    assert f"`{cap.split('(', 1)[0]}` crossing" in err.message
    assert "may not be written to durable storage" in err.message


def test_every_persistence_scope_reads_its_head_through_a_ceiling():
    """The unit half of #3194, on the whole set: the ceiling is stripped before
    the head split, so every scope in the set is still recognised bare and
    through a dotted verb, and a non-sink stays a non-sink with a ceiling too."""
    for scope in sorted(R.PERSISTENCE_SINK_SCOPES):
        assert R.persistence_sink_of([f"{scope}(calls=1)"]) == scope
        assert R.persistence_sink_of([f"{scope}.insert(calls=1)"]) == scope
    assert R.persistence_sink_of(["log(calls=1)"]) is None


def test_the_refusal_names_the_policy_and_the_deadline_it_missed():
    err = _refuses(PAST + FLOW.format(cap="db.insert"))
    assert "customer_pii" in err.message
    assert "2020-01-01T00:00:00Z" in err.hint


def test_a_retained_value_carried_out_of_a_closure_is_refused():
    """Issue #3110: a closure CAPTURE is a carrier for `Retained[T, P]` too.
    `f("z")` carries a clean literal, but the callee VALUE holds the retained
    origin; the callee value's taint joins for every indirect call, INDEPENDENT
    of `any_sink` (this program declares no `Trusted[T]` sink)."""
    src = PAST + FLOW.format(cap="db.insert").replace(
        "      let row = load(k)\n      let n = emit sink(row)",
        "      let row = load(k)\n"
        "      let f = (a: Str) => row\n"
        "      let n = emit sink(f(\"z\"))")
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "may not be written to durable storage" in err.message
    assert "load() -> f() -> sink" in err.message, err.message


def test_a_live_retained_value_carried_out_of_a_closure_compiles():
    """The control that keeps the closure join exact: a value whose policy has
    NOT expired is admitted at the same persistence sink, including when a
    closure carries it there."""
    src = FUTURE + FLOW.format(cap="db.insert").replace(
        "      let row = load(k)\n      let n = emit sink(row)",
        "      let row = load(k)\n"
        "      let f = (a: Str) => row\n"
        "      let n = emit sink(f(\"z\"))")
    compile_source(src, "retain.rvl")


def test_a_retained_value_passed_to_a_closure_that_writes_it_is_refused():
    """A parameter that shadows the retained binding still reaches the sink
    INSIDE the arrow body: the seeded walk computes the closure's value, and the
    body is also walked against the enclosing environment as it always was, so
    `(row: Str) => db_put(row)` called with the retained `row` refuses. The seed
    must not hide a write behind a shadowing parameter."""
    src = PAST + '''extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py { return k }
extern emission[db.insert] fn db_put(row: Str) -> Int = @py { return 0 }
service Ops { emission fn go(k: Str) -> Int }
component Store provides ops: Ops {
  provide ops {
    fn go(k) {
      let row = load(k)
      let f = (row: Str) => emit db_put(row)
      let n = f(row)
      return n
    }
  }
}
'''
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "may not be written to durable storage" in err.message


def test_a_declared_persistence_sink_parameter_is_refused_on_the_declaration():
    """The declaration half: a crossing that DECLARES it persists a retained
    value is refused off the declaration alone, with no call site needed."""
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str, customer_pii]"))
    assert err.code == "G-RETAIN"
    assert "`db_put` declares a persistence sink (`db`)" in err.message


def test_a_ceiling_on_the_declared_sink_scope_does_not_hide_the_refusal():
    """The declaration half reads the same `persistence_sink_of`, so a ceiling
    on a bare sink head must not turn the declaration check off either."""
    src = DECLARED_SINK.replace("emission[db.insert]", "emission[db(calls=3)]")
    assert "emission[db(calls=3)]" in src, "the ceiling substitution did not land"
    err = _refuses(PAST + src.format(t="Retained[Str, customer_pii]"))
    assert err.code == "G-RETAIN"
    assert "`db_put` declares a persistence sink (`db`)" in err.message


def test_a_policy_whose_deadline_has_not_passed_compiles():
    compile_source(FUTURE + FLOW.format(cap="db.insert"), "retain.rvl")
    compile_source(FUTURE + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
                   "retain.rvl")


@pytest.mark.parametrize("scope", sorted(R.PERSISTENCE_SINK_SCOPES))
def test_every_declared_persistence_scope_refuses(scope):
    """Sink-ness is read off the crossing's DECLARED capability, so every scope
    in the set refuses and the set is the whole surface."""
    err = _refuses(PAST + FLOW.format(cap=scope))
    assert err.code == "G-RETAIN"
    assert f"`{scope}` crossing" in err.message


def test_a_non_persistence_sink_does_not_refuse():
    """A log is a disclosure sink, not a persistence sink. Retention is about
    durable storage; widening it to every crossing would make the refusal mean
    something the policy never said."""
    compile_source(PAST + FLOW.format(cap="log"), "retain.rvl")


# ---------------------------------------------------------------------------
# 1b. The same refusal ACROSS A SERVICE SEAM.
#
# The interface parameter a caller can see is unqualified — nothing else can
# cross a unit boundary, so a qualifier on it would be a promise the caller
# could not check — and the sink lives in the PROVIDER's body, so the caller
# never names it. The refusal therefore has to follow the value through the
# operation's inferred reach, exactly as the return half already follows the
# provider's body. Without it the guarantee is argument-blind at precisely the
# boundary it exists to police: the caller hands over a value that has outlived
# its deadline and the compiler says nothing, because the side that knows about
# the store is not the side that holds the value.
# ---------------------------------------------------------------------------

_LOAD = ('extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py '
         '{ return k }\n')
_DB_PUT = ('extern emission[db.insert] fn db_put(row: Str) -> Int = @py '
           '{ return 0 }\n')
_LOG_WRITE = ('extern emission[log.write] fn log_write(row: Str) -> Int = @py '
              '{ return 0 }\n')
_HELPER = 'fn write_row(row: Str) -> Int { return db_put(row) }\n'

_PERSISTS = 'let n = emit db_put(row)\n      return n'


def _seam(*, externs: str = _DB_PUT, sig: str = 'row: Str', impl: str = 'row',
          store: str = _PERSISTS, call: str = 'row') -> str:
    """One seam source, parameterised at the four points the tests vary: what
    the provider body does with the value, how the caller hands it over, and
    whether the value is the first argument."""
    return _LOAD + externs + f'''service Vault {{ emission fn stash({sig}) -> Int }}
service Ops {{ emission fn go(k: Str) -> Int }}
component Store provides vault: Vault {{
  provide vault {{
    fn stash({impl}) {{
      {store}
    }}
  }}
}}
component Caller requires vault: Vault provides ops: Ops {{
  provide ops {{
    fn go(k) {{
      let row = load(k)
      let n = emit vault.stash({call})
      return n
    }}
  }}
}}
'''


SEAM_PERSISTS = _seam()
# a provider that only RECEIVES the value: no sink, so no refusal to make
SEAM_PURE = _seam(externs='', store='return 0')
SEAM_LOG = _seam(externs=_LOG_WRITE,
                 store='let n = emit log_write(row)\n      return n')
# the sink one hop further out: the provider reaches it through a helper fn, so
# the reach has to be carried interprocedurally or the seam loses it again
SEAM_HELPER = _seam(externs=_DB_PUT + _HELPER,
                    store='let n = emit write_row(row)\n      return n')
# the retained value is NOT the first argument: the reach is per-parameter, so a
# seam that only guarded position 0 would admit this
SEAM_INDEX = _seam(sig='tag: Str, row: Str', impl='tag, row', call='"x", row')
# the caller hands over something with no retention origin at all
SEAM_CLEAN = _seam(call='k')


def test_a_retained_value_crossing_a_service_seam_is_refused_at_the_sink():
    """The exit criterion for the seam: the caller names no sink, and the value
    is still refused, by the name of the sink it actually reached."""
    err = _refuses(PAST + SEAM_PERSISTS)
    assert err.code == "G-RETAIN"
    assert "`db_put`" in err.message, err.message
    assert "`db` crossing" in err.message
    assert "may not be written to durable storage" in err.message


def test_the_seam_refusal_names_the_path_through_the_provider():
    """A refusal that named only the sink would leave the caller looking for a
    call it never wrote. The chain names the seam."""
    err = _refuses(PAST + SEAM_PERSISTS)
    assert "load() -> Store.stash -> db_put" in err.message, err.message


def test_a_retained_value_reaching_the_sink_through_a_helper_fn_is_refused():
    """The reach is carried interprocedurally: the provider's helper is not a
    place the guarantee gets to forget about."""
    err = _refuses(PAST + SEAM_HELPER)
    assert err.code == "G-RETAIN"
    assert "`db_put`" in err.message, err.message
    assert "write_row -> db_put" in err.message, err.message


def test_a_retained_value_at_a_non_first_seam_argument_is_refused():
    """The reach is per-parameter. Guarding position 0 alone would admit every
    seam that happens to take a tag first."""
    err = _refuses(PAST + SEAM_INDEX)
    assert err.code == "G-RETAIN"
    assert "at argument 2" in err.message, err.message


def test_a_pure_service_body_does_not_refuse_a_retained_value():
    """The negative control that keeps the seam rule honest: the provider has to
    actually persist. A crossing that only receives the value is not a sink."""
    compile_source(PAST + SEAM_PURE, "retain.rvl")


def test_a_seam_whose_provider_only_logs_does_not_refuse():
    """A log is a disclosure sink, not a persistence sink — through a seam as
    much as directly."""
    compile_source(PAST + SEAM_LOG, "retain.rvl")


def test_a_clean_value_at_a_persisting_seam_does_not_refuse():
    """The reach is a reach, not a verdict: an argument with no retention origin
    is admitted however durable the sink behind it is."""
    compile_source(PAST + SEAM_CLEAN, "retain.rvl")


def test_a_declared_legal_hold_overrides_the_deadline_across_the_seam():
    """The hold is a property of the policy, so it survives the seam: the same
    source, one added `hold:` line, admitted."""
    compile_source(HELD + SEAM_PERSISTS, "retain.rvl")


def test_a_seam_policy_whose_deadline_has_not_passed_compiles():
    compile_source(FUTURE + SEAM_PERSISTS, "retain.rvl")


# ===========================================================================
# 1c. The derivative relation: DECLARED in source, then checked.
# ===========================================================================

# The item's prerequisite 4, the half issue #2695 asks for: a way to say, in
# source, that a value is a derivative of a retained value, so the qualifier
# follows it and the persistence check and the receipt both apply to it. The
# relation is DECLARED, never inferred through an opaque host body.

# a policy that covers the class the derivation below declares
DERIVE_PAST = PAST.replace("derivatives: summary, index",
                           "derivatives: embedding")
DERIVE_FUTURE = DERIVE_PAST.replace("2020-01-01", "2099-01-01")

DERIVE_SOURCES = '''extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py { return k }
extern emission[db.insert] fn db_put(row: Str) -> Int = @py { return 0 }
'''

DERIVE_FN = '''fn embed(x: Retained[Str, customer_pii]) -> Str derives embedding of x { return x }

service Ops { emission fn go(k: Str) -> Int }

component Store provides ops: Ops {
  provide ops {
    fn go(k) {
      let row = load(k)
      let v = embed(row)
      let n = emit db_put(v)
      return n
    }
  }
}
'''


def test_a_declared_derivative_written_past_the_deadline_is_refused_by_name():
    """The exit criterion: the value written is a DECLARED embedding of a
    retained record, so the refusal names the sink AND the derivation, not only
    the policy."""
    err = _refuses(DERIVE_PAST + DERIVE_SOURCES + DERIVE_FN)
    assert err.code == "G-RETAIN"
    assert "`db_put`" in err.message, err.message
    assert "`db` crossing" in err.message, err.message
    assert "declared `embedding` derivative of `x`, made by `embed`" \
        in err.message, err.message
    assert "`derives embedding of x`" in err.message, err.message


def test_a_declared_derivative_on_an_extern_carries_the_policy_too():
    """A crossing (an extern) may declare the relation as well: the host body is
    opaque, so the declaration is the only place the fact can be written."""
    extern_fn = DERIVE_FN.replace(
        "fn embed(x: Retained[Str, customer_pii]) -> Str derives embedding of x "
        "{ return x }",
        "extern pure fn embed(x: Retained[Str, customer_pii]) -> Str "
        "derives embedding of x = @py { return x }")
    err = _refuses(DERIVE_PAST + DERIVE_SOURCES + extern_fn)
    assert err.code == "G-RETAIN"
    assert "`db_put`" in err.message, err.message
    assert "declared `embedding` derivative of `x`, made by `embed`" \
        in err.message, err.message


def test_a_derivative_class_the_policy_does_not_cover_is_refused_at_the_declaration():
    """The second acceptance criterion: the SAME program under a policy that
    covers only summaries is refused at the derivation, not reported later as
    `not-covered` at erasure time."""
    src = DERIVE_PAST.replace("derivatives: embedding",
                              "derivatives: summary") + DERIVE_SOURCES + DERIVE_FN
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "`embed` declares `derives embedding of x`" in err.message, err.message
    assert "does not cover `embedding` derivatives" in err.message, err.message
    assert "it covers `summary`" in err.message, err.message
    # AT THE DECLARATION: the flow refusal (which this same program would also
    # meet at the sink) must not be the one that fires.
    assert "flows into the persistence sink" not in err.message, err.message
    expected_line = src.splitlines().index(
        "fn embed(x: Retained[Str, customer_pii]) -> Str "
        "derives embedding of x { return x }") + 1
    assert err.line == expected_line, (err.line, expected_line)


def test_a_covered_derivative_class_is_admitted():
    """The control: the same source with the class the policy covers, and a
    deadline that has not passed, compiles."""
    compile_source(DERIVE_FUTURE + DERIVE_SOURCES + DERIVE_FN, "retain.rvl")


def test_a_derivation_of_an_unknown_class_is_refused_by_name():
    src = DERIVE_PAST + DERIVE_FN.replace("derives embedding of x",
                                          "derives telepathy of x")
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "`telepathy` is not a derivative class" in err.message, err.message
    assert "`embedding`" in err.hint, err.hint


def test_a_derivation_naming_a_parameter_the_callable_does_not_have():
    src = DERIVE_PAST + DERIVE_FN.replace("derives embedding of x",
                                          "derives embedding of row")
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "has no parameter `row`" in err.message, err.message


def test_a_derivation_source_must_be_a_retained_parameter():
    """A derivation names the RETAINED value it is made from: that is where the
    policy, the deadline and the covered classes come from. A plain `Str`
    parameter has no policy, so the declaration is refused rather than read as
    an annotation nothing enforces."""
    src = DERIVE_PAST + DERIVE_FN.replace(
        "x: Retained[Str, customer_pii]) -> Str derives embedding of x",
        "x: Str) -> Str derives embedding of x")
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "parameter `x` is not `Retained[T, <policy>]`" in err.message, \
        err.message


def test_a_retained_return_and_a_derivation_are_refused_as_two_claims():
    src = DERIVE_PAST + DERIVE_FN.replace(
        "-> Str derives embedding of x",
        "-> Retained[Str, customer_pii] derives embedding of x")
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "both a `Retained` return and `derives embedding of x`" \
        in err.message, err.message


DERIVE_STATEMENT_EMIT = ('      let n = emit db_put(v)\n      return n\n',
                         '      emit db_put(v)\n      return 0\n')


def _reach_and_rest(src: str):
    ir = copy.deepcopy(compile_source(src, "retain.rvl"))
    reach = ir["components"][0]["taint"].pop("reaches")
    return reach, ir


@pytest.mark.parametrize("form", ["value", "statement"])
def test_the_derivation_declaration_adds_one_origin_to_the_reach_list(form):
    """The clause changes no emitted code: with and without it the IR is equal
    everywhere except the component's reach list, which gains exactly the one
    derivation origin. Pinned for a value-form and a statement-form emission."""
    with_clause = DERIVE_FUTURE + DERIVE_SOURCES + DERIVE_FN
    if form == "statement":
        with_clause = with_clause.replace(*DERIVE_STATEMENT_EMIT)
        assert DERIVE_STATEMENT_EMIT[1] in with_clause
    without = with_clause.replace(" derives embedding of x", "")
    assert without != with_clause
    reach_with, rest_with = _reach_and_rest(with_clause)
    reach_without, rest_without = _reach_and_rest(without)
    assert reach_without == ["retained:customer_pii"]
    assert reach_with == ["retained:customer_pii",
                          "retained:customer_pii!embedding@embed.x"]
    assert rest_with == rest_without


# The clause ADDS a retention origin; it must never REPLACE the provenance a
# return qualifier, a body flow or a declassifier already put on the result.
# An implementation that REPLACES the result's origin set admits the first
# three programs below, which is the laundering defect these pin; the fourth
# pins that a declassifier's clean result also carries the added origin.

def _derive_body(calls: str) -> str:
    return ('service Ops { emission fn go(k: Str) -> Int }\n'
            'component C provides ops: Ops {\n'
            '  provide ops {\n'
            '    fn go(k) {\n'
            '      let row = load(k)\n' + calls +
            '    }\n'
            '  }\n'
            '}\n')


AUTHORITY_SINK = ('extern emission[shell] fn run(cmd: Trusted[Str]) -> Int '
                  '= @py { return 0 }\n')


def test_a_derivation_never_replaces_an_untrusted_return():
    """`-> Untrusted[Str] derives embedding of x` still yields the untrusted
    origin, so the authority sink refuses (G9). A replacing origin would admit
    it, which is the hole this pins."""
    src = DERIVE_FUTURE + DERIVE_SOURCES + AUTHORITY_SINK + (
        'extern pure fn embed(x: Retained[Str, customer_pii]) -> Untrusted[Str] '
        'derives embedding of x = @py { return x }\n') + _derive_body(
        '      let v = embed(row)\n'
        '      let n = emit run(v)\n'
        '      return n\n')
    err = _refuses(src)
    assert err.code == "G9", err.message


def test_a_derivation_never_replaces_a_secret_return():
    """`-> Secret[Str] derives ...` still yields `confidential`, so handing the
    result to an ordinary extern is refused as a disclosure (G-SECRET-FLOW)."""
    src = DERIVE_FUTURE + DERIVE_SOURCES + (
        'extern pure fn embed(x: Retained[Str, customer_pii]) -> Secret[Str] '
        'derives embedding of x = @py { return x }\n'
        'extern pure fn logit(s: Str) -> Str = @py { return s }\n') + _derive_body(
        '      let s = embed(row)\n'
        '      let x = logit(s)\n'
        '      return 0\n')
    err = _refuses(src)
    assert err.code == "G-SECRET-FLOW", err.message


def test_a_derivation_never_launders_an_untrusted_body_flow():
    """The untrusted origin can enter through a SECOND parameter, so validation
    of the return type alone cannot save the rule: the derivation origin has to
    JOIN the signature propagation, not stand in for it."""
    src = DERIVE_FUTURE + DERIVE_SOURCES + AUTHORITY_SINK + (
        'extern emission[web.fetch] fn fetch(u: Str) -> Untrusted[Str] '
        '= @py { return u }\n'
        'fn embed(x: Retained[Str, customer_pii], u: Untrusted[Str]) -> Str '
        'derives embedding of x { return u }\n') + _derive_body(
        '      let u = emit fetch(k)\n'
        '      let v = embed(row, u)\n'
        '      let n = emit run(v)\n'
        '      return n\n')
    err = _refuses(src)
    assert err.code == "G9", err.message


def test_a_derived_result_survives_a_declassifier_as_a_retained_value():
    """A `verified fn` returning `Trusted[Str]` still declassifies its argument
    (the authority sink admits it), and the derivation origin is ADDED to the
    clean result rather than turning the fn into a retention source that would
    pre-empt the declassifier. The retained result is refused at the
    persistence sink, naming the derivation."""
    src = DERIVE_PAST + DERIVE_SOURCES + AUTHORITY_SINK + (
        'extern emission[web.fetch] fn fetch(u: Str) -> Untrusted[Str] '
        '= @py { return u }\n'
        'verified fn clean(x: Retained[Str, customer_pii], u: Untrusted[Str]) '
        '-> Trusted[Str] derives embedding of x { return u }\n') + _derive_body(
        '      let u = emit fetch(k)\n'
        '      let t = clean(row, u)\n'
        '      let n = emit run(t)\n'
        '      let m = emit db_put(t)\n'
        '      return n\n')
    err = _refuses(src)
    assert err.code == "G-RETAIN", err.message
    assert "declared `embedding` derivative of `x`, made by `clean`" \
        in err.message, err.message


def test_the_receipt_lists_a_declared_derivative_as_a_row():
    """The third acceptance criterion: the receipt a fixture's declaration
    enumerates names the derivative, covered by the policy that reaches it."""
    program = Parser(DERIVE_PAST + DERIVE_SOURCES + DERIVE_FN, "f.rvl").parse()
    rows = R.declared_derivatives(program)
    assert [(r.name, r.kind) for r in rows] == [("embed(x)", "embedding")]
    policy = R.policies(program, "f.rvl")["customer_pii"]
    receipt = R.make_receipt(policy, "dpo", REPLICAS, rows, KEY, now=NOW)
    assert receipt["derivatives"][0]["derivativeKind"] == "embedding"
    assert receipt["derivatives"][0]["covered"] is True
    assert receipt["derivatives"][0]["disposition"] == "covered"
    assert receipt["derivatives"][0]["token"] == "derive:embed:x"
    assert receipt["summary"]["coveredDerivatives"] == 1
    ok, reason = R.verify_receipt(receipt, KEY)
    assert ok, reason


def test_the_declaration_refusal_of_a_persisting_derivation_names_it():
    """When the crossing that DECLARES the relation is itself the persistence
    sink, the existing declaration-level `G-RETAIN` says which derivative it
    writes rather than leaving the reader with only a policy name."""
    src = PAST + '''extern emission[index.add] fn index_add(x: Retained[Str, customer_pii]) -> Int derives index of x = @py { return 0 }
service Ops { emission fn go(k: Str) -> Int }
component C provides ops: Ops {
  provide ops {
    fn go(k) { return 0 }
  }
}
'''
    err = _refuses(src)
    assert err.code == "G-RETAIN"
    assert "declared as the source of the `index` derivative" in err.message, \
        err.message
    assert "`derives index of x`" in err.message, err.message


def test_a_derivation_origin_reads_as_its_policy_and_names_its_declaration():
    origin = R.derivation_origin("customer_pii", "embedding", "embed", "x")
    assert R.policy_of_origin(origin) == "customer_pii"
    assert R.derivation_of_origin(origin) == ("embedding", "embed", "x")
    # a plain retained origin carries no derivation, and still reads its policy
    assert R.policy_of_origin(R.origin_for("customer_pii")) == "customer_pii"
    assert R.derivation_of_origin(R.origin_for("customer_pii")) is None


# ===========================================================================
# 2. The legal hold overrides the deadline, as declared.
# ===========================================================================

def test_a_declared_legal_hold_overrides_the_deadline_at_the_sink():
    """The same expired deadline, the same sink, one added `hold:` line."""
    compile_source(HELD + FLOW.format(cap="db.insert"), "retain.rvl")
    compile_source(HELD + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
                   "retain.rvl")


def test_the_hold_is_applied_inside_the_policy_not_at_the_call_sites():
    """`expired` applies the hold itself, so no caller can consult the deadline
    while forgetting the exception."""
    held = _policy(HELD)
    assert held.held is True
    assert held.expired(NOW) is False
    assert _policy(PAST).expired(NOW) is True
    assert _policy(FUTURE).expired(NOW) is False


def test_an_empty_hold_is_refused_rather_than_read_as_no_hold():
    src = PAST.replace('  residence: "eu"', '  residence: "eu"\n  hold: ""')
    with pytest.raises(RevlError) as e:
        _policy(src)
    assert "has to NAME the hold" in e.value.hint


# ===========================================================================
# 3. The qualifier is orthogonal: the IR stays byte-identical.
# ===========================================================================

def test_the_qualifier_strips_to_its_base_type_and_the_ir_is_unchanged():
    """The whole point of joining the `Untrusted`/`Trusted`/`Secret` family: no
    backend needs to know this qualifier exists."""
    plain = compile_source(DECLARED_SINK.format(t="Str"), "retain.rvl")
    tagged = compile_source(
        FUTURE + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
        "retain.rvl")
    assert json.dumps(plain, sort_keys=True) == json.dumps(tagged, sort_keys=True)


def test_strip_qualifiers_handles_the_two_argument_head_anywhere():
    from revl.taint import strip_qualifiers, top_qualifier, retained_policy

    assert strip_qualifiers("Retained[Str, pii]") == "Str"
    assert strip_qualifiers("Result[Retained[Int, pii], Str]") == "Result[Int, Str]"
    assert strip_qualifiers("List[Retained[Str, pii]]") == "List[Str]"
    assert top_qualifier("Retained[Str, pii]") == "Retained"
    assert retained_policy("Retained[Str, pii]") == "pii"
    # arity is checked, so a user type that merely shares the name is left alone
    assert strip_qualifiers("Retained[A, B, C]") == "Retained[A, B, C]"
    assert top_qualifier("Retained[A, B, C]") is None


def test_a_retention_origin_is_not_an_authority_origin():
    """Retention is a time-and-residence dimension, orthogonal to 249's
    provenance dimension: a retained value at a `Trusted[T]` sink is not a G9
    refusal, because nothing about it is untrusted."""
    src = FUTURE + (
        'extern pure fn load(k: Str) -> Retained[Str, customer_pii] '
        '= @py { return k }\n'
        'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int '
        '= @py { return 0 }\n'
        'service Ops { emission fn go(k: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(k) {\n'
        '      let row = load(k)\n'
        '      let n = emit run(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    compile_source(src, "retain.rvl")


def test_an_untrusted_and_retained_value_is_still_refused_at_an_authority_sink():
    """The filter never admits an authority flow: a value that is BOTH keeps its
    untrusted origin and is still refused by G9."""
    src = FUTURE + (
        'extern emission[web.fetch] fn fetch(u: Str) -> Untrusted[Str] '
        '= @py { return u }\n'
        'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int '
        '= @py { return 0 }\n'
        'service Ops { emission fn go(u: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(u) {\n'
        '      let row = emit fetch(u)\n'
        '      let n = emit run(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    err = _refuses(src)
    assert err.code == "G9"


# ===========================================================================
# 4. The policy declaration: every rule is a refusal, not a shrug.
# ===========================================================================

def test_a_retention_qualifier_with_no_policy_is_refused():
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str]"))
    assert "with no retention policy" in err.message


def test_a_qualifier_naming_an_undeclared_policy_is_refused():
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str, nope]"))
    assert "which this program does not declare" in err.message
    assert "customer_pii" in err.hint


@pytest.mark.parametrize("field", ["until", "residence", "deleters"])
def test_a_missing_required_field_is_refused(field):
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith(field + ":")]
    with pytest.raises(RevlError) as e:
        _policy("".join(lines))
    assert f"missing `{field}`" in e.value.message


def test_a_deadline_without_a_utc_offset_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('"2020-01-01T00:00:00Z"', '"2020-01-01T00:00:00"'))
    assert "no UTC offset" in e.value.message


def test_a_deadline_that_is_not_an_instant_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('"2020-01-01T00:00:00Z"', '"whenever"'))
    assert "not an RFC-3339 instant" in e.value.message


def test_an_unknown_derivative_class_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace("summary, index", "summary, telepathy"))
    assert "unknown derivative class" in e.value.message
    assert "`telepathy`" in e.value.message


def test_derivatives_all_and_none_are_the_two_sentinels():
    assert _policy(PAST.replace("summary, index", "all")).derivatives == \
        R.DERIVATIVE_CLASSES
    assert _policy(PAST.replace("summary, index", "none")).derivatives == ()
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace("summary, index", "all, summary"))
    assert "mixes" in e.value.message


def test_omitting_derivatives_covers_none():
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith("derivatives:")]
    assert _policy("".join(lines)).derivatives == ()


def test_an_unknown_policy_field_is_refused_by_name():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('  residence: "eu"',
                             '  residence: "eu"\n  forever: "yes"'))
    assert "has no field `forever`" in e.value.message


def test_a_duplicate_policy_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST + PAST)
    assert "duplicate `retention customer_pii`" in e.value.message


def test_the_evaluation_instant_is_overridable_for_a_reproducible_build():
    env = {R.AS_OF_ENV: "2021-06-01T00:00:00Z"}
    assert R.evaluation_instant(env) == datetime(2021, 6, 1, tzinfo=timezone.utc)
    assert not _policy(FUTURE).expired(R.evaluation_instant(env))
    assert _policy(PAST).expired(R.evaluation_instant(env))


def test_a_program_with_no_retention_declaration_reads_no_clock():
    """The whole surface is inert without a declaration, which is what keeps it
    free for every program that does not use it."""
    from revl.taint import extract_and_normalize

    model = extract_and_normalize(Parser(
        DECLARED_SINK.format(t="Str"), "p.rvl").parse())
    assert model.retention_policies == {}
    assert model.retention_as_of is None


# ===========================================================================
# 5. The erasure receipt: covered replicas AND covered derivatives, named.
# ===========================================================================

REPLICAS = (
    R.Replica("host:Store:db_put", "eu"),
    R.Replica("emit:Store:archive.put", "eu"),
    R.Replica("host:Store:backup_put", "us-east-1"),
)
DERIVATIVES = (
    R.Derivative("daily_digest", "summary", "emit:Store:sum.add"),
    R.Derivative("search_rows", "index", "host:Store:index_add"),
    R.Derivative("vector_index", "embedding", "host:Store:embed"),
)


def _receipt(policy=None, requester="dpo", replicas=REPLICAS,
             derivatives=DERIVATIVES, **kw):
    return R.make_receipt(policy or _policy(), requester, replicas,
                          derivatives, KEY, now=NOW, **kw)


def test_the_receipt_names_every_covered_replica_and_derivative():
    receipt = _receipt()
    ok, why = R.verify_receipt(receipt, KEY)
    assert ok, why
    named = {row["token"] for row in receipt["replicas"]}
    assert named == {r.token for r in REPLICAS}
    covered = {row["name"] for row in receipt["derivatives"] if row["covered"]}
    assert covered == {"daily_digest", "search_rows"}
    # each covered derivative names the crossing it was reached through
    for row in receipt["derivatives"]:
        if row["covered"]:
            assert row["token"]


def test_an_uncovered_derivative_is_reported_as_such_not_claimed_erased():
    """The exit criterion's fourth clause. `vector_index` is an embedding and
    the policy covers only summaries and indexes, so the row says so IN THE
    SIGNED BODY rather than being omitted or quietly counted as erased."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if r["name"] == "vector_index")
    assert row["covered"] is False
    assert row["disposition"] == "not-covered"
    assert row["claim"] == R.NOT_CLAIMED
    assert "not claimed erased" in row["claim"]
    assert receipt["summary"]["uncoveredDerivatives"] == ["vector_index"]
    assert receipt["summary"]["coveredDerivatives"] == 2
    assert R.uncovered_derivatives(receipt) == [{
        "name": "vector_index", "derivativeKind": "embedding",
        "token": "host:Store:embed", "claim": R.NOT_CLAIMED}]


def test_a_policy_covering_no_derivative_claims_none_of_them():
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith("derivatives:")]
    receipt = _receipt(policy=_policy("".join(lines)))
    assert all(row["covered"] is False for row in receipt["derivatives"])
    assert receipt["summary"]["uncoveredDerivatives"] == \
        sorted(d.name for d in DERIVATIVES)


def test_a_replica_outside_the_declared_residence_is_a_finding():
    receipt = _receipt()
    row = next(r for r in receipt["replicas"]
               if r["token"] == "host:Store:backup_put")
    assert row["disposition"] == "residence-mismatch"
    assert row["residence"] == "us-east-1"
    assert receipt["policy"]["residence"] == "eu"


def test_an_unauthorised_requester_is_refused_rather_than_signed():
    with pytest.raises(RevlError) as e:
        _receipt(requester="marketing")
    assert "may not request deletion" in e.value.message
    assert "`dpo`" in e.value.hint and "`subject`" in e.value.hint


def test_a_derivative_of_an_unknown_class_is_refused():
    with pytest.raises(RevlError) as e:
        _receipt(derivatives=(R.Derivative("x", "telepathy"),))
    assert "unknown class" in e.value.message


def test_a_legal_hold_withholds_every_row_rather_than_erasing_it():
    """Under a hold, nothing is reported as erasable — the hold is an
    instruction to KEEP, and a receipt that said otherwise would be signing the
    opposite of the policy in the same document."""
    receipt = _receipt(policy=_policy(HELD))
    assert {r["disposition"] for r in receipt["replicas"]} == \
        {"withheld:legal-hold"}
    held_derivatives = {r["name"]: r["disposition"]
                        for r in receipt["derivatives"]}
    assert held_derivatives["daily_digest"] == "withheld:legal-hold"
    # ...and an uncovered derivative is STILL uncovered under a hold: a hold
    # does not extend a policy's reach.
    assert held_derivatives["vector_index"] == "not-covered"
    assert R.verify_receipt(receipt, KEY)[0]


def test_the_receipt_is_deterministic_given_now():
    assert _receipt() == _receipt()


# ===========================================================================
# 6. The signature and the envelope.
# ===========================================================================

def test_a_wrong_key_is_refused_and_names_the_key_it_needs():
    ok, why = R.verify_receipt(_receipt(), b"another-key-0123456789abcdefghij")
    assert not ok
    assert "signed by key" in why


def test_an_edited_row_breaks_the_signature():
    receipt = _receipt()
    receipt["replicas"][0]["token"] = "host:Store:somewhere_else"
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "altered after it was issued" in why


def test_a_dropped_claim_is_refused_by_the_envelope():
    """The `not claimed erased` statement cannot be detached from its row."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if not r["covered"])
    del row["claim"]
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "does not carry the `not claimed erased` statement" in why


def test_a_derivative_claimed_covered_by_a_policy_that_does_not_cover_it():
    """The load-bearing envelope check: `covered` is a reading of the policy in
    the SAME signed body, so an over-claim is refused even with an intact MAC."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if r["name"] == "vector_index")
    row["covered"] = True
    row["disposition"] = "covered"
    del row["claim"]
    receipt["summary"]["coveredDerivatives"] = 3
    receipt["summary"]["uncoveredDerivatives"] = []
    receipt["summary"]["byDerivativeDisposition"] = {
        "covered": 3, "not-covered": 0, "withheld:legal-hold": 0}
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "while the policy covers" in why


def test_a_summary_that_disagrees_with_its_own_rows_is_refused():
    receipt = _receipt()
    receipt["summary"]["replicas"] = 99
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "not the tally of this receipt's own rows" in why


def test_a_requester_outside_the_policys_own_deleters_is_refused():
    receipt = _receipt()
    receipt["request"]["requester"] = "marketing"
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "is not one of the policy's own deleters" in why


def test_a_mislabelled_document_is_refused_before_the_mac():
    receipt = _receipt()
    receipt["kind"] = "revl.erasure-receipt"
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "envelope refused: kind" in why


def test_the_domain_tag_separates_this_protocol_from_the_erasure_receipt():
    """Four signed protocols, four domains: without a distinct tag a retention
    receipt would verify as an erase-report receipt under the same key."""
    assert R.RECEIPT_DOMAIN != erasure_receipt.RECEIPT_DOMAIN
    assert R.KEY_ID_DOMAIN != erasure_receipt.KEY_ID_DOMAIN
    assert R.key_id(KEY) != erasure_receipt.key_id(KEY)
    body = {k: v for k, v in _receipt().items() if k != "signature"}
    assert R._mac(body, KEY) != erasure_receipt._mac(body, KEY)


def test_the_canonical_bytes_and_the_key_rule_are_attests():
    """Called rather than restated, which is what makes a third-party verifier's
    reconstruction exact (the drift `erasure_receipt` was corrected for)."""
    from revl import attest

    receipt = _receipt(signer="Jose Muller")
    assert R.verify_receipt(receipt, KEY)[0]
    body = {k: v for k, v in receipt.items() if k != "signature"}
    assert attest._canonical_bytes(body).decode("utf-8")


def test_a_signer_with_no_utf8_spelling_refuses_rather_than_crashing():
    class Unspellable:
        def __repr__(self):
            return "<unspellable>"

    with pytest.raises(RevlError) as e:
        _receipt(signer=Unspellable())
    assert "cannot be signed" in e.value.message


def test_an_empty_key_is_refused_on_both_sides():
    with pytest.raises(RevlError):
        _receipt_key_empty()
    assert R.verify_receipt(_receipt(), b"")[0] is False


def _receipt_key_empty():
    return R.make_receipt(_policy(), "dpo", REPLICAS, DERIVATIVES, b"", now=NOW)


# ===========================================================================
# 7. Honest scope: what the artifact says about itself.
# ===========================================================================

def test_the_scope_travels_inside_the_signed_body():
    receipt = _receipt()
    assert receipt["scope"]["doesNotProve"]
    assert any("not a proof of destruction" in line
               for line in receipt["scope"]["doesNotProve"])
    assert any("outside revl's boundary" in line
               for line in receipt["scope"]["doesNotProve"])
    # removing it breaks the signature, so the disclaimer cannot be detached
    del receipt["scope"]
    assert R.verify_receipt(receipt, KEY)[0] is False


def test_the_erasure_receipt_modules_retention_disclaimer_is_now_out_of_date():
    """`erasure_receipt` was landed with a paragraph saying the retention half
    is not implemented. This file is that half, so the paragraph is updated
    there; this test is what keeps the two from drifting apart again."""
    text = (ROOT / "src" / "revl" / "erasure_receipt.py").read_text(encoding="utf-8")
    assert "revl.retention" in text


# ===========================================================================
# 8. The multi-file path (the item-256 defect class).
# ===========================================================================

def test_a_policy_survives_the_multi_file_merge(tmp_path):
    """`compile_files` once dropped `program.secrets`, so the single-source path
    admitted a program the CLI path refused. Same list, same discipline."""
    from revl import compile_files

    (tmp_path / "policy.rvl").write_text(
        FUTURE + 'pub extern pure fn load(k: Str) -> '
                 'Retained[Str, customer_pii] = @py { return k }\n')
    (tmp_path / "app.rvl").write_text(
        'use "policy.rvl" { load }\n'
        'extern emission[log] fn logit(row: Str) -> Int = @py { return 0 }\n'
        'service Ops { emission fn go(k: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(k) {\n'
        '      let row = load(k)\n'
        '      let n = emit logit(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    compile_files([str(tmp_path / "app.rvl")])


def test_the_rejection_fixture_refuses_under_any_clock():
    """The corpus fixture's deadline is absolutely past, so it is not a test
    that starts failing on a particular date."""
    from revl import compile_files

    path = ROOT / "examples" / "rejections" / \
        "gretain_expired_at_persistence_sink.rvl"
    with pytest.raises(RevlError) as e:
        compile_files([str(path)])
    assert e.value.code == "G-RETAIN"
    assert _policy().until < datetime.now(timezone.utc) - timedelta(days=365)
