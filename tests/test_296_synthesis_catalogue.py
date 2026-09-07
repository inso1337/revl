"""Item 296, slice 2 completion + slice 4 standing proof.

Slice 2 landed `render_adapter` for the flagship shapes only (emission/plain
passthrough, B1 trailing defaults, B4 identity, B4 `Result[V,E] -> Opt[V]`
merge). Every other return shape the PREDICATE (`bridge_plan`) admits fell
through to the identity passthrough `fn m(..) = backing.m(..)`, which for a
head-changing return (`Opt[V] -> Result[V,E]`, `Opt[V] -> V`, an explicit
`Result[V,E1] -> Result[V,E2]` error map, a B6 record projection) emits source
the gate rejects -- silently, in violation of section 4's contract ("a shape
this renderer does not cover raises so it is never emitted as wrong source").

This module (1) pins the twin re-admit for each newly-synthesized return shape,
and (2) lands the slice-4 GENERATIVE DICHOTOMY standing proof (design section 7
slice 4, section 8): over a matrix of declaration pairs -- including
nominal-vs-structural surfaces, wildcard (`Any`/`Value`) positions and
closed-variant error types -- every pair either yields a REFUSAL, or an adapter
that the renderer declines to spell (`ValueError`), or a synthesized artifact
the ORDINARY GATE ACCEPTS. The forbidden third outcome -- the predicate admits
and the renderer emits source the gate rejects -- is a predicate/gate
disagreement and a bug; the sweep is the standing proof it does not happen.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.parser import MethodDecl, ServiceDecl        # noqa: E402
from revl.adapt import bridge_plan, render_adapter, CLAUSES  # noqa: E402
from revl.compiler import compile_source                # noqa: E402


# ------------------------------------------------------------------- fixtures

# The shared type universe both sides resolve against: an opaque record error,
# a closed variant error, and a superset/subset record pair for B6.
_PREAMBLE = (
    "type Error = { code: Str }\n"
    "type Verr = NotFound | Unavailable\n"
    "type Big = { id: Int, name: Str, extra: Str }\n"
    "type Small = { id: Int, name: Str }\n"
)

_TYPES = {
    "Error": {"kind": "record", "fields": {"code": "Str"}},
    "Verr": {"kind": "variant",
             "cases": [{"name": "NotFound", "payload": None},
                       {"name": "Unavailable", "payload": None}]},
    "Big": {"kind": "record",
            "fields": {"id": "Int", "name": "Str", "extra": "Str"}},
    "Small": {"kind": "record", "fields": {"id": "Int", "name": "Str"}},
}


def _m(name, params, returns):
    return MethodDecl(name, params, returns, False, 0)


def _svc(name, returns, params=(("key", "Str"),)):
    return ServiceDecl(name, {"get": _m("get", list(params), returns)}, 0)


def _svc_src(name, returns, params=(("key", "Str"),)):
    ps = ", ".join(f"{n}: {t}" for n, t in params)
    return f"service {name} {{ fn get({ps}) -> {returns} }}\n"


def _admits_and_compiles(rret, pret, opt, *, rparams=(("key", "Str"),),
                         pparams=(("key", "Str"),)):
    """Render the adapter for this pair and compile it through the ordinary
    gate. Returns the rendered source; asserts the plan admits and the gate
    accepts the artifact."""
    req = _svc("Req", rret, rparams)
    prov = _svc("Prov", pret, pparams)
    plan = bridge_plan(req, prov, opt, req_types=_TYPES, prov_types=_TYPES)
    assert plan.ok, (rret, pret, opt, plan.refusals)
    src = render_adapter("Adapter", req, prov, opt, provide_key="k",
                         require_key="backing", prov_types=_TYPES,
                         req_types=_TYPES)
    full = (_PREAMBLE + _svc_src("Prov", pret, pparams)
            + _svc_src("Req", rret, rparams) + src)
    ir = compile_source(full, "committed.rvl")
    assert "Adapter" in {c["name"] for c in ir["components"]}
    return src


# ------------------------------------------- newly-synthesized catalogue rows
# Each of these ADMITTED at the predicate before this slice, and rendered
# `fn get(key) = backing.get(key)` -- source the gate rejects. They now render
# the real bridge and re-admit.

def test_opt_to_result_fabrication_readmits():
    # B4 `Opt[V] -> Result[V, E]`: fabricate an error for `None` (opt-in).
    src = _admits_and_compiles(
        "Result[Str, Error]", "Opt[Str]",
        {"get": {"return": {"on_none": 'Err({code: "ENOENT"})'}}})
    assert "None => Err(" in src
    assert "Some(v) => Ok(v)" in src


def test_opt_to_value_fabrication_readmits():
    # B4 `Opt[V] -> V`: send `None` to an explicit value (opt-in).
    src = _admits_and_compiles(
        "Str", "Opt[Str]", {"get": {"return": {"on_none": '"fallback"'}}})
    assert 'None => "fallback"' in src
    assert "Some(v) => v" in src


def test_result_error_map_readmits_opaque():
    # B4 `Result[V, E1] -> Result[V, E2]` explicit error map (opt-in).
    src = _admits_and_compiles(
        "Result[Str, Error]", "Result[Str, Verr]",
        {"get": {"return": {"err_map": 'Err({code: "boom"})'}}})
    assert "Ok(v) => Ok(v)" in src
    assert "Err(e) =>" in src


def test_result_error_map_readmits_per_variant():
    # A per-variant `Err` map renders one arm per named variant.
    src = _admits_and_compiles(
        "Result[Str, Verr]", "Result[Str, Error]",
        {"get": {"return": {"err_map": {"e": "Err(NotFound)"}}}})
    assert "Err(e) => Err(NotFound)" in src


def test_record_projection_readmits_auto():
    # B6 return projection: the candidate's `Big` projects onto `Small`; the
    # unobserved `extra` is dropped (S4b), no opt-in needed.
    src = _admits_and_compiles("Small", "Big", {})
    assert "let r = backing.get(key)" in src
    assert "id: r.id" in src and "name: r.name" in src
    assert "extra" not in src


def test_record_projection_readmits_with_fabricated_field():
    # B6 return: the consumer needs `extra` the candidate lacks -> opt-in
    # fabrication renders the pure field expression.
    src = _admits_and_compiles(
        "Big", "Small", {"get": {"return": {"fabricate": {"extra": '"n/a"'}}}})
    assert 'extra: "n/a"' in src
    assert "id: r.id" in src


def test_per_variant_merge_readmits():
    # The closed-variant outcome-merge (E10 admit): every variant mapped.
    src = _admits_and_compiles(
        "Opt[Str]", "Result[Str, Verr]",
        {"get": {"return": {"merge": {"NotFound": "None",
                                      "Unavailable": "None"}}}})
    assert "Err(NotFound) => None" in src
    assert "Err(Unavailable) => None" in src


# --------------------------------------------------------- the twin (E5-style)
# The synthesized adapter and a byte-identical hand-written component admit the
# same way: nothing in the gate learns what an adapter is.

def test_synthesized_body_is_byte_identical_to_a_handwritten_wrapper():
    req = _svc("Req", "Result[Str, Error]")
    prov = _svc("Prov", "Opt[Str]")
    opt = {"get": {"return": {"on_none": 'Err({code: "ENOENT"})'}}}
    src = render_adapter("Adapter", req, prov, opt, provide_key="k",
                         require_key="backing", prov_types=_TYPES,
                         req_types=_TYPES)
    # the same component, hand-written verbatim, is the same source and admits.
    handwritten = _PREAMBLE + _svc_src("Prov", "Opt[Str]") + \
        _svc_src("Req", "Result[Str, Error]") + src
    ir = compile_source(handwritten, "twin.rvl")
    assert "Adapter" in {c["name"] for c in ir["components"]}
    # the bridge is visible in the source a reviewer reads (proposed-not-silent)
    assert "match backing.get(key)" in src


# ------------------------------------------------ slice 4: the dichotomy proof

# Nominal-vs-structural surfaces, wildcard positions, closed-variant errors, and
# the numeric/optional coercions -- the exact surfaces where the permissive
# `compatible` and the restricted `compatible_total` disagree (design slice 4).
_RETURNS = [
    "Str", "Int", "Float", "Opt[Str]",
    "Result[Str, Error]", "Result[Str, Verr]",
    "Big", "Small", "Any", "Value",
]


def _compile_error(src):
    try:
        compile_source(src, "dichotomy.rvl")
        return None
    except Exception as exc:                       # noqa: BLE001 - proof needs it
        return str(exc)


def test_dichotomy_auto_sweep_never_disagrees():
    """opt={} over every return pair: the truly generative half. With no
    author expression to mistype, an admitted plan must render source the gate
    accepts (or the renderer must decline it) -- never wrong source."""
    admitted = refused = declined = 0
    for rret in _RETURNS:
        for pret in _RETURNS:
            req = _svc("Req", rret)
            prov = _svc("Prov", pret)
            plan = bridge_plan(req, prov, {}, req_types=_TYPES,
                               prov_types=_TYPES)
            if not plan.ok:
                refused += 1
                clauses = {r.clause for r in plan.refusals}
                assert clauses, (rret, pret, "refusal names no clause")
                assert clauses <= CLAUSES, (rret, pret, clauses - CLAUSES)
                continue
            try:
                src = render_adapter("Adapter", req, prov, {}, provide_key="k",
                                     require_key="backing", prov_types=_TYPES,
                                     req_types=_TYPES)
            except ValueError:
                declined += 1
                continue
            full = (_PREAMBLE + _svc_src("Prov", pret)
                    + _svc_src("Req", rret) + src)
            err = _compile_error(full)
            assert err is None, (
                f"DICHOTOMY VIOLATION: bridge_plan admitted `{pret}` -> `{rret}`"
                f" but the gate rejected the synthesized adapter:\n{src}\n{err}")
            admitted += 1
    # the sweep is not vacuous: it exercises real admits and real refusals.
    assert admitted >= 8, admitted
    assert refused >= 8, refused


def _well_typed_optins(rret, pret):
    """Target-appropriate, well-typed opt-in maps to try for a pair. A wrong
    opt-in (e.g. a per-variant map over an opaque error) simply refuses, which
    the invariant still covers; the point is that no entry mistypes the RETURN,
    so an admitted plan's only way to fail the gate is a real disagreement."""
    outs = []
    if rret == "Opt[Str]" and pret.startswith("Result"):
        outs.append({"return": {"merge": "total"}})
        outs.append({"return": {"merge": {"NotFound": "None",
                                          "Unavailable": "None"}}})
    if rret.startswith("Result") and pret == "Opt[Str]":
        err = 'Err({code: "x"})' if "Error" in rret else "Err(NotFound)"
        outs.append({"return": {"on_none": err}})
    if rret in ("Str",) and pret == "Opt[Str]":
        outs.append({"return": {"on_none": '"x"'}})
    if rret.startswith("Result") and pret.startswith("Result") and rret != pret:
        err = 'Err({code: "x"})' if "Error" in rret else "Err(NotFound)"
        outs.append({"return": {"err_map": err}})
    if rret == "Big" and pret == "Small":
        outs.append({"return": {"fabricate": {"extra": '"x"'}}})
    return [{"get": o} for o in outs]


def test_dichotomy_optin_matrix_never_disagrees():
    """The opt-in half: for each pair, every target-appropriate opt-in either
    refuses or renders an artifact the gate accepts."""
    admitted = 0
    for rret in _RETURNS:
        for pret in _RETURNS:
            for opt in _well_typed_optins(rret, pret):
                req = _svc("Req", rret)
                prov = _svc("Prov", pret)
                plan = bridge_plan(req, prov, opt, req_types=_TYPES,
                                   prov_types=_TYPES)
                if not plan.ok:
                    continue
                try:
                    src = render_adapter("Adapter", req, prov, opt,
                                         provide_key="k", require_key="backing",
                                         prov_types=_TYPES, req_types=_TYPES)
                except ValueError:
                    continue
                full = (_PREAMBLE + _svc_src("Prov", pret)
                        + _svc_src("Req", rret) + src)
                err = _compile_error(full)
                assert err is None, (
                    f"DICHOTOMY VIOLATION: admitted `{pret}` -> `{rret}` "
                    f"opt={opt} but the gate rejected it:\n{src}\n{err}")
                admitted += 1
    assert admitted >= 5, admitted
