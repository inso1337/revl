"""The admission kernel as a capability, enumerated in one file (item 544).

Item 520 (issue #1194) states the invariant the self-evolution programme rests
on: *the system may evolve its behaviour, but it may not unilaterally evolve
the rules that govern its authority.* Stated as a policy, that is a rule some
later generation can propose a change to, and the loop's proposal channel is
exactly the mechanism for proposing changes to rules. So it is not stated as a
policy here. It is stated as a capability the candidate cannot hold.

A DIFF CHECK IS NOT A CAPABILITY
--------------------------------
`tools/evolution_controller.py` (item 520) refuses a candidate whose CHANGED
FILES reach the kernel. That is a necessary check and its own design says what
it cannot do: it is answered after the fact against a changed-file set, so any
route that reaches the same state without editing an enumerated file walks past
it. The question this module asks is the other one, and it is structural:

    can this component HOLD the authority that reaches the kernel at all?

It is answered the way every other authority question in revl is answered, by
the attenuation product in `docs/capability-attenuation.md`: two capability
sets and one fold. The kernel's own held set is on the LEFT.

    held(kernel) n effective(C)  =  {}   ->  admit
    held(kernel) n effective(C) !=  {}   ->  REFUSE, naming both sets

There is no new algebra. `cap_order.disjoint` is the fold, the same predicate
`parallel.py` uses to prove two emissions independent, and the kernel elements
are ordinary `Cap`s under a reserved token namespace.

WHICH WAY IT FAILS
------------------
Toward refusing, at every unknown, and the unknowns are named rather than
defaulted:

* The unnameable `*` (a host emission or a first-class dispatch that no
  `requires` key can name) is **disjoint from nothing** - that is already
  `cap_order.disjoint`'s own rule, written there and not invented here, and it
  is the reading this check needs: a boundary that may reach anything may reach
  the kernel.
* An OMITTED `reaches [...]` clause on a `model role` resolves to that same
  `*` rather than to the reach its record renders (`effective_from_model_
  reach`), so an absent declaration is never read as a proof of narrowness.
  That is item 519's own resolution of the same question, and the choice
  `lower._spawn_emission_surface` already makes for a method that declares
  `emission` with no capability list, where `None` becomes `*` and not
  `set()`.
* A `key:`-namespaced element - a boundary whose service method declares
  `emission` with no capability list - is ALSO an undeclared reach, and it is
  the one this slice does not refuse. `_undeclared` says what the measurement
  was and §7 of the design carries it as the named residual. It is written
  down rather than silently assumed away.

WHAT IS ON THE KERNEL SIDE, AND WHY RETENTION IS
------------------------------------------------
`KERNEL_PATHS` is the enumeration: the admission decider, the attestation
chain, the taint lattice, the retention discipline, the gate crate, the census
baseline with its `NEVER_BASELINED` list, and `formal/`. Every entry is
asserted to exist in the tree by `tests/test_kernel_boundary_544.py`, because
an enumeration naming a file that is not there protects nothing.

Retention is on this side, and it is the entry a reader will argue with. A loop
permitted to "update indexes and retention policies" as ordinary behaviour
tuning is a loop that can extend its own `Retained[T, P]` deadline. That is an
authority change wearing the clothes of a cache setting, and `G-RETAIN` is the
guarantee it quietly relaxes. Item 532 measured how subtle the surface already
is with no loop touching it: the retention refusal was argument-blind across a
service seam, so a value past its deadline reached a provider's store with the
compiler silent.

ONE ENUMERATION, TWO CONSUMERS
------------------------------
`KERNEL_PATHS` here is the single list issue #1223 asks for. The diff-side
consumer is `tools/evolution_controller.py`, whose own `KERNEL_PATHS` is the
same tuple in the same order; when both land, that file imports this one rather
than keeping a copy, which is what "enumerated in one place" has to mean if it
is to mean anything. The capability-side consumer is `lower._check_kernel_
boundary`, which is this module's reason for sitting under `src/revl/` rather
than under `tools/`: the capability is enforced by the compiler, and the
compiler cannot import from `tools/`.
"""

from __future__ import annotations

from dataclasses import dataclass


#: The reserved capability namespace. A token whose head is `kernel` names a
#: boundary that reaches the admission kernel. No service in the tree declares
#: one, which is the point: the namespace exists so that a candidate REACHING
#: the kernel has to say so in the one vocabulary the attenuation product
#: already folds, and so that saying so is refusable.
KERNEL_NAMESPACE = "kernel"

#: Tree paths, matched segment-wise as PREFIXES. This is the enumeration item
#: 544 asks for in one file. It is the subject of an existence assertion in
#: `tests/test_kernel_boundary_544.py`.
#:
#: What is NOT here matters as much as what is, and the reasoning is the one
#: `tools/heldout_scoring.py` states for `HELD_OUT_FENCE`: `src/` at large,
#: `selfhost/`, `backends/` and the rest of `crates/` are the SUBJECT of the
#: loop. Fencing them would forbid the work the loop exists to produce.
KERNEL_PATHS: tuple[str, ...] = (
    "src/revl/admission.py",
    "src/revl/admit_profile.py",
    "src/revl/attest.py",
    "src/revl/taint.py",
    "src/revl/retention.py",
    "src/revl/kernel_boundary.py",
    "crates/revl-gate",
    "formal",
    "tools/gate_reference_census.py",
    "tools/gate_reference_census_baseline.json",
)


@dataclass(frozen=True)
class KernelCap:
    """One member of the kernel capability set.

    `token` is the capability token a component would have to hold to reach it.
    `guarantee` is the code the refusal CITES - the guarantee this member
    defends, not a label for the member. `paths` are the `KERNEL_PATHS` entries
    the member stands for, so a refusal can say which part of the tree the
    authority is the authority over. `why` is the one sentence a reader needs.
    """

    token: str
    guarantee: str
    paths: tuple[str, ...]
    why: str


#: The kernel's own held set. This is the LEFT side of every refusal below.
KERNEL_CAPS: tuple[KernelCap, ...] = (
    KernelCap(
        "kernel.admission", "G8",
        ("src/revl/admission.py", "src/revl/admit_profile.py",
         "src/revl/kernel_boundary.py"),
        "the admit decider and the profile a candidate is admitted under; a "
        "component that reaches it chooses the terms of its own admission",
    ),
    KernelCap(
        "kernel.attest", "G8",
        ("src/revl/attest.py",),
        "the attestation chain; a component that reaches it signs its own "
        "provenance",
    ),
    KernelCap(
        "kernel.taint", "G9",
        ("src/revl/taint.py",),
        "the authority lattice and its declassification rule; a component that "
        "reaches it mints its own trust",
    ),
    KernelCap(
        "kernel.retention", "G-RETAIN",
        ("src/revl/retention.py",),
        "the retention deadline; a component that reaches it extends the date "
        "its own data may be held to, which is an authority change wearing the "
        "clothes of a cache setting",
    ),
    KernelCap(
        "kernel.gate", "G8",
        ("crates/revl-gate",),
        "the embedded admission gate; a component that reaches it decides what "
        "the gate admits",
    ),
    KernelCap(
        "kernel.census", "G8",
        ("tools/gate_reference_census.py",
         "tools/gate_reference_census_baseline.json"),
        "the gate/reference census and its baseline, including the "
        "NEVER_BASELINED list; a component that reaches it edits the record of "
        "where the gate and the reference disagree",
    ),
    KernelCap(
        "kernel.formal", "G8",
        ("formal",),
        "the formal models; a component that reaches them changes what the "
        "proofs are about",
    ),
)

#: token -> KernelCap, for the refusal's lookup.
BY_TOKEN: dict[str, KernelCap] = {c.token: c for c in KERNEL_CAPS}

#: The retention member, named because two call sites cite it directly (the
#: capability fold here and the structural refusal in `admit_profile`).
RETENTION = BY_TOKEN["kernel.retention"]


def kernel_held() -> list:
    """The kernel's held set as `cap_order.Cap`s - the left side of the fold.

    Built rather than stored so there is one spelling of a kernel token in this
    file and the order's own parser is what reads it."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    return [cap_order.parse_cap(c.token) for c in KERNEL_CAPS]


def is_kernel_token(token: str) -> bool:
    """Whether a capability token names the kernel namespace.

    Matched on the NAMESPACE, not on membership of `KERNEL_CAPS`: a token
    spelled `kernel.something_not_enumerated_yet` is still a claim on the
    kernel, and reading it as an ordinary boundary because the enumeration has
    not caught up is the fail-open shape. An unenumerated kernel token is
    refused under `G8`, which is the namespace's own guarantee."""
    return token == KERNEL_NAMESPACE or token.startswith(KERNEL_NAMESPACE + ".")


def _undeclared(cap) -> bool:
    """Whether a fold element is an UNNAMEABLE boundary rather than a named one.

    `*` is the whole of it: a host emission or a first-class dispatch that no
    `requires` key can name. `cap_order.disjoint` already answers False for it,
    by its own rule and not by anything invented here - "it may reach any
    boundary, so it is never provably independent of anything" - and that is
    exactly the reading the kernel question needs.

    It is also where an omitted `reaches [...]` clause on a `model role` lands:
    `effective_from_model_reach` resolves `reach_declared: false` to `*` rather
    than to the reach the record happens to render, so an absent declaration is
    not read as a proof of narrowness. That is item 519's own resolution of the
    same question, and `lower._spawn_emission_surface`'s precedent of mapping
    `None` to `*` rather than `set()`.

    WHAT IS DELIBERATELY NOT HERE, measured rather than assumed. A
    `key:`-namespaced element - `lower._wire_cap`'s stand-in for a boundary
    whose service method declares `emission` with no capability list - is also
    an undeclared reach, and reading it as provably disjoint from the kernel is
    the weaker answer. It is not refused in this slice, and the reason is a
    measurement, not a preference: on this tree it is the ORDINARY spelling, so
    refusing it turns 14 tests across 7 files red and changes what
    `mcp.session.admit` accepts on every turn whose granted services spell
    `emission` bare. That is a composition-side change - the operator's
    services are the ones that would have to declare their tokens - and it is
    not this item's to make. `docs/design/545-kernel-boundary-capability.md`
    §7 carries it as the named residual, and
    `tests/test_kernel_boundary_544.py` pins it so it stays visible.
    """
    return cap.token == "*"


def offending(effective, *, undeclared_reaches_kernel: bool) -> list:
    """The kernel capabilities `effective` is NOT provably disjoint from.

    `effective` is a component's effective ceiling as `cap_order.Cap`s: what it
    holds, folded with what any authority surrogate it routes through can reach
    (item 519). Empty result means admitted - the component is provably unable
    to hold kernel authority. A non-empty result names, in `KERNEL_CAPS` order,
    each kernel member the component may reach.

    `undeclared_reaches_kernel` decides the UNDECLARED elements described in
    `_undeclared`, and it is a scope switch, never a fail-open default. It is
    True for a candidate admitted under the untrusted-author profile - source
    whose author is a model, which is what "a generated component" means here -
    and False for the first-party tree, which is the SUBJECT of the loop rather
    than a candidate passing through admission. `tools/evolution_controller.py`
    states the same boundary for its file fence: the fence is the judge, never
    the subject. A DECLARED kernel token is refused on both sides.

    Returns a list of `(KernelCap, Cap)`: the kernel member, and the element of
    `effective` that reaches it."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    held = kernel_held()
    hits: list = []
    for kernel_cap in held:
        member = BY_TOKEN.get(kernel_cap.token)
        for element in sorted(effective, key=lambda c: c.to_str()):
            if _undeclared(element):
                if undeclared_reaches_kernel:
                    hits.append((member, element))
                    break
                continue
            if is_kernel_token(element.token):
                # a declared claim on the namespace. `disjoint` answers this
                # correctly for an enumerated token; an UNENUMERATED one
                # (`kernel.whatever`) has no member to compare against, so it
                # is caught by `unenumerated` below rather than here.
                if not cap_order.disjoint(element, kernel_cap):
                    hits.append((member, element))
                    break
    return hits


def unenumerated(effective) -> list:
    """Declared kernel-namespace elements that `KERNEL_CAPS` does not enumerate.

    A candidate that writes `emission [kernel.something_new]` has made a claim
    on the kernel namespace that this file has no member for. Reading it as an
    ordinary boundary because the enumeration is behind is the fail-open shape,
    so it is refused under `G8` - the namespace's own guarantee - with the
    enumeration named so the reader can see what the tree does know about."""
    return sorted((c for c in effective
                   if is_kernel_token(c.token) and c.token not in BY_TOKEN),
                  key=lambda c: c.to_str())


def effective_from_model_reach(component: str, manifest: dict | None) -> list:
    """The per-edge effective ceiling item 519 already computed, if it is there.

    `lower._check_model_attenuation` (item 519, PR #1253) writes one record per
    `route model` edge into `manifest["model_reach"]`, each carrying:

      * `effective` - the pair's ceiling as a list of capability strings. This
        is the statement this check wants and the reason it does not re-derive
        reach from the AST: two derivations of one ceiling are two things that
        can disagree, and the product's own record is the authoritative one.
      * `reach_declared` - False when the role wrote no `reaches [...]` clause.
        An absent clause is NOT a proof of narrowness, so a record carrying
        `reach_declared: false` contributes the unnameable `*` rather than the
        (empty, hence narrow-looking) reach the record renders.

    Returns the extra fold elements this component's model edges contribute, or
    `[]` when the composition declares no model role - which is every
    composition that does not opt in, and every composition on the tree until
    item 519 lands. Written against the record SHAPE rather than against the
    branch, so it is inert and harmless if the shape never arrives."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    rows = (manifest or {}).get("model_reach") or []
    extra: list = []
    for row in rows:
        if row.get("component") != component:
            continue
        if not row.get("reach_declared", False):
            # an omitted clause resolves to the unnameable `*`, which no held
            # set covers and which is disjoint from nothing - item 519's own
            # resolution of the same question.
            extra.append(cap_order.Cap("*", ()))
            continue
        for text in row.get("effective") or []:
            try:
                extra.append(cap_order.parse_cap(text))
            except cap_order.CapError:
                extra.append(cap_order.Cap("*", ()))
    return extra
