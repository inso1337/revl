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
