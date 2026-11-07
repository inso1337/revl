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
* A `svc:`-namespaced element - a boundary whose service method declares
  `emission` with no capability list - is also an undeclared reach, and it is
  refused by the same switch. It cannot be decided from the element: the same
  token spells a service with a bare `emission` method (an undeclared reach)
  and a service with no emission method at all (a proof of the opposite, which
  the coverage fold needs as a boundary identity). The fact lives in the
  declarations, so the set is computed there (`lower._undeclared_elements`) and
  passed to `offending`. `_undeclared` says why, and what the three
  measurements of the namespace-wide arm disagreed about.

WHAT IS ON THE KERNEL SIDE, AND WHY RETENTION IS
------------------------------------------------
`KERNEL_PATHS` is the kernel's view of `EVALUATOR_PATHS` below: the admission
decider, the attestation chain, the taint lattice, the retention discipline, the
gate crate, the census baseline with its `NEVER_BASELINED` list, and `formal/`.
Every entry is asserted to exist in the tree by `tests/test_kernel_boundary_544.py`
and `tests/test_evaluator_protocol.py`, because an enumeration naming a file that
is not there protects nothing.

Retention is on this side, and it is the entry a reader will argue with. A loop
permitted to "update indexes and retention policies" as ordinary behaviour
tuning is a loop that can extend its own `Retained[T, P]` deadline. That is an
authority change wearing the clothes of a cache setting, and `G-RETAIN` is the
guarantee it quietly relaxes. Item 532 measured how subtle the surface already
is with no loop touching it: the retention refusal was argument-blind across a
service seam, so a value past its deadline reached a provider's store with the
compiler silent.

ONE ENUMERATION, THREE CONSUMERS
--------------------------------
`KERNEL_PATHS` here was the single list issue #1223 asks for. The diff-side
consumer is `tools/evolution_controller.py`, which IMPORTS it rather than
keeping a copy, which is what "enumerated in one place" has to mean if it is to
mean anything. The capability-side consumer is `lower._check_kernel_boundary`,
which is this module's reason for sitting under `src/revl/` rather than under
`tools/`: the capability is enforced by the compiler, and the compiler cannot
import from `tools/`.

THE EVALUATOR, NOT ONLY THE KERNEL
----------------------------------
Issue #2802 measured a second list problem above the kernel. The judges of the
evolution loop were three hand-written path lists (`AUTHORITY_FENCE`,
`HELD_OUT_FENCE`, `KERNEL_PATHS`), and the modules that assemble the scorecard
(`tools/evolution_reward.py`, `tools/evolution_progress.py`) were on none of
them, so a diff could edit the scorer, or the retention rule, and be judged by
the code it had just edited. `EVALUATOR_PATHS` is the single enumeration: the
evaluator's code, data and protocol. Each row also names the refusals it drives,
and `KERNEL_PATHS`, `AUTHORITY_FENCE` and `HELD_OUT_FENCE` are views of it
rather than copies. The kernel is one view among three, which is why the list
that used to be the whole boundary is now a projection of the boundary.

`protocol_digest` is the identity half of the same argument. A scorecard could
carry the census baseline's digest, the formal axioms' digest and the draw's
digest, and still say nothing about which scorer, component list or retention
rule produced it. The digest is taken from the SCORER's own checkout, so two
rounds can show they were scored by the same protocol. It proves identity, not
correctness: who holds the expected value and how it rotates is the custody
question in issue #2762, and a digest is not a solution to it.
"""

from __future__ import annotations

from dataclasses import dataclass


#: The reserved capability namespace. A token whose head is `kernel` names a
#: boundary that reaches the admission kernel. No service in the tree declares
#: one, which is the point: the namespace exists so that a candidate REACHING
#: the kernel has to say so in the one vocabulary the attenuation product
#: already folds, and so that saying so is refusable.
KERNEL_NAMESPACE = "kernel"

# --------------------------------------------------------------- the evaluator

#: The evaluator: every file or tree that decides what a scorecard measures, what
#: the held-out draw is, what counts as an admission, and what the controller
#: refuses. It is ONE enumeration, and the three path constants the refusals use
#: are views of it:
#:
#:   "kernel"    -> KERNEL_PATHS      the controller's KERNEL_INTERSECTION
#:   "authority" -> AUTHORITY_FENCE   the controller's FENCE_TOUCHED
#:   "held_out"  -> HELD_OUT_FENCE    the held-out scorer's `diff-reaches-fence`
#:
#: Each row is `(path, refusals)`. `path` is matched segment-wise as a PREFIX
#: (`tools/evolution_controller.py::_under`), exactly as `KERNEL_PATHS` entries
#: were. A path that drives two refusals appears ONCE: the census and its
#: baseline are kernel paths and also decide the held-out draw's allowance, so
#: they carry both tags.
#:
#: What is NOT here matters as much as what is, and the reasoning is the one
#: `tools/heldout_scoring.py` states for `HELD_OUT_FENCE`: `src/` at large,
#: `selfhost/`, `backends/` and the rest of `crates/` are the SUBJECT of the
#: loop. Fencing them would forbid the work the loop exists to produce. The
#: evaluator is the judge, never the subject.
_EVALUATOR: tuple[tuple[str, tuple[str, ...]], ...] = (
    # The admission kernel (item 544). The census tool and its baseline decide
    # the census allowance AND the held-out bypass allowance, so they drive two
    # refusals and are named once.
    ("src/revl/admission.py", ("kernel",)),
    ("src/revl/admit_profile.py", ("kernel",)),
    ("src/revl/attest.py", ("kernel",)),
    ("src/revl/taint.py", ("kernel",)),
    ("src/revl/retention.py", ("kernel",)),
    # This file DEFINES the held-out fence, so it is on that fence too: a diff
    # that edits the enumeration must not be scored by the fence it widened.
    # The controller still refuses it with KERNEL_INTERSECTION, not this fence.
    ("src/revl/kernel_boundary.py", ("kernel", "held_out")),
    ("crates/revl-gate", ("kernel",)),
    ("formal", ("kernel",)),
    ("tools/gate_reference_census.py", ("kernel", "held_out")),
    ("tools/gate_reference_census_baseline.json", ("kernel", "held_out")),
    # The held-out draw and the files that decide what counts as a divergence.
    # The controller refuses them too: a proposal that edited the scorer has not
    # been scored by it.
    ("tools/heldout_scoring.py", ("held_out", "authority")),
    ("tests/test_heldout_scoring.py", ("held_out", "authority")),
    ("tools/build_gate_crate.py", ("held_out", "authority")),
    ("tests/test_selfhost_lower.py", ("held_out", "authority")),
    # The scoring protocol: the reward conjunction, the progress counters, the
    # lifecycle that reduces stage records, and the loader that puts this
    # enumeration in front of the tools without importing the compiler.
    ("tools/evolution_controller.py", ("authority",)),
    ("tests/test_evolution_controller.py", ("authority",)),
    ("tools/evolution_reward.py", ("authority",)),
    ("tests/test_evolution_reward.py", ("authority",)),
    ("tools/evolution_progress.py", ("authority",)),
    ("tests/test_evolution_progress.py", ("authority",)),
    ("tools/evaluator_protocol.py", ("held_out", "authority")),
    ("tests/test_evaluator_protocol.py", ("authority",)),
)

#: Every file or tree the evaluator is made of, in one enumeration.
EVALUATOR_PATHS: tuple[str, ...] = tuple(path for path, _ in _EVALUATOR)


def evaluator_fence(name: str) -> tuple[str, ...]:
    """The evaluator paths one refusal is driven by, in enumeration order.

    `name` is `"kernel"`, `"authority"` or `"held_out"`. There is no fourth
    value: a name that matches nothing returns an EMPTY tuple, so a typo in a
    caller narrows a fence to nothing rather than widening it to everything.
    """
    return tuple(path for path, refusals in _EVALUATOR if name in refusals)


#: Tree paths, matched segment-wise as PREFIXES. This is the kernel view of
#: `EVALUATOR_PATHS` (issue #1223). It is the subject of an existence assertion
#: in `tests/test_kernel_boundary_544.py` and `tests/test_evaluator_protocol.py`.
KERNEL_PATHS: tuple[str, ...] = evaluator_fence("kernel")


def protocol_digest(root, paths=EVALUATOR_PATHS) -> str:
    """`sha256:<hex>` over the evaluator's bytes in `root`, its own checkout.

    The digest covers the TRACKED files under `paths`, read from the working
    tree. Two consequences, both deliberate:

    * an edit that has not been committed is visible, which is the point: the
      question is what this process is about to run, not what it once committed;
    * build output a run left behind (`formal/.lake`, `crates/*/target`) is NOT
      visible, because hashing a directory would make two rounds differ because
      one of them happened to have been built. The tracked set is what a
      candidate can read and change, the same definition `seed_is_in_tree` uses.

    A `path` that names no tracked file is a refusal, not a silent omission: an
    enumeration missing a member is the hole this function exists to make
    visible. An EMPTY `paths` is refused for the same reason: `git ls-files`
    with no pathspec answers with the whole tree, which would make a typo in a
    fence name look like a protocol instead of an empty one. `paths` is a
    parameter so a test can hash a staged checkout; production callers take
    `EVALUATOR_PATHS`.
    """
    import hashlib  # noqa: PLC0415 - the compiler must not pay for this module
    from pathlib import Path  # noqa: PLC0415

    if not paths:
        raise ValueError(
            "protocol_digest: an empty path list names no protocol, and "
            "`git ls-files` with no pathspec would hash the whole tree")
    root = Path(root)
    files = _evaluator_files(root, paths)
    hasher = hashlib.sha256()
    for rel in files:
        data = (root / rel).read_bytes()
        hasher.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        hasher.update(str(len(data)).encode("ascii") + b"\0")
        hasher.update(data)
    return "sha256:" + hasher.hexdigest()


def _evaluator_files(root, paths) -> list:
    """The tracked files under `paths`, sorted, or a refusal.

    `git ls-files` rather than a walk: it is the tracked set that both sides of
    the digest agree on, and it excludes a build directory without a list of
    directories to exclude. A path that resolves to no tracked file raises,
    because a digest that silently drops a member names a protocol it did not
    read.
    """
    import subprocess  # noqa: PLC0415

    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *paths],
        capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            "protocol_digest: git ls-files failed in " + str(root) + ": "
            + proc.stderr.decode("utf-8", "replace").strip())
    files = sorted(p for p in proc.stdout.decode(
        "utf-8", "surrogateescape").split("\0") if p)
    for path in paths:
        prefix = path.rstrip("/") + "/"
        if not any(f == path or f.startswith(prefix) for f in files):
            raise FileNotFoundError(
                path + " names no tracked file under " + str(root)
                + ", so the protocol digest would omit a member")
    return files


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


def _undeclared(cap, undeclared=()) -> bool:
    """Whether a fold element is an UNDECLARED reach rather than a declared one.

    Two elements answer yes, and the second is the arm issue #1265's exit asked
    for (taken by issue #2105):

    * `*` - a host emission or a first-class dispatch that no `requires` key
      can name. `cap_order.disjoint` already answers False for it, by its own
      rule and not by anything invented here - "it may reach any boundary, so
      it is never provably independent of anything" - and that is exactly the
      reading the kernel question needs. It is also where an omitted
      `reaches [...]` clause on a `model role` lands:
      `effective_from_model_reach` resolves `reach_declared: false` to `*`
      rather than to the reach the record happens to render, so an absent
      declaration is not read as a proof of narrowness. That is item 519's own
      resolution of the same question, and `lower._spawn_emission_surface`'s
      precedent of mapping `None` to `*` rather than `set()`.
    * a `svc:`-namespaced element - `lower._undeclared_cap`'s stand-in for a
      boundary whose service method declares `emission` with no capability
      list - when `undeclared` names it. Item 561 moved that element off the
      consumer's wiring key and onto the SERVICE the method is declared on,
      which changed what it is named BY and not whether it is undeclared: it is
      a nameable token either way, so `cap_order.disjoint` still answers True
      against every kernel token and the `*` arm alone admits it. Item 545
      measured the namespace-wide arm at 14 tests across 7 files and item 561
      re-measured 17 and then 9; the measurements disagreed because the
      namespace alone is not the rule.

    WHY THE SET IS A PARAMETER, and not read off the token. `lower._held_
    capabilities_pairs` builds a `svc:` element on TWO occasions and only one of
    them is an undeclared reach:

    =======================================  ==============================
    a required service with a bare `emission`  an effect is declared, its reach
    method                                     is not - undeclared
    a required service with NO emission        there is no effect - a proof of
    method at all                              the opposite
    =======================================  ==============================

    The second exists only so the coverage fold has a boundary identity to
    compare, and the tree enforces it: a provider of a plain `fn` that emits is
    refused under G4 with `` `Kv.get` is declared plain, but this implementation
    reaches `fs.write` ``. Once both are `Cap`s the token cannot tell them
    apart, so a predicate reading the namespace alone refuses a candidate
    composing only PURE services - the ordinary admitted turn, and 16 of the 17
    reds the namespace-wide arm measured. The fact lives in the DECLARATIONS, so
    it is computed there (`lower._undeclared_elements`, where the service table
    is) and passed here. The default is empty, so a caller that has no
    declarations to consult gets the `*` arm and nothing else.
    """
    return cap.token == "*" or cap.token in undeclared


def offending(effective, *, undeclared_reaches_kernel: bool,
              undeclared=()) -> list:
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

    `undeclared` is the second half of the same scope decision and is the
    caller's to supply, because it is a fact about the DECLARATIONS rather than
    about the element (`_undeclared`): the `svc:` elements whose service leaves
    the reach undeclared. A caller with no service table passes nothing, and
    then only `*` is an undeclared reach.

    Returns a list of `(KernelCap, Cap)`: the kernel member, and the element of
    `effective` that reaches it."""
    from . import cap_order  # noqa: PLC0415 - lazy, avoids an import cycle
    held = kernel_held()
    hits: list = []
    for kernel_cap in held:
        member = BY_TOKEN.get(kernel_cap.token)
        for element in sorted(effective, key=lambda c: c.to_str()):
            if _undeclared(element, undeclared):
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
                extra.append(cap_order.parse_stored_cap(text))
            except cap_order.CapError:
                extra.append(cap_order.Cap("*", ()))
    return extra
