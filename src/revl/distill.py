"""Approval distillation - the ledger becomes policy (roadmap item 251, Slice 1).

The item-248 approval ledger is an append-only fact stream about consent: one
``approval-granted`` record per human yes to a class-(c) crossing. Distillation
is a PURE fold over that stream. It notices that the same operator keeps saying
yes to the same *shape* of crossing session after session, and writes down the
`AutoApproveRule` (`revl.policy`) that would have said yes for them - a rule an
operator could have typed by hand, checked on the same path, never a new grant
mechanism (design §"the one thing to get right", S1).

This module is Slice 1: DETECTION and the TYPED DIFF, all pure, no I/O beyond
being handed records. It reads a list of ledger records, projects each granted
class-(c) crossing to the **resource-scoped shape key** (§1.2), applies the
threshold (§1.3) INCLUDING the resource join-or-refuse clause, and emits either a
candidate `AutoApproveRule` with its blast radius, or a first-class typed
"cannot distill" verdict (never a silent `None`). It applies NO policy and holds
no runtime authority, so it cannot widen anything - the runtime consume path is
Slice 2.

The shape key (§1.2) is deliberately exactly the tuple an item-33 rule scopes
over, so the emitted rule is expressible in that language and no other:

    (capability_token_with_resource_params, realm, taint_origin_set)

Fail-closed this slice (the recording of the bound resource valuation and the
post-endorsement taint lands in Slice 2):

  * a capability that carries a `cap_order._REGISTRY` resource order but whose
    resource valuation is not recorded returns ``resource-scope-unrecorded``;
  * a taint-relevant shape whose ``taintOrigins`` is unrecorded returns
    ``taint-unknown``.

A bare-token capability (no resource order) with no taint relevance distills
fully here, so the slice is useful on its own: it turns the ledger into
reviewable typed offers with honest blast radii, before any crossing is ever
auto-approved.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase

from . import cap_order
from .policy import TAINT_FOLD_ORIGINS, AutoApproveRule

# The item-33 shared realm: an un-isolated component is its own bucket, keyed by
# this sentinel (mirrors `lower.SHARED_REALM`). Kept local so the pure distiller
# does not import the lowering pipeline.
SHARED_REALM = ""

# §1.3 defaults: N grants over M distinct sessions.
DEFAULT_MIN_GRANTS = 5
DEFAULT_MIN_SESSIONS = 2

# the WAL record kinds the fold reads. A record with no ``record`` field is
# treated as a grant, a convenience for hand-written fixtures.
_GRANTED = "approval-granted"
_DENIED = "approval-denied"


class Reason(enum.Enum):
    """The first-class typed reasons a shape cannot distill (design §1.3), in the
    discipline item 252 set for its shell classifier: never a silent
    fallthrough, always a named verdict the operator sees instead of an offer."""

    RESOURCE_SCOPE_UNRECORDED = "resource-scope-unrecorded"
    TAINT_UNKNOWN = "taint-unknown"
    VARYING_SCOPE = "varying-scope"
    BELOW_THRESHOLD = "below-threshold"
    MIXED_OPERATOR = "mixed-operator"
    HAD_DENIAL = "had-denial"


@dataclass(frozen=True)
class ShapeKey:
    """The resource-scoped shape key (§1.2). ``token`` is the class-(c)
    capability token, ``realm`` the item-33 realm scope (``""`` = shared),
    ``taint`` the recorded item-249 taint-fold origin set (of the five)."""

    token: str
    realm: str
    taint: frozenset[str]


@dataclass(frozen=True)
class CannotDistill:
    """A typed refusal - what the operator sees INSTEAD of an offer (§1.3)."""

    reason: Reason
    token: str
    realm: str
    detail: str
    taint: frozenset[str] | None = None

    def render(self) -> str:
        where = f" in realm {self.realm}" if self.realm else ""
        return (f"cannot distill {self.token}{where} "
                f"({self.reason.value}): {self.detail}")


@dataclass(frozen=True)
class NotCovered:
    """One windowed grant a candidate rule would NOT have auto-approved, with the
    reason it fell out (§3.1): a taint origin the rule excludes, a realm out of
    scope, a component outside the glob, or a resource value outside the scope."""

    component: str
    capability: str
    reason: str            # "taint" | "realm" | "glob" | "resource"


@dataclass(frozen=True)
class BlastRadius:
    """The blast-radius fold's partition of the ledger window (§3), plus the
    negative guarantee (§3.2): the origins the rule can NEVER admit, the
    complement of its `admitting` set over the five taint-fold origins."""

    covered: int
    not_covered: tuple[NotCovered, ...]
    negative_guarantee: frozenset[str]
    resource_scope: str | None            # the cone the rule names, or None (bare)
    destinations: tuple[str, ...]         # distinct resource values seen in window

    @property
    def total(self) -> int:
        return self.covered + len(self.not_covered)


@dataclass(frozen=True)
class DistilledOffer:
    """A candidate rule the operator may review, with everything the review needs
    (design §4): the rule text, the shape it was distilled from, the attributed
    operator, the sessions, the grant count, and the blast radius."""

    rule: AutoApproveRule
    shape: ShapeKey
    operator: str
    sessions: tuple[str, ...]
    grant_count: int
    blast: BlastRadius

    @property
    def rule_text(self) -> str:
        return self.rule.to_dsl()


@dataclass(frozen=True)
class DistillationResult:
    """The whole fold's output: the offers, and the typed refusals. Pure data."""

    offers: tuple[DistilledOffer, ...] = ()
    refusals: tuple[CannotDistill, ...] = ()


# --------------------------------------------------------------- projection


@dataclass(frozen=True)
class _Projected:
    """One granted class-(c) capability projected to its shape, carrying a
    back-reference to the record's recurrence / attribution fields."""

    token: str
    realm: str
    taint: frozenset[str]
    resource: cap_order.Cap | None   # the bound resource cone, or None (bare token)
    component: str
    session: str
    operator: str


def _resource_params(cap: cap_order.Cap) -> list[tuple[str, object]]:
    """The registered RESOURCE-kind parameters bound on a cap (host/path/table),
    the projection §1.2 keys on. Ceiling params (uses counters) are not resource
    scope and are excluded."""
    return [(n, v) for n, v in cap.params
            if cap_order.is_registered(n) and not cap_order.is_ceiling(n)]


def _admission_taint(taint: frozenset[str] | None, taint_relevant: bool) \
        -> frozenset[str]:
    """The taint set to enforce at ADMISSION (the H2 floor, §2.2). Unknown or
    empty taint on a taint-RELEVANT crossing is treated as ALL FIVE origins
    present (fail-closed, over-prompt is safe), never as an empty set a
    ``{} subset admitting`` test would wave through. A non-taint-relevant
    crossing carries no taint."""
    if taint:
        return taint
    if taint_relevant:
        return TAINT_FOLD_ORIGINS
    return frozenset()


def _project_taint(rec: dict) -> tuple[frozenset[str] | None, bool]:
    """Read a record's recorded taint origin set and whether the crossing is
    taint-relevant. Returns ``(origins, relevant)`` where ``origins is None``
    means unrecorded. A recorded (possibly empty) set means known."""
    relevant = bool(rec.get("taintRelevant"))
    raw = rec.get("taintOrigins")
    if raw is None:
        return None, relevant
    origins = frozenset(str(o) for o in raw)
    return origins, True


def _project_resource(rec: dict, cap: cap_order.Cap) \
        -> tuple[cap_order.Cap | None, bool]:
    """Resolve a capability's bound resource scope for the shape key (§1.2, N1).

    Returns ``(cone, resource_bearing)``:
      * an inline-parameterised spelling (`gateway.send(host="x")`, the Slice-2
        recorded form) yields its resource projection directly;
      * otherwise the record's ``resourceScopes`` map is the resource channel: a
        token mapped to a canonical spelling yields that cone; a token mapped to
        ``None`` is resource-bearing but UNRECORDED (Slice 1 fail-closed);
      * a bare token absent from ``resourceScopes`` has no resource order and
        keys bare, byte-for-byte as before.
    ``cone is None`` with ``resource_bearing True`` is the unrecorded case."""
    inline = _resource_params(cap)
    if inline:
        # already-canonical params (cap came from parse_cap); construct the
        # resource projection directly rather than re-canonicalizing.
        return cap_order.Cap(cap.token, tuple(inline)), True
    scopes = rec.get("resourceScopes") or {}
    if cap.token in scopes:
        val = scopes[cap.token]
        if val is None:
            return None, True                       # order present, value missing
        parsed = cap_order.parse_cap(str(val))
        return cap_order.Cap(parsed.token, tuple(_resource_params(parsed))), True
    return None, False


def _project(rec: dict, cap_str: str) \
        -> "_Projected | CannotDistill":
    """Project one granted class-(c) capability to its shape, or a typed refusal.

    Taint is resolved first: an unknown taint on a taint-relevant crossing cannot
    even form the shape's taint dimension, so it is the dominant refusal. Then the
    resource scope: a resource-bearing capability with no recorded valuation fails
    closed. A bare, taint-clean capability keys fully."""
    realm = str(rec.get("realm", SHARED_REALM))
    cap = cap_order.parse_cap(cap_str)
    token = cap.token

    taint, relevant = _project_taint(rec)
    if taint is None:
        if relevant:
            return CannotDistill(
                Reason.TAINT_UNKNOWN, token, realm,
                "the post-endorsement taint of the crossing arguments is not "
                "recorded, so the negative guarantee has no honest floor "
                "(recorded in Slice 2)")
        taint = frozenset()

    cone, resource_bearing = _project_resource(rec, cap)
    if resource_bearing and cone is None:
        return CannotDistill(
            Reason.RESOURCE_SCOPE_UNRECORDED, token, realm,
            "the capability carries a resource order (host/path/table) but the "
            "bound resource valuation is not recorded, so the rule cannot name "
            "the destination it crossed (recorded in Slice 2)",
            taint=taint)

    return _Projected(token, realm, taint, cone,
                      str(rec.get("component", "")),
                      str(rec.get("session", "")),
                      str(rec.get("operator", "")))


# ------------------------------------------------------------- resource join


def _resource_join(cones: list[cap_order.Cap]) -> cap_order.Cap | None:
    """Join distinct resource cones to a single expressible cone, or ``None`` when
    they do not share one (§1.3). Discrete params (host/table) join only by
    equality; a `path` param joins on the longest common ancestor cone that
    ``covers`` every value. An empty path prefix (siblings whose only common
    ancestor is the bare token) has no expressible cone and returns ``None``."""
    token = cones[0].token
    # collect each param's values across the cones; a param bound on some cones
    # but not all cannot join (one side is the wider bare-in-that-param).
    names = {n for c in cones for n, _ in c.params}
    joined: list[tuple[str, object]] = []
    for name in sorted(names):
        values = [c.param_map().get(name) for c in cones]
        if any(v is None for v in values):
            return None
        _kind, order = cap_order._REGISTRY[name]
        if order == "path":
            prefix = _common_path_prefix(values)
            if not prefix:
                return None
            joined.append((name, "/" + "/".join(prefix)))
        else:                                   # discrete: equality only
            if len(set(values)) != 1:
                return None
            joined.append((name, values[0]))
    candidate = cap_order.make_cap(token, joined)
    # the join must actually COVER every observed value (a sanity gate: it can
    # only widen to a real common ancestor, never sideways).
    if not all(cap_order.covers(candidate, c) for c in cones):
        return None
    return candidate


def _common_path_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    """The longest component-wise common prefix of canonical path tuples - the
    cone that ``covers`` all of them (component-wise, never string-prefix)."""
    if not paths:
        return ()
    prefix = list(paths[0])
    for p in paths[1:]:
        i = 0
        while i < len(prefix) and i < len(p) and prefix[i] == p[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return tuple(prefix)


# ------------------------------------------------------------- component glob


def _component_glob(names: list[str]) -> str:
    """The component selector the emitted rule names. One component keys
    literally; several fold to their longest common prefix plus ``*`` (the
    `billing:*` a hand-written rule would use). The reviewed blast set enumerates
    the actual members, so the glob is legible against the concrete grants (the
    §6 A1 membership binding lands with enforcement in Slice 2)."""
    uniq = sorted(set(n for n in names if n))
    if not uniq:
        return "*"
    if len(uniq) == 1:
        return uniq[0]
    prefix = os.path.commonprefix(uniq)
    return (prefix + "*") if prefix else "*"


# ------------------------------------------------------------- blast radius


def blast_radius(rule: AutoApproveRule, window: list[dict]) -> BlastRadius:
    """Fold the ledger window against a candidate rule: for each granted
    class-(c) crossing, "would this rule have auto-approved this grant?", using
    the SAME predicates the runtime consent path uses - the item-33
    `fnmatchcase` glob, the `cap_order.covers` resource order, and the taint
    subset gate with the H2 admission floor (§3.1). Returns the covered count,
    the not-covered partition with per-grant reasons, and the negative
    guarantee (§3.2).

    The taint gate uses the admission floor (§2.2): a taint-relevant crossing
    whose admission taint set is empty or unknown is treated as ALL FIVE origins
    (fail-closed), so an empty set never slips a taint-relevant crossing through
    a `{} subset admitting` test."""
    rule_caps = [cap_order.parse_cap(c) for c in rule.caps]
    covered = 0
    not_covered: list[NotCovered] = []
    destinations: set[str] = set()
    for rec in _grants(window):
        comp = str(rec.get("component", ""))
        realm = str(rec.get("realm", SHARED_REALM))
        taint_raw, relevant = _project_taint(rec)
        adm_taint = _admission_taint(taint_raw, relevant)
        for cap_str in rec.get("classCCapabilities") or []:
            crossing = cap_order.parse_cap(cap_str)
            cone, _bearing = _project_resource(rec, crossing)
            live = cone if cone is not None else crossing
            for n, v in _resource_params(live):
                destinations.add(cap_order._render_value(n, v))
            reason = _coverage_reason(rule, rule_caps, comp, realm, live, adm_taint)
            if reason is None:
                covered += 1
            else:
                not_covered.append(NotCovered(comp, cap_str, reason))
    scope = None
    for cap in rule_caps:
        if _resource_params(cap):
            scope = cap.to_str()
            break
    return BlastRadius(covered, tuple(not_covered),
                       rule.negative_guarantee(), scope,
                       tuple(sorted(destinations)))


def _coverage_reason(rule: AutoApproveRule, rule_caps: list[cap_order.Cap],
                     component: str, realm: str, crossing: cap_order.Cap,
                     admission_taint: frozenset[str]) -> str | None:
    """Why a rule would NOT cover a crossing, or ``None`` if it would. Order:
    glob, realm, capability/resource cover, taint subset - the order the runtime
    gate applies them."""
    if not fnmatchcase(component, rule.component):
        return "glob"
    if rule.realm is not None and rule.realm != realm:
        return "realm"
    if not any(cap_order.covers(rc, crossing) for rc in rule_caps):
        # distinguish a resource-value miss from a token miss for the operator.
        same_token = any(rc.token == crossing.token for rc in rule_caps)
        return "resource" if same_token else "glob"
    if not (admission_taint <= rule.admitting):
        return "taint"
    return None


# ------------------------------------------------------------------ the fold


def _grants(records: list[dict]) -> list[dict]:
    return [r for r in records
            if r.get("record", _GRANTED) == _GRANTED]


def _denials(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("record") == _DENIED]


def _denied_keys(records: list[dict]) -> set[tuple[str, str, frozenset[str]]]:
    """The shape keys denied in the window (§1.3): a shape the operator sometimes
    refuses is not a settled decision. Keyed on (token, realm, taint) so a denial
    of the same shape blocks its distillation; a denial with unrecorded taint
    blocks the token+realm across any taint (fail-closed)."""
    out: set[tuple[str, str, frozenset[str]]] = set()
    for rec in _denials(records):
        realm = str(rec.get("realm", SHARED_REALM))
        taint, _relevant = _project_taint(rec)
        for cap_str in rec.get("classCCapabilities") or []:
            token = cap_order.parse_cap(cap_str).token
            out.add((token, realm, taint if taint is not None else None))
    return out


def _shape_denied(key: tuple[str, str, frozenset[str]],
                  denied: set) -> bool:
    token, realm, taint = key
    if (token, realm, taint) in denied:
        return True
    return (token, realm, None) in denied      # unrecorded-taint denial blocks all


def distill(records: list[dict], *,
            min_grants: int = DEFAULT_MIN_GRANTS,
            min_sessions: int = DEFAULT_MIN_SESSIONS) -> DistillationResult:
    """Fold a ledger window to offers and typed refusals (§1, §2, §3). Pure: it
    reads records and returns data, applies no policy, holds no authority.

    Grants are grouped by ``(token, realm, taint)`` - the shape key WITHOUT the
    exact resource value, so grants to sibling resource values can be considered
    for a common cone (§1.3). Each group is then either emitted as an offer (with
    the joined resource cone in its rule) or refused with a typed reason."""
    denied = _denied_keys(records)
    groups: dict[tuple[str, str, frozenset[str]], list[_Projected]] = {}
    refusals: dict[tuple, CannotDistill] = {}

    for rec in _grants(records):
        for cap_str in rec.get("classCCapabilities") or []:
            proj = _project(rec, cap_str)
            if isinstance(proj, CannotDistill):
                refusals.setdefault(
                    (proj.reason, proj.token, proj.realm), proj)
                continue
            groups.setdefault((proj.token, proj.realm, proj.taint), []).append(proj)

    offers: list[DistilledOffer] = []
    for key, members in groups.items():
        result = _distill_group(key, members, records, denied,
                                min_grants, min_sessions)
        if isinstance(result, CannotDistill):
            refusals.setdefault((result.reason, result.token, result.realm),
                                result)
        else:
            offers.append(result)

    return DistillationResult(tuple(offers), tuple(refusals.values()))


def _distill_group(key: tuple[str, str, frozenset[str]],
                   members: list[_Projected], records: list[dict],
                   denied: set, min_grants: int, min_sessions: int) \
        -> "DistilledOffer | CannotDistill":
    """Apply the §1.3 threshold to one (token, realm, taint) group and, if it
    settles, emit the offer with its joined resource cone and blast radius."""
    token, realm, taint = key

    # the resource join-or-refuse (§1.3): a resource-bearing group must join to a
    # single expressible cone, else it is refused rather than widened.
    cones = [m.resource for m in members if m.resource is not None]
    resource_bearing = bool(cones)
    joined: cap_order.Cap | None = None
    if resource_bearing:
        joined = _resource_join(cones)
        if joined is None:
            return CannotDistill(
                Reason.VARYING_SCOPE, token, realm,
                "the grants span resource values with no common cone an operator "
                "could narrow to (e.g. distinct hosts, or sibling path cones); "
                "refused rather than widened to the resource-free rule",
                taint=taint)

    sessions = tuple(sorted({m.session for m in members if m.session}))
    operators = sorted({m.operator for m in members if m.operator})

    if len(members) < min_grants or len(sessions) < min_sessions:
        return CannotDistill(
            Reason.BELOW_THRESHOLD, token, realm,
            f"{len(members)} grant(s) over {len(sessions)} session(s); needs "
            f">= {min_grants} grants over >= {min_sessions} sessions",
            taint=taint)

    if len(operators) > 1:
        return CannotDistill(
            Reason.MIXED_OPERATOR, token, realm,
            f"the grants are attributed to more than one operator "
            f"({', '.join(operators)}); a distilled rule has no single author "
            f"(offered as per-operator sub-distillations instead)",
            taint=taint)

    if _shape_denied(key, denied):
        return CannotDistill(
            Reason.HAD_DENIAL, token, realm,
            "the same shape was DENIED in the window; a shape the operator "
            "sometimes refuses is not a settled yes",
            taint=taint)

    cap_spelling = joined.to_str() if joined is not None else token
    rule = AutoApproveRule(
        component=_component_glob([m.component for m in members]),
        caps=(cap_spelling,),
        realm=(realm if realm != SHARED_REALM else None),
        admitting=taint,
    )
    blast = blast_radius(rule, records)
    return DistilledOffer(rule, ShapeKey(token, realm, taint),
                          operators[0], sessions, len(members), blast)


# ------------------------------------------------ crossing enumeration (§3.3)


def class_c_capabilities(ir: dict, key: str, method: str) -> frozenset[str]:
    """The class-(c) capability set a call reaches, via the SAME approval
    `ClassMap` closure fold the runtime consent path uses (design §3.1, §3.3),
    not a bespoke re-walk. The blast-radius fold consumes the ledger's recorded
    `classCCapabilities`, which are produced by exactly this fold, so the two can
    never disagree about which crossing kinds a rule admits. Exposed so the 414
    reach-completeness row can prove the fold visits every crossing kind."""
    from .mcp.approval import ClassMap        # noqa: PLC0415 - lazy, avoids cycle

    reach = ClassMap(ir).classify_call(key, method)
    if reach is None:
        return frozenset()
    return frozenset(reach.get("classC") or ())
