"""Navigable refusals - the machine-facing map a policy deny carries beside its
verdict (roadmap item 274).

Where `diagnostics.classify` (item 286) attaches the STATIC per-code `fix`
grammar lesson, this module builds the DYNAMIC per-instance `navigate` record:
the nearest allowed space computed from the very tables that refused. It is a
projection of the refusal, never a second decision, so it grants nothing and
evaluates nothing at runtime (design §4).

Two invariants dominate the shape, both from the adversarial review:

  * HIGH - a `clears-this-gate` proof marker is sound ONLY on a predicate whose
    operands are immutable at the refusal site. A predicate over a
    runtime-mutable operand (a lease/ceiling counter, a standing-grant ledger
    membership, any leased/time-bounded value) is TOCTOU and must be
    `candidate`. `alternative()` enforces this: pass `mutable_operand=True` and
    a requested `clears-this-gate` is forced down to `candidate`.

  * CRITICAL - under the untrusted-author profile, a POLICY-family refusal must
    not let the author reconstruct the operator's policy topology. `record()`
    collapses every such refusal to ONE non-discriminating verdict (`blocked:
    true`, a generic reason, no true family, no proof, no alternatives), so a
    matrix of granted-service operations tripping every family yields
    mutually byte-identical records.
"""

from __future__ import annotations

# ------------------------------------------------------------- closed vocab

# the closed family enum (design §5). Slice 1 wires `taint-sink` and the four
# boundary-policy families; the rest are listed so the enum is total from the
# start and a later slice adds only the builder, not the vocabulary.
FAMILIES = frozenset({
    "taint-sink", "taint-declassify", "policy-capability", "policy-deny",
    "policy-tenant", "mcp-sandbox", "taint-flow", "approval", "ceiling",
    "ownership", "evidence", "adapter", "cache", "admit-profile",
})

# who enacts an alternative - the field a harness routes on (design §3).
ENACTS_AUTHOR = "author"
ENACTS_OPERATOR = "operator"
ENACTS_RUNTIME_APPROVAL = "runtime-approval"
_ENACTS = (ENACTS_AUTHOR, ENACTS_OPERATOR, ENACTS_RUNTIME_APPROVAL)
_ENACTS_ORDER = {name: i for i, name in enumerate(_ENACTS)}

# the two-value proof marker (design §3).
PROOF_CLEARS = "clears-this-gate"
PROOF_CANDIDATE = "candidate"

# the single generic verdict an untrusted author sees for ANY policy-family
# refusal (design §4, the CRITICAL collapse). It names no family and no gate.
UNTRUSTED_FAMILY = "unavailable"
UNTRUSTED_REASON = "this operation is not available to this profile from here"


def is_untrusted(profile) -> bool:
    """Whether `profile` redacts navigation to the collapsed untrusted-author
    view. True for `AdmissionProfile.untrusted_author(...)`; False for a trusted
    author, INCLUDING one compiling with `--taint-strict` alone (that flag does
    not distrust the author)."""
    if profile is None:
        return False
    return bool(getattr(profile, "untrusted", False))


def alternative(*, enacts: str, action: str, ref: str | None = None,
                clears: bool = False, mutable_operand: bool = False) -> dict:
    """One navigable alternative.

    `clears` requests the `clears-this-gate` marker: the compiler re-checked the
    alternative against the same predicate that refused and it passes THIS gate.
    `mutable_operand=True` records that the predicate's operand is runtime-mutable
    (a lease/ceiling counter, ledger membership, a leased/time-bounded value); the
    HIGH fix forces such an alternative to `candidate` no matter what `clears`
    asked, because the fact can change between the refusal and the retry (TOCTOU).
    An `operator`/`runtime-approval` alternative is never `clears-this-gate`
    either: its success depends on a decision the compiler does not hold, and it
    is never author-enactable (the self-mint invariant, design §3)."""
    if enacts not in _ENACTS:
        raise ValueError(f"unknown enacts {enacts!r}")
    author_enactable = enacts == ENACTS_AUTHOR
    proof = (PROOF_CLEARS
             if (clears and author_enactable and not mutable_operand)
             else PROOF_CANDIDATE)
    alt = {"enacts": enacts, "proof": proof, "action": action}
    if ref is not None:
        alt["ref"] = ref
    if mutable_operand:
        # names the operand as live so the candidate wording is honest, and lets
        # the soundness sweep locate lease/ledger predicates mechanically.
        alt["live"] = True
    return alt


def _order_key(alt: dict) -> tuple:
    """Deterministic order: author alternatives first, then operator, then
    runtime-approval; ties broken lexicographically on the action string, then
    the ref. So the same refusal yields a byte-identical record every compile,
    and redaction removes items wholesale (no index-gap tell, design §7)."""
    return (_ENACTS_ORDER.get(alt.get("enacts"), 99),
            alt.get("action") or "", alt.get("ref") or "")


def collapsed() -> dict:
    """The single non-discriminating verdict an untrusted author sees for ANY
    policy-family refusal (design §4). No true family, no gate-specific reason,
    no proof, no alternatives - so every family is mutually indistinguishable and
    a redacted-operator-only refusal is byte-identical to a genuine block. A
    fresh dict each call so callers may not mutate a shared one."""
    return {
        "family": UNTRUSTED_FAMILY,
        "blocked": True,
        "reason": UNTRUSTED_REASON,
        "alternatives": [],
    }


def record(*, family: str, refused: dict | None = None,
           blocked: bool = False, reason: str | None = None,
           alternatives: list[dict] | None = None,
           profile=None) -> dict:
    """Assemble a `navigate` record for a policy-family refusal.

    Under the untrusted-author profile every policy-family refusal collapses to
    `collapsed()` (design §4, CRITICAL): the redaction is by fact
    class, and for the wired families no author-enactable-and-non-discriminating
    alternative survives, so the list is empty and the family is hidden.

    On the trusted view the record carries the true family, the ordered
    alternatives, `blocked`, and the family-specific reason.
    """
    if family not in FAMILIES:
        raise ValueError(f"unknown navigate family {family!r}")
    if is_untrusted(profile):
        return collapsed()
    alts = sorted(alternatives or [], key=_order_key)
    out: dict = {"family": family, "blocked": bool(blocked),
                 "alternatives": alts}
    if refused is not None:
        out["refused"] = refused
    if reason is not None:
        out["reason"] = reason
    return out


def blocked_record(*, family: str, reason: str, refused: dict | None = None,
                   profile=None) -> dict:
    """A first-class `blocked` verdict: the honest wall with a sign on it
    (design §3). Empty `alternatives`, `blocked: true`, the one-line reason.
    Collapses to the untrusted view like any other policy-family refusal."""
    return record(family=family, refused=refused, blocked=True, reason=reason,
                  alternatives=[], profile=profile)


# ============================================================ family builders
#
# Slice 2 (item 274). Each builder is PURE: it takes only the primitive facts
# the gate already held at the refusal site, and returns the `navigate` record.
# The raise site calls it with the tables it already has; no gate's control flow
# changes (design §5, "every change is 'also attach this'"). Two invariants ride
# every builder:
#
#   * §4 collapse - a POLICY-family refusal (approval/ceiling/evidence, joining
#     slice 1's taint/boundary) that leaks OPERATOR policy topology collapses to
#     `collapsed()` under the untrusted-author profile. Author-structural
#     families that leak only the author's own source (ownership/cache/adapter)
#     collapse too (over-redaction is always sound and keeps the matrix byte-
#     identical); the ONE exception is `admit-profile`, whose granted-set
#     enumeration IS the author's own contract (§2.9) and is built with
#     `profile=None` so it survives.
#   * §3 proof marker - `clears-this-gate` sits ONLY on an author-enactable edit
#     over an operand IMMUTABLE at the refusal site. A ceiling/lease counter or a
#     grant ledger entry is runtime-mutable (TOCTOU): `alternative(...,
#     mutable_operand=True)` forces `candidate` and flags the value `live`.


def approval_navigate(*, token: str, ttl_ms: int | None = None,
                      standing_grant: str | None = None, profile=None) -> dict:
    """Approval (item 246, design §2.3). The acquire-and-thread recipe is the
    always-present alternative, enacted by the runtime approval surface (revl has
    no principal directory, so never `author`). When the grant ledger holds a
    covering standing grant, name it - but ledger membership is RUNTIME-MUTABLE
    (revoked or expired between the refusal and the retry, TOCTOU), so it is
    always `candidate`/`live`, never `clears-this-gate` (the HIGH fix)."""
    alts = [alternative(
        enacts=ENACTS_RUNTIME_APPROVAL,
        action=(f"acquire an approval and thread it: "
                f"`let a = await approval[{token}] {{ ... }}` then "
                f"`emit … with a`"),
        ref=token)]
    if standing_grant is not None:
        alts.append(alternative(
            enacts=ENACTS_RUNTIME_APPROVAL,
            action=(f"a standing grant covering `{token}` is on the ledger; "
                    f"thread it instead of minting a fresh prompt (it is a live "
                    f"value that may be revoked or expire before the retry)"),
            ref=standing_grant, clears=True, mutable_operand=True))
    refused: dict = {"token": token}
    if ttl_ms is not None:
        refused["ttl_ms"] = ttl_ms
    return record(family="approval", refused=refused, blocked=False,
                  alternatives=alts, profile=profile)


def ceiling_navigate(*, param: str, child_value: str, parent_bound: str,
                     bound_site: str, is_budget: bool, profile=None) -> dict:
    """Ceilings (items 294/260, design §2.4). The largest in-bounds valuation is
    the failing `covers` comparison re-read as a suggestion (zero new analysis).
    A resource bound (a path prefix / host match declared at the grant site) is
    immutable at the refusal site, so narrowing the child to it `clears-this-gate`;
    a BUDGET bound (a `remainingUses`/`calls` counter) is a live counter that may
    already be spent at the retry (TOCTOU), so it is `candidate`/`live` (the HIGH
    fix and the soundness sweep). Raising the bound is operator-enacted at its
    declaration site - a child can never mint the widening itself (attenuation)."""
    if is_budget:
        narrow = alternative(
            enacts=ENACTS_AUTHOR,
            action=(f"narrow `{param}` on the child to the parent's bound "
                    f"{parent_bound} (the largest in-bounds value; {parent_bound} "
                    f"is a live budget counter that may already be spent at the "
                    f"retry)"),
            ref=param, clears=True, mutable_operand=True)
    else:
        narrow = alternative(
            enacts=ENACTS_AUTHOR,
            action=(f"narrow the child's `{param}` to the parent's bound "
                    f"`{parent_bound}` (the largest in-bounds value)"),
            ref=param, clears=True)
    raise_bound = alternative(
        enacts=ENACTS_OPERATOR,
        action=(f"raise the bound at its declaration site ({bound_site}) so the "
                f"parent holds what it grants (a child can never mint the "
                f"widening itself)"),
        ref=bound_site)
    return record(family="ceiling",
                  refused={"param": param, "child": child_value,
                           "parent": parent_bound, "budget": is_budget},
                  blocked=False, alternatives=[narrow, raise_bound],
                  profile=profile)


def ownership_navigate(*, kind: str, resource: str | None = None,
                       mode: str | None = None, clause: str | None = None,
                       binding: str | None = None, returns: str | None = None,
                       handle_name: str | None = None, profile=None) -> dict:
    """Ownership (item 308, design §2.5). All alternatives are author-enacted -
    there is no policy knob, so the hint must not invent one. They are
    `candidate`, never `clears-this-gate`: the compiler does not re-synthesize the
    restructured source to re-run the gate, so it cannot promise the rewrite
    clears (§3). `kind` selects O1 / B1 / R0."""
    alts = []
    if kind == "o1":
        alts.append(alternative(
            enacts=ENACTS_AUTHOR,
            action=("let teardown run the inverse; the only legal explicit close "
                    "is the acquiring binding's own `undo`"),
            ref=binding or resource))
    elif kind == "b1":
        if clause == "compensate":
            alts.append(alternative(
                enacts=ENACTS_AUTHOR,
                action=(f"carry the data out as a value, not the resource handle "
                        f"`{resource}`; a compensate runs after every bracket "
                        f"closed"),
                ref="compensate"))
        else:
            alts.append(alternative(
                enacts=ENACTS_AUTHOR,
                action=(f"pass `{resource}` down as a call argument instead (the "
                        f"owner lends it per call); a borrow may not be "
                        f"{_B1_ESCAPE_TEXT.get(clause, 'escaped')}"),
                ref=clause))
    elif kind == "r0":
        stem = (handle_name or "Log")
        alts.append(alternative(
            enacts=ENACTS_AUTHOR,
            action=(f"declare a nominal opaque handle type (e.g. "
                    f"`type {stem}Handle`) and return it, not `{returns}`"),
            ref="handle-type"))
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unknown ownership navigate kind {kind!r}")
    return record(family="ownership",
                  refused={"kind": kind, "resource": resource, "mode": mode,
                           "clause": clause},
                  blocked=False, alternatives=alts, profile=profile)


_B1_ESCAPE_TEXT = {
    "state": "parked in activation state",
    "capture": "captured by a closure",
    "return": "returned across a signature",
    "carrier": "placed in an escaping carrier",
    "undo": "placed in an `undo` expression",
    "witnessed": "passed to a witnessed effect",
    "spawn": "seated in a `spawn` config",
    "handoff": "carried by a `handoff`",
}


def evidence_navigate(*, facet: str, threshold: str, fact: str,
                      producer: str, rule_line: str | None = None,
                      profile=None) -> dict:
    """Evidence (item 290, design §2.6). Split honestly on the recorded standing:

      * MISSING (`unavailable`): the facet has a closed-registry producer; name
        it (author) and offer the operator's unrooted-threshold acknowledgment;
      * RECORDED-but-below-threshold: `blocked`. No command manufactures
        confidence - the operator may lower the named rule, or the component must
        earn the facet via its named producer. The hint names the mechanism,
        never the outcome (re-running the producer may still fail)."""
    missing = fact.startswith("unavailable")
    if not missing:
        reason = (f"`{facet}` is recorded as `{fact}`, below the required "
                  f"`{facet} {threshold}`; no command manufactures confidence - "
                  f"the operator may lower the rule"
                  + (f" ({rule_line})" if rule_line else "")
                  + f", or the component must earn the facet via {producer}")
        return blocked_record(family="evidence", reason=reason,
                              refused={"facet": facet, "threshold": threshold,
                                       "fact": fact}, profile=profile)
    alts = [
        alternative(
            enacts=ENACTS_AUTHOR,
            action=(f"produce the `{facet}` facet: run {producer} so the fact is "
                    f"recorded, then re-admit"),
            ref=facet),
        alternative(
            enacts=ENACTS_OPERATOR,
            action=(f"acknowledge the unrooted `{facet}` threshold at the operator "
                    f"surface (an acknowledged PolicyError)"),
            ref=facet),
    ]
    return record(family="evidence",
                  refused={"facet": facet, "threshold": threshold, "fact": fact},
                  blocked=False, alternatives=alts, profile=profile)


def cache_navigate(*, kind: str, what: str, clause: str | None = None,
                   category: str | None = None, profile=None) -> dict:
    """Cache (item 310, design §2.8). Three shapes:

      * `add`: `cache external` with no freshness bound - name the missing clause
        with its grammar (author). Adding a `ttl` clears the freshness predicate
        by construction (a static duration, immutable operand), so `clears`;
      * `drop`: a `cache pure` freshness bound or a `cache capability`
        `invalidated_by` - name the clause to remove (author, `clears`);
      * `blocked`: an uncacheable category (a witnessed/acquire/deferred/
        compensate reach, a resource-carrying result, an interior extern) - there
        is no nearest-allowed spelling, and the hint must NOT imply reclassifying
        the extern (design §2.8, the unsafe suggestion)."""
    if kind == "add":
        alt = alternative(
            enacts=ENACTS_AUTHOR,
            action=(f"add a freshness bound to {what}: `cache external "
                    f"invalidated_by <token> ttl 5m` (an emission token some "
                    f"crossing fires, and/or a ttl)"),
            ref="freshness", clears=True)
        return record(family="cache", refused={"what": what},
                      blocked=False, alternatives=[alt], profile=profile)
    if kind == "drop":
        alt = alternative(
            enacts=ENACTS_AUTHOR,
            action=f"drop the `{clause}` clause on {what}",
            ref=clause, clears=True)
        return record(family="cache", refused={"what": what, "clause": clause},
                      blocked=False, alternatives=[alt], profile=profile)
    if kind == "blocked":
        reason = (f"cache on {what} is uncacheable ({category}); there is no "
                  f"freshness spelling that admits it - do not reclassify the "
                  f"extern to force a hit")
        return blocked_record(family="cache", reason=reason,
                              refused={"what": what, "category": category},
                              profile=profile)
    raise ValueError(f"unknown cache navigate kind {kind!r}")  # pragma: no cover
