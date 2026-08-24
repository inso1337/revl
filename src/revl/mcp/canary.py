"""`revl canary` — progressive delivery with a derived rollback (roadmap §59).

`revl swap` (item 23) is an all-or-nothing cutover: predecessor drains, the
successor takes the whole service. A **canary** is the gradual form — run both
generations at once, hand the successor a *designated slice*, and decide on
evidence: promote to the remainder, or revert the slice. This module is the
orchestration; every mechanism it needs already landed, and it reuses each
rather than reinventing it.

  * **The slice is a realm (item 10), because G2 says it must be.** G2 forbids
    two providers of one key in one realm, so a canary provider serving the
    same key as the baseline can only exist in a *different* realm. A realm
    (a tenant instance, a sandbox, a percentage carved into its own realm) is
    therefore the natural unit of a canary, and G2 is never weakened to get it:
    the second provider is legal precisely because it is somewhere else.
    Selection is `placement.slice_partition`.

  * **Divergence is a replay comparison, not a metric.** Both generations'
    activations are recorded *worlds* — ordered timelines of effects,
    provisions and boundary crossings (docs/replay.md), the same `replay.Step`
    vocabulary the backwards-replay engine records. The canary compares the
    baseline generation's timeline for the slice against the candidate's and
    reports the first step that differs, attributed to the exact `(component,
    realm)` that produced it. Divergence is a difference in the recorded world,
    named to a code site — never a threshold on a counter.

  * **Revert is the derived LIFO teardown of the slice, not a redeploy.** It
    reuses `erase_report.build_report(ir, realm)` verbatim: the runtime R4
    no-residue proof for in-process state, and the `survivors` set (EXACT, from
    `query.withdrawal`) proving every component *outside* the slice keeps every
    provision — the other N-1 tenants provably untouched. G2 is what makes that
    proof exact: a realm's provisions have no consumer in another realm, so
    tearing the slice down cannot orphan a sibling.

  * **Promote is item 23's swap, for the remainder.** Once the evidence says
    go, the sibling realms' providers are swapped to the candidate generation.
    This module reports the promote as *admissible* (reusing
    `placement.swap_admission`, the same gate a hot-swap runs) and leaves the
    execution to `revl swap` — the canary decides, swap acts.

SCOPE — the **stateless** canary. A canary provider serves a slice; promote via
swap; revert via LIFO teardown; divergence via replay comparison. Verified
state handoff (item 53) is NOT landed, so a *stateful* canary — one whose
candidate must inherit the baseline's effect-created world across the cutover —
is a follow-on, noted in docs/verified-canary.md, not built here. This module
does not schedule, discover, or ramp traffic percentages; it decides one slice
on recorded evidence and proves the revert clean.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..compiler import compile_files, compile_source
from ..errors import RevlError
from ..placement import slice_partition, swap_admission
from .session import replay_module

CANARY_VERSION = "1.0"
CANARY_KIND = "revl.canary"


# --------------------------------------------------------------- slice model

@dataclass(frozen=True)
class CanarySlice:
    """The designated slice a canary serves: a realm, the providers that serve
    into it, its member components, and the remainder a promote would swap."""

    realm: str
    providers: dict          # {key: provider component}
    members: list            # component names isolating a key into the realm
    remainder_realms: list   # sibling realms a promote swaps
    remainder_providers: dict  # {realm: {key: provider}}

    @property
    def provider(self) -> str | None:
        """The single provider component of the slice (the common canary case:
        one provider per realm). ``None`` if the slice serves several keys from
        different components — the caller then names one explicitly."""
        names = sorted(set(self.providers.values()))
        return names[0] if len(names) == 1 else None


def select_slice(ir: dict, realm: str) -> CanarySlice:
    """Resolve the designated slice from the linked composition. Raises
    ``RevlError`` naming the known realms when `realm` is not one of them."""
    part = slice_partition(ir, realm)
    if not part["members"]:
        from ..placement import slice_realms  # noqa: PLC0415
        known = ", ".join(slice_realms(ir)) or "(none)"
        raise RevlError("<canary>", 0,
                        f"no realm {realm!r} in this composition",
                        f"known realms: {known}. A canary slice is a realm "
                        f"(a tenant, a sandbox); designate one that exists.")
    return CanarySlice(
        realm=realm,
        providers=part["providers"],
        members=part["members"],
        remainder_realms=part["remainderRealms"],
        remainder_providers=part["remainderProviders"],
    )


# --------------------------------------------------- recorded-world timeline

def _expr_label(expr) -> str:
    """A canonical, generation-comparable label for one IR expression. Two
    generations that call the same host/service op with the same literal args
    produce equal labels; any behavioural difference produces different ones."""
    if not isinstance(expr, dict):
        return repr(expr)
    kind = expr.get("kind")
    if kind == "lit":
        return repr(expr.get("value"))
    if kind == "name":
        return str(expr.get("id"))
    if kind == "req":
        return str(expr.get("name"))
    if kind == "host":
        args = ", ".join(_expr_label(a) for a in expr.get("args") or [])
        return f"{expr.get('fn')}({args})"
    if kind == "call":
        target = _expr_label(expr.get("target"))
        args = ", ".join(_expr_label(a) for a in expr.get("args") or [])
        return f"{target}.{expr.get('method')}({args})"
    # a shape the walker does not special-case: fall back to a stable rendering
    return str(expr.get("kind"))


def _walk_steps(nodes, timeline, replay, origin: str) -> None:
    """Append one `replay.Step` per boundary-relevant node, in source order.

    The recorded world of a provider is its activation body followed by each
    provided method — the ordered effects, provisions and emissions a
    generation performs. This is the substrate the canary compares; it uses the
    replay engine's own step vocabulary so a divergence reads in the same terms
    a step-back does.
    """
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        step = node.get("step")
        if step == "let-effect":
            label = f"let {node.get('bind')} = {_expr_label(node.get('acquire'))}"
            timeline._add(replay.KIND_EFFECT, label, node.get("bind"),
                          detail={"origin": origin, "undo": _expr_label(node.get("undo"))})
        elif step == "effect":
            timeline._add(replay.KIND_EFFECT, _expr_label(node.get("acquire")), None,
                          detail={"origin": origin, "undo": _expr_label(node.get("undo"))})
        elif step == "emit":
            timeline._add(replay.KIND_EMISSION, _expr_label(node.get("expr")), None,
                          detail={"origin": origin,
                                  "compensate": _expr_label(node.get("compensate"))
                                  if node.get("compensate") is not None else None},
                          note="an emission is a one-way boundary crossing")
        elif step == "await":
            timeline._add(replay.KIND_BOUNDARY, "await", None,
                          detail={"origin": origin})
        elif step == "provide":
            key = node.get("name")
            timeline._add(replay.KIND_PROVISION, f"provide {key}: {node.get('service')}",
                          key, detail={"origin": origin})
            for method in node.get("methods") or []:
                _walk_steps(method.get("body"), timeline, replay,
                            f"{origin}:{key}.{method.get('name')}")


def slice_timeline(ir: dict, provider: str):
    """The recorded world of a slice's provider, as a `replay.Timeline`. Reads
    the provider component's IR body in source order; no runtime is required —
    the timeline is the ordered account of what the generation *would* record,
    which is exactly what two generations are compared on."""
    replay = replay_module()
    comp = next((c for c in ir.get("components") or []
                 if c.get("name") == provider), None)
    if comp is None:
        raise RevlError("<canary>", 0,
                        f"provider {provider!r} is not a component of this generation")
    timeline = replay.Timeline(provider)
    _walk_steps(comp.get("body"), timeline, replay, provider)
    return timeline


def compare_timelines(baseline, candidate) -> dict:
    """Attribute divergence between two recorded worlds. Walks both step lists
    in lock-step and reports the first index whose `(kind, label)` differs — or
    a length mismatch — as the divergence, in the terms the replay engine uses.
    A clean comparison (`diverged: False`) is the evidence to promote.
    """
    b_steps, c_steps = baseline.steps, candidate.steps
    for i in range(min(len(b_steps), len(c_steps))):
        b, c = b_steps[i], c_steps[i]
        if (b.kind, b.label) != (c.kind, c.label):
            return {
                "diverged": True,
                "atIndex": i,
                "baseline": {"kind": b.kind, "label": b.label},
                "candidate": {"kind": c.kind, "label": c.label},
                "reason": f"step {i} diverges: baseline `{b.kind} {b.label}` vs "
                          f"candidate `{c.kind} {c.label}`",
            }
    if len(b_steps) != len(c_steps):
        longer, idx = ("candidate", len(b_steps)) if len(c_steps) > len(b_steps) \
            else ("baseline", len(c_steps))
        extra = (c_steps if longer == "candidate" else b_steps)[idx]
        return {
            "diverged": True,
            "atIndex": idx,
            "baseline": None if longer == "candidate" else {"kind": extra.kind, "label": extra.label},
            "candidate": {"kind": extra.kind, "label": extra.label} if longer == "candidate" else None,
            "reason": f"the {longer} generation records an extra step at index "
                      f"{idx}: `{extra.kind} {extra.label}` (the generations "
                      f"differ in length — a behavioural change)",
        }
    return {"diverged": False, "reason": "the two recorded worlds are identical "
                                         "step-for-step — no divergence to attribute"}


# ------------------------------------------------------ revert (derived LIFO)

def revert(ir: dict, realm: str, *, prove_residue: bool = True) -> dict:
    """The revert half: the derived LIFO teardown of the canary slice, with the
    residue and survivors proof. Reuses `erase_report.build_report` verbatim —
    the runtime R4 no-residue proof plus the EXACT `survivors` set that proves
    the other N-1 tenants keep every provision (G2). Not a redeploy: the slice
    unwinds through the accumulator it built."""
    from ..erase_report import build_report  # noqa: PLC0415 — read-only reuse
    report = build_report(ir, realm, prove_residue=prove_residue)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error"),
                "knownRealms": report.get("knownRealms")}
    others = report["otherRealmsUntouched"]
    residue = report["inProcessStateGone"]["noResidueProof"]
    return {
        "ok": True,
        "realm": realm,
        "withdrawnComponents": others["withdrawnComponents"],
        "survivors": others["survivors"],
        "breached": others["breached"],
        "untouched": others["untouched"],
        "residueProof": residue,
        "guarantee": others["guarantee"],
        "mechanism": "derived LIFO teardown of the slice's accumulator (G7) — "
                     "the same unwind a swap tears the old provider down with, "
                     "scoped to the realm. Reused from erase_report, not "
                     "re-derived.",
    }


# ------------------------------------------------------ promote (= swap)

def promote_admission(files, running_ir: dict, remainder_providers: dict,
                      backend: str) -> dict:
    """Promote = item 23's swap, for the remainder. For each sibling realm's
    provider, run the *same* admission gate a hot-swap runs
    (`placement.swap_admission`) against the running composition. Reports
    whether each remainder provider is admissible; execution is left to
    `revl swap` (the canary decides, swap acts)."""
    verdicts = []
    admissible = True
    for realm, served in sorted(remainder_providers.items()):
        for _key, provider in sorted(served.items()):
            candidate, error = swap_admission(files, running_ir, provider, backend)
            ok = candidate is not None
            admissible = admissible and ok
            verdicts.append({"realm": realm, "provider": provider,
                             "admissible": ok, "diagnostic": error})
    return {
        "admissible": admissible,
        "backend": backend,
        "verdicts": verdicts,
        "note": "promote is `revl swap <provider> --to <backend>` for each "
                "remainder provider — this reports the swap admission gate's "
                "verdict; it does not cut over.",
    }


# --------------------------------------------------------------- entry point

def run_canary(running_ir: dict, candidate_files=None, candidate_source=None,
               *, realm: str, provider: str | None = None,
               promote_to: str | None = None, prove_residue: bool = True) -> dict:
    """The composed canary verdict for one slice.

    `running_ir` is the baseline composition. The candidate (source or files)
    is the successor generation of the slice's provider, admitted against the
    running manifest with `replacing=(provider,)` — the same gate a swap uses.
    Returns the divergence attribution, the revert proof (survivors +
    residue), and, when `promote_to` is given, the promote admission verdict.
    """
    try:
        slice_ = select_slice(running_ir, realm)
    except RevlError as error:
        return {"ok": False, "kind": CANARY_KIND, "schema_version": CANARY_VERSION,
                "error": str(error)}

    provider = provider or slice_.provider
    if provider is None:
        return {"ok": False, "kind": CANARY_KIND, "schema_version": CANARY_VERSION,
                "error": f"realm {realm!r} is served by several providers "
                         f"({', '.join(sorted(set(slice_.providers.values())))}); "
                         f"name the one to canary with `provider`",
                "providers": slice_.providers}

    # admit the candidate against the running composition (promote gate's twin)
    try:
        if candidate_source is not None:
            candidate_ir = compile_source(candidate_source, "<canary-candidate>.rvl",
                                          manifest=running_ir, replacing=(provider,))
        else:
            candidate_ir = compile_files(list(candidate_files or []),
                                         manifest=running_ir, replacing=(provider,))
    except RevlError as error:
        return {"ok": False, "kind": CANARY_KIND, "schema_version": CANARY_VERSION,
                "realm": realm, "provider": provider,
                "admitted": False,
                "error": f"candidate refused by the admission gate: {error}"}

    baseline_tl = slice_timeline(running_ir, provider)
    candidate_tl = slice_timeline(candidate_ir, provider)
    divergence = compare_timelines(baseline_tl, candidate_tl)
    divergence["attribution"] = {"component": provider, "realm": realm}

    revert_report = revert(running_ir, realm, prove_residue=prove_residue)

    promote = None
    if promote_to is not None and candidate_files:
        promote = promote_admission(candidate_files, running_ir,
                                    slice_.remainder_providers, promote_to)

    # the verdict: divergence -> revert (and the revert proves it is clean);
    # a clean comparison -> promote is the recommendation.
    if divergence["diverged"]:
        recommendation = "revert"
        rationale = ("the candidate's recorded world diverges from the "
                     "baseline's on the slice — revert the slice (the other "
                     "tenants are provably untouched)")
    else:
        recommendation = "promote"
        rationale = ("no divergence on the slice — promote by swapping the "
                     "remainder to the candidate generation")

    return {
        "ok": True,
        "kind": CANARY_KIND,
        "schema_version": CANARY_VERSION,
        "realm": realm,
        "provider": provider,
        "admitted": True,
        "slice": {
            "providers": slice_.providers,
            "members": slice_.members,
            "remainderRealms": slice_.remainder_realms,
        },
        "divergence": divergence,
        "revert": revert_report,
        "promote": promote,
        "recommendation": recommendation,
        "rationale": rationale,
        "guarantee": replay_module().GUARANTEE,
    }


# --------------------------------------------------------------- rendering

def render(report: dict) -> str:
    """Human view of the canary verdict — divergence attributed to a site, the
    revert's survivors and residue, the promote verdict."""
    if not report.get("ok"):
        lines = [f"canary: {report.get('error')}"]
        if report.get("knownRealms"):
            lines.append("  known realms: " + ", ".join(report["knownRealms"]))
        return "\n".join(lines)

    out = [
        f"CANARY — slice `{report['realm']}` (provider {report['provider']})",
        f"  {report['kind']} v{report['schema_version']}",
        "",
    ]
    members = report["slice"]["members"]
    out.append("  slice members: " + ", ".join(members))
    rem = report["slice"]["remainderRealms"]
    out.append("  remainder realms (promote swaps these): "
               + (", ".join(rem) or "none"))

    div = report["divergence"]
    out.append("")
    if div["diverged"]:
        out.append(f"  [DIVERGENCE] at step {div['atIndex']} — "
                   f"{div['attribution']['component']} in realm "
                   f"`{div['attribution']['realm']}`")
        out.append(f"      {div['reason']}")
    else:
        out.append("  [NO DIVERGENCE] the two recorded worlds are identical")

    rev = report["revert"]
    out.append("")
    out.append("  [REVERT] derived LIFO teardown of the slice")
    if rev.get("ok"):
        verdict = "PROVEN untouched" if rev["untouched"] \
            else f"BREACHED — {', '.join(rev['breached'])}"
        out.append(f"      other tenants: {verdict}")
        out.append("      survivors (keep every provision): "
                   + (", ".join(rev["survivors"]) or "none"))
        residue = rev["residueProof"]
        if residue.get("available"):
            out.append("      runtime residue (R4): "
                       + ("no residue" if residue["proven"] else "RESIDUE LEFT"))
        else:
            out.append(f"      runtime residue (R4): not run — {residue.get('reason')}")
    else:
        out.append(f"      error: {rev.get('error')}")

    if report.get("promote"):
        prom = report["promote"]
        out.append("")
        verdict = "ADMISSIBLE" if prom["admissible"] else "REFUSED"
        out.append(f"  [PROMOTE = swap --to {prom['backend']}] {verdict}")
        for v in prom["verdicts"]:
            mark = "ok  " if v["admissible"] else "FAIL"
            line = f"      {mark} {v['provider']} (realm {v['realm']})"
            if v["diagnostic"]:
                line += f" — {v['diagnostic']}"
            out.append(line)

    out.append("")
    out.append(f"  => recommendation: {report['recommendation'].upper()} — "
               f"{report['rationale']}")
    return "\n".join(out)
