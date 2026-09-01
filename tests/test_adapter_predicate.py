"""Item 296, slice 1: the pure adapter-synthesis predicate (`revl.adapt`).

Every catalogue row (B1..B6) and every refusal clause is exercised. The
predicate reads only two `ServiceDecl`s and the opt-in map `D`; it never reads
a body. These mirror the design's exit tests E1..E11.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.parser import MethodDecl, ServiceDecl  # noqa: E402
from revl.adapt import bridge_plan, compatible_total, CLAUSES  # noqa: E402


def _m(name, params, returns, *, emission=False, caps=None, async_=False,
       commutative=False):
    return MethodDecl(name, params, returns, emission, 0, async_=async_,
                      commutative=commutative, capabilities=caps)


def _svc(name, *methods):
    return ServiceDecl(name, {m.name: m for m in methods}, 0)


def _clauses(res):
    return {r.clause for r in res.refusals}


# ------------------------------------------------------------------ E1 / B1+B4

def test_e1_item_pair_adapts_with_total_waiver():
    req = _svc("Cache", _m("get", [("key", "Str")], "Opt[Str]"))
    prov = _svc("Vendor", _m("get", [("key", "Str"), ("options", "Options")],
                             "Result[Str, Error]"))
    prov_types = {"Options": {"kind": "record", "fields": {}},
                  "Error": {"kind": "record", "fields": {"code": "Str"}}}
    res = bridge_plan(req, prov, {"get": {"return": {"merge": "total"}}},
                      prov_types=prov_types)
    assert res.ok, res.refusals
    assert "merge-total" in res.merges
    steps = {s.position: s for s in res.methods[0].steps}
    assert steps["options"].transformation == "B1"      # empty record default
    assert steps["return"].merge_shape == "merge-total"


def test_e2_error_discard_without_optin_refused():
    req = _svc("Cache", _m("get", [("key", "Str")], "Opt[Str]"))
    prov = _svc("Vendor", _m("get", [("key", "Str")], "Result[Str, Error]"))
    prov_types = {"Error": {"kind": "record", "fields": {"code": "Str"}}}
    res = bridge_plan(req, prov, {}, prov_types=prov_types)
    assert not res.ok
    assert "outcome-merge" in _clauses(res)


# ------------------------------------------------------------------ E3 (S2/S3)

def test_e3a_emitting_candidate_behind_plain_requirement():
    req = _svc("R", _m("get", [("k", "Str")], "Str"))
    prov = _svc("P", _m("get", [("k", "Str")], "Str", emission=True,
                        caps=("net",)))
    res = bridge_plan(req, prov, {})
    assert "effect-missing-declaration" in _clauses(res)


def test_e3b_candidate_exceeds_declared_bound():
    req = _svc("R", _m("get", [("k", "Str")], "Str", emission=True, caps=("db",)))
    prov = _svc("P", _m("get", [("k", "Str")], "Str", emission=True,
                        caps=("db", "net")))
    res = bridge_plan(req, prov, {})
    cl = _clauses(res)
    assert "effect-exceeds-bound" in cl
    assert any("net" in r.reason for r in res.refusals)


def test_bare_emission_candidate_under_scoped_requirement_refused():
    req = _svc("R", _m("get", [("k", "Str")], "Str", emission=True, caps=("db",)))
    prov = _svc("P", _m("get", [("k", "Str")], "Str", emission=True, caps=None))
    res = bridge_plan(req, prov, {})
    assert "effect-exceeds-bound" in _clauses(res)


# ------------------------------------------------------------------ E4 (B3)

def test_e4_non_total_conversion_refused():
    # candidate wants Int32 where consumer sends Int
    req = _svc("R", _m("f", [("n", "Int")], "Str"))
    prov = _svc("P", _m("f", [("n", "Int32")], "Str"))
    res = bridge_plan(req, prov, {})
    assert "non-total-conversion" in _clauses(res)


def test_e4_wildcard_position_refused():
    req = _svc("R", _m("f", [("n", "Any")], "Str"))
    prov = _svc("P", _m("f", [("n", "Int")], "Str"))
    res = bridge_plan(req, prov, {})
    assert "non-total-conversion" in _clauses(res)


def test_compatible_total_nominal_resolution():
    rt = {"U": {"kind": "record", "fields": {"name": "Str", "age": "Int"}}}
    assert compatible_total("{name: Str, age: Int}", "U", e_types={}, a_types=rt)
    assert not compatible_total("{name: Str, age: Int}", "U",
                                e_types={}, a_types={})  # unresolved -> refuse
    assert not compatible_total("Value", "Int", e_types={}, a_types={})


# ------------------------------------------------------------------ E7 (B1)

def test_e7_absence_shaped_defaults_auto():
    req = _svc("R", _m("f", [], "Str"))
    prov = _svc("P", _m("f", [("o", "Opt[Str]"), ("xs", "List[Int]")], "Str"))
    res = bridge_plan(req, prov, {})
    assert res.ok, res.refusals


def test_e7_scalar_default_refused_then_explicit_admits():
    req = _svc("R", _m("f", [], "Str"))
    prov = _svc("P", _m("f", [("n", "Int")], "Str"))
    res = bridge_plan(req, prov, {})
    assert "no-canonical-default" in _clauses(res)
    res2 = bridge_plan(req, prov, {"f": {"default": {"n": "0"}}})
    assert res2.ok, res2.refusals


# ------------------------------------------------------------------ E9 (pairing)

def test_e9_arity_pairing_refuses_wrong_positional_bridge():
    # consumer log(message: Str); candidate log(category: Str, message: Opt[Str])
    req = _svc("R", _m("log", [("message", "Str")], None))
    prov = _svc("P", _m("log", [("category", "Str"), ("message", "Opt[Str]")],
                        None))
    res = bridge_plan(req, prov, {})
    # message pairs with message; category: Str has no canonical default
    assert "no-canonical-default" in _clauses(res)
    assert any(r.position == "category" for r in res.refusals)


def test_e9_explicit_default_for_category_admits():
    req = _svc("R", _m("log", [("message", "Str")], None))
    prov = _svc("P", _m("log", [("category", "Str"), ("message", "Opt[Str]")],
                        None))
    res = bridge_plan(req, prov, {"log": {"default": {"category": '"general"'}}})
    assert res.ok, res.refusals


def test_e9_ambiguous_pairing_refused():
    req = _svc("R", _m("f", [("x", "Str")], None))
    prov = _svc("P", _m("f", [("x", "Str"), ("x", "Str")], None))
    res = bridge_plan(req, prov, {})
    assert "ambiguous-pairing" in _clauses(res)


# ------------------------------------------------------------------ E10 (variant)

def _revocation(error_kind):
    req = _svc("Rev", _m("revoked", [("t", "Str")], "Opt[Instant]"))
    prov = _svc("Backend", _m("revoked", [("t", "Str")], "Result[Instant, Error]"))
    return req, prov, {"Error": error_kind}


_CLOSED = {"kind": "variant",
           "cases": [{"name": "NotFound", "payload": None},
                     {"name": "Unavailable", "payload": None}]}


def test_e10_blanket_waiver_over_closed_variant_refused():
    req, prov, pt = _revocation(_CLOSED)
    res = bridge_plan(req, prov, {"revoked": {"return": {"merge": "total"}}},
                      prov_types=pt)
    assert "outcome-merge" in _clauses(res)


def test_e10_partial_variant_map_names_unmapped():
    req, prov, pt = _revocation(_CLOSED)
    res = bridge_plan(
        req, prov,
        {"revoked": {"return": {"merge": {"NotFound": "None"}}}}, prov_types=pt)
    assert "unmapped-error-variant" in _clauses(res)
    assert any("Unavailable" in r.reason for r in res.refusals)


def test_e10_full_variant_map_admits_merge_variant():
    req, prov, pt = _revocation(_CLOSED)
    res = bridge_plan(
        req, prov,
        {"revoked": {"return": {"merge": {"NotFound": "None",
                                          "Unavailable": "None"}}}},
        prov_types=pt)
    assert res.ok, res.refusals
    assert "merge-variant" in res.merges


def test_e10_opaque_error_total_waiver_admits():
    req, prov, pt = _revocation({"kind": "record", "fields": {"m": "Str"}})
    res = bridge_plan(req, prov, {"revoked": {"return": {"merge": "total"}}},
                      prov_types=pt)
    assert res.ok, res.refusals
    assert "merge-total" in res.merges


# ------------------------------------------------------------------ E11 (color)

def test_e11_sync_candidate_under_async_admits():
    req = _svc("R", _m("f", [], "Str", async_=True))
    prov = _svc("P", _m("f", [], "Str", async_=False))
    assert bridge_plan(req, prov, {}).ok


def test_e11_async_candidate_under_sync_refused():
    req = _svc("R", _m("f", [], "Str", async_=False))
    prov = _svc("P", _m("f", [], "Str", async_=True))
    assert "color-mismatch" in _clauses(bridge_plan(req, prov, {}))


def test_e11_commutative_requirement_over_noncommutative_refused():
    req = _svc("R", _m("f", [], "Str", commutative=True))
    prov = _svc("P", _m("f", [], "Str", commutative=False))
    assert "commutative-mismatch" in _clauses(bridge_plan(req, prov, {}))


def test_e11_commutative_candidate_under_plain_admits():
    req = _svc("R", _m("f", [], "Str", commutative=False))
    prov = _svc("P", _m("f", [], "Str", commutative=True))
    assert bridge_plan(req, prov, {}).ok


# ------------------------------------------------------------------ misc

def test_method_missing_refused():
    req = _svc("R", _m("get", [("k", "Str")], "Str"))
    prov = _svc("P", _m("fetch", [("k", "Str")], "Str"))
    assert "method-missing" in _clauses(bridge_plan(req, prov, {}))


def test_supplied_value_drop_needs_optin():
    req = _svc("R", _m("f", [("k", "Str"), ("extra", "Str")], None))
    prov = _svc("P", _m("f", [("k", "Str")], None))
    res = bridge_plan(req, prov, {})
    assert "supplied-value-dropped" in _clauses(res)
    res2 = bridge_plan(req, prov, {"f": {"drop": ["extra"]}})
    assert res2.ok, res2.refusals


def test_unnameable_reach_refused():
    req = _svc("R", _m("f", [], "Str", emission=True, caps=("*",)))
    prov = _svc("P", _m("f", [], "Str", emission=True, caps=("db",)))
    assert "unnameable-reach" in _clauses(bridge_plan(req, prov, {}))


def test_record_projection_auto():
    req = _svc("R", _m("f", [], "{name: Str}"))
    prov = _svc("P", _m("f", [], "{name: Str, age: Int}"))
    res = bridge_plan(req, prov, {})
    assert res.ok, res.refusals
    assert res.methods[0].steps[-1].transformation == "B6"


def test_all_refusal_clauses_are_in_the_closed_enum():
    # every clause a refusal can name is a member of CLAUSES (a closed enum)
    req = _svc("R", _m("get", [("k", "Str")], "Str"))
    prov = _svc("P", _m("fetch", [("k", "Str")], "Str"))
    for r in bridge_plan(req, prov, {}).refusals:
        assert r.clause in CLAUSES
