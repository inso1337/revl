"""The repair loop — faults that fix themselves, within policy (roadmap item 62).

Every piece this loop needs already landed. What was missing was the sentence
that ties them together: *a component faults at runtime, and the system repairs
itself — inside declared bounds, and stops for a human exactly when it would
step outside them.* This module is that orchestration and nothing else: it
reimplements none of the machinery, it wires it.

The loop, one landed piece per step:

    fault      the runtime's causal trace (item 27, `why_runtime.Trace`) — the
               *why*: the cause chain behind the component's recorded failure.
    slice      the fault's timeline slice (item 40, `session.bisect`) — the
               first recorded step at which the failure predicate flips.
    eligible?  the SELF-REPAIR POLICY (this module) — may this component
               self-repair at all, and which capabilities may a repair touch?
               Closed by default: with no policy, nothing self-repairs.
    candidate  a regenerated component the agent supplies, OR one the reuse
               check finds already built (item 49, `registry.resolve`) — maybe
               the fix already exists and need not be generated.
    gauntlet   the proving ground (item 31, `mcp.gauntlet`) — admission +
               lifecycle no-residue, graded not thrown.
    policy     the boundary policy (item 33, `revl.policy`) — nothing reaches
               what it may not.
    widen?     the boundary-widening rule (item 21, `revl.audit_diff`) — a
               repair that WIDENS what the composition reaches outside the
               system stops for a human ack instead of auto-swapping. This is
               the one place the unattended loop hands control back.
    swap       the hot-swap (item 23, `session.swap`) — the remediation. Wired
               as a pluggable strategy so item 59's verified canary can drop in
               later as a second `RemediationStrategy` without touching the loop.
    authority  who/what authorized it (item 55, operator authority) — in
               unattended mode the authority IS the self-repair policy; a bound
               operator token is recorded alongside it.

The **incident dossier** is the point of the whole exercise: a structured
report that reconstructs every step above — fault, why, slice, candidate,
gauntlet/policy/widening verdicts, swap, authority — from the causal trace and
the loop's own inputs, so an incident can be read back end to end after the
fact with no live runtime.

Graded, never thrown (the gauntlet's discipline, docs/gauntlet.md): a missing
trace, an ineligible component, a candidate that will not compile, a widening
that needs a human — each is a *status* in the dossier, not an exception. The
running composition is mutated only by the final `session.swap`, and only when
every gate before it was green.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import audit_diff, why_runtime
from ..errors import RevlError
from ..policy import Policy, component_realms, evaluate as policy_evaluate
from ..why import CHAIN, TraceStep, WhyTrace

_ROADMAP_ITEM = 62

# incident statuses (the `incident.status` field). Exactly one is terminal per
# run; the dossier always carries the full step record regardless.
STATUS_REPAIRED = "repaired"          # swapped, unattended, every gate green
STATUS_AWAITING_ACK = "awaiting-ack"  # a widening stopped the loop (item 21)
STATUS_INELIGIBLE = "ineligible"      # the self-repair policy forbids it
STATUS_NO_CANDIDATE = "no-candidate"  # nothing to swap, and reuse found nothing
STATUS_REJECTED = "rejected"          # gauntlet refused / policy violated
STATUS_PLANNED = "planned"            # apply=False: every gate green, not swapped


# ======================================================================
# the self-repair policy (this module's own contribution)
# ======================================================================
#
# Item 33's boundary policy answers "what may anything in the composition
# reach?" — a property of the *composition*. The self-repair policy answers a
# different question the repair loop needs: "which components may repair
# themselves unattended, and how far may a repair go before a human must weigh
# in?" It reuses item 33's realm resolution (`component_realms`) and the same
# fnmatch glob idiom, so a realm-scoped eligibility rule means the same thing it
# does everywhere else.


@dataclass(frozen=True)
class Eligibility:
    """One rule naming a set of self-repairable subjects by glob.

    `scope` is "component" (match the component name) or "realm" (match any
    realm the component is isolated into — resolved with item 33's
    `component_realms`, so isolation means the same thing here as in the policy).
    """

    scope: str            # "component" | "realm"
    selector: str         # the glob

    def selects(self, name: str, realms: frozenset[str]) -> bool:
        from fnmatch import fnmatchcase  # noqa: PLC0415
        if self.scope == "component":
            return fnmatchcase(name, self.selector)
        return any(fnmatchcase(realm, self.selector) for realm in realms)


@dataclass(frozen=True)
class SelfRepairPolicy:
    """Which components may self-repair, which capabilities a repair may touch,
    and whether a capability-widening repair must stop for a human ack.

    Closed by default: an empty policy makes *nothing* eligible, so a system
    with no declared self-repair bounds never repairs itself unattended — the
    safe floor, exactly as item 55's operator profile leaves everything ungated
    only when explicitly absent, but inverted here because self-repair is the
    privileged direction.
    """

    eligibility: tuple[Eligibility, ...] = ()
    # capability globs a repair's boundary may reach; None = "inherit the
    # running boundary" (no new capability may appear without an ack).
    may_touch: tuple[str, ...] | None = None
    # a repair that widens the composition's outward reach stops for a human
    # ack rather than auto-swapping (item 21's boundary-widening rule).
    ack_on_widen: bool = True
    source: str | None = None

    def eligible(self, name: str, realms: frozenset[str]) -> Eligibility | None:
        for rule in self.eligibility:
            if rule.selects(name, realms):
                return rule
        return None

    def out_of_bounds(self, capabilities: list[str]) -> list[str]:
        """Capabilities the candidate reaches that the policy's `may_touch`
        bound does not permit. Empty when `may_touch` is None (inherit) — a new
        capability is then caught by the widening check instead, which is the
        gentler gate (ack, not refuse)."""
        if self.may_touch is None:
            return []
        from fnmatch import fnmatchcase  # noqa: PLC0415
        permitted = self.may_touch
        return sorted(
            cap for cap in capabilities
            # `*` (unnameable reach) is bounded only by a literal `*`, mirroring
            # revl.policy._allowed — an unnameable reach is never in-bounds by a
            # named glob.
            if not ((cap == "*" and "*" in permitted)
                    or (cap != "*" and any(fnmatchcase(cap, p) for p in permitted))))


class SelfRepairPolicyError(RevlError):
    """A malformed self-repair policy (a parse error, not a refusal)."""


def parse_self_repair_policy(spec, source: str | None = None) -> SelfRepairPolicy:
    """Parse a self-repair policy from a dict (JSON) or the small line DSL.

        component Cache*  may self-repair
        realm     edge    may self-repair
        self-repair may touch kv, log*
        self-repair may widen              # turns the ack-on-widen rule OFF

    JSON equivalent:

        {"eligible": [{"component": "Cache*"}, {"realm": "edge"}],
         "mayTouch": ["kv", "log*"], "ackOnWiden": true}
    """
    if isinstance(spec, SelfRepairPolicy):
        return spec
    if isinstance(spec, dict):
        return _parse_json(spec, source)
    if isinstance(spec, str):
        return _parse_dsl(spec, source)
    raise SelfRepairPolicyError(
        source, 1, "a self-repair policy must be a dict, DSL text, or SelfRepairPolicy")


def _parse_json(doc: dict, source: str | None) -> SelfRepairPolicy:
    rules: list[Eligibility] = []
    for entry in doc.get("eligible") or []:
        if "component" in entry:
            rules.append(Eligibility("component", entry["component"]))
        elif "realm" in entry:
            rules.append(Eligibility("realm", entry["realm"]))
        else:
            raise SelfRepairPolicyError(
                source, 1, "an eligibility entry needs `component` or `realm`")
    may_touch = doc.get("mayTouch")
    return SelfRepairPolicy(
        tuple(rules),
        tuple(may_touch) if may_touch is not None else None,
        bool(doc.get("ackOnWiden", True)),
        source)


def _parse_dsl(text: str, source: str | None) -> SelfRepairPolicy:
    rules: list[Eligibility] = []
    may_touch: list[str] | None = None
    ack_on_widen = True
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("self-repair"):
            rest = line[len("self-repair"):].strip()
            if rest.lower().startswith("may touch"):
                tokens = rest[len("may touch"):]
                may_touch = [t.strip() for t in tokens.split(",") if t.strip()]
            elif rest.lower() in ("may widen", "may-widen"):
                ack_on_widen = False
            elif rest.lower() in ("needs ack on widen", "ack on widen"):
                ack_on_widen = True
            else:
                raise SelfRepairPolicyError(
                    source, lineno,
                    f"expected `self-repair may touch ...` / `may widen`: {raw.strip()!r}")
            continue
        parts = line.split(None, 1)
        scope = parts[0].lower()
        if scope not in ("component", "realm") or len(parts) < 2:
            raise SelfRepairPolicyError(
                source, lineno,
                f"expected `component <glob> may self-repair` or "
                f"`realm <glob> may self-repair`: {raw.strip()!r}")
        rest = parts[1]
        if "may self-repair" not in rest.lower():
            raise SelfRepairPolicyError(
                source, lineno, f"expected `may self-repair`: {raw.strip()!r}")
        selector = rest[:rest.lower().index("may self-repair")].strip()
        if not selector:
            raise SelfRepairPolicyError(
                source, lineno, f"rule names no {scope}: {raw.strip()!r}")
        rules.append(Eligibility(scope, selector))
    return SelfRepairPolicy(tuple(rules),
                            tuple(may_touch) if may_touch is not None else None,
                            ack_on_widen, source)


# ======================================================================
# small pure helpers over the landed surfaces
# ======================================================================


def _compile(candidate: dict, manifest: dict | None = None,
             over_the_transport: bool = True):
    """Compile a candidate (`source` inline or `files` paths) through
    `server.compile_under_authoring`, the one compiler door for agent-supplied
    source. Returns the IR, or raises RevlError — the caller grades that.

    A repair candidate is agent-authored like any other, and the loop hands it
    to `gauntlet.run` (which boots it). Compiling it with no profile made the
    repair loop a second door past the authoring trust `revl_check` /
    `revl_admit` / `revl_swap` enforce. Lazy import: `server` imports this
    module."""
    from .server import compile_under_authoring  # noqa: PLC0415 — cycle

    source = candidate.get("source")
    files = candidate.get("files")
    if source is None and not files:
        raise ValueError("candidate provides neither `source` nor `files`")
    return compile_under_authoring(source, files, manifest=manifest,
                                   modules=candidate.get("modules"),
                                   over_the_transport=over_the_transport)


def _capabilities_reached(audit: dict) -> list[str]:
    """Every capability token the audit's boundary reaches, across all
    components — the set the self-repair `may_touch` bound is checked against.
    Reuses item 33's `component_reach` so 'capability' means one thing."""
    from ..policy import component_reach  # noqa: PLC0415
    tokens: set[str] = set()
    for name in (audit.get("boundary") or {}):
        for reach in component_reach(audit, name):
            tokens.add(reach.token)
    return sorted(tokens)


def fault_section(component: str, trace: why_runtime.Trace,
                  running_ir: dict | None) -> dict:
    """The *why* (item 27) and, when a running IR is given, the prediction-vs-
    actuality oracle. The cause chain is the fault's causal explanation; its
    root is the head of the incident narrative."""
    frames = trace.cause_chain(component)
    chain = [
        {"component": f.component, "event": f.event,
         "transition": f.transition, "cause": f.cause, "note": f.note}
        for f in frames
    ]
    root = chain[-1] if chain else None
    section = {
        "component": component,
        "chain": chain,
        "root": root,
        "recorded": bool(frames) and frames[0].cause.get("kind") != "unrecorded",
        "render": why_runtime.render_chain(component, frames),
    }
    if running_ir is not None:
        try:
            section["oracle"] = why_runtime.oracle(running_ir, component, trace)
        except Exception as exc:  # noqa: BLE001 — the oracle is best-effort here
            section["oracle"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return section


def slice_section(session, component: str, predicate: str | None) -> dict:
    """The fault's timeline slice (item 40): the first recorded step at which
    `predicate` flips, by binary search over the recording. Reported as
    `unavailable` — never crashed — when the session has no recording or no
    predicate was given; the loop does not require it."""
    if predicate is None:
        return {"available": False,
                "reason": "no bisect predicate supplied — pass `predicate` to "
                          "localize the fault to a step (item 40)"}
    try:
        result = session.bisect(component, predicate)
    except Exception as exc:  # noqa: BLE001 — a session without recording, etc.
        return {"available": False, "predicate": predicate,
                "reason": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "predicate": predicate, "bisect": result}


# ======================================================================
# remediation strategy — swap now, canary pluggable later
# ======================================================================


class RemediationStrategy:
    """How a proved candidate is put into the running system.

    The loop calls `remediate(session, ir, origin)` and records whatever dict it
    returns. Two strategies are anticipated; only the swap is built here,
    deliberately (item 59's verified canary is being built in parallel and is
    not a dependency of this loop):

      * `SwapRemediation` — the landed hot-swap (item 23). Atomic: a rejected
        migration rolls the whole thing back (session.swap's own contract).
      * a future `CanaryRemediation` — route a fraction of traffic to the
        candidate, watch it, then promote or abort (item 59). It implements the
        SAME interface, so when it lands it drops in as a constructor argument
        to `run_repair` with no change to the loop below.
    """

    name = "abstract"

    def remediate(self, session, ir: dict, origin: dict | None) -> dict:
        raise NotImplementedError


class SwapRemediation(RemediationStrategy):
    """Remediate by hot-swapping the candidate in (item 23)."""

    name = "swap"

    def remediate(self, session, ir: dict, origin: dict | None) -> dict:
        state = session.swap(ir, origin=origin)
        return {"strategy": self.name, "applied": True, "swap": state}


def _canary_follow_on() -> dict:
    """The pluggable-canary hook, surfaced in every dossier so the follow-on is
    documented at the seam it will attach to — not depended on (item 59 is not
    on main; see docs/repair-loop.md)."""
    return {
        "roadmapItem": 59,
        "strategy": "canary",
        "status": "not-wired",
        "note": "remediation is a pluggable RemediationStrategy. Today the loop "
                "wires the landed hot-swap (item 23). When the verified canary "
                "(item 59) lands, a CanaryRemediation implementing the same "
                "interface is passed to run_repair — the loop does not change.",
    }


# ======================================================================
# the loop
# ======================================================================


def run_repair(session, arguments: dict,
               strategy: RemediationStrategy | None = None, *,
               over_the_transport: bool = True) -> dict:
    """Run the unattended repair loop for one faulting component and return the
    incident dossier.

    Arguments (all JSON-friendly, MCP-shaped):
      * `component`   (required) — the faulting component.
      * `trace`       — the causal trace (item 27): a list of event dicts, or
                        `traceFile` a path to a `revl run --trace` JSONL.
      * `predicate`   — a bisect predicate to slice the fault to a step (item 40).
      * `candidate`   — the regenerated repair: `{source|files|modules}`.
      * `need`        — a need spec; when no candidate is given (or to prefer
                        reuse), the registry is asked whether the fix already
                        exists (item 49).
      * `selfRepairPolicy` — the self-repair policy (dict, DSL text, or object).
      * `boundaryPolicy`   — an item-33 boundary policy (Policy or its text).
      * `accept`      — crossings pre-acknowledged for the widening gate (item 21).
      * `apply`       — perform the swap (default True); False plans only.

    The running composition is mutated only by the swap, and only when every
    gate is green and no unacknowledged widening remains. Every other outcome
    leaves it untouched and is reported as a status.
    """
    strategy = strategy or SwapRemediation()
    component = arguments.get("component")
    if not component:
        return _session_result(STATUS_REJECTED, component,
                               note="`component` is required — the faulting "
                                    "component to repair")

    running_ir = session.ir if getattr(session, "loaded", False) else None

    # 1. FAULT — the why (item 27) and the timeline slice (item 40).
    trace = _load_trace(arguments)
    fault = (fault_section(component, trace, running_ir) if trace is not None
             else {"component": component, "recorded": False,
                   "reason": "no causal trace supplied (`trace` or `traceFile`)"})
    sliced = slice_section(session, component, arguments.get("predicate"))

    # 2. ELIGIBILITY — the self-repair policy gate (this module).
    policy = _load_self_repair_policy(arguments)
    realms = (component_realms(running_ir.get("manifest") or {}, component)
              if running_ir is not None else frozenset())
    rule = policy.eligible(component, realms)
    eligibility = {
        "eligible": rule is not None,
        "by": {"scope": rule.scope, "selector": rule.selector} if rule else None,
        "policySource": policy.source,
        "note": ("the self-repair policy authorizes this component to repair "
                 "itself unattended" if rule is not None else
                 "no self-repair rule selects this component — closed by "
                 "default, the loop halts and hands off to a human"),
    }
    dossier_steps: list[dict] = []
    if rule is None:
        return _assemble(STATUS_INELIGIBLE, component, fault, sliced, eligibility,
                         candidate=None, verdicts=None, remediation=None,
                         authority=_authority(session, rule, applied=False),
                         steps=dossier_steps)

    # 3. CANDIDATE — regenerate (supplied) or reuse (item 49).
    candidate_arg, reuse = _resolve_candidate(arguments, running_ir)
    if candidate_arg is None:
        candidate = {"origin": "none", "reuse": reuse,
                     "note": "no candidate supplied and the registry reuse check "
                             "found nothing admissible — nothing to swap"}
        return _assemble(STATUS_NO_CANDIDATE, component, fault, sliced, eligibility,
                         candidate=candidate, verdicts=None, remediation=None,
                         authority=_authority(session, rule, applied=False),
                         steps=dossier_steps)
    candidate = {
        "origin": "reused" if reuse and reuse.get("chosen") else "regenerated",
        "reuse": reuse,
    }

    # 4/5/6/7. GAUNTLET (31) -> POLICY (33) -> WIDENING (21).
    verdicts, gate = _run_gates(session, candidate_arg, running_ir, policy,
                                set(arguments.get("accept") or []),
                                over_the_transport=over_the_transport)
    if gate["blocked"]:
        status = (STATUS_AWAITING_ACK if gate["reason"] == "widening"
                  else STATUS_REJECTED)
        return _assemble(status, component, fault, sliced, eligibility,
                         candidate=candidate, verdicts=verdicts,
                         remediation={"strategy": strategy.name, "applied": False,
                                      "blockedBy": gate["reason"],
                                      "canaryFollowOn": _canary_follow_on()},
                         authority=_authority(session, rule, applied=False,
                                              pending_ack=gate["reason"] == "widening"),
                         steps=dossier_steps, gate=gate)

    # 8. SWAP (23) — the remediation. Only reached with every gate green.
    apply = arguments.get("apply", True)
    if not apply:
        remediation = {"strategy": strategy.name, "applied": False,
                       "note": "apply=False — every gate is green; the swap was "
                               "not performed (a rehearsal, item revl_plan-style)",
                       "canaryFollowOn": _canary_follow_on()}
        return _assemble(STATUS_PLANNED, component, fault, sliced, eligibility,
                         candidate=candidate, verdicts=verdicts,
                         remediation=remediation,
                         authority=_authority(session, rule, applied=False),
                         steps=dossier_steps, gate=gate)

    try:
        remediation = strategy.remediate(session, gate["candidate_ir"],
                                         origin=candidate_arg)
    except Exception as exc:  # noqa: BLE001 — a swap failure is a graded result
        remediation = {"strategy": strategy.name, "applied": False,
                       "error": f"{type(exc).__name__}: {exc}",
                       "note": "the remediation raised; the session reports the "
                               "running composition's state (swap is atomic)"}
        remediation["canaryFollowOn"] = _canary_follow_on()
        return _assemble(STATUS_REJECTED, component, fault, sliced, eligibility,
                         candidate=candidate, verdicts=verdicts,
                         remediation=remediation,
                         authority=_authority(session, rule, applied=False),
                         steps=dossier_steps, gate=gate)
    remediation["canaryFollowOn"] = _canary_follow_on()
    return _assemble(STATUS_REPAIRED, component, fault, sliced, eligibility,
                     candidate=candidate, verdicts=verdicts,
                     remediation=remediation,
                     authority=_authority(session, rule, applied=True),
                     steps=dossier_steps, gate=gate)


# ---------------------------------------------------------------- gate stages


def _run_gates(session, candidate_arg: dict, running_ir: dict | None,
               policy: SelfRepairPolicy, accepted: set,
               over_the_transport: bool = True) -> tuple[dict, dict]:
    """Run gauntlet (31) -> boundary policy (33) -> widening/may-touch (21) in
    order, short-circuiting at the first block. Returns `(verdicts, gate)` where
    `gate` carries `blocked`, `reason`, and — when everything passed — the
    compiled candidate IR to swap."""
    from . import gauntlet as _gauntlet  # noqa: PLC0415 — sibling, avoid a cycle

    verdicts: dict = {}

    # -- gauntlet (item 31): admission + lifecycle no-residue, graded ---------
    gauntlet_dossier = _gauntlet.run(session, dict(candidate_arg),
                                     over_the_transport=over_the_transport)
    verdicts["gauntlet"] = gauntlet_dossier
    if gauntlet_dossier.get("verdict") != "admissible":
        return verdicts, {"blocked": True, "reason": "gauntlet"}

    # the candidate as a standalone composition — what a swap installs and what
    # the boundary/policy/widening gates read.
    try:
        candidate_ir = _compile(candidate_arg,
                                over_the_transport=over_the_transport)
    except (RevlError, ValueError) as error:
        verdicts["policy"] = {"evaluated": False,
                              "reason": f"candidate is not a whole composition: {error}"}
        return verdicts, {"blocked": True, "reason": "gauntlet"}

    candidate_audit = audit_diff.audit_report(candidate_ir)

    # -- boundary policy (item 33): nothing reaches what it may not -----------
    boundary_policy = _load_boundary_policy(session)
    if boundary_policy is not None and not boundary_policy.is_empty():
        violations = policy_evaluate(boundary_policy, candidate_audit)
        verdicts["policy"] = {
            "evaluated": True,
            "clean": not violations,
            "violations": [
                {"kind": v.kind, "component": v.component, "token": v.token,
                 "message": v.message, "why": v.why.to_json()}
                for v in violations
            ],
        }
        if violations:
            return verdicts, {"blocked": True, "reason": "policy"}
    else:
        verdicts["policy"] = {"evaluated": False,
                              "reason": "no boundary policy bound to the session"}

    # -- self-repair may_touch bound (this module) ---------------------------
    reached = _capabilities_reached(candidate_audit)
    out_of_bounds = policy.out_of_bounds(reached)
    verdicts["mayTouch"] = {
        "bound": list(policy.may_touch) if policy.may_touch is not None else None,
        "reached": reached,
        "outOfBounds": out_of_bounds,
        "note": ("the self-repair policy caps which capabilities a repair may "
                 "touch; a reach outside the cap refuses the repair"
                 if policy.may_touch is not None else
                 "no may-touch cap; new capabilities are caught by the widening "
                 "gate (ack, not refuse)"),
    }
    if out_of_bounds:
        return verdicts, {"blocked": True, "reason": "may-touch"}

    # -- boundary widening (item 21): a widening stops for a human ack --------
    if running_ir is not None:
        running_audit = audit_diff.audit_report(running_ir)
        widen = audit_diff.evaluate(running_audit, candidate_audit,
                                    accepted=accepted)
        verdicts["widening"] = widen
        if widen["widened"] and policy.ack_on_widen:
            return verdicts, {"blocked": True, "reason": "widening",
                              "candidate_ir": candidate_ir}
    else:
        verdicts["widening"] = {"evaluated": False,
                                "reason": "nothing loaded — no running boundary to "
                                          "diff the candidate against"}

    return verdicts, {"blocked": False, "reason": None, "candidate_ir": candidate_ir}


# ---------------------------------------------------------------- candidate


def _resolve_candidate(arguments: dict, running_ir: dict | None) \
        -> tuple[dict | None, dict | None]:
    """Pick the candidate: the supplied regeneration, or — when `need` is given —
    the registry's best reuse match (item 49). Returns `(candidate_arg, reuse)`.

    Reuse is the 'maybe the fix already exists' step: if a `need` is supplied and
    the registry ranks an admissible provider, its inline source becomes the
    candidate (no regeneration). An explicitly supplied candidate wins over reuse
    unless `preferReuse` is set."""
    supplied = arguments.get("candidate")
    has_supplied = isinstance(supplied, dict) and (
        supplied.get("source") is not None or supplied.get("files"))
    reuse = None
    if arguments.get("need") is not None:
        reuse = _reuse_check(arguments, running_ir)

    prefer_reuse = arguments.get("preferReuse", not has_supplied)
    if prefer_reuse and reuse and reuse.get("chosen"):
        chosen = reuse["chosen"]
        return {"source": chosen["source"], "modules": chosen.get("modules")}, reuse
    if has_supplied:
        return dict(supplied), reuse
    if reuse and reuse.get("chosen"):
        chosen = reuse["chosen"]
        return {"source": chosen["source"], "modules": chosen.get("modules")}, reuse
    return None, reuse


def _reuse_check(arguments: dict, running_ir: dict | None) -> dict:
    """Ask the registry whether an admissible provider already exists (item 49).
    Best-effort: an absent index is 'found nothing', never an error."""
    try:
        from ..registry import Registry, resolve as registry_resolve  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    registry_dir = arguments.get("registry")
    if registry_dir is None:
        import os  # noqa: PLC0415
        registry_dir = os.environ.get("REVL_REGISTRY")
    if registry_dir is None:
        return {"ran": False, "reason": "no registry configured (`registry` or "
                                        "$REVL_REGISTRY) — reuse check skipped"}
    try:
        from pathlib import Path  # noqa: PLC0415
        if not (Path(registry_dir) / "index.json").exists():
            return {"ran": True, "chosen": None, "candidates": [],
                    "reason": f"no registry index at {registry_dir}"}
        result = registry_resolve(
            Registry.from_dir(registry_dir), arguments["need"],
            manifest=running_ir, limit=int(arguments.get("limit", 5)),
            # DIRECT reuse only. This check answers "does an admissible
            # provider already exist?", and it hands `chosen` straight to the
            # repair loop - a candidate that needs an adapter does NOT already
            # exist as a provider, it needs an author's `adapt` decision first
            # (item 296, proposed-not-silent). Offering one here would make the
            # loop assert reuse for a component nobody has written.
            adapt=False)
    except Exception as exc:  # noqa: BLE001 — reuse never crashes the loop
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    candidates = result.get("candidates") or []
    return {"ran": True, "candidates": candidates,
            "chosen": candidates[0] if candidates else None}


# ---------------------------------------------------------------- inputs


def _load_trace(arguments: dict) -> why_runtime.Trace | None:
    events = arguments.get("trace")
    if isinstance(events, list):
        return why_runtime.Trace(events)
    path = arguments.get("traceFile")
    if path:
        try:
            return why_runtime.Trace.load(path)
        except (OSError, ValueError):
            return None
    return None


def _load_self_repair_policy(arguments: dict) -> SelfRepairPolicy:
    spec = arguments.get("selfRepairPolicy")
    if spec is None:
        return SelfRepairPolicy()  # closed by default: nothing self-repairs
    return parse_self_repair_policy(spec)


def _load_boundary_policy(session) -> Policy | None:
    """An item-33 boundary policy for the reach gate, taken from the session if
    it carries one (`session.boundary_policy`), else None."""
    policy = getattr(session, "boundary_policy", None)
    if policy is None:
        return None
    if isinstance(policy, Policy):
        return policy
    from ..policy import parse_policy  # noqa: PLC0415
    return parse_policy(policy)


# ---------------------------------------------------------------- authority


def _authority(session, rule: Eligibility | None, applied: bool,
               pending_ack: bool = False) -> dict:
    """Who/what authorized the action (item 55). Unattended, the authority IS
    the self-repair policy — the loop acts *as* the eligibility rule that named
    the component. A human operator token bound to the session is recorded
    alongside, so 'on whose authority' is answerable either way.

    The trace-side stamp mirrors `mcp.server._stamp_authority`: an authorized
    management action names its authority in the causal record."""
    operator = getattr(session, "operator", None)
    record = {
        "unattended": True,
        "authority": "self-repair-policy",
        "verb": "swap",
        "applied": applied,
        "pendingAck": pending_ack,
        "eligibleBy": {"scope": rule.scope, "selector": rule.selector}
        if rule is not None else None,
        "operator": getattr(operator, "token", None),
        "note": ("authorized by the self-repair policy; no human was in the loop"
                 if applied else
                 "the self-repair policy did not authorize a swap — "
                 + ("a human ack is pending (a widening)" if pending_ack
                    else "a gate before the swap was not green")),
    }
    return record


def _authority_why(component: str, rule: Eligibility | None) -> WhyTrace:
    """The authority as a why-trace (item 55's idiom), for the dossier: the
    self-repair rule -> the component it authorized."""
    selector = f"{rule.scope} {rule.selector}" if rule is not None else "none"
    return WhyTrace(
        kind="self-repair-authority", subject="self-repair-policy", shape=CHAIN,
        steps=[
            TraceStep("self-repair-policy", "authority", None, None,
                      f"authorizes self-repair via `{selector}`"),
            TraceStep(component, "component", None, None,
                      "repaired unattended, within policy"),
        ])


# ---------------------------------------------------------------- dossier


def _narrative(component: str, status: str, fault: dict, sliced: dict,
               eligibility: dict, candidate: dict | None, verdicts: dict | None,
               remediation: dict | None, authority: dict,
               gate: dict | None) -> list[dict]:
    """The incident narrative: one ordered step per stage of the loop,
    reconstructed from the sections above (which are themselves built from the
    causal trace and the loop's inputs). This is the 'reconstruct every step
    from the causal trace alone' deliverable, made explicit and machine-first."""
    steps: list[dict] = []

    def add(stage: str, item: int, done: bool, detail: str) -> None:
        steps.append({"stage": stage, "roadmapItem": item,
                      "reached": done, "detail": detail})

    root = (fault.get("root") or {}) if fault else {}
    add("fault", 27, bool(fault and fault.get("recorded")),
        f"{component} failed; root cause: {root.get('note', 'not recorded')}")
    add("slice", 40, bool(sliced.get("available")),
        (f"localized to the first step where `{sliced.get('predicate')}` flips"
         if sliced.get("available")
         else sliced.get("reason", "not sliced")))
    add("eligibility", 62, eligibility["eligible"],
        eligibility["note"])
    if candidate is not None:
        add("candidate", 49, candidate.get("origin") != "none",
            f"candidate origin: {candidate.get('origin')}"
            + (" (reused an existing component — the fix already existed)"
               if candidate.get("origin") == "reused" else ""))
    if verdicts is not None:
        g = (verdicts.get("gauntlet") or {}).get("verdict")
        add("gauntlet", 31, g == "admissible", f"gauntlet verdict: {g}")
        pol = verdicts.get("policy") or {}
        add("policy", 33, pol.get("clean", not pol.get("evaluated")),
            "boundary policy: "
            + ("clean" if pol.get("clean") else
               "not evaluated" if not pol.get("evaluated") else "violated"))
        widen = verdicts.get("widening") or {}
        add("widening", 21, not widen.get("widened", False),
            "no capability widening" if not widen.get("widened")
            else f"WIDENS: {', '.join(widen.get('unacknowledged', []))} — human ack required")
    if remediation is not None:
        add("swap", 23, bool(remediation.get("applied")),
            f"remediation `{remediation.get('strategy')}` "
            + ("applied — hot-swapped in" if remediation.get("applied")
               else f"not applied ({remediation.get('blockedBy') or remediation.get('note', 'held')})"))
    add("authority", 55, True,
        f"authorized by {authority['authority']}"
        + (f" (+operator `{authority['operator']}`)" if authority.get("operator") else "")
        + f"; unattended={authority['unattended']}")
    add("incident", 62, True, f"incident closed with status `{status}`")
    return steps


def _assemble(status: str, component: str, fault: dict, sliced: dict,
              eligibility: dict, candidate: dict | None, verdicts: dict | None,
              remediation: dict | None, authority: dict, steps: list,
              gate: dict | None = None) -> dict:
    """Build the full incident dossier from the section results."""
    narrative = _narrative(component, status, fault, sliced, eligibility,
                           candidate, verdicts, remediation, authority, gate)
    return {
        "ok": True,
        "roadmapItem": _ROADMAP_ITEM,
        "incident": {
            "component": component,
            "status": status,
            "unattended": authority.get("unattended", True),
            "swapped": bool(remediation and remediation.get("applied")),
            "note": _status_note(status),
        },
        "fault": fault,
        "slice": sliced,
        "eligibility": eligibility,
        "candidate": candidate,
        "verdicts": verdicts,
        "remediation": remediation,
        "authority": {**authority,
                      "why": _authority_why(component,
                                            _eligibility_rule(eligibility)).to_json()},
        "dossier": {
            "note": "every step reconstructed from the causal trace and the "
                    "loop's inputs — the incident, end to end.",
            "steps": narrative,
        },
    }


def _eligibility_rule(eligibility: dict) -> Eligibility | None:
    by = eligibility.get("by")
    if not by:
        return None
    return Eligibility(by["scope"], by["selector"])


def _status_note(status: str) -> str:
    return {
        STATUS_REPAIRED: "the fault was detected, repaired, verified, and "
                         "swapped with zero human input — within policy.",
        STATUS_AWAITING_ACK: "the candidate repair would WIDEN what the "
                             "composition reaches outside the system; the loop "
                             "paused for a human ack instead of auto-swapping "
                             "(item 21). The running composition is untouched.",
        STATUS_INELIGIBLE: "the self-repair policy does not authorize this "
                           "component to repair itself; the loop halts. The "
                           "running composition is untouched.",
        STATUS_NO_CANDIDATE: "no repair candidate was supplied and the reuse "
                             "check found nothing admissible. Untouched.",
        STATUS_REJECTED: "a gate refused the candidate (gauntlet, policy, or "
                         "the may-touch bound). The running composition is "
                         "untouched.",
        STATUS_PLANNED: "every gate is green; the swap was not performed "
                        "(apply=False) — a rehearsal.",
    }.get(status, status)


def render_incident(dossier: dict) -> str:
    """Human rendering of an incident dossier — the courtesy text view over the
    machine-first structure (docs/queries.md §1). The step narrative is the
    spine; the sections carry the evidence."""
    if not dossier.get("ok") and "incident" not in dossier:
        return "error: " + "; ".join(d.get("message", "")
                                     for d in dossier.get("diagnostics") or [])
    incident = dossier.get("incident") or {}
    lines = [
        f"incident: {incident.get('component')} — status "
        f"`{incident.get('status')}`",
        f"  {incident.get('note', '')}",
        "",
        "steps (reconstructed from the causal trace):",
    ]
    for step in (dossier.get("dossier") or {}).get("steps") or []:
        mark = "OK " if step.get("reached") else " . "
        lines.append(f"  [{mark}] {step['stage']:<11} (item {step['roadmapItem']}): "
                     f"{step['detail']}")
    authority = dossier.get("authority") or {}
    lines.append("")
    lines.append(f"authority: {authority.get('authority')} — unattended="
                 f"{authority.get('unattended')}, applied={authority.get('applied')}"
                 + (f", operator=`{authority['operator']}`"
                    if authority.get("operator") else ""))
    return "\n".join(lines)


def _session_result(status: str, component, note: str) -> dict:
    return {
        "ok": False,
        "roadmapItem": _ROADMAP_ITEM,
        "incident": {"component": component, "status": status, "note": note},
        "diagnostics": [{"severity": "error", "code": "REVL",
                         "category": "repair", "message": note}],
    }
