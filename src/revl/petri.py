"""A dependency-free Petri-net engine: nets, markings, enablement, bounded
reachability, and P-semiflows by Martinez-Silva signed elimination.

This module knows NOTHING about revl (roadmap item 438). It is structural
mathematics over a place/transition net, so it is tested on its own bare nets
(``tests/test_petri.py``) and reused unchanged by ``revl.liveness``, which is
the only file that knows what a place or a transition MEANS for a composition.

Three arc kinds, because a liveness question needs to tell "reads a shared
service" apart from "consumes a single-consumer token":

* ``consume`` — an input arc: the tokens are removed when the transition fires.
* ``produce`` — an output arc: the tokens are added when the transition fires.
* ``read``    — a test arc: the tokens must be PRESENT to enable the transition
  but are not removed, so any number of readers coexist. Read arcs never enter
  the incidence matrix (they change no marking), so they never affect a
  P-semiflow; they only gate enablement.

A marking is a mapping place -> token count. Its canonical form (used as a hash
key during reachability) is the sorted tuple of ``(place, count)`` pairs with
zero counts dropped, so two markings that agree on every nonzero place are the
same state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from math import gcd


Marking = dict[str, int]
_Canon = tuple[tuple[str, int], ...]


def canon(marking: Marking) -> _Canon:
    """The hashable canonical form of a marking: sorted nonzero places."""
    return tuple(sorted((p, n) for p, n in marking.items() if n))


@dataclass(frozen=True)
class Transition:
    """One transition. ``consume``/``produce``/``read`` map a place to a strictly
    positive integer weight; a place absent from a map has weight zero there."""

    id: str
    consume: dict[str, int] = field(default_factory=dict)
    produce: dict[str, int] = field(default_factory=dict)
    read: dict[str, int] = field(default_factory=dict)

    def enabled(self, marking: Marking) -> bool:
        """A transition is enabled when every consumed place holds at least its
        consume weight AND every read place holds at least its read weight."""
        for place, weight in self.consume.items():
            if marking.get(place, 0) < weight:
                return False
        for place, weight in self.read.items():
            if marking.get(place, 0) < weight:
                return False
        return True

    def fire(self, marking: Marking) -> Marking:
        """The marking after firing (caller checks ``enabled`` first). Read arcs
        leave the marking untouched — only consume/produce move tokens."""
        out = dict(marking)
        for place, weight in self.consume.items():
            out[place] = out.get(place, 0) - weight
        for place, weight in self.produce.items():
            out[place] = out.get(place, 0) + weight
        return {p: n for p, n in out.items() if n}


class Net:
    """A place/transition net. Places are inferred from the transitions plus any
    place named only by the initial marking, so a caller declares transitions
    and a marking and nothing else."""

    def __init__(self, transitions: list[Transition]):
        self.transitions = list(transitions)
        places: set[str] = set()
        for t in self.transitions:
            places.update(t.consume, t.produce, t.read)
        self.places = places

    def enabled(self, marking: Marking) -> list[Transition]:
        """Every transition enabled at ``marking``, in declaration order."""
        return [t for t in self.transitions if t.enabled(marking)]

    # ---------------------------------------------------------------- incidence

    def incidence(self, places: list[str] | None = None) -> dict[str, dict[str, int]]:
        """The incidence matrix C as place -> {transition -> net token change}.
        ``C[p][t] = produce(t, p) - consume(t, p)``. Read arcs are absent by
        construction. Used by :func:`p_semiflows`."""
        order = places if places is not None else sorted(self.places)
        matrix: dict[str, dict[str, int]] = {p: {} for p in order}
        for t in self.transitions:
            for p in order:
                delta = t.produce.get(p, 0) - t.consume.get(p, 0)
                if delta:
                    matrix[p][t.id] = delta
        return matrix


# ------------------------------------------------------------ bounded reachability


@dataclass
class Reachability:
    """The result of a bounded reachability walk.

    * ``markings`` — canonical forms of every reachable marking explored.
    * ``dead_markings`` — reachable markings (canonical) at which NO transition
      is enabled: total deadlocks.
    * ``fired`` — transition ids enabled at some explored marking (i.e. that can
      fire on some reachable path).
    * ``dead_transitions`` — transitions that never became enabled anywhere in
      the explored space: they can never fire.
    * ``bound_hit`` — True when the walk stopped because a cap was reached, so
      the space is only PARTIALLY explored and any "no deadlock" conclusion is
      provisional (see :meth:`conclusive`).
    * ``token_overflow`` — True when some place exceeded ``max_tokens``, evidence
      the net may be unbounded along that place.
    """

    markings: set[_Canon]
    dead_markings: list[_Canon]
    fired: set[str]
    dead_transitions: set[str]
    bound_hit: bool
    token_overflow: bool

    def conclusive(self) -> bool:
        """Whether the walk saw the whole reachable space. Only then does the
        ABSENCE of a dead marking actually establish deadlock-freedom."""
        return not self.bound_hit and not self.token_overflow


def reachable(
    net: Net,
    initial: Marking,
    *,
    max_states: int = 20000,
    max_tokens: int = 64,
) -> Reachability:
    """Breadth-first walk of the marking graph from ``initial``.

    Bounded two ways, because the reachability set of a general Petri net can be
    infinite: ``max_states`` caps the number of distinct markings visited, and
    ``max_tokens`` caps the count allowed on any one place (a marking that would
    exceed it is not enqueued and flags ``token_overflow``). Either cap sets
    ``bound_hit``/``token_overflow`` so the caller never reads a truncated walk
    as a proof — this is item 418's rule made mechanical: the walk reports the
    bound it ran under and refuses to claim past it.
    """
    start = canon(initial)
    seen: set[_Canon] = {start}
    queue: deque[Marking] = deque([dict(initial)])
    dead: list[_Canon] = []
    fired: set[str] = set()
    bound_hit = False
    overflow = False

    while queue:
        marking = queue.popleft()
        ready = net.enabled(marking)
        if not ready:
            dead.append(canon(marking))
            continue
        for t in ready:
            fired.add(t.id)
            nxt = t.fire(marking)
            if any(n > max_tokens for n in nxt.values()):
                overflow = True
                continue
            key = canon(nxt)
            if key in seen:
                continue
            if len(seen) >= max_states:
                bound_hit = True
                continue
            seen.add(key)
            queue.append(nxt)

    dead_transitions = {t.id for t in net.transitions} - fired
    return Reachability(
        markings=seen,
        dead_markings=dead,
        fired=fired,
        dead_transitions=dead_transitions,
        bound_hit=bound_hit,
        token_overflow=overflow,
    )


# ------------------------------------------------------- P-semiflows (Martinez-Silva)


def _normalize(row: dict[str, int]) -> dict[str, int]:
    """Divide an integer row by the gcd of its entries, so semiflows come out in
    lowest terms and equal invariants compare equal."""
    values = [abs(v) for v in row.values() if v]
    if not values:
        return {}
    g = 0
    for v in values:
        g = gcd(g, v)
    if g <= 1:
        return dict(row)
    return {k: v // g for k, v in row.items() if v}


def _combine(r1: dict[str, int], w1: int, r2: dict[str, int], w2: int) -> dict[str, int]:
    """The nonnegative combination ``w1*r1 + w2*r2`` over the union of keys."""
    out: dict[str, int] = {}
    for k in set(r1) | set(r2):
        v = w1 * r1.get(k, 0) + w2 * r2.get(k, 0)
        if v:
            out[k] = v
    return out


def p_semiflows(net: Net, places: list[str] | None = None) -> list[dict[str, int]]:
    """Minimal P-semiflows of the net by the Martinez-Silva signed-elimination
    algorithm: nonnegative integer place vectors ``y`` with ``y . C = 0``.

    A P-semiflow is a conserved weighted token sum — an invariant that holds in
    EVERY reachable marking regardless of firing order — and a place carried by
    one (positive weight) is structurally bounded. The classic construction
    keeps an identity block alongside the incidence rows and, column by column
    (transition by transition), forms only NONNEGATIVE combinations of a
    positive-entry row with a negative-entry row to annul that column; because
    coefficients never go negative, the surviving identity rows are exactly the
    semiflows. Rows are kept minimal (dropping any whose support is a strict
    superset of another's) so the result is the minimal generating set.
    """
    order = places if places is not None else sorted(net.places)
    incidence = net.incidence(order)
    # a: current transition-space rows, keyed by transition id.
    # b: the place-combination that produced each a-row (the candidate semiflow).
    rows: list[tuple[dict[str, int], dict[str, int]]] = []
    for p in order:
        a_row = {t.id: incidence[p].get(t.id, 0) for t in net.transitions
                 if incidence[p].get(t.id, 0)}
        rows.append((a_row, {p: 1}))

    for t in net.transitions:
        tid = t.id
        pos = [(a, b) for a, b in rows if a.get(tid, 0) > 0]
        neg = [(a, b) for a, b in rows if a.get(tid, 0) < 0]
        zero = [(a, b) for a, b in rows if a.get(tid, 0) == 0]
        combined: list[tuple[dict[str, int], dict[str, int]]] = list(zero)
        for a1, b1 in pos:
            for a2, b2 in neg:
                # weights that annul column tid: c1*a1[tid] + c2*a2[tid] = 0
                c1 = abs(a2[tid])
                c2 = abs(a1[tid])
                new_a = _combine(a1, c1, a2, c2)
                new_a.pop(tid, None)  # annulled; drop the residual zero
                new_b = _combine(b1, c1, b2, c2)
                combined.append(_normalize_pair(new_a, new_b))
        rows = _drop_nonminimal(combined)

    semiflows = [b for a, b in rows if b and all(v > 0 for v in b.values())]
    return _dedupe(semiflows)


def _normalize_pair(a: dict[str, int], b: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Normalize the b-vector (the semiflow) by its own gcd and scale a with the
    same divisor so the pair stays consistent."""
    values = [abs(v) for v in b.values() if v]
    if not values:
        return (a, b)
    g = 0
    for v in values:
        g = gcd(g, v)
    if g <= 1:
        return (a, b)
    return ({k: v // g for k, v in a.items() if v},
            {k: v // g for k, v in b.items() if v})


def _drop_nonminimal(
    rows: list[tuple[dict[str, int], dict[str, int]]]
) -> list[tuple[dict[str, int], dict[str, int]]]:
    """Keep one row per distinct semiflow support, dropping any whose support is
    a strict superset of another's (the minimality rule that stops the row set
    from blowing up combinatorially across columns)."""
    uniq: dict[frozenset, tuple[dict[str, int], dict[str, int]]] = {}
    for a, b in rows:
        supp = frozenset(b)
        if supp not in uniq:
            uniq[supp] = (a, b)
    kept: list[tuple[dict[str, int], dict[str, int]]] = []
    supports = list(uniq)
    for supp in supports:
        if supp and any(other < supp for other in supports):
            continue
        kept.append(uniq[supp])
    return kept


def _dedupe(semiflows: list[dict[str, int]]) -> list[dict[str, int]]:
    """Drop duplicate semiflows (same normalized weight vector)."""
    out: list[dict[str, int]] = []
    seen: set[tuple] = set()
    for y in semiflows:
        key = tuple(sorted(_normalize(y).items()))
        if key and key not in seen:
            seen.add(key)
            out.append(_normalize(y))
    return out


def covered_places(net: Net, places: list[str] | None = None) -> set[str]:
    """The places carried (positive weight) by some P-semiflow. A place here is
    structurally bounded. If this equals the net's place set, the whole net is
    structurally bounded and its reachability graph is finite."""
    order = places if places is not None else sorted(net.places)
    covered: set[str] = set()
    for y in p_semiflows(net, order):
        covered.update(p for p, w in y.items() if w > 0)
    return covered


def structurally_bounded(net: Net, places: list[str] | None = None) -> bool:
    """True when every place is covered by a positive P-semiflow."""
    order = places if places is not None else sorted(net.places)
    return set(order) <= covered_places(net, order)


# A tiny rational helper kept for callers that want exact arithmetic on
# incidence rows; the semiflow path above stays in integers on purpose (Farkas
# combinations are integral), but exposing Fraction keeps the module honest
# about the field the null space actually lives in.
def as_fraction_row(row: dict[str, int]) -> dict[str, Fraction]:
    return {k: Fraction(v) for k, v in row.items()}
