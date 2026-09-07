"""Authority monotonicity across a delegation / retry chain (item toward #480,
Slice 1: the AUTHORITY-MONOTONICITY invariant).

This is the first concrete slice of the "verifiable private peer pool" feature
(#480). The pool itself — signed peer attestation, the lawful-retry dispatcher,
the F8 network seam (#421) it rides on — is designed in
``docs/design/480-verifiable-private-peer-pool.md``; none of that transport is
here. What *is* here is the one primitive #480 calls a G-theorem candidate and
the most self-contained of the three: **a peer may receive no more authority,
data, time, money, or retry budget than the delegating composition possesses.**
Authority may only *narrow* along a delegation edge and along a retry reissue;
it may never *widen*.

Why this lives on `cap_order`, not next to it
---------------------------------------------
revl already has the algebra this invariant needs. `cap_order.covers` is the
capability partial order — same token, every parameter narrowed — and
`cap_order.covers_set(held, reach)` already returns exactly "the reach elements
NOT covered by any held element", the widenings. The spawn-attenuation fold
(`lower._check_spawn_attenuation`, item 66/294) applies that same relation to
one specific boundary: a spawned child's capabilities must be covered by its
spawner's. A delegation/retry chain is the *general* case of that idea — an
ordered sequence of holders, each of which must be covered by the one before —
so this module is a thin walk over `covers_set`, holder by holder. It adds no
capability algebra; reusing `cap_order` is the whole point (one definition of
`covers`, the representation mandate of docs/design/294-...md).

The two dimensions, and why budgets are separate from caps
----------------------------------------------------------
#480 names five monotone quantities: **authority, data, time, money, retry
budget**. Three of them are already capability parameters `cap_order` orders:
authority/resource reach is the token + `path`/`host`/`table` cone, *time* is
the `time=` duration ceiling, *data* is the `size=`/`bytes=` byte ceiling. So a
grant spelled as capability strings — `net.fetch(host="api.internal")`,
`fs.read(path="/data", size="10MB")`, `model.complete(time="30s")` — has its
authority, data, and time monotonicity decided by `covers` for free, ceilings
included (a wider ceiling downstream is a widening, exactly as `_param_leq`
already says).

The remaining two — **money** and **retry budget** — are not in `cap_order`'s
CLOSED registry, and this slice deliberately does not add them there. Adding a
registry row changes the capability grammar and the emitted-IR/digest surface
(and would drag the gate crates with it); this invariant does not need that.
Money and retry budget are plain non-negative scalar allowances, so they ride a
separate ``budgets`` map on each grant, checked by the same monotone rule:
non-increasing along the chain, and — fail-closed — a budget the delegator does
not name is a budget it does not hold, so granting it downstream is a widening.
That mirrors `cap_order`'s own "a dropped parameter widens": you cannot hand
down what you were never shown to possess.

Fail-closed, everywhere
-----------------------
Every ambiguous case refuses. An unparseable capability string raises rather
than being skipped. A budget present downstream but absent upstream widens. A
retry reissue is just another hop: it is held to the SAME rule as a fresh
delegation, so a dispatcher cannot launder a wider grant through a "retry".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import cap_order


class AuthorityWidening(ValueError):
    """A delegation or retry hop grants more than its delegator holds.

    Structured so a caller (the dispatcher, a test, an audit view) can read the
    offending hop without parsing the message: :attr:`index` is the position of
    the widening grant in the chain (>= 1; the root at 0 has no delegator),
    :attr:`holder`/:attr:`delegator` name the two ends of the edge,
    :attr:`caps` the capability spellings that widened, and :attr:`budgets` the
    budget keys that widened (each as ``(key, held, granted)``; ``held`` is
    ``None`` when the delegator did not hold the budget at all)."""

    def __init__(
        self,
        index: int,
        holder: str,
        delegator: str,
        caps: tuple[str, ...],
        budgets: tuple[tuple[str, "int | None", int], ...],
    ) -> None:
        self.index = index
        self.holder = holder
        self.delegator = delegator
        self.caps = caps
        self.budgets = budgets
        super().__init__(self._message())

    def _message(self) -> str:
        parts: list[str] = []
        if self.caps:
            parts.append("capabilities " + ", ".join(f"`{c}`" for c in self.caps))
        for key, held, granted in self.budgets:
            if held is None:
                parts.append(f"budget `{key}={granted}` (delegator holds no `{key}`)")
            else:
                parts.append(f"budget `{key}={granted}` (delegator holds `{key}={held}`)")
        granted = "; ".join(parts) if parts else "authority"
        return (
            f"delegation hop {self.index}: `{self.delegator}` grants `{self.holder}` "
            f"{granted}, but a peer may receive no more authority, data, time, money, "
            f"or retry budget than its delegator holds — authority may only narrow "
            f"along a delegation or retry chain, never widen"
        )


@dataclass(frozen=True)
class Grant:
    """One holder in a delegation/retry chain and the authority it holds.

    The chain's element 0 is the delegating composition's OWN authority (its
    root grant, answerable to G4/item-66 by the ordinary attenuation checks);
    every later element is a peer that received a delegated grant, or a retry
    reissue of an earlier attempt. Each names:

    * ``caps`` — capability spellings in `cap_order`'s grammar
      (``net.fetch(host="api.internal")``, ``fs.read(path="/data", size="10MB")``).
      Authority, resource cone, time (`time=`) and data (`size=`) monotonicity
      are all decided over these by `cap_order.covers`.
    * ``budgets`` — non-negative scalar allowances `cap_order` does not name,
      chiefly ``retries`` (the retry budget) and ``money``. Non-increasing along
      the chain; a key absent from a delegator is treated as unheld (0), so
      granting it downstream widens.
    """

    holder: str
    caps: tuple[str, ...] = ()
    budgets: Mapping[str, int] = field(default_factory=dict)

    def parsed_caps(self) -> list[cap_order.Cap]:
        """The `caps` spellings as `cap_order.Cap`s. Raises `cap_order.CapError`
        on a malformed or unregistered spelling — fail-closed: an unparseable
        grant is never silently admitted."""
        return [cap_order.parse_cap(c) for c in self.caps]


def _budget_widenings(
    delegator: Mapping[str, int], delegate: Mapping[str, int]
) -> tuple[tuple[str, "int | None", int], ...]:
    """Budget keys the delegate grants beyond what the delegator holds.

    A key the delegator does not name is unheld: granting any positive amount of
    it widens (held reported as ``None``). A granted amount strictly greater than
    the held amount widens. A grant of 0 never widens (it hands down nothing).
    Keys are compared in sorted order so the report is deterministic."""
    out: list[tuple[str, int | None, int]] = []
    for key in sorted(delegate):
        granted = delegate[key]
        if granted < 0:
            raise ValueError(
                f"budget `{key}={granted}` is negative; a budget is a "
                f"non-negative allowance"
            )
        if granted == 0:
            continue
        if key not in delegator:
            out.append((key, None, granted))
        elif granted > delegator[key]:
            out.append((key, delegator[key], granted))
    return tuple(out)


def grant_widenings(
    delegator: Grant, delegate: Grant
) -> tuple[tuple[str, ...], tuple[tuple[str, "int | None", int], ...]]:
    """The authority `delegate` receives that `delegator` does not hold, as
    ``(widened_caps, widened_budgets)``. Both empty iff the hop is monotone.

    Capabilities use `cap_order.covers_set` (the same fold spawn attenuation
    runs), so a downstream cap must be covered by SOME held cap — narrower cone,
    smaller ceiling. Budgets use the non-increasing/fail-closed rule above."""
    extra_caps = cap_order.covers_set(delegator.parsed_caps(), delegate.parsed_caps())
    widened_caps = tuple(sorted(c.to_str() for c in extra_caps))
    widened_budgets = _budget_widenings(delegator.budgets, delegate.budgets)
    return widened_caps, widened_budgets


def check_hop(index: int, delegator: Grant, delegate: Grant) -> None:
    """Refuse a single delegation/retry edge that widens authority. Raises
    :class:`AuthorityWidening` naming the offending caps/budgets; returns
    ``None`` when the hop only narrows (or holds steady)."""
    caps, budgets = grant_widenings(delegator, delegate)
    if caps or budgets:
        raise AuthorityWidening(index, delegate.holder, delegator.holder, caps, budgets)


def check_delegation_chain(chain: Sequence[Grant]) -> None:
    """Prove authority monotonicity down a whole delegation/retry chain.

    ``chain[0]`` is the delegating composition's own authority; each later grant
    must be covered by the one IMMEDIATELY before it (the transitive property
    then makes every grant covered by the root, since `covers` is transitive).
    A retry reissue is passed as an ordinary later hop and held to the same
    rule. Raises :class:`AuthorityWidening` at the first widening edge; an empty
    or single-element chain is vacuously monotone."""
    for i in range(1, len(chain)):
        check_hop(i, chain[i - 1], chain[i])


def chain_is_monotone(chain: Sequence[Grant]) -> bool:
    """Boolean form of :func:`check_delegation_chain` for a dispatcher that
    wants to choose a peer rather than raise (e.g. skip a peer whose offered
    grant would widen). Fail-closed: a malformed capability spelling propagates
    as `cap_order.CapError` rather than reading as ``True``."""
    try:
        check_delegation_chain(chain)
    except AuthorityWidening:
        return False
    return True
