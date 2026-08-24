"""Component leases — the composition as a multi-agent workspace (item 61).

Item 55 answers *who may drive this session*. It does not answer the question
that appears the moment **two** agents drive the same running system: *who is
allowed to replace `UserCache` right now?* Both may hold the `swap` grant for
it; nothing stops agent A from hot-swapping a new `UserCache` out from under
agent B while B is mid-iteration on its own candidate. The swap races, and the
loser silently loses work — the running component keeps serving, but the
*right to replace it* was contended and no one arbitrated.

A **lease** is that arbitration. It is an operator-scoped (item 55), TTL-bound
claim on a component *name*: "agent B is iterating on `UserCache` until 14:32".

It is emphatically **not a lock on the running component** — the component
keeps serving every call throughout, exactly as before. A lease governs only
who may *replace* it, and it does so in three escalating registers:

  * **surfaced** — ``revl_state`` shows every active lease (holder, component,
    expiry), so an agent can see the workspace before it acts.
  * **advisory** (default) — a plan/swap that would replace a component leased
    by *another* operator is *warned* ("leased by B until 14:32; your swap will
    race") but proceeds. Coordination without coercion.
  * **enforced** (where policy says so) — under a boundary policy (item 33)
    that declares ``leases enforced``, that same swap is *refused* at admission,
    with the policy-style why-trace every other refusal here carries. The
    running system is untouched; the lease held.

Leases live on the session (:class:`LeaseBook`), so they persist for the life
of the served composition and travel with it through a snapshot's meta. The
holder identity is item 55's operator token — no new identity notion — and
every claim/renew/release/expiry is stamped into the causal trace (item 27),
so "who held what lease when" is one query over the same trace as everything
else.

TTL is wall-clock: a lease with no renewal expires on its own, so a crashed or
walked-away agent never wedges the workspace. Every read prunes expired leases
first (recording an ``expired`` trace event as it goes), so ``active`` is
always the live set and expiry needs no background timer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..why import CHAIN, TraceStep, WhyTrace

# The holder recorded for a claim made by a session with no operator profile
# bound (item 55). Without profiles a served composition has a single root
# driver, so all its claims share one identity — the advisory/enforced
# "another operator" distinctions only bite once profiles bind *distinct*
# operator tokens, which is exactly the multi-agent case leases are for.
DEFAULT_HOLDER = "operator"

# The default lease duration (seconds) when a claim names no TTL. Short enough
# that a forgotten lease clears itself within minutes, long enough to cover an
# agent's generate→check→admit→swap loop.
DEFAULT_TTL = 300.0


class LeaseError(RuntimeError):
    """A lease operation the book refuses (claiming one another holder owns,
    renewing/releasing one you do not hold). The server maps it to the same
    ``ok: false`` session-error shape every other refusal here carries."""


# --------------------------------------------------------------------- model


@dataclass(frozen=True)
class Lease:
    """One operator-scoped, TTL-bound claim on a component *name*.

    ``expiry`` is an absolute wall-clock epoch: the claim is live while
    ``now < expiry`` and needs no timer to end. ``acquired`` is when it was
    first claimed (renewals keep it, so the trace shows the whole span)."""

    component: str
    holder: str
    acquired: float
    expiry: float

    def active(self, now: float) -> bool:
        return now < self.expiry

    def remaining(self, now: float) -> float:
        return max(0.0, self.expiry - now)

    def to_json(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "component": self.component,
            "holder": self.holder,
            "acquired": self.acquired,
            "expiry": self.expiry,
            "expiresInSeconds": round(self.remaining(now), 3),
        }


class LeaseBook:
    """Every active lease on a session, keyed by component name (one holder at
    a time per name), with a stamped event log for the causal trace.

    Deliberately self-contained: it holds no runtime handle and touches no
    component, so it can be reasoned about — and tested — as pure bookkeeping
    over the clock. All the session/plan/admission wiring is elsewhere and
    reads this."""

    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        # the stamped history of every claim/renew/release/expiry — the item-27
        # story of "who held what lease when", drained into the causal trace.
        self.events: list[dict] = []

    # -- internal ----------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Drop leases whose TTL has elapsed, stamping an ``expired`` event for
        each so the trace records the end of the span, not just its start."""
        for name, lease in list(self._leases.items()):
            if not lease.active(now):
                del self._leases[name]
                self._stamp("expired", lease, now)

    def _stamp(self, action: str, lease: Lease, now: float) -> dict:
        """Record one lease event (item 27). Channel ``lease`` so a trace query
        can select the workspace story out of the lifecycle stream."""
        detail = (f"{action} lease on `{lease.component}` by `{lease.holder}` "
                  f"(expires in {round(lease.remaining(now), 1)}s)"
                  if action != "expired"
                  else f"lease on `{lease.component}` held by `{lease.holder}` "
                       f"expired")
        event = {"channel": "lease", "subject": lease.component,
                 "detail": detail, "action": action, "holder": lease.holder,
                 "expiry": lease.expiry, "at": now}
        self.events.append(event)
        return event

    def drain_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    # -- operations --------------------------------------------------------

    def claim(self, component: str, holder: str, ttl: float | None = None,
              now: float | None = None) -> Lease:
        """Claim (or, by the same holder, extend) a lease on ``component``.

        Refuses when a *different* operator holds an active lease on it — the
        book never silently steals a live claim, even for a claim that would
        otherwise succeed. Re-claiming your own live lease is a renewal."""
        now = time.time() if now is None else now
        ttl = DEFAULT_TTL if ttl is None else float(ttl)
        if ttl <= 0:
            raise LeaseError(f"a lease TTL must be positive (got {ttl})")
        self._prune(now)
        current = self._leases.get(component)
        if current is not None and current.holder != holder:
            raise LeaseError(
                f"`{component}` is already leased by `{current.holder}` for "
                f"another {round(current.remaining(now), 1)}s — a lease is not "
                f"stealable while live; wait for it to expire or have that "
                f"holder release it (component leases, item 61)")
        acquired = current.acquired if current is not None else now
        lease = Lease(component, holder, acquired, now + ttl)
        self._leases[component] = lease
        self._stamp("renew" if current is not None else "claim", lease, now)
        return lease

    def renew(self, component: str, holder: str, ttl: float | None = None,
              now: float | None = None) -> Lease:
        """Extend a lease you already hold. Refuses if there is no active lease,
        or one another holder owns."""
        now = time.time() if now is None else now
        self._prune(now)
        current = self._leases.get(component)
        if current is None:
            raise LeaseError(
                f"no active lease on `{component}` to renew — claim one first "
                f"(component leases, item 61)")
        if current.holder != holder:
            raise LeaseError(
                f"`{component}` is leased by `{current.holder}`, not `{holder}` "
                f"— only the holder may renew it (component leases, item 61)")
        return self.claim(component, holder, ttl, now)

    def release(self, component: str, holder: str,
                now: float | None = None) -> bool:
        """Drop a lease you hold. Refuses releasing one another holder owns;
        releasing an absent (or already-expired) lease is a quiet no-op."""
        now = time.time() if now is None else now
        self._prune(now)
        current = self._leases.get(component)
        if current is None:
            return False
        if current.holder != holder:
            raise LeaseError(
                f"`{component}` is leased by `{current.holder}`, not `{holder}` "
                f"— only the holder may release it (component leases, item 61)")
        del self._leases[component]
        self._stamp("release", current, now)
        return True

    def reinstate(self, component: str, holder: str, acquired: float,
                  expiry: float, now: float | None = None) -> Lease | None:
        """Re-seat a lease at its *absolute* expiry (a persist rehydrate, item
        15). Unlike :meth:`claim` it keeps the original expiry rather than
        recomputing from a TTL, and quietly drops one already elapsed — a
        wall-clock claim does not come back from the dead across a restart."""
        now = time.time() if now is None else now
        if now >= expiry:
            return None
        lease = Lease(component, holder, acquired, expiry)
        self._leases[component] = lease
        return lease

    # -- reads -------------------------------------------------------------

    def active(self, now: float | None = None) -> list[Lease]:
        now = time.time() if now is None else now
        self._prune(now)
        return sorted(self._leases.values(), key=lambda l: l.component)

    def holder_of(self, component: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        self._prune(now)
        lease = self._leases.get(component)
        return lease.holder if lease is not None else None

    def document(self, now: float | None = None) -> list[dict]:
        """Active leases as JSON — what ``revl_state`` surfaces."""
        now = time.time() if now is None else now
        return [lease.to_json(now) for lease in self.active(now)]


# ---------------------------------------------------------------- identity


def holder_identity(session) -> str:
    """The operator token a session's lease actions are attributed to — item
    55's identity, or :data:`DEFAULT_HOLDER` when no profile is bound."""
    operator = getattr(session, "operator", None)
    token = getattr(operator, "token", None)
    return token if token else DEFAULT_HOLDER


def _book(session) -> LeaseBook:
    """The session's lease book, created lazily so an older session (or a test
    double) that predates the attribute still works."""
    book = getattr(session, "leases", None)
    if book is None:
        book = LeaseBook()
        try:
            session.leases = book
        except AttributeError:  # a frozen/slots double — caller still gets one
            pass
    return book


# ------------------------------------------------------ swap-target derivation


def _swap_targets(session, arguments: dict) -> list[str] | None:
    """The component names a swap with these arguments would *replace*, reusing
    item 55's target derivation (read-only). ``None`` means undecidable — the
    candidate will not compile, or nothing is loaded — in which case the
    handler itself refuses and mutates nothing, so leases stay out of the way,
    exactly as the operator gate does."""
    from . import operator as _operator  # noqa: PLC0415 — read-only reuse of item 55

    targets = _operator._targets("swap", session, arguments)
    if targets is None:
        return None
    return [name for name, _labels in targets]


def enforced(session) -> bool:
    """Does the session's boundary policy (item 33) declare leases enforced?
    ``session.sandbox`` is the item-33 ``Policy`` bound at serve time; a policy
    with ``leases enforced`` promotes the advisory warning to a refusal."""
    policy = getattr(session, "sandbox", None)
    return bool(policy is not None and getattr(policy, "leases_enforced", False))


# ----------------------------------------------------------------- advisory


def advise(session, targets: list[str] | None,
           now: float | None = None) -> list[dict]:
    """The advisory warnings for a swap/plan over ``targets``: one per target a
    *different* operator holds an active lease on. Never raises and never
    blocks — this is the default, coordination-without-coercion register."""
    if not targets:
        return []
    now = time.time() if now is None else now
    book = _book(session)
    me = holder_identity(session)
    live = {l.component: l for l in book.active(now)}
    warnings: list[dict] = []
    for name in targets:
        lease = live.get(name)
        if lease is None or lease.holder == me:
            continue
        warnings.append({
            "component": name,
            "leasedBy": lease.holder,
            "expiry": lease.expiry,
            "expiresInSeconds": round(lease.remaining(now), 3),
            "message": (f"`{name}` is leased by `{lease.holder}` for another "
                        f"{round(lease.remaining(now), 1)}s — your swap will "
                        f"race their iteration (component leases, item 61; "
                        f"advisory unless policy enforces leases)"),
        })
    return warnings


def advise_plan(session, arguments: dict, now: float | None = None) -> list[dict]:
    """Advisory warnings for ``revl_plan`` — defensive, so a plan is never
    broken by lease bookkeeping."""
    if not getattr(session, "loaded", False):
        return []
    try:
        targets = _swap_targets(session, arguments)
    except Exception:  # noqa: BLE001 — advice must never break the plan
        return []
    return advise(session, targets, now)


# ---------------------------------------------------------------- enforcement


@dataclass(frozen=True)
class LeaseRefusal:
    """A swap refused because it would replace a component another operator
    leases, under an enforcing policy. Same why-trace shape as item 55."""

    holder: str
    component: str
    heldBy: str
    expiry: float
    why: WhyTrace
    message: str


def _refusal(holder: str, lease: Lease, now: float) -> LeaseRefusal:
    message = (
        f"operator `{holder}` may not replace `{lease.component}` — it is "
        f"leased by `{lease.holder}` for another "
        f"{round(lease.remaining(now), 1)}s and this composition's policy "
        f"enforces leases (component leases, item 61; boundary policy, item 33)")
    why = WhyTrace(
        kind="component-lease", subject=holder, shape=CHAIN,
        steps=[
            TraceStep(holder, "operator", None, None,
                      f"attempts to replace `{lease.component}`"),
            TraceStep(lease.component, "component", None, None,
                      f"leased by `{lease.holder}` until expiry", (lease.holder,)),
        ])
    return LeaseRefusal(holder, lease.component, lease.holder, lease.expiry,
                        why, message)


def check_swap(session, arguments: dict,
               now: float | None = None) -> LeaseRefusal | None:
    """The enforcement decision for a swap. Returns a :class:`LeaseRefusal`
    when the policy enforces leases and the swap would replace a component held
    by *another* operator; ``None`` otherwise (advisory-only, or clear).

    All-or-nothing like admission: the first offending target refuses the whole
    swap, and the server leaves the running composition untouched."""
    if not enforced(session):
        return None
    now = time.time() if now is None else now
    try:
        targets = _swap_targets(session, arguments)
    except Exception:  # noqa: BLE001 — an undecidable candidate defers to the handler
        return None
    if not targets:
        return None
    book = _book(session)
    me = holder_identity(session)
    live = {l.component: l for l in book.active(now)}
    for name in targets:
        lease = live.get(name)
        if lease is not None and lease.holder != me:
            return _refusal(me, lease, now)
    return None
