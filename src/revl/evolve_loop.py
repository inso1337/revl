"""The evolve feedback loop over the Gate service (roadmap item 148,
Fuller Path A).

Item 144 shipped the Gate: `compile_files` reachable as a bridge service, so a
candidate composition is *admitted* into a running world by passing revl's own
guarantees (G2 one-provider-per-key, G4 capability-containment, A1
sync-reaching-async, …), and a refusal carries a why-trace naming the violated
G-rule, the offending component/call-path, and the mapped fix
(`src/revl/gate_service.py`, `docs/gate-as-a-service.md`).

Admission is one-shot. **evolve** is the loop that turns a refusal into a next
attempt:

    1. admit a candidate through the Gate;
    2. on REFUSAL, hand the structured why-trace to a `propose` step that
       returns a revised candidate;
    3. retry, bounded by a retry budget (max admit attempts). Terminate on an
       admit (success) or on budget exhaustion (give up, returning the full
       attempt history and the final why-trace).

**The `propose` step is an EXTERN hook, not an LLM.** The actual regeneration
is external — the agent/LLM in the automorph harness (built separately). This
module owns the *orchestration and the agent-readable rejection payload*: the
loop, the `propose(candidate, why_trace) -> candidate` seam, the budget, the
accumulated attempt trace, and the clean structured payload a generator acts on
(the violated G-rule as a machine key, the offending subject/call-path, the
suggested fix). The harness fills the real generator via `register_proposer`;
`scripted_proposer` is the trivial deterministic in-repo impl that proves the
loop end to end. This mechanism is what the harness's `evolve` wires to — see
docs/evolve-loop.md §"Harness coordination".

The loop reaches the Gate through `gate_service.admit_structured` (the
machine-readable sibling of item 144's `admit`), so it inherits the exact same
guarantees — evolve orchestrates the gate, it does not re-implement admission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

# The Gate seam evolve admits through. `admit_structured` returns the verdict
# with the machine-readable `rejection` payload (docs/evolve-loop.md).
from revl.gate_service import admit_structured

# --------------------------------------------------------------------------
# The candidate: sources + the running manifest it is admitted against.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A composition offered to the Gate: its files as `{path: source}` and the
    running manifest JSON it is admitted against (`""` for a fresh world). The
    manifest is the fixed running world; `propose` revises `sources`, not it."""

    sources: dict[str, str]
    manifest: str = ""

    def sources_json(self) -> str:
        return json.dumps(self.sources)

    def to_json(self) -> str:
        return json.dumps({"sources": self.sources, "manifest": self.manifest})

    @staticmethod
    def from_json(text: str) -> "Candidate":
        obj = json.loads(text) if text else {}
        return Candidate(sources=obj.get("sources") or {},
                         manifest=obj.get("manifest") or "")


# --------------------------------------------------------------------------
# The rejection payload: the agent-readable projection of one refusal. This is
# the contract `propose` receives and the harness's generator branches on.
# It is `diagnostics.classify`'s record (already the agent-facing projection of
# a RevlError), augmented by the gate with the mapped `fix`. The stable,
# generator-facing keys:
#
#   g_rule    : the violated guarantee as a machine key, e.g. "G2", "G4", "A1"
#               (classify's "code"). The primary branch key.
#   guarantee : the one-line human description of that guarantee.
#   subject   : the offending key/type/name (why.subject), e.g. the clashing
#               provision key for G2.
#   call_path : the why-trace steps — the providers/edges/emission chain that
#               produced the verdict (why.steps): each a {name, kind, file,
#               line, detail}.
#   component : best-effort single offending component (the last named step),
#               a convenience over call_path.
#   fix       : the mapped one-line rewrite (diagnostics.FIXES).
#   file/line/message : the source location and verbatim compiler message.
#
# `rejection_payload` normalizes the gate's `rejection` record into this view;
# a generator may also read the raw record fields directly.
# --------------------------------------------------------------------------


def rejection_payload(record: dict) -> dict:
    """Normalize the gate's structured `rejection` record into the stable,
    generator-facing payload documented above. Total: an unclassified refusal
    still yields `g_rule` "REVL" and an empty call path rather than raising."""
    why = record.get("why") or {}
    steps = why.get("steps") or []
    # the offending component: the last *named* step of the why-trace (for a
    # G2 conflict that is the colliding provider; for a G4 chain, the sink).
    named = [s.get("name") for s in steps if s.get("name")]
    return {
        "g_rule": record.get("code") or "REVL",
        "guarantee": record.get("guarantee"),
        "category": record.get("category"),
        "subject": why.get("subject"),
        "component": named[-1] if named else None,
        "call_path": steps,
        "fix": record.get("fix"),
        "file": record.get("file"),
        "line": record.get("line"),
        "message": record.get("message"),
    }


# --------------------------------------------------------------------------
# The propose extern seam.
#
# A proposer takes the current candidate and the rejection payload and returns
# a revised candidate. This is the seam the harness fills with the real
# generator (an agent/LLM); it is deliberately a plain callable so the loop has
# no LLM dependency of its own. Signature:
#
#     propose(candidate: Candidate, why_trace: dict) -> Candidate
#
# `why_trace` is the `rejection_payload` view. A proposer that cannot improve
# the candidate returns it unchanged (or any candidate) — the loop still bounds
# retries by the budget, so a stuck proposer terminates in budget exhaustion
# rather than looping forever.
# --------------------------------------------------------------------------

Proposer = Callable[["Candidate", dict], "Candidate"]

# The harness registers its real generator here; `evolve` uses it when no
# proposer is passed explicitly. Left None so a missing seam is a clear error
# at the call site rather than a silent no-op.
_HARNESS_PROPOSER: Optional[Proposer] = None


def register_proposer(proposer: Proposer) -> None:
    """Install the harness's real generator as the default `propose` seam.
    The automorph harness calls this at startup; `evolve` then needs no
    explicit `proposer` argument. See docs/evolve-loop.md §"Harness
    coordination"."""
    global _HARNESS_PROPOSER
    _HARNESS_PROPOSER = proposer


# --------------------------------------------------------------------------
# The attempt trace and the loop result.
# --------------------------------------------------------------------------


@dataclass
class Attempt:
    """One turn of the loop: the candidate offered, the Gate verdict, and — on
    a refusal — the structured payload that was handed to `propose`."""

    n: int
    candidate: Candidate
    admitted: bool
    diagnostic: str
    verdict: dict
    rejection: Optional[dict] = None  # the rejection_payload view, or None

    def to_json(self) -> dict:
        return {
            "n": self.n,
            "sources": self.candidate.sources,
            "admitted": self.admitted,
            "diagnostic": self.diagnostic,
            "rejection": self.rejection,
        }


@dataclass
class EvolveResult:
    """The loop's outcome: whether the candidate was admitted, the full attempt
    history (every candidate offered, in order), and — on give-up — the final
    rejection payload."""

    admitted: bool
    attempts: list[Attempt] = field(default_factory=list)
    final_rejection: Optional[dict] = None

    @property
    def final_candidate(self) -> Optional[Candidate]:
        return self.attempts[-1].candidate if self.attempts else None

    def to_json(self) -> dict:
        return {
            "admitted": self.admitted,
            "attempts": [a.to_json() for a in self.attempts],
            "final_rejection": self.final_rejection,
        }


# --------------------------------------------------------------------------
# The loop.
# --------------------------------------------------------------------------


def evolve(
    candidate: Candidate,
    proposer: Optional[Proposer] = None,
    budget: int = 3,
    admit: Callable[[str, str], str] = admit_structured,
) -> EvolveResult:
    """Admit `candidate` through the Gate; on refusal feed the why-trace to
    `proposer` and retry, up to `budget` admit attempts.

    Control flow:
      * up to `budget` iterations; each iteration admits the current candidate;
      * an admit → `EvolveResult(admitted=True, ...)` with the full history;
      * a refusal with attempts still left → `proposer(candidate, why_trace)`
        produces the next candidate and the loop retries;
      * a refusal on the final attempt → `EvolveResult(admitted=False, ...)`
        carrying the whole history and the final rejection payload.

    `proposer` defaults to the harness-registered generator
    (`register_proposer`); passing one explicitly (e.g. `scripted_proposer`)
    overrides it. `budget` must be >= 1. The running manifest is fixed across
    the loop — only the candidate's sources evolve."""
    if budget < 1:
        raise ValueError(f"evolve budget must be >= 1, got {budget}")
    propose = proposer if proposer is not None else _HARNESS_PROPOSER
    if propose is None:
        raise ValueError(
            "no propose seam: pass `proposer=` or install one with "
            "register_proposer() (the harness fills this)")

    result = EvolveResult(admitted=False)
    current = candidate
    for n in range(1, budget + 1):
        verdict = json.loads(admit(current.sources_json(), current.manifest))
        if verdict.get("ok"):
            result.attempts.append(Attempt(
                n=n, candidate=current, admitted=True,
                diagnostic="", verdict=verdict, rejection=None))
            result.admitted = True
            result.final_rejection = None
            return result

        payload = rejection_payload(verdict.get("rejection") or {})
        result.attempts.append(Attempt(
            n=n, candidate=current, admitted=False,
            diagnostic=verdict.get("diagnostic", ""),
            verdict=verdict, rejection=payload))
        result.final_rejection = payload

        # regenerate only when there is a further attempt to spend it on
        if n < budget:
            current = propose(current, payload)

    return result


# --------------------------------------------------------------------------
# scripted_proposer: the trivial deterministic in-repo `propose`, for tests.
#
# The real generator is external (the harness). To prove the loop end to end
# without an LLM, this proposer applies scripted, verifiable source rewrites
# driven by the rejection payload. It is NOT the production seam — it exists so
# a known-broken candidate is repaired deterministically and the loop's
# control flow is pinned by tests.
# --------------------------------------------------------------------------


def rename_provision_key(sources: dict[str, str], old: str, new: str) -> dict[str, str]:
    """A deterministic G2 fixup: rewrite a candidate's provision of key `old`
    to a fresh key `new`, so a double-provide clash resolves. Rewrites both the
    `provides <old>:` declaration and the `provide <old> {` body across every
    file. A pure string transform — the scripted stand-in for what a generator
    would do more cleverly."""
    out = {}
    for path, text in sources.items():
        text = text.replace(f"provides {old}:", f"provides {new}:")
        text = text.replace(f"provide {old} ", f"provide {new} ")
        text = text.replace(f"provide {old}{{", f"provide {new} {{")
        out[path] = text
    return out


def scripted_proposer(fixups: dict[str, Proposer]) -> Proposer:
    """Build a deterministic proposer from a `{g_rule: fixup}` table. On a
    refusal it dispatches on the payload's `g_rule` to the matching fixup
    (itself a `(candidate, why_trace) -> candidate` callable); an unmatched
    g_rule returns the candidate unchanged, so the loop runs out its budget and
    gives up with the full history — the budget-exhaustion path."""

    def propose(candidate: Candidate, why_trace: dict) -> Candidate:
        fixup = fixups.get(why_trace.get("g_rule"))
        if fixup is None:
            return candidate
        return fixup(candidate, why_trace)

    return propose


# --------------------------------------------------------------------------
# JSON-string bridges: the value-typed entry points the `examples/evolve_loop.rvl`
# externs bind to (`@py { from revl.evolve_loop import ... }`). They marshal the
# loop across the same value-only seam the Gate uses — Str in, Str out — so an
# Evolve service is transport-safe for the same reason `Gate.admit` is.
# --------------------------------------------------------------------------


def propose_bridge(candidate_json: str, why_trace_json: str) -> str:
    """The `propose` extern seam as a value-typed function: parse the candidate
    and why-trace, dispatch to the harness-registered proposer, return the
    revised candidate JSON. The harness installs the real generator with
    `register_proposer`; without one this raises (the seam is unfilled)."""
    if _HARNESS_PROPOSER is None:
        raise ValueError(
            "no propose seam registered: call register_proposer() first")
    candidate = Candidate.from_json(candidate_json)
    why_trace = json.loads(why_trace_json) if why_trace_json else {}
    return _HARNESS_PROPOSER(candidate, why_trace).to_json()


def evolve_bridge(candidate_json: str, budget: int) -> str:
    """The whole loop as a value-typed function, for the `Evolve` service body:
    parse the candidate, run `evolve` with the registered proposer, return the
    `EvolveResult` JSON. Never raises across the boundary for a *refusal* — a
    give-up is a value (`admitted: false` + history) — only for a missing seam
    or a bad budget, which are programming errors, not admission outcomes."""
    result = evolve(Candidate.from_json(candidate_json), budget=budget)
    return json.dumps(result.to_json())


def g2_key_bump_proposer(new_suffix: str = "_v2") -> Proposer:
    """A ready-made scripted proposer that repairs a G2 provision conflict by
    bumping the colliding key (the payload's `subject`) to `subject + suffix`.
    Given the item-144 `collide` candidate (a second provider of `thing`), it
    yields a candidate providing a fresh key — admitted on the next attempt."""

    def fix_g2(candidate: Candidate, why_trace: dict) -> Candidate:
        subject = why_trace.get("subject")
        if not subject:
            return candidate
        renamed = rename_provision_key(
            candidate.sources, subject, f"{subject}{new_suffix}")
        return Candidate(sources=renamed, manifest=candidate.manifest)

    return scripted_proposer({"G2": fix_g2})
