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
loop, the `propose(packet) -> candidate` seam, the budget, the accumulated
attempt trace, and the clean structured payload a generator acts on
(the violated G-rule as a machine key, the offending subject/call-path, the
suggested fix). The harness fills the real generator via `register_proposer`;
`scripted_proposer` is the trivial deterministic in-repo impl that proves the
loop end to end. This mechanism is what the harness's `evolve` wires to — see
docs/evolve-loop.md §"Harness coordination".

The loop reaches the Gate through `gate_service.admit_structured` (the
machine-readable sibling of item 144's `admit`), so it inherits the exact same
guarantees — evolve orchestrates the gate, it does not re-implement admission.

The proposer is handed a **`ProposerPacket`**: the brief, the parent
candidate, the last refusal, and a capped list of notes. Every refused attempt
records the digest of the packet it handed over (`Attempt.packet_digest`).
`rounds` wraps the loop: each bounded run ends at a retained component, and the
next round starts from a fresh packet built from that component and the
original brief, with no refusal text carried across the round boundary. See
docs/evolve-loop.md §"The proposer packet" and §"Rounds".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

# The Gate seam evolve admits through. `admit_structured` returns the verdict
# with the machine-readable `rejection` payload (docs/evolve-loop.md).
from revl.gate_service import admit_structured

# The probe's candidate identity and its coverage test. Both are pure (no WAL
# read, no compile), so the loop and the CLI probe row share one identity
# without the loop reaching for a recording.
from revl.counterfactual import row_coverage_gap, sources_digest

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
# The proposer packet: the ONE bounded input a proposer is handed.
#
# A named input is what makes a fresh-context round possible and auditable. A
# packet holds exactly four things:
#
#   brief        : the original task text, unchanged across every round;
#   candidate    : the parent candidate the proposer starts from (for a new
#                  round, the component the previous round retained);
#   last_refusal : the rejection_payload that refused the last attempt, or None
#                  when the packet seeds a round (a fresh start);
#   notes        : short summaries of earlier refusals in the same round, capped
#                  so a long run cannot grow the input without bound.
#
# It never carries the discarded attempt history (that stays in EvolveResult,
# for audit), the proposer's own earlier conversation, or admitted authority. A
# round-two packet is built from the retained component and the original brief
# only: forgetting applies to the proposer's context, never to the component's
# declared capabilities and effects.
# --------------------------------------------------------------------------

MAX_PACKET_NOTES = 8
MAX_NOTE_CHARS = 160


def candidate_digest(candidate: Candidate) -> str:
    """The canonical content hash of a candidate: sorted source paths, the
    manifest string, compact JSON, UTF-8. A round-two packet carries the hash
    of the component the previous round retained, so a reader can check that
    the restart began from the admitted artifact and nothing else."""
    doc = {
        "sources": {path: candidate.sources[path]
                    for path in sorted(candidate.sources)},
        "manifest": candidate.manifest,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProposerPacket:
    """The bounded proposer input. `to_json()` is canonical (sorted keys,
    compact separators) and `digest()` is its hash: the value an `Attempt`
    records as `packet_digest`, so a later reader can check exactly what the
    proposer was shown."""

    brief: str
    candidate: Candidate
    last_refusal: Optional[dict] = None
    notes: tuple[str, ...] = ()

    @property
    def candidate_digest(self) -> str:
        return candidate_digest(self.candidate)

    def to_json(self) -> dict:
        return {
            "brief": self.brief,
            "candidate": {
                "sources": {path: self.candidate.sources[path]
                            for path in sorted(self.candidate.sources)},
                "manifest": self.candidate.manifest,
            },
            "candidate_digest": self.candidate_digest,
            "last_refusal": self.last_refusal,
            "notes": list(self.notes),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)

    def digest(self) -> str:
        return hashlib.sha256(
            self.canonical_json().encode("utf-8")).hexdigest()


def refusal_note(n: int, payload: dict) -> str:
    """A bounded one-line summary of a refusal, safe to surface to a proposer
    that has no memory of the attempt that produced it. The full payload stays
    in the attempt record."""
    rule = payload.get("g_rule") or "REVL"
    subject = payload.get("subject")
    message = payload.get("message") or payload.get("guarantee") or ""
    note = f"attempt {n}: {rule}"
    if subject:
        note += f" {subject}"
    if message:
        note += f" - {message}"
    return note[:MAX_NOTE_CHARS]


# --------------------------------------------------------------------------
# The propose extern seam.
#
# A proposer takes ONE `ProposerPacket` and returns a revised candidate. This
# is the seam the harness fills with the real generator (an agent/LLM); it is
# deliberately a plain callable so the loop has no LLM dependency of its own:
#
#     propose(packet: ProposerPacket) -> Candidate
#
# The packet is bounded (see above). A proposer that cannot improve the
# candidate returns `packet.candidate` unchanged — the loop still bounds
# retries by the budget, so a stuck proposer terminates in budget exhaustion
# rather than looping forever.
# --------------------------------------------------------------------------

Proposer = Callable[["ProposerPacket"], "Candidate"]

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
    a refusal that still had an attempt left, the digest of the
    `ProposerPacket` that was handed to `propose`."""

    n: int
    candidate: Candidate
    admitted: bool
    diagnostic: str
    verdict: dict
    rejection: Optional[dict] = None  # the rejection_payload view, or None
    packet_digest: Optional[str] = None  # hash of the packet handed to propose

    def to_json(self) -> dict:
        return {
            "n": self.n,
            "sources": self.candidate.sources,
            "admitted": self.admitted,
            "diagnostic": self.diagnostic,
            "rejection": self.rejection,
            "packet_digest": self.packet_digest,
        }


@dataclass(frozen=True)
class Deferred:
    """A candidate `evolve_ordered` did not offer to the Gate: its probe
    identity (`probe_identity`) and the probe row that deferred it."""

    candidate: Candidate
    identity: str
    row: Optional[dict]

    def to_json(self) -> dict:
        return {"candidate": self.identity, "sources": self.candidate.sources,
                "row": self.row}


@dataclass
class EvolveResult:
    """The loop's outcome: whether the candidate was admitted, the full attempt
    history (every candidate offered, in order), and — on give-up — the final
    rejection payload.

    `evolve_ordered` also fills `deferred` (the candidates a probe row kept
    from the Gate, with their rows) and `unoffered` (ordered candidates the
    shared budget ran out before). `all_deferred` is true when the probe
    deferred every candidate, so no attempt was made: a caller can tell that
    apart from an empty candidate list, which has no attempts and no deferral."""

    admitted: bool
    attempts: list[Attempt] = field(default_factory=list)
    final_rejection: Optional[dict] = None
    deferred: list[Deferred] = field(default_factory=list)
    unoffered: list[Candidate] = field(default_factory=list)

    @property
    def final_candidate(self) -> Optional[Candidate]:
        return self.attempts[-1].candidate if self.attempts else None

    @property
    def all_deferred(self) -> bool:
        return not self.attempts and not self.unoffered and bool(self.deferred)

    def to_json(self) -> dict:
        return {
            "admitted": self.admitted,
            "attempts": [a.to_json() for a in self.attempts],
            "final_rejection": self.final_rejection,
            "deferred": [d.to_json() for d in self.deferred],
            "unoffered": [c.sources for c in self.unoffered],
            "all_deferred": self.all_deferred,
        }


@dataclass
class Round:
    """One bounded run of `evolve`: the fresh-context packet that seeded it and
    the attempt history it produced. `packet_digest` is the hash of that seed
    packet, taken before the proposer sees it."""

    n: int
    packet: ProposerPacket
    packet_digest: str
    result: EvolveResult


@dataclass
class RoundsResult:
    """The bounded multi-round run: every round in order, whether the last
    round ended at a retained component, and (on a failed round) the final
    rejection. `final_candidate` is the last component a round retained, or
    `None` when no round admitted."""

    admitted: bool
    rounds: list[Round] = field(default_factory=list)
    final_rejection: Optional[dict] = None

    @property
    def final_candidate(self) -> Optional[Candidate]:
        for r in reversed(self.rounds):
            if r.result.admitted:
                return r.result.final_candidate
        return None

    def to_json(self) -> dict:
        return {
            "admitted": self.admitted,
            "rounds": [
                {"n": r.n,
                 "packet_digest": r.packet_digest,
                 "packet": r.packet.to_json(),
                 "result": r.result.to_json()}
                for r in self.rounds
            ],
            "final_rejection": self.final_rejection,
        }


# --------------------------------------------------------------------------
# The loop.
# --------------------------------------------------------------------------


def _resolve_proposer(proposer: Optional[Proposer]) -> Proposer:
    propose = proposer if proposer is not None else _HARNESS_PROPOSER
    if propose is None:
        raise ValueError(
            "no propose seam: pass `proposer=` or install one with "
            "register_proposer() (the harness fills this)")
    return propose


def evolve(
    candidate: Candidate,
    proposer: Optional[Proposer] = None,
    budget: int = 3,
    admit: Callable[[str, str], str] = admit_structured,
    brief: str = "",
) -> EvolveResult:
    """Admit `candidate` through the Gate; on refusal hand `proposer` a
    `ProposerPacket` and retry, up to `budget` admit attempts.

    Control flow:
      * up to `budget` iterations; each iteration admits the current candidate;
      * an admit → `EvolveResult(admitted=True, ...)` with the full history;
      * a refusal with attempts still left → a packet (the brief, the current
        candidate, this refusal, and capped notes on earlier refusals) goes to
        `proposer`, whose revised candidate the loop retries;
      * a refusal on the final attempt → `EvolveResult(admitted=False, ...)`
        carrying the whole history and the final rejection payload.

    `proposer` defaults to the harness-registered generator
    (`register_proposer`); passing one explicitly (e.g. `scripted_proposer`)
    overrides it. `budget` must be >= 1. The running manifest is fixed across
    the loop — only the candidate's sources evolve. `brief` is carried
    unchanged in every packet (`""` when the caller has no task text). Each
    refused attempt that hands a packet to `propose` records that packet's
    digest as `Attempt.packet_digest`; an admitted attempt and the final
    refusal record `None` because nothing was handed over."""
    if budget < 1:
        raise ValueError(f"evolve budget must be >= 1, got {budget}")
    propose = _resolve_proposer(proposer)

    result = EvolveResult(admitted=False)
    current = candidate
    notes: list[str] = []
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
        attempt = Attempt(
            n=n, candidate=current, admitted=False,
            diagnostic=verdict.get("diagnostic", ""),
            verdict=verdict, rejection=payload)
        result.final_rejection = payload

        # regenerate only when there is a further attempt to spend it on
        if n < budget:
            packet = ProposerPacket(
                brief=brief, candidate=current, last_refusal=payload,
                notes=tuple(notes[-MAX_PACKET_NOTES:]))
            attempt.packet_digest = packet.digest()
            result.attempts.append(attempt)
            current = propose(packet)
            notes = (notes + [refusal_note(n, payload)])[-MAX_PACKET_NOTES:]
        else:
            result.attempts.append(attempt)

    return result


# --------------------------------------------------------------------------
# Probe ordering (roadmap item 2806, Slice 2).
#
# A counterfactual probe regrades a recording against a candidate edit
# (`revl replay WAL --under POLICY --candidate FILE --probe`, item 467). It is
# EVIDENCE, not an admission: contrasts between runs are noisy and share the
# proposer's biases, so a probe may order candidates and may never admit one.
# This hook is that one direction and only that one.
#
#   * a candidate whose row names a first dangerous crossing, or whose row
#     newly refuses a recorded crossing, is DEFERRED: `probe_order` returns it
#     in `deferred` and `evolve_ordered` does not offer it to the Gate;
#   * a candidate whose answerable row has a coverage gap (it regraded fewer
#     crossings than the record holds, or the record holds none) is DEMOTED:
#     it is still offered, after every candidate without a gap;
#   * a candidate with a clean row, or with no row at all (not answerable),
#     keeps its place and is admitted by the Gate and nothing else;
#   * a clean row never admits. `probe_order` never calls the Gate, and
#     `evolve_ordered` sends every ordered candidate through `evolve`, whose
#     only admission is the Gate's verdict.
#
# The rows are the counterfactual probe report's rows. This module imports only
# the pure identity and coverage helpers from `revl.counterfactual`: the caller
# supplies the `probe` callable, so the loop takes a row-shaped value and a
# live WAL read stays at the edge.
# --------------------------------------------------------------------------


def probe_row_is_adverse(row: Optional[dict]) -> bool:
    """Whether one probe row defers its candidate.

    Adverse means the recorded run crossed a boundary this candidate would not
    be permitted to cross: a first dangerous crossing, or any newly refused
    recorded crossing. A row that is absent or not answerable is NOT adverse;
    it carries no evidence either way and ordering must not turn silence into a
    deferral. A row with a coverage gap is not adverse either: it is demoted in
    the order (`probe_row_is_demoted`), never deferred."""
    if not row:
        return False
    if row.get("firstDangerousCrossing"):
        return True
    return int(row.get("newlyRefused") or 0) > 0


def probe_row_is_demoted(row: Optional[dict]) -> bool:
    """Whether one probe row demotes its candidate: an answerable row with a
    coverage gap (`counterfactual.row_coverage_gap`). Such a row regraded fewer
    crossings than the record holds, or the record holds none, so its
    `newlyRefused 0` was not checked against them. An absent or not-answerable
    row is not demoted."""
    if not row or row.get("answerable") is False:
        return False
    return row_coverage_gap(row)


def probe_identity(candidate: Candidate) -> str:
    """The candidate's probe identity: `counterfactual.sources_digest` over its
    sources, the same hash the CLI probe row carries as `candidate` for the
    same files. The running manifest is not part of it because the probe never
    reads it. This is deliberately not `candidate_digest`, which also hashes
    the manifest and names a candidate inside a `ProposerPacket`."""
    return sources_digest(candidate.sources)


def probe_order(candidates: list[Candidate],
                probe: Callable[[Candidate], Optional[dict]]) -> dict:
    """Order candidates by their probe rows, deferring the adverse ones.

    One `probe(candidate)` call per candidate, in the order given, so the
    caller can batch a probe report over the same compositions. A candidate
    with an adverse row is returned in `deferred`. `ordered` holds every other
    candidate in two groups: first the candidates whose row is not demoted
    (an answerable row with no coverage gap, or an absent or not-answerable
    row), then the candidates whose answerable row has a coverage gap
    (`probe_row_is_demoted`). Inside each group the input order is kept, so
    the order is stable. `rows` is positional, one entry
    per candidate in the order given, each `{"candidate": probe_identity,
    "row": row}`, so two equal candidates keep two rows rather than one
    overwriting the other.

    This function never admits and never calls the Gate: it returns an ordering.
    `evolve_ordered` is the loop that consumes it."""
    if not isinstance(candidates, list):
        raise TypeError("probe_order takes a list of candidates")
    ordered: list[Candidate] = []
    demoted: list[Candidate] = []
    deferred: list[Candidate] = []
    rows: list[dict] = []
    for candidate in candidates:
        row = probe(candidate)
        rows.append({"candidate": probe_identity(candidate), "row": row})
        if probe_row_is_adverse(row):
            deferred.append(candidate)
        elif probe_row_is_demoted(row):
            demoted.append(candidate)
        else:
            ordered.append(candidate)
    return {"ordered": ordered + demoted, "deferred": deferred, "rows": rows}


def evolve_ordered(
    candidates: list[Candidate],
    probe: Callable[[Candidate], Optional[dict]],
    proposer: Optional[Proposer] = None,
    budget: int = 3,
    admit: Callable[[str, str], str] = admit_structured,
    brief: str = "",
) -> EvolveResult:
    """Probe, order, then run the Gate's loop over the ordered candidates.

    A candidate with an adverse probe row is deferred and is not offered to the
    Gate. Every other candidate goes through `evolve` unchanged, so the Gate is
    the only thing that can admit it; a clean probe row is never an admission.

    `budget` is ONE budget of admit attempts shared across the whole ordered
    list, so N candidates never spend more than `budget` attempts in total.
    `evolve` spends its whole allowance on a refused candidate, so each
    candidate's run is capped to leave one attempt for every later ordered
    candidate: with `budget=3` and two ordered candidates the first gets at
    most two attempts and the second one. When `budget` is smaller than the
    ordered list, the candidates the budget does not reach are recorded in
    `unoffered`.

    The result is one `EvolveResult` for the whole run: the attempts of every
    offered candidate concatenated in order (`Attempt.n` counts across the
    run), the final rejection of the last refused attempt, the deferred
    candidates with their probe rows, and `admitted` when some candidate was
    admitted (the loop stops there). When every candidate is deferred there
    are no attempts and `all_deferred` is true. The deferred list is the
    caller's to re-probe or drop: this loop never offers it to the Gate."""
    if budget < 1:
        raise ValueError(f"evolve budget must be >= 1, got {budget}")
    order = probe_order(candidates, probe)
    result = EvolveResult(admitted=False)
    rows = order["rows"]
    for candidate, entry in zip(candidates, rows):
        if probe_row_is_adverse(entry["row"]):
            result.deferred.append(Deferred(
                candidate=candidate, identity=entry["candidate"],
                row=entry["row"]))

    ordered = order["ordered"]
    remaining = budget
    for i, candidate in enumerate(ordered):
        if remaining < 1:
            result.unoffered.extend(ordered[i:])
            break
        later = len(ordered) - i - 1
        run = evolve(candidate, proposer=proposer,
                     budget=max(1, remaining - later), admit=admit,
                     brief=brief)
        for attempt in run.attempts:
            attempt.n = len(result.attempts) + 1
            result.attempts.append(attempt)
        remaining -= len(run.attempts)
        if run.admitted:
            result.admitted = True
            result.final_rejection = None
            return result
        result.final_rejection = run.final_rejection
    return result


def rounds(
    candidate: Candidate,
    brief: str,
    proposer: Optional[Proposer] = None,
    budget: int = 3,
    max_rounds: int = 2,
    admit: Callable[[str, str], str] = admit_structured,
) -> RoundsResult:
    """Run `max_rounds` bounded fresh-context rounds over `evolve`.

    Each round starts from a **seed packet** built from the current candidate
    and the original `brief`, with no `last_refusal` and no notes, so the
    proposer never inherits the previous round's attempt history. The proposer
    turns that packet into the round's first candidate; `evolve` then runs to
    its own `budget`. A round that admits retains its last admitted candidate
    and the next round starts from it; a round that exhausts its budget ends
    the run with that round's final rejection (`admitted=False`), while
    `final_candidate` still names the last component a round retained, or
    `None` when no round admitted.

    `budget` (per round) and `max_rounds` are fixed before the run starts. The
    round-one seed packet is built from the caller's candidate; a round-two
    seed packet carries the retained component and the original brief, so the
    two digests differ whenever a round changed the component. Forgetting
    applies to the proposer's context: authority lives in the retained
    component's declared capabilities and effects, which a restart neither
    loses nor gains."""
    if budget < 1:
        raise ValueError(f"evolve budget must be >= 1, got {budget}")
    if max_rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {max_rounds}")
    propose = _resolve_proposer(proposer)

    out = RoundsResult(admitted=False)
    current = candidate
    for n in range(1, max_rounds + 1):
        packet = ProposerPacket(brief=brief, candidate=current)
        digest = packet.digest()
        seed = propose(packet)
        result = evolve(seed, proposer=propose, budget=budget,
                        admit=admit, brief=brief)
        out.rounds.append(Round(n=n, packet=packet, packet_digest=digest,
                                result=result))
        if not result.admitted:
            out.admitted = False
            out.final_rejection = result.final_rejection
            return out
        out.admitted = True
        current = result.final_candidate

    return out


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
    """Build a deterministic proposer from a `{g_rule: fixup}` table. A fixup
    receives the whole packet and returns the revised candidate; the dispatch
    key is the packet's `last_refusal` g_rule (a seed packet has none). An
    unmatched g_rule returns the parent candidate unchanged, so the loop runs
    out its budget and gives up with the full history — the budget-exhaustion
    path."""

    def propose(packet: ProposerPacket) -> Candidate:
        refusal = packet.last_refusal or {}
        fixup = fixups.get(refusal.get("g_rule"))
        if fixup is None:
            return packet.candidate
        return fixup(packet)

    return propose


# --------------------------------------------------------------------------
# JSON-string bridges: the value-typed entry points the `examples/evolve_loop.rvl`
# externs bind to (`@py { from revl.evolve_loop import ... }`). They marshal the
# loop across the same value-only seam the Gate uses — Str in, Str out — so an
# Evolve service is transport-safe for the same reason `Gate.admit` is.
# --------------------------------------------------------------------------


def propose_bridge(candidate_json: str, why_trace_json: str) -> str:
    """The `propose` extern seam as a value-typed function: parse the candidate
    and why-trace, build the packet the loop would hand over, dispatch to the
    harness-registered proposer, return the revised candidate JSON. The
    transport seam carries no brief or notes, so the reconstructed packet has
    an empty brief and no notes. The harness installs the real generator with
    `register_proposer`; without one this raises (the seam is unfilled)."""
    if _HARNESS_PROPOSER is None:
        raise ValueError(
            "no propose seam registered: call register_proposer() first")
    candidate = Candidate.from_json(candidate_json)
    why_trace = json.loads(why_trace_json) if why_trace_json else None
    packet = ProposerPacket(brief="", candidate=candidate,
                            last_refusal=why_trace or None)
    return _HARNESS_PROPOSER(packet).to_json()


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
    bumping the colliding key (the refusal payload's `subject`) to
    `subject + suffix`. Given the item-144 `collide` candidate (a second
    provider of `thing`), it yields a candidate providing a fresh key —
    admitted on the next attempt. A seed packet (no refusal) leaves the
    candidate unchanged."""

    def fix_g2(packet: ProposerPacket) -> Candidate:
        refusal = packet.last_refusal or {}
        subject = refusal.get("subject")
        if not subject:
            return packet.candidate
        renamed = rename_provision_key(
            packet.candidate.sources, subject, f"{subject}{new_suffix}")
        return Candidate(sources=renamed, manifest=packet.candidate.manifest)

    return scripted_proposer({"G2": fix_g2})
