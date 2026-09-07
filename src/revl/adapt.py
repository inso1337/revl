"""Roadmap item 296, slice 1: the safe adapter-synthesis predicate.

`bridge_plan(required, provided, opt_ins, ...)` reads a consumer's required
`ServiceDecl` and a candidate's provided `ServiceDecl` and either produces a
per-method bridge plan (from which synthesis is deterministic) or a list of
named refusals. It reads only the two declarations and the author-supplied
`adapt` opt-in map `D`; it never reads a body. This is the pure predicate of
docs/design/296-adapter-synthesis.md section 2; synthesis, the gate change
(alias token carry-over), and the resolver surface ride on top.

The predicate is decidable and syntax-directed. Every position uses exactly
one catalogue transformation (section 2.2) and the whole method must satisfy
the five global clauses S1..S5 (section 2.1). One failing position refuses the
whole adapter, with the position named from a closed clause enum (section 5).

`compatible_total` (section 2.2, B3) is the RESTRICTED, table-carrying
subrelation of `typecheck.compatible` the bridges use: it excludes the
deliberately permissive rules of `compatible` (wildcards, `Value`, and the
structural-meets-nominal rule that returns True only because no type table is
present). A permissive True at a bridged position would prove nothing, so those
positions refuse (`non-total-conversion`).

Slice 2 landed the alias-token-carry gate change (see parser `require_carry` /
emission_analysis) and IR-level synthesis (`render_adapter`), now COMPLETE for
every catalogue return shape the predicate admits, raising rather than emitting
wrong source for any it cannot spell (section 4's contract). Slice 3 landed the
resolver surface (`registry.resolve` reports `compatible-with-adapter` below
direct-compatible, with chain depth and the outcome-merge evidence discount) on
the `adapter_marking` header this module renders, `revl adapt --check` chain
FLATTENING (`flatten_committed_hop`, section 6.4), and the `federation.check`
satisfied-via-adapter pin. Slice 4 landed the generative DICHOTOMY standing
proof (`tests/test_296_synthesis_catalogue.py`): the predicate and the gate
never disagree, AND the E6 414-matrix rows (`tests/test_296_folds_see_through.py`):
every in-scope authority-derivation surface attributes the CANDIDATE's real
emission through the adapter hop (§6.3). Closing E6 fixed one real
completeness gap: the approval `ClassMap` fold classed an `emit alias.method`
crossing by the internal alias key, not the candidate's boundary. The
`carrying(...)` binding is now carried in the component IR (additive) and the
fold resolves the crossing through it (with the launder-safety `*`), exactly as
the checker's G4 attribution does. TODO(296-slice3, remaining): `revl diff`
(item 123) shows the adapter as an added bridge component - a projection of the
already-committed adapter into the 123 diff surface, tracked on item 123.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .typecheck import parse_type, structural_fields

# The catalogue version pins the predicate's behavior into every derivation
# hash (section 4): a change here is a change of adapter identity.
CATALOGUE_VERSION = "catalogue-v1"

# The closed clause enum (section 5). A refusal names exactly one of these.
CLAUSES: frozenset[str] = frozenset({
    "no-canonical-default",
    "ambiguous-pairing",
    "supplied-value-dropped",
    "outcome-merge",
    "unmapped-error-variant",
    "fabricated-return",
    "non-total-conversion",
    "effect-missing-declaration",
    "effect-exceeds-bound",
    "color-mismatch",
    "commutative-mismatch",
    "method-missing",
    "unnameable-reach",
})

# The absence-shaped default table (section 2.2, B1). A canonical inhabitant
# exists ONLY for these heads; a scalar has no canonical default and is refused
# in auto (`no-canonical-default`). An empty/all-defaultable structural record is
# handled structurally below.
_ABSENCE_DEFAULT = {
    "Opt": "None",
    "List": "[]",
    "Map": "{}",
}


@dataclass(frozen=True)
class Refusal:
    """One reason a bridge is not synthesizable, mirroring `admission._Drift`.

    `position` is a parameter name, `"return"`, or a record field path; `clause`
    is a member of `CLAUSES`."""
    method: str | None
    position: str | None
    transformation: str | None
    clause: str
    reason: str
    hint: str = ""

    def __post_init__(self) -> None:
        assert self.clause in CLAUSES, f"unknown refusal clause {self.clause!r}"


@dataclass(frozen=True)
class Step:
    """One position's transformation in a method's bridge plan."""
    position: str            # a parameter name, "return", or a field path
    transformation: str      # B1..B6 catalogue row
    detail: str = ""         # human summary for the emitted comment
    # `merge-variant` (a per-variant arm map) or `merge-total` (the opaque-E
    # waiver), recorded so the 123 diff and 127 attestation can distinguish
    # "merged NotFound" from "merged everything" (section 5).
    merge_shape: str | None = None


@dataclass(frozen=True)
class MethodPlan:
    method: str
    steps: tuple[Step, ...]
    # the candidate call, `backing.<call_name>(args...)`, for the emitter
    call_name: str = ""


@dataclass(frozen=True)
class BridgeResult:
    """A plan (whole-or-nothing) or a refusal list. `ok` iff synthesizable."""
    ok: bool
    methods: tuple[MethodPlan, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    # the outcome-merge shapes present, so a caller can discount error-semantics
    # evidence (section 6.1) without re-reading the arms.
    merges: tuple[str, ...] = ()


# ---------------------------------------------------- item 274: navigable map
#
# The adapter refusal is already navigable in its OWN shape (a closed clause enum
# per position). 274's work is a projection into the SHARED `navigate` record
# (design §2.7), NOT a second decision. A REPAIRABLE clause (a missing default, an
# unmapped Err variant, an ambiguous pairing, a dropped value, an outcome merge)
# becomes one author-enacted `candidate` alternative naming the concrete edit. A
# clause whose only "fix" would EXPAND capability (an unbounded/uncovered reach)
# is TERMINAL: no alternative is emitted for it, because suggesting a capability
# expansion is exactly the unsafe hint this design forbids (§2.7, the never-
# unsafe test). When no repairable clause survives, the record is `blocked`.

# clauses whose repair would add authority, or that the adapter simply cannot
# synthesize — never an author-enactable adapter alternative.
_ADAPTER_TERMINAL: frozenset[str] = frozenset({
    "effect-missing-declaration", "effect-exceeds-bound", "unnameable-reach",
    "color-mismatch", "commutative-mismatch", "fabricated-return",
    "non-total-conversion", "method-missing",
})


def navigate_for_refusals(refusals, profile=None) -> dict:
    """Project a `Refusal` list into one shared `navigate` record (family
    `adapter`). Repairable clauses become author `candidate` alternatives; a
    capability-expanding or otherwise-terminal clause contributes no alternative
    (never a capability-expansion suggestion). `blocked` iff nothing repairable
    survives. Collapses to the untrusted view like any other family."""
    from . import navigate as nav  # noqa: PLC0415 — lazy, avoids a cycle
    alts = []
    for r in refusals:
        if r.clause in _ADAPTER_TERMINAL:
            continue
        action = (r.hint.strip() or r.reason.strip()
                  or f"repair the `{r.clause}` position")
        where = f"{r.method or '?'}:{r.position or r.clause}"
        alts.append(nav.alternative(
            enacts=nav.ENACTS_AUTHOR, action=action, ref=where))
    if not alts:
        first = refusals[0] if refusals else None
        reason = (first.reason if first is not None
                  else "the adapter is not synthesizable")
        return nav.blocked_record(family="adapter", reason=reason,
                                  profile=profile)
    return nav.record(family="adapter", blocked=False, alternatives=alts,
                      profile=profile)


# ---------------------------------------------------------------- type helpers


def _resolve_struct(type_name: str | None,
                    types: dict) -> dict[str, str | None] | None:
    """The field set of a structural or nominal record, or None if it is not a
    record (or cannot be resolved). A nominal is expanded through `types`;
    an unresolvable nominal returns None so the caller refuses rather than
    presumes (section 2.2)."""
    sfields = structural_fields(type_name)
    if sfields is not None:
        return sfields
    head, _ = parse_type(type_name)
    if not head:
        return None
    entry = types.get(head)
    if entry is None:
        return None
    if entry.get("kind") == "record":
        return dict(entry.get("fields") or {})
    if entry.get("kind") == "alias":
        return _resolve_struct(entry.get("target"), types)
    return None


def _variant_cases(type_name: str | None, types: dict) -> list[str] | None:
    """The constructor names of a CLOSED variant type `E`, or None when `E` is
    opaque (a scalar, a record, an alias to a non-variant, or unresolvable).
    A closed variant demands per-variant error mapping (section 2.1, S4c)."""
    head, _ = parse_type(type_name)
    if not head:
        return None
    entry = types.get(head)
    if entry is None:
        return None
    if entry.get("kind") == "variant":
        return [c.get("name") for c in entry.get("cases") or []]
    if entry.get("kind") == "alias":
        return _variant_cases(entry.get("target"), types)
    return None


# Positions where `compatible` is permissive on purpose and a bridge must not
# be: a wildcard or the erased-dynamic `Value` proves nothing at a bridged
# position (section 2.2, "The restriction exists because...").
_WILDCARD_HEADS = frozenset({"Any", "Never", "Value"})


def _is_wildcardish(type_name: str | None) -> bool:
    head, _ = parse_type(type_name)
    if head is None:
        return True
    if head in _WILDCARD_HEADS:
        return True
    # an implicit inference type parameter (`?T`) or poison
    return head.startswith("?") or head.startswith("!")


def compatible_total(expected: str | None, actual: str | None, *,
                     e_types: dict, a_types: dict) -> bool:
    """The restricted, table-carrying subrelation of `typecheck.compatible`
    (section 2.2, B3). Returns True only for TOTAL, value-directed coercions:

    * identity on equal resolved types;
    * `Int -> Float`, `Int32 -> Int`, `Int32 -> Float`;
    * `T -> Opt[T]` injection;
    * structural records with equal field sets, elementwise, nominals on
      EITHER side resolved to their field sets first (unresolvable refuses);
    * same-head containers, elementwise;
    * nothing else.

    A wildcard / `Value` / unresolved-nominal position returns False (the caller
    refuses `non-total-conversion`). `e_types`/`a_types` are the two sides' type
    tables, so a nominal is expanded before comparison (section 2.2)."""
    # wildcard/Value at either side proves nothing -> not a total conversion
    if _is_wildcardish(expected) or _is_wildcardish(actual):
        return False
    if expected == actual:
        return True

    e_struct = _resolve_struct(expected, e_types)
    a_struct = _resolve_struct(actual, a_types)
    # If exactly one side is a record and the other is a plain nominal that did
    # not resolve to a record, refuse (the structural-meets-nominal permissive
    # rule of `compatible` is exactly what we exclude here).
    if (e_struct is None) != (a_struct is None):
        # one is a record shape, the other is not resolvable to one
        e_head, _ = parse_type(expected)
        a_head, _ = parse_type(actual)
        # allow the ordinary same-head container / scalar path below only when
        # neither side is a record; here one side IS a record, so refuse.
        return False
    if e_struct is not None and a_struct is not None:
        if set(e_struct) != set(a_struct):
            return False
        return all(
            compatible_total(e_struct[k], a_struct[k],
                             e_types=e_types, a_types=a_types)
            for k in e_struct)

    ehead, eargs = parse_type(expected)
    ahead, aargs = parse_type(actual)
    # numeric widenings, total by construction
    if ehead == "Float" and ahead == "Int":
        return True
    if ehead in ("Int", "Float") and ahead == "Int32":
        return True
    # T -> Opt[T] injection
    if ehead == "Opt":
        einner = eargs[0] if eargs else None
        if ahead == "Opt":
            return compatible_total(einner, aargs[0] if aargs else None,
                                    e_types=e_types, a_types=a_types)
        return compatible_total(einner, actual,
                                e_types=e_types, a_types=a_types)
    # same-head container, elementwise
    if ehead == ahead and len(eargs) == len(aargs) and eargs:
        return all(
            compatible_total(e, a, e_types=e_types, a_types=a_types)
            for e, a in zip(eargs, aargs))
    return False


# ---------------------------------------------------------------- S2 clauses


def _cap_refusal(method: str, required, provided) -> Refusal | None:
    """S2 effect-class checks over two `MethodDecl`s, from their declared
    classification alone (section 2.1). Token comparison here is by declared
    set; the JOINT-wiring resolution (294 valuations) is applied by the gate at
    admission (alias token carry-over) and is TODO(296-slice2) at the predicate
    level.
    """
    # emission implication: an emitting candidate never hides behind a plain
    # requirement.
    if provided.emission and not required.emission:
        return Refusal(
            method, "return", None, "effect-missing-declaration",
            f"candidate `{method}` is an emission but the requirement is plain",
            hint="an adapter never hides a boundary crossing; require "
                 "`emission` at the call site, or pick a plain candidate")
    if required.emission and provided.emission:
        req_caps = required.capabilities
        prov_caps = provided.capabilities
        # bare `emission` requirement (None) is the widest bound -> admits any
        # candidate reach. A scoped requirement bounds the candidate.
        if req_caps is not None:
            if prov_caps is None:
                return Refusal(
                    method, "return", None, "effect-exceeds-bound",
                    f"candidate `{method}` is bare `emission` (unbounded) but "
                    f"the requirement is `emission[{', '.join(req_caps)}]`",
                    hint="an unbounded candidate cannot fit a scoped "
                         "requirement; the adapter never widens the declaration")
            extra = [c for c in prov_caps if c not in set(req_caps)]
            if extra:
                return Refusal(
                    method, "return", None, "effect-exceeds-bound",
                    f"candidate `{method}` reaches "
                    f"`[{', '.join(prov_caps)}]`, outside the required "
                    f"`[{', '.join(req_caps)}]` ({', '.join(extra)} uncovered)",
                    hint=f"widen the required declaration to include "
                         f"{', '.join(extra)} if the consumer accepts that "
                         f"boundary, or pick a candidate that does not reach it. "
                         f"An adapter never adds authority")
    # color: async(p) implies async(m). A sync candidate under an async
    # requirement is admissible (purer body); the reverse fabricates a promise.
    if provided.async_ and not required.async_:
        return Refusal(
            method, "return", None, "color-mismatch",
            f"candidate `{method}` is `async` but the requirement is sync",
            hint="an async candidate cannot hide behind a sync requirement "
                 "(the call shape would change); require `async` or pick a "
                 "sync candidate")
    # commutativity: commutative(m) implies commutative(p). A commutative
    # requirement is never satisfied by a candidate that made no promise.
    if required.commutative and not provided.commutative:
        return Refusal(
            method, "return", None, "commutative-mismatch",
            f"the requirement `{method}` is `commutative` but the candidate "
            f"made no reordering promise",
            hint="a reordering promise cannot be fabricated; drop the "
                 "`commutative` requirement or pick a commutative candidate")
    return None


# ---------------------------------------------------------------- pairing (B1)


def _pair_params(method: str, req_params: list, prov_params: list,
                 explicit: dict) -> tuple[dict[int, int], list[int], Refusal | None]:
    """Name-pair the consumer's parameters onto the candidate's (section 2.2,
    B1 pairing). Returns `(req_index -> prov_index, unpaired_prov_indices,
    refusal)`. Positional order is never evidence of correspondence under arity
    change: pairing is by NAME, an explicit `D` mapping overrides, and an
    ambiguous same-typed pairing refuses rather than resolving by position."""
    prov_by_name: dict[str, list[int]] = {}
    for j, (pn, _pt) in enumerate(prov_params):
        prov_by_name.setdefault(pn, []).append(j)
    pairing: dict[int, int] = {}
    used_prov: set[int] = set()
    for i, (rn, _rt) in enumerate(req_params):
        target = explicit.get(rn)
        if target is not None:
            js = [j for j, (pn, _pt) in enumerate(prov_params) if pn == target]
            if not js:
                return {}, [], Refusal(
                    method, rn, "B1", "ambiguous-pairing",
                    f"explicit pairing sends `{rn}` to `{target}`, which the "
                    f"candidate `{method}` does not declare", hint="")
            pairing[i] = js[0]
            used_prov.add(js[0])
            continue
        js = prov_by_name.get(rn, [])
        if len(js) > 1:
            return {}, [], Refusal(
                method, rn, "B1", "ambiguous-pairing",
                f"consumer parameter `{rn}` matches more than one candidate "
                f"parameter of `{method}`",
                hint="name the pairing explicitly in the `adapt` declaration")
        if js:
            pairing[i] = js[0]
            used_prov.add(js[0])
    unpaired = [j for j in range(len(prov_params)) if j not in used_prov]
    return pairing, unpaired, None


# ---------------------------------------------------------------- the predicate


def _method_bridge(mname: str, req, prov, opt: dict, *,
                   req_types: dict, prov_types: dict) -> tuple[list[Step], list[Refusal]]:
    """Bridge one method. Returns `(steps, refusals)`; a non-empty refusal list
    means the whole adapter is refused (section 2.3, whole-or-nothing)."""
    steps: list[Step] = []
    refusals: list[Refusal] = []

    # S2: effect class, up front, to refuse early with a resolution-time
    # message. The extended gate would catch a violation regardless (section
    # 2.1). `*` is never bridgeable (section 2.4).
    if req.capabilities and "*" in req.capabilities:
        refusals.append(Refusal(
            mname, "return", None, "unnameable-reach",
            "the requirement names `*`, an unnameable reach", hint=""))
    if prov.capabilities and "*" in prov.capabilities:
        refusals.append(Refusal(
            mname, "return", None, "unnameable-reach",
            f"candidate `{mname}` reaches `*`, an unnameable boundary", hint=""))
    cap_ref = _cap_refusal(mname, req, prov)
    if cap_ref is not None:
        refusals.append(cap_ref)

    # ---- parameters: pairing (B1) then per-position bridge (B3/B1/B2)
    explicit_pairing = (opt.get("pairing") or {})
    pairing, unpaired, pref = _pair_params(
        mname, req.params, prov.params, explicit_pairing)
    if pref is not None:
        refusals.append(pref)
    else:
        # each consumer parameter must bridge (contravariantly) into its paired
        # candidate parameter under compatible_total (B3), unless dropped (B2).
        drops = set(opt.get("drop") or [])
        for i, (rn, rt) in enumerate(req.params):
            if i in pairing:
                j = pairing[i]
                pn, pt = prov.params[j]
                if not compatible_total(pt, rt, e_types=prov_types,
                                        a_types=req_types):
                    refusals.append(Refusal(
                        mname, rn, "B3", "non-total-conversion",
                        f"parameter `{rn}`: `{rt}` does not totally convert to "
                        f"the candidate's `{pt}`",
                        hint="only the total implicit coercions bridge a "
                             "parameter; spell a lossy conversion by hand"))
                else:
                    steps.append(Step(rn, "B3", f"`{rt}` -> `{pt}`"))
            elif rn in drops:
                # B2: consumer-supplied argument dropped, opt-in per parameter.
                steps.append(Step(rn, "B2", "supplied argument dropped (opt-in)"))
            else:
                # a consumer parameter with no candidate home and no drop opt-in
                refusals.append(Refusal(
                    mname, rn, "B2", "supplied-value-dropped",
                    f"consumer parameter `{rn}` has no candidate parameter and "
                    f"no `drop` opt-in",
                    hint=f"the candidate ignores `{rn}`; opt in with "
                         f"`drop {rn}` to make the discard auditable"))
        # each unpaired candidate parameter must be defaultable (B1)
        explicit_defaults = (opt.get("default") or {})
        for j in unpaired:
            pn, pt = prov.params[j]
            if pn in explicit_defaults:
                steps.append(Step(pn, "B1",
                                  f"defaulted to `{explicit_defaults[pn]}` "
                                  f"(explicit)"))
                continue
            default = _canonical_default(pt, prov_types)
            if default is None:
                refusals.append(Refusal(
                    mname, pn, "B1", "no-canonical-default",
                    f"candidate parameter `{pn}: {pt}` is unpaired and has no "
                    f"absence-shaped default",
                    hint=f"only `Opt`/`List`/`Map`/empty-record default "
                         f"automatically; supply an explicit default for `{pn}` "
                         f"in the `adapt` declaration if absence is not meant"))
            else:
                steps.append(Step(pn, "B1",
                                  f"defaulted to `{default}` (auto, "
                                  f"absence-shaped)"))

    # ---- return: B4 / B6
    rret, pret = req.returns, prov.returns
    ret_opt = opt.get("return") or {}
    _bridge_return(mname, rret, pret, ret_opt, req_types, prov_types,
                   steps, refusals)
    return steps, refusals


def _canonical_default(type_name: str | None, types: dict) -> str | None:
    """The absence-shaped canonical inhabitant of a type, or None (section
    2.2, B1 defaulting). A structural/nominal record is defaultable iff it is
    empty or every field is defaultable."""
    head, _ = parse_type(type_name)
    if head in _ABSENCE_DEFAULT:
        return _ABSENCE_DEFAULT[head]
    struct = _resolve_struct(type_name, types)
    if struct is not None:
        if not struct:
            return "{}"
        field_defaults = {}
        for fname, ftype in struct.items():
            d = _canonical_default(ftype, types)
            if d is None:
                return None
            field_defaults[fname] = d
        body = ", ".join(f"{k}: {field_defaults[k]}" for k in field_defaults)
        return "{" + body + "}"
    return None


def _bridge_return(mname: str, rret: str | None, pret: str | None,
                   ret_opt: dict, req_types: dict, prov_types: dict,
                   steps: list[Step], refusals: list[Refusal]) -> None:
    """B4/B6 over the return position (section 2.2)."""
    if rret is None or pret is None or rret == pret:
        if rret == pret:
            steps.append(Step("return", "B4", "identity"))
        return
    rhead, rargs = parse_type(rret)
    phead, pargs = parse_type(pret)

    # B4 auto: compatible_total covers identity, numeric widenings, V->Opt[V]
    # injection, resolved structural equality.
    if compatible_total(rret, pret, e_types=req_types, a_types=prov_types):
        steps.append(Step("return", "B4", f"`{pret}` -> `{rret}` (total)"))
        return

    # B4 Result[V, E] -> Opt[V]: the outcome-merge position (S4c).
    if rhead == "Opt" and phead == "Result":
        pv = pargs[0] if pargs else None
        pe = pargs[1] if len(pargs) > 1 else None
        rv = rargs[0] if rargs else None
        # the Ok value must totally convert V -> the consumer's Opt inner
        if not compatible_total(rv, pv, e_types=req_types, a_types=prov_types):
            refusals.append(Refusal(
                mname, "return", "B4", "non-total-conversion",
                f"`Result[{pv}, {pe}]` bridges to `Opt[{rv}]` only if the value "
                f"`{pv}` totally converts to `{rv}`", hint=""))
            return
        _bridge_error_merge(mname, pe, ret_opt, prov_types, steps, refusals)
        return

    # B4 Opt[V] -> Result[V, E]: fabricating an error for None (opt-in).
    if rhead == "Result" and phead == "Opt":
        if "on_none" in ret_opt:
            steps.append(Step("return", "B4",
                              f"None => Err({ret_opt['on_none']}) (opt-in)"))
        else:
            refusals.append(Refusal(
                mname, "return", "B4", "fabricated-return",
                f"`Opt[..]` -> `{rret}` must fabricate an error for `None`",
                hint="supply an explicit error expression "
                     "(`None => Err(...)`) in the `adapt` declaration"))
        return

    # B4 Result[V, E1] -> Result[V, E2]: error bridge, never permissive.
    if rhead == "Result" and phead == "Result":
        re = rargs[1] if len(rargs) > 1 else None
        pe = pargs[1] if len(pargs) > 1 else None
        if compatible_total(re, pe, e_types=req_types, a_types=prov_types):
            steps.append(Step("return", "B4",
                              f"error `{pe}` -> `{re}` (total)"))
        elif "err_map" in ret_opt:
            steps.append(Step("return", "B4", "explicit error map (opt-in)"))
        else:
            refusals.append(Refusal(
                mname, "return", "B4", "non-total-conversion",
                f"error type `{pe}` does not totally convert to `{re}`",
                hint="supply an explicit `Err` mapping (per-variant when the "
                     "error is a closed variant)"))
        return

    # B4 Opt[V] -> V: refused in auto (a None has nowhere honest to go).
    if phead == "Opt" and rhead != "Opt":
        if "on_none" in ret_opt:
            steps.append(Step("return", "B4",
                              f"None => {ret_opt['on_none']} (opt-in, merges "
                              f"absence into data)"))
        else:
            refusals.append(Refusal(
                mname, "return", "B4", "fabricated-return",
                f"`Opt[..]` -> `{rret}` has nowhere honest to send `None`",
                hint="supply an explicit value for `None` (this merges absence "
                     "into data)"))
        return

    # B6 return record projection: consumer field set subset of candidate's.
    r_struct = _resolve_struct(rret, req_types)
    p_struct = _resolve_struct(pret, prov_types)
    if r_struct is not None and p_struct is not None:
        _bridge_record_return(mname, r_struct, p_struct, ret_opt,
                              req_types, prov_types, steps, refusals)
        return

    refusals.append(Refusal(
        mname, "return", "B4", "non-total-conversion",
        f"no catalogue bridge from `{pret}` to `{rret}`",
        hint="hand-write the wrapper for this return shape"))


def _bridge_error_merge(mname: str, pe: str | None, ret_opt: dict,
                        prov_types: dict, steps: list[Step],
                        refusals: list[Refusal]) -> None:
    """The graded outcome-merge opt-in for `Result[V, E] -> Opt[V]` (S4c).

    Closed variant E -> per-variant map by name (`merge-variant`); a blanket
    `Err(_) => None` over a closed variant is refused outright. Opaque E ->
    the total waiver `Err(_) => None` (`merge-total`), opt-in.
    """
    merge = ret_opt.get("merge")
    cases = _variant_cases(pe, prov_types)
    if cases is not None:
        # closed variant: demand a per-variant map, refuse the blanket waiver
        if merge == "total":
            refusals.append(Refusal(
                mname, "return", "B4", "outcome-merge",
                f"error `{pe}` is a closed variant; a blanket `Err(_) => None` "
                f"is a fail-open trap",
                hint="map every variant honestly by name, or require `Result` "
                     "and decide at the call site"))
            return
        arms = merge if isinstance(merge, dict) else None
        if not arms:
            refusals.append(Refusal(
                mname, "return", "B4", "outcome-merge",
                f"`Result[.., {pe}] -> Opt[..]` merges outcomes; `{pe}` is a "
                f"closed variant needing a per-variant map",
                hint="in the `adapt` declaration, map each variant "
                     "(`NotFound => None`, each other variant with an honest "
                     "arm); a missing variant refuses"))
            return
        missing = [c for c in cases if c not in arms]
        if missing:
            refusals.append(Refusal(
                mname, "return", "B4", "unmapped-error-variant",
                f"the plan maps some variants of `{pe}` but names no arm for "
                f"{', '.join(missing)}",
                hint="map every variant honestly; a backend-outage variant "
                     "must not read as absence"))
            return
        steps.append(Step("return", "B4",
                          f"Result[.., {pe}] -> Opt (per-variant merge)",
                          merge_shape="merge-variant"))
        return
    # opaque E: the total waiver is the only opt-in
    if merge == "total":
        steps.append(Step("return", "B4",
                          f"Err(_) => None over opaque `{pe}` (total waiver)",
                          merge_shape="merge-total"))
        return
    refusals.append(Refusal(
        mname, "return", "B4", "outcome-merge",
        f"`Result[.., {pe}]` return merges outcomes; folding `Err` into `None` "
        f"makes a failure indistinguishable from a miss",
        hint=f"`{pe}` is opaque, so the only opt-in is the total waiver "
             f"`Err(_) => None`: every error this candidate can ever produce "
             f"will read as absence. Opt in only if absence is a safe reading "
             f"of any failure whatsoever, or require `Result` and handle the "
             f"error at the call site"))


def _bridge_record_return(mname: str, r_struct: dict, p_struct: dict,
                          ret_opt: dict, req_types: dict, prov_types: dict,
                          steps: list[Step], refusals: list[Refusal]) -> None:
    """B6 return side: project the candidate's record onto the consumer's."""
    missing = [f for f in r_struct if f not in p_struct]
    fabricate = ret_opt.get("fabricate") or {}
    for f in missing:
        if f not in fabricate:
            refusals.append(Refusal(
                mname, f"return.{f}", "B6", "fabricated-return",
                f"the requirement's return needs field `{f}`, which the "
                f"candidate does not provide",
                hint=f"supply an explicit pure expression for `{f}` (the "
                     f"consumer will read it as truth)"))
    if any(f not in p_struct and f not in fabricate for f in r_struct):
        return
    # every consumer field bridges from the candidate's field (B4)
    for f, rt in r_struct.items():
        if f in p_struct and not compatible_total(
                rt, p_struct[f], e_types=req_types, a_types=prov_types):
            refusals.append(Refusal(
                mname, f"return.{f}", "B6", "non-total-conversion",
                f"return field `{f}`: `{p_struct[f]}` does not totally convert "
                f"to `{rt}`", hint=""))
    if not refusals:
        dropped = [f for f in p_struct if f not in r_struct]
        detail = "record projection"
        if dropped:
            detail += f" (drops unobserved {', '.join(sorted(dropped))})"
        steps.append(Step("return", "B6", detail))


def bridge_plan(required, provided, opt_ins: dict | None = None, *,
                req_types: dict | None = None,
                prov_types: dict | None = None) -> BridgeResult:
    """ADAPT(R, P, D) (section 2.3). `required`/`provided` are `ServiceDecl`s,
    `opt_ins` is `D` keyed by method name, `req_types`/`prov_types` are the two
    sides' type tables for nominal resolution in `compatible_total`.

    Returns a whole-or-nothing `BridgeResult`: either every method has a plan,
    or the accumulated refusal list (section 2.3, "the plan admits whole or
    refuses named")."""
    opt_ins = opt_ins or {}
    req_types = req_types or {}
    prov_types = prov_types or {}
    all_refusals: list[Refusal] = []
    plans: list[MethodPlan] = []
    merges: list[str] = []
    for mname, rm in required.methods.items():
        pm = provided.methods.get(mname)
        if pm is None:
            # v1: name-only matching, no rename mapping (section 2.4 / 8).
            all_refusals.append(Refusal(
                mname, None, None, "method-missing",
                f"candidate provides no method named `{mname}`",
                hint="v1 matches methods by name; rename mapping is a listed "
                     "extension"))
            continue
        steps, refusals = _method_bridge(
            mname, rm, pm, opt_ins.get(mname) or {},
            req_types=req_types, prov_types=prov_types)
        if refusals:
            all_refusals.extend(refusals)
        else:
            plans.append(MethodPlan(mname, tuple(steps), call_name=mname))
            merges.extend(s.merge_shape for s in steps if s.merge_shape)
    if all_refusals:
        return BridgeResult(ok=False, refusals=tuple(all_refusals))
    return BridgeResult(ok=True, methods=tuple(plans), merges=tuple(merges))


def render_adapter(component_name: str, required, provided,
                   opt_ins: dict | None = None, *,
                   provide_key: str, require_key: str = "backing",
                   carried_tokens: tuple[str, ...] = (),
                   prov_types: dict | None = None,
                   req_types: dict | None = None,
                   derivation: str | None = None,
                   chain_depth: int = 1) -> str:
    """Render the synthesized adapter as ordinary `.rvl` source (section 4, the
    artifact + `revl adapt --emit`). The component requires the candidate under
    a fresh alias, provides the consumer-facing key, and wraps each method with
    the bridge derived from the plan. It rides the existing require seam and
    passes the same admission gate as hand-written code (carrying(...) supplies
    the alias token carry-over).

    `derivation` (the `derivation_hash` of this synthesis) stamps the
    MACHINE-READABLE marking of section 4 onto the rendered source, together
    with the catalogue version and the adapter's `chain_depth` (1 for a bridge
    onto ordinary code, n+1 for one onto an adapter of depth n). `resolve`
    reads that marking back with `adapter_marking` to report chain depth and
    rank a chain below a fresh single bridge (section 6.4); a rendering with no
    `derivation` carries no marking, and a later resolve reads it as depth 0 -
    ordinary code - which is the honest reading of an unmarked component.

    Renders every RETURN shape `bridge_plan` admits (section 2.2, B4/B6):
    emission/plain passthrough and the implicit total coercions (identity,
    `V->Opt[V]`, numeric widening, resolved equal-field records); the
    `Result[V,E] -> Opt[V]` outcome-merge (total waiver and per-variant); the
    `Opt[V] -> Result[V,E]` and `Opt[V] -> V` fabrications (opt-in `on_none`);
    the explicit `Result[V,E1] -> Result[V,E2]` error map (opt-in `err_map`,
    per-variant or single arm); and B6 record projection with opt-in field
    fabrication. Argument-side B1 defaults / B2 drops / B3 coercions render via
    `_render_call_args`. A shape this renderer cannot spell RAISES `ValueError`
    so it is never emitted as wrong source (section 4's contract); the three
    callers (`registry.resolve`, `revl adapt`, `federation.check`) catch it and
    fall back to a plan-only report. The slice-4 dichotomy sweep
    (`tests/test_296_synthesis_catalogue.py`) is the standing proof that an
    admitted plan never renders source the ordinary gate rejects."""
    opt_ins = opt_ins or {}
    carry = ""
    if carried_tokens:
        carry = f" carrying({', '.join(carried_tokens)})"
    lines = [
        f"// generated: revl adapt {provide_key} from {require_key}",
    ]
    if derivation:
        lines.append(
            f"{ADAPTER_MARK_PREFIX} sha256:{derivation} "
            f"{CATALOGUE_VERSION} depth={int(chain_depth)}")
    lines += [
        f"component {component_name} requires {require_key}: {provided.name}"
        f"{carry} provides {provide_key}: {required.name} {{",
        f"  provide {provide_key} {{",
    ]
    for mname, rm in required.methods.items():
        pm = provided.methods[mname]
        lines.extend("    " + ln for ln in
                     _render_method(mname, rm, pm, opt_ins.get(mname) or {},
                                    require_key, prov_types or {},
                                    req_types or {}))
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_call_args(mname, rm, pm, opt: dict, prov_types: dict) -> list[str]:
    explicit_pairing = (opt.get("pairing") or {})
    pairing, unpaired, ref = _pair_params(
        mname, rm.params, pm.params, explicit_pairing)
    if ref is not None:
        raise ValueError(f"cannot render {mname}: {ref.reason}")
    rev = {j: i for i, j in pairing.items()}
    defaults = (opt.get("default") or {})
    args: list[str] = []
    for j, (pn, pt) in enumerate(pm.params):
        if j in rev:
            args.append(rm.params[rev[j]][0])
        elif pn in defaults:
            args.append(defaults[pn])
        else:
            d = _canonical_default(pt, prov_types)
            if d is None:
                raise ValueError(f"cannot render default for {pn}: {pt}")
            args.append(d)
    return args


def _match_body(call: str, arms: list[str]) -> list[str]:
    """A `return match <call> { <arms> }` block body (the arms already carry
    their own trailing comma and indentation)."""
    return [f"  return match {call} {{"] + arms + ["  }"]


def _render_return(mname: str, rret: str | None, pret: str | None,
                   ret_opt: dict, call: str,
                   req_types: dict, prov_types: dict) -> list[str]:
    """Render the return position of one method (section 2.2, B4/B6), mirroring
    `_bridge_return`'s admitting branches EXACTLY.

    Returns the body of `fn m(params) <body>`: either a single expression line
    (`= <expr>`) or the inner lines of a `{ ... }` block. Every shape
    `bridge_plan` admits renders here to source the ordinary gate accepts; a
    shape not covered RAISES `ValueError` rather than emitting source that would
    type-check wrong, so the three callers (`registry.resolve`, `revl adapt`,
    `federation.check`) fall back to a plan-only report and never a broken
    artifact (the "raises, never emits wrong source" contract of section 4).
    """
    # Passthrough: identity, or an implicit TOTAL coercion the checker applies
    # for us (`V -> Opt[V]` injection, numeric widening, same-head containers,
    # resolved equal-field records). `= call` is correct because the compiler
    # coerces the candidate's return into the declared one, exactly as any
    # hand-written wrapper would rely on.
    if rret is None or pret is None or rret == pret or compatible_total(
            rret, pret, e_types=req_types, a_types=prov_types):
        return [f"= {call}"]
    rhead, _ = parse_type(rret)
    phead, _ = parse_type(pret)

    # B4 `Result[V, E] -> Opt[V]`: the outcome-merge (S4c). Total waiver over an
    # opaque `E`, or a per-variant map over a closed variant `E`.
    if rhead == "Opt" and phead == "Result":
        merge = ret_opt.get("merge")
        if merge == "total":
            arms = ["    Ok(v) => Some(v),", "    Err(_) => None,"]
        elif isinstance(merge, dict):
            arms = ["    Ok(v) => Some(v),"]
            for variant, target in merge.items():
                arms.append(f"    Err({variant}) => {target},")
        else:
            raise ValueError(f"cannot render merge for {mname}")
        return _match_body(call, arms)

    # B4 `Opt[V] -> Result[V, E]`: fabricate an error for `None` (opt-in). The
    # predicate refuses this without `on_none`, so an admitted plan has it.
    if rhead == "Result" and phead == "Opt":
        on_none = ret_opt.get("on_none")
        if on_none is None:
            raise ValueError(f"cannot render fabricated return for {mname}")
        return _match_body(call, ["    Some(v) => Ok(v),",
                                  f"    None => {on_none},"])

    # B4 `Result[V, E1] -> Result[V, E2]`: reached ONLY when the error types are
    # not `compatible_total` (the auto case took the passthrough above), so an
    # explicit `Err` map is required, and an admitted plan carries it.
    if rhead == "Result" and phead == "Result":
        err_map = ret_opt.get("err_map")
        if err_map is None:
            raise ValueError(f"cannot render error map for {mname}")
        if isinstance(err_map, dict):
            arms = ["    Ok(v) => Ok(v),"]
            for variant, target in err_map.items():
                arms.append(f"    Err({variant}) => {target},")
        else:
            arms = ["    Ok(v) => Ok(v),", f"    Err(e) => {err_map},"]
        return _match_body(call, arms)

    # B4 `Opt[V] -> V`: send `None` to an explicit value (opt-in; merges absence
    # into data). Refused without `on_none`, so an admitted plan carries it.
    if phead == "Opt" and rhead != "Opt":
        on_none = ret_opt.get("on_none")
        if on_none is None:
            raise ValueError(f"cannot render Opt->value for {mname}")
        return _match_body(call, ["    Some(v) => v,",
                                  f"    None => {on_none},"])

    # B6 return record projection: bind the candidate record once, project the
    # consumer's field set (S4b: fields the consumer never named are dropped),
    # fabricating opted-in fields the candidate lacks.
    r_struct = _resolve_struct(rret, req_types)
    p_struct = _resolve_struct(pret, prov_types)
    if r_struct is not None and p_struct is not None:
        fabricate = ret_opt.get("fabricate") or {}
        fields: list[str] = []
        for f in r_struct:
            if f in p_struct:
                fields.append(f"{f}: r.{f}")
            elif f in fabricate:
                fields.append(f"{f}: {fabricate[f]}")
            else:
                raise ValueError(
                    f"cannot render record field `{f}` for {mname}")
        return [f"  let r = {call}", "  return { " + ", ".join(fields) + " }"]

    raise ValueError(
        f"cannot render return bridge for {mname}: `{pret}` -> `{rret}`")


def _render_method(mname, rm, pm, opt: dict, require_key: str,
                   prov_types: dict, req_types: dict | None = None) -> list[str]:
    req_types = req_types or {}
    args = _render_call_args(mname, rm, pm, opt, prov_types)
    params = ", ".join(pn for pn, _ in rm.params)
    call = f"{require_key}.{mname}({', '.join(args)})"
    if pm.emission:
        call = f"emit {call}"
    ret_opt = opt.get("return") or {}
    body = _render_return(mname, rm.returns, pm.returns, ret_opt, call,
                          req_types, prov_types)
    if len(body) == 1 and body[0].startswith("="):
        return [f"fn {mname}({params}) {body[0]}"]
    return [f"fn {mname}({params}) {{"] + body + ["}"]


def service_surface(decl) -> str:
    """The canonical byte spelling of a `ServiceDecl` for a derivation hash.

    Derived from the DECLARATION, not from whichever IR document it arrived in,
    so `revl adapt` and `registry.resolve` compute the SAME adapter identity for
    the same pair. The hash is the adapter's identity for evidence and for
    staleness (section 4); two surfaces disagreeing about it would defeat both.
    """
    return json.dumps({
        "service": {"commutative": bool(getattr(decl, "commutative", False))},
        "methods": {
            name: {
                "params": [[pn, pt] for pn, pt in m.params],
                "returns": m.returns,
                "emission": bool(m.emission),
                "async": bool(getattr(m, "async_", False)),
                "commutative": bool(getattr(m, "commutative", False)),
                "capabilities": (list(m.capabilities)
                                 if m.capabilities is not None else None),
            }
            for name, m in decl.methods.items()
        },
    }, sort_keys=True)


def derivation_hash(required_surface: str, provided_surface: str,
                    provided_sha: str, adapt_decl: str) -> str:
    """The adapter's identity (section 4): sha256 over consumer surface,
    candidate surface + sha, the `adapt` declaration, and the catalogue
    version. Byte-stable, so re-running synthesis reproduces it and a change on
    either side changes it (staleness)."""
    h = hashlib.sha256()
    for part in (required_surface, provided_surface, provided_sha,
                 adapt_decl, CATALOGUE_VERSION):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# ------------------------------------------------- the synthesized-adapter mark
#
# Section 4's derivation header, in the one spelling a machine reads:
#
#   // derivation: sha256:<hex> catalogue-v1 depth=1
#
# It is the ONLY thing that distinguishes a committed adapter from any other
# component, and it is deliberately a comment: nothing in the gate, the type
# system, or the wiring learns what an adapter is (design section 3). `resolve`
# reads it to report chain depth and to rank a chain below a fresh single bridge
# (section 6.4). An unmarked component reads as depth 0 - ordinary code - which
# is the honest reading: the marking is a claim the synthesizer makes about
# itself, so it can only ever rank a candidate DOWN, never up. A component that
# forged one would be asking to be ranked below where it otherwise sits.
ADAPTER_MARK_PREFIX = "// derivation:"
_ADAPTER_MARK_RE = re.compile(
    r"^//\s*derivation:\s*sha256:(?P<hash>[0-9a-f]{64})"
    r"(?:\s+(?P<catalogue>\S+))?"
    r"(?:\s+depth=(?P<depth>\d+))?\s*$")


def adapter_marking(source: str) -> dict | None:
    """Read the section-4 derivation marking off a component's source.

    Returns ``{"derivation": hex, "catalogue": str, "depth": int}`` for a
    synthesized adapter, or None for ordinary code. Every `//` line is scanned,
    not just the file head: a committed adapter carries its own `type` and
    `service` declarations above the generated component, so the marking is not
    always the first thing in the file. Scanning wide is safe precisely because
    the marking only ever ranks a candidate DOWN - a forged one asks to be
    ranked below where it would otherwise sit.
    """
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            continue
        match = _ADAPTER_MARK_RE.match(stripped)
        if match:
            return {
                "derivation": match.group("hash"),
                "catalogue": match.group("catalogue") or "",
                "depth": int(match.group("depth") or 1),
            }
    return None


def chain_depth_for(candidate_source: str) -> int:
    """The chain depth a fresh bridge onto `candidate_source` would have: 1 onto
    ordinary code, n+1 onto a committed adapter that marks itself depth n
    (section 6.4, "depth only ever ranks down")."""
    mark = adapter_marking(candidate_source)
    return 1 + (mark["depth"] if mark else 0)


# --------------------------------------------------- section 6.4: flatten a chain
#
# A committed adapter is an ordinary component providing the consumer-facing key,
# so a later `revl adapt --check` can find IT as a candidate and propose a second
# bridge in front of it. The composed loss then lives in no single reviewed
# artifact (a merge in the committed hop, a default in the new one), which is the
# failure mode section 6.4 closes. `flatten_committed_hop` reconstructs the
# committed inner hop from the candidate's own file so `--check` can re-display
# every hop's merges, defaults and drops in one listing (E12): what gets attested
# is the actual composed loss, not the last hop's slice of it.


@dataclass(frozen=True)
class InnerHop:
    """One committed hop a flattened chain re-displays (section 6.4). `result`
    is the re-derived committed plan; `opaque` is set (with a reason) when the
    committed body uses a construct outside the flagship reconstruction, so the
    listing degrades honestly to "see the committed adapter's attestation"
    rather than inventing a plan the committed adapter never ran."""
    require_key: str
    provided_service: str      # the consumer-facing service this hop yields
    backing_service: str       # the service this committed hop requires
    result: BridgeResult
    opaque: str | None = None


def _committed_method_opt_ins(method_ir: dict, req_m, prov_m) -> dict:
    """Recover the opt-in map (`D`) a committed adapter method's body encodes, so
    re-deriving its plan with `bridge_plan` reproduces the COMMITTED plan rather
    than refusing a discard/merge the author already opted into and had audited
    at commit time. Bounded to the `render_adapter` body shape; a shape it does
    not recognize leaves the corresponding opt-in unset, and the re-derivation
    then refuses (the hop is reported opaque)."""
    opt: dict = {}
    # B2 drops: v1 pairs by name and has no rename mapping (section 8), so a
    # consumer parameter with no same-named backing parameter was necessarily
    # dropped - exactly the `supplied-value-dropped` opt-in the committed
    # adapter carried.
    prov_names = {pn for pn, _pt in prov_m.params}
    drops = [rn for rn, _rt in req_m.params if rn not in prov_names]
    if drops:
        opt["drop"] = drops
    # B4 outcome merge: only when the shapes force one (Opt <- Result). The
    # total waiver is `Err(_) => None` (a single wildcard-bound Err arm); a
    # per-variant closed mapping is left unrecovered on purpose - its arm map is
    # attested in the committed adapter itself, and reconstructing it here would
    # duplicate that record in a place no one reviewed.
    rhead, _ = parse_type(req_m.returns)
    phead, _ = parse_type(prov_m.returns)
    if rhead == "Opt" and phead == "Result":
        body = method_ir.get("body") or []
        if len(body) == 1 and body[0].get("step") == "return":
            expr = body[0].get("expr") or {}
            if expr.get("kind") == "match":
                err_arms = [a for a in (expr.get("arms") or [])
                            if a.get("pattern") == "Err"]
                if (len(err_arms) == 1
                        and err_arms[0].get("bind") in ("_", "__")):
                    opt["return"] = {"merge": "total"}
    return opt


def flatten_committed_hop(candidate_ir: dict,
                          provided_service: str) -> InnerHop | None:
    """Reconstruct the committed inner hop of a candidate that is itself an
    adapter (section 6.4, E12), so `revl adapt --check` can flatten the chain.

    Finds the component in `candidate_ir` that provides `provided_service` and,
    for the single backing service it requires, re-derives the committed hop's
    plan with `bridge_plan` over the two service declarations in the candidate's
    own file. Returns `None` when the providing component is ordinary code with
    no single-backing require to flatten (the honest reading: there is no inner
    hop visible in this file)."""
    from .admission import _service_from_ir  # noqa: PLC0415 — avoids a cycle

    services = candidate_ir.get("services") or {}
    types = candidate_ir.get("types") or {}
    component = None
    provide_key = None
    for comp in candidate_ir.get("components") or []:
        for key, svc in (comp.get("provides") or {}).items():
            if svc == provided_service:
                component, provide_key = comp, key
                break
        if component is not None:
            break
    if component is None:
        return None
    requires = component.get("requires") or {}
    if len(requires) != 1:
        # zero (a leaf) or several backings: no single committed hop to
        # re-derive here. Deeper structure is out of this file's scope.
        return None
    require_key, backing_service = next(iter(requires.items()))
    if backing_service not in services or provided_service not in services:
        return None
    req = _service_from_ir(provided_service, services[provided_service])
    prov = _service_from_ir(backing_service, services[backing_service])
    # the committed method bodies, by provided method name.
    bodies: dict[str, dict] = {}
    for step in component.get("body") or []:
        if step.get("step") == "provide" and step.get("name") == provide_key:
            for m in step.get("methods") or []:
                bodies[m.get("name")] = m
    opt_ins: dict = {}
    for mname, rm in req.methods.items():
        pm = prov.methods.get(mname)
        if pm is None:
            continue
        recovered = _committed_method_opt_ins(bodies.get(mname) or {}, rm, pm)
        if recovered:
            opt_ins[mname] = recovered
    result = bridge_plan(req, prov, opt_ins, req_types=types, prov_types=types)
    opaque = None
    if not result.ok:
        opaque = ("the committed adapter uses a construct outside the flagship "
                  "reconstruction (per-variant merge, explicit default, or "
                  "fabrication); its own attestation carries the audited plan")
    return InnerHop(require_key=require_key, provided_service=provided_service,
                    backing_service=backing_service, result=result,
                    opaque=opaque)
