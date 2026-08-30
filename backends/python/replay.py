"""Backwards replay: time-travel over a component's effect accumulator.

The paradigm already keeps, at runtime and in order, the one thing a debugger
normally has to guess at — *the inverse of every effect that ran*.  G7 unwinds
that accumulator LIFO on teardown.  This module unwinds it **partially**, to an
arbitrary point, and leaves the component live instead of tearing it down.  See
``docs/replay.md`` for the model and, more importantly, for what it refuses to
promise.

How the recording is obtained
-----------------------------
Emitted output is **not** changed — the golden files and the cross-tier emit
matrix stay byte-identical.  Instead, recording is an opt-in wrapper installed
around a component's ``apply``: the emitted body is handed a delegating
:class:`_RecordingContext` in place of the real ``cordis`` context, and every
accumulator event flows through it:

===============================  ==================================
emitted shape                    what the recorder sees
===============================  ==================================
``frame.install(_body)``         ``ctx.effect(_body, 'C/body')``
``yield lambda: undo()``         a yield out of that generator
``yield ctx.provide('k')``       ``ctx.provide`` + its own yield
``yield frame.drain``            the accumulator hinge
``yield None``                   the A1 iteration boundary
``frame.acquire(label, …)``      ``ctx.effect(_setup, label)``
``frame.adopt(ctx.effect(…))``   ``ctx.effect(fn, label)``
``ctx.db.execute(sql)``          an **emission** (from the IR's
                                 ``emission`` flag on the method)
===============================  ==================================

Two classification rules are worth stating because they are assumptions about
the emitter's shape rather than facts about the runtime, and
``tests/test_replay.py`` pins both against the real emitter:

1. A provision is identified by **object identity** — the value the recorder
   handed back from ``ctx.provide(key)`` is the value the body yields next.
   No guessing.
2. A *compensation* (A5) is identified by **source adjacency**: the emitter
   writes an ``emit`` step's call and its ``yield lambda: <compensate>`` on
   consecutive lines, so an inverse whose ``co_firstlineno`` is exactly one
   past a recorded emission's call site is that emission's compensation.
   Anything else is an ordinary inverse.  A misclassification here would
   downgrade a compensation to an inverse (or vice versa) in the *report*; it
   cannot make an un-runnable thing run.

Everything recording touches is opt-in.  With recording off, ``apply`` is the
emitted function, the real context is passed straight through, and this module
is never imported.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Any, Callable, Optional

__all__ = [
    "GUARANTEE", "IrreversibleStep", "KINDS", "Recorder", "ReplayError",
    "Step", "Timeline",
    # crash recovery (roadmap item 47): the accumulator as a write-ahead log
    "WAL_VERSION", "WAL_GUARANTEE", "WriteAheadLog", "inverse_descriptor",
    "boundary_of",
]


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

KIND_EFFECT = "effect"            # an author-declared effect + its inverse
KIND_PROVISION = "provision"      # ctx.provide(key); the inverse withdraws it
KIND_EMISSION = "emission"        # a boundary crossing — NOT revertible
KIND_COMPENSATION = "compensation"  # A5: best-effort cleanup for an emission
KIND_BOUNDARY = "boundary"        # A1 iteration boundary (`yield None`)
KIND_HINGE = "hinge"              # `yield frame.drain` — runtime scaffolding
KIND_OPAQUE = "opaque"            # a disposer the recorder cannot describe

KINDS = (KIND_EFFECT, KIND_PROVISION, KIND_EMISSION, KIND_COMPENSATION,
         KIND_BOUNDARY, KIND_HINGE, KIND_OPAQUE)

#: The single sentence this module is allowed to claim after a step-back.
#: Deliberately not "state was restored" — see docs/replay.md §"What this
#: cannot promise".
GUARANTEE = (
    "the inverses registered for the unwound steps ran, newest first (LIFO). "
    "Whether that restores state is the application's own equivalence, not "
    "something the runtime observes or asserts."
)

#: The builtins a `bisect` predicate may use.  A predicate is an expression
#: over the `inspect` view, so it needs the comprehension/aggregate helpers and
#: nothing that touches the world.
_SAFE_BUILTINS = {
    name: getattr(__import__("builtins"), name) for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "int", "len", "list", "max", "min", "range", "repr", "reversed",
        "set", "sorted", "str", "sum", "tuple", "zip",
        "True", "False", "None",
    )
}


class ReplayError(RuntimeError):
    """The timeline cannot do what was asked."""


class IrreversibleStep(ReplayError):
    """Stepping back would cross an emission that has no compensation.

    Refused by default rather than silently skipped: an unwind that steps
    over a boundary crossing has not returned the world to step *k*, and a
    debugger that reported otherwise would be lying.  Pass ``force=True`` to
    cross anyway — the report then carries the crossed emissions explicitly.
    """

    def __init__(self, message: str, emissions: list) -> None:
        super().__init__(message)
        self.emissions = emissions


# ---------------------------------------------------------------------------
# emitted-source registry (so a step can quote the line it came from)
# ---------------------------------------------------------------------------


class Sources:
    """filename -> emitted source, so a recorded step can quote its own line.

    Emitted modules are ``exec``'d from memory, so ``linecache`` cannot find
    them; the driver hands the text over here instead.
    """

    def __init__(self) -> None:
        self._lines: dict = {}

    def register(self, filename: str, text: str) -> None:
        self._lines[filename] = text.splitlines()

    def line(self, filename: Optional[str], lineno: Optional[int]) -> Optional[str]:
        lines = self._lines.get(filename or "")
        if not lines or not lineno or not (1 <= lineno <= len(lines)):
            return None
        return lines[lineno - 1].strip()


def _code_site(value: Any) -> tuple:
    code = getattr(value, "__code__", None)
    if code is None:
        return None, None
    return code.co_filename, code.co_firstlineno


# ---------------------------------------------------------------------------
# one recorded step
# ---------------------------------------------------------------------------


class Step:
    """One entry in the recorded timeline.

    ``undone`` says *the inverse ran*, nothing more.  There is intentionally
    no ``restored`` field: the runtime does not know.
    """

    __slots__ = ("index", "kind", "label", "effect", "file", "lineno", "source",
                 "detail", "origin", "undo", "undone", "undone_by", "crossed",
                 "compensation", "note", "error")

    def __init__(self, index: int, kind: str, label: str, effect: Optional[str],
                 origin: dict, file=None, lineno=None, source=None,
                 detail=None, note=None) -> None:
        self.index = index
        self.kind = kind
        self.label = label
        self.effect = effect
        self.origin = origin
        self.file = file
        self.lineno = lineno
        self.source = source
        self.detail = detail
        self.note = note
        self.undo: Optional[Callable] = None
        self.undone = False
        self.undone_by: Optional[str] = None
        self.crossed = False          # an emission the unwind stepped over
        self.compensation: Optional[int] = None  # index of its compensation
        self.error: Optional[str] = None

    # -- properties --------------------------------------------------------

    @property
    def revertible(self) -> bool:
        """Does an inverse exist for this step?

        False for emissions (by definition), for the A1 boundary and for the
        drain hinge (nothing of the author's to undo).
        """
        return self.undo is not None and self.kind != KIND_HINGE

    @property
    def site(self) -> Optional[str]:
        if self.file is None:
            return None
        return f"{self.file}:{self.lineno}"

    def as_dict(self) -> dict:
        out = {
            "index": self.index,
            "kind": self.kind,
            "label": self.label,
            "effect": self.effect,
            "revertible": self.revertible,
            "site": self.site,
            "source": self.source,
            "origin": self.origin,
            "undone": self.undone,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        if self.undone_by:
            out["undoneBy"] = self.undone_by
        if self.crossed:
            out["crossed"] = True
        if self.compensation is not None:
            out["compensatedBy"] = self.compensation
        if self.note:
            out["note"] = self.note
        if self.error:
            out["error"] = self.error
        return out

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<step {self.index} {self.kind} {self.label!r}>"


# ---------------------------------------------------------------------------
# the timeline
# ---------------------------------------------------------------------------


class Timeline:
    """The recorded activation of one component, and the operations over it."""

    def __init__(self, component: str, sources: Optional[Sources] = None) -> None:
        self.component = component
        self.sources = sources or Sources()
        self.steps: list = []
        self.origin: dict = {"phase": "activation"}
        # id(disposer returned by ctx.provide) -> provision key
        self._provisions: dict = {}
        # (file, lineno) of a recorded emission -> its Step
        self._emission_sites: dict = {}
        # crash-recovery WAL sink (roadmap item 47): when attached, each
        # committed step is appended to a durable log as it commits. Opt-in and
        # additive — None here means the recorder behaves exactly as before.
        self._wal = None
        self._wal_ir: Optional[dict] = None

    # -- write-ahead log (roadmap item 47) ---------------------------------

    def attach_wal(self, wal: "WriteAheadLog", ir: Optional[dict] = None) -> None:
        """Persist each committed step to ``wal`` as it commits."""
        self._wal = wal
        self._wal_ir = ir

    def _wal_append(self, step: Step) -> None:
        if self._wal is not None:
            self._wal.append_step(step, self.component)

    # -- recording ---------------------------------------------------------

    def _add(self, kind: str, label: str, effect: Optional[str], **kwargs) -> Step:
        step = Step(len(self.steps), kind, label, effect, dict(self.origin), **kwargs)
        self.steps.append(step)
        return step

    def note_provision(self, key: str, disposer: Any) -> None:
        """``ctx.provide(key)`` returned ``disposer``; remember it by identity
        so the yield that follows can be named exactly."""
        try:
            self._provisions[id(disposer)] = key
        except TypeError:  # pragma: no cover — id() never raises, belt and braces
            pass

    def record_emission(self, key: str, method: str, args: tuple,
                        service: Optional[str], site: tuple) -> Step:
        file, lineno = site
        step = self._add(
            KIND_EMISSION, f"{key}.{method}", None,
            file=file, lineno=lineno, source=self.sources.line(file, lineno),
            detail={"key": key, "method": method, "service": service,
                    "args": [_describe(a) for a in args]},
            note="an emission is a one-way boundary crossing: it has no inverse",
        )
        if file is not None:
            self._emission_sites[(file, lineno)] = step
        self._wal_append(step)
        return step

    def record_yield(self, value: Any, effect_label: Optional[str]) -> tuple:
        """Classify one yield out of a recorded effect generator.

        Returns ``(step, value_to_yield)``.  For anything with an inverse the
        value handed on to the runtime is a **once-only** wrapper, so a
        replayed inverse cannot be run a second time by the fiber's own
        teardown (and vice versa).
        """
        if value is None:
            step = self._add(KIND_BOUNDARY, _lbl(effect_label, "boundary"),
                             effect_label,
                             note="A1 iteration boundary — nothing accumulated")
            self._wal_append(step)
            return step, None

        if inspect.ismethod(value) and getattr(value, "__name__", "") == "drain":
            # `yield frame.drain`: the Frame's adopted-effect hinge.  Left
            # completely untouched — its ordering is load-bearing for R1 and
            # the adopted effects are already in this timeline individually.
            step = self._add(KIND_HINGE, _lbl(effect_label, "drain"), effect_label,
                             note="Frame.drain — runtime scaffolding, not an "
                                  "author step; step-back skips it")
            self._wal_append(step)
            return step, value

        key = self._provisions.pop(id(value), None)
        file, lineno = _code_site(value)
        source = self.sources.line(file, lineno)

        if key is not None:
            step = self._add(KIND_PROVISION, f"provide {key}", effect_label,
                             detail={"key": key},
                             note="the withdrawal inverse is runtime-derived (R5)")
        elif not callable(value):
            step = self._add(KIND_OPAQUE, _lbl(effect_label, "opaque"), effect_label,
                             detail={"repr": _describe(value)},
                             note="a non-callable disposer — the recorder cannot "
                                  "run it, so step-back will not claim to")
            self._wal_append(step)
            return step, value
        else:
            emission = self._emission_sites.get((file, lineno - 1)) if lineno else None
            if emission is not None:
                step = self._add(
                    KIND_COMPENSATION, f"compensate {emission.label}", effect_label,
                    file=file, lineno=lineno, source=source,
                    detail={"for": emission.index},
                    note="compensation is not inversion (paper §6.1): it is a "
                         "second boundary crossing chosen to offset the first",
                )
                emission.compensation = step.index
            else:
                step = self._add(KIND_EFFECT, _lbl(effect_label, "effect"), effect_label,
                                 file=file, lineno=lineno, source=source)
                if file is None:
                    step.note = ("a disposer with no python code object — origin "
                                 "unknown to the recorder")

        step.undo = _once(step, value)
        self._wal_append(step)
        return step, step.undo

    # -- reading -----------------------------------------------------------

    def _bound(self, k: int) -> int:
        if not isinstance(k, int) or isinstance(k, bool):
            raise ReplayError(f"step index must be an integer (got {k!r})")
        if k < -1 or k >= len(self.steps):
            raise ReplayError(
                f"{self.component}: step {k} is out of range "
                f"(-1 .. {len(self.steps) - 1}; -1 means 'before every step')")
        return k

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "steps": [step.as_dict() for step in self.steps],
            "accumulated": [step.index for step in self._live()],
            "guarantee": GUARANTEE,
        }

    def _live(self) -> list:
        """The accumulator as it stands: steps whose inverse has not run,
        newest first — exactly the order a teardown would use."""
        return [s for s in reversed(self.steps) if s.revertible and not s.undone]

    def inspect(self, k: int) -> dict:
        """What the composition looks like at step ``k``."""
        k = self._bound(k)
        applied = [s for s in self.steps if s.index <= k]
        tail = [s for s in self.steps if s.index > k]
        provisions = [s for s in applied
                      if s.kind == KIND_PROVISION and not s.undone]
        withdrawn = [s for s in self.steps
                     if s.kind == KIND_PROVISION and s.undone]
        return {
            "component": self.component,
            "at": k,
            "atLabel": self.steps[k].label if k >= 0 else "(before activation)",
            "activeProvisions": _keys(provisions),
            "withdrawnProvisions": _keys(withdrawn),
            "accumulated": [s.as_dict() for s in self._live() if s.index <= k],
            "unwound": [s.as_dict() for s in self.steps
                        if s.undone and s.index > k],
            "aheadOfHere": [s.as_dict() for s in tail],
            "emissionsSoFar": [s.as_dict() for s in applied
                               if s.kind == KIND_EMISSION],
            "note": ("`accumulated` is the live accumulator at or below step k, "
                     "newest first. It reflects which inverses have run, not "
                     "what the application's state actually is."),
        }

    # -- bisecting ---------------------------------------------------------

    def bisect(self, predicate: str,
               scope: Optional[dict] = None) -> dict:
        """Binary-search the timeline for the FIRST step at which ``predicate``
        flips from the value it had before any step ran.

        ``predicate`` is a Python expression evaluated in the same scope
        :meth:`inspect` produces — every key of the ``inspect(k)`` view is a
        name in scope (``at``, ``atLabel``, ``activeProvisions``,
        ``withdrawnProvisions``, ``accumulated``, ``unwound``, ``aheadOfHere``,
        ``emissionsSoFar``, ``component``), plus ``step`` (the step dict at
        ``k``, or ``None`` at ``k == -1``).  ``scope`` adds further read-only
        names.

        The search reconstructs the composition at a probe step with
        :meth:`inspect` — the same view :meth:`step_back` would restore state to
        — and never mutates the timeline, so it can run the probes in any order
        and reach the answer in ``ceil(log2(N)) + 2`` evaluations instead of
        ``N``.  It returns the found step's full record (who ran, what it
        touched, which realm) and, critically, the **verified/unverified**
        status of the effects on the path to it: bisect trusts the recorded
        inverses, and nothing here checks that an inverse actually restores
        state (that is roadmap item 26, `verified effect`, and it is not built),
        so today every effect on the path reads as *unverified*.

        Assumes, as ``git bisect`` does, that the predicate is **monotone** over
        the timeline — once flipped it stays flipped.  For a non-monotone
        predicate it still returns *a* flip boundary, but not provably the
        first; the report says so.
        """
        n = len(self.steps)
        probes: list = []

        def evaluate(k: int) -> bool:
            probes.append(k)
            view = self.inspect(k)
            names = dict(view)
            names["step"] = self.steps[k].as_dict() if k >= 0 else None
            if scope:
                for key, value in scope.items():
                    names.setdefault(key, value)
            try:
                return bool(eval(predicate,  # noqa: S307 — a dev tool, same
                                 {"__builtins__": _SAFE_BUILTINS}, names))  # trust as the REPL
            except Exception as exc:  # noqa: BLE001
                raise ReplayError(
                    f"bisect predicate {predicate!r} failed at step {k}: "
                    f"{type(exc).__name__}: {exc}") from None

        base = {
            "component": self.component,
            "predicate": predicate,
            "steps": n,
            "guarantee": GUARANTEE,
            "note": ("bisect reconstructs each probed step with `inspect` (the "
                     "state `step_back` would restore to) and never mutates the "
                     "timeline; it assumes the predicate is monotone over the "
                     "timeline, exactly as `git bisect` assumes good->bad is "
                     "monotone."),
        }

        if n == 0:
            return {**base, "flipped": False, "found": None, "evaluations": 0,
                    "reason": "the timeline is empty — nothing to bisect"}

        before = evaluate(-1)         # the value before any step ran
        after = evaluate(n - 1)       # the value once every step has run
        if after == before:
            return {**base, "flipped": False, "found": None,
                    "evaluations": len(probes),
                    "valueThroughout": before,
                    "reason": ("the predicate never flips: it is "
                               f"{before!r} before step 0 and still {before!r} "
                               "after the last step")}

        # invariant: predicate(lo) == before, predicate(hi) != before.
        lo, hi = -1, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if evaluate(mid) == before:
                lo = mid
            else:
                hi = mid

        step = self.steps[hi]
        return {
            **base,
            "flipped": True,
            "found": hi,
            "fromValue": before,
            "toValue": after,
            "evaluations": len(probes),
            "probes": probes,
            "reduction": f"{n} step(s) searched in {len(probes)} evaluation(s)",
            "step": step.as_dict(),
            "record": self._record(step),
            "at": self.inspect(hi),
            "verified": self._verified_path(hi),
        }

    def _record(self, step: Step) -> dict:
        """The 'who ran / what it touched / which realm' of one step."""
        origin = step.origin or {}
        phase = origin.get("phase")
        if phase == "call":
            who = (f"{origin.get('key')}.{origin.get('method')}"
                   f"({', '.join(repr(a) for a in origin.get('args') or [])})")
        elif phase == "repl":
            who = f"REPL: {origin.get('line')}"
        else:
            who = "activation body"
        return {
            "realm": self.component,
            "whoRan": who,
            "origin": origin,
            "touched": step.detail or {},
            "label": step.label,
            "kind": step.kind,
            "source": step.source,
            "site": step.site,
        }

    def _verified_path(self, k: int) -> dict:
        """The honest verification status of the effects on the path to ``k``.

        bisect trusts the recorded inverses.  Whether an inverse actually
        restores state — `verified effect`, roadmap item 26 — is not built, so
        a step carries no ``verified`` marker and every revertible effect on the
        path reads as unverified.  Written to light up on its own the day a
        step *does* carry that marker.
        """
        path = [s for s in self.steps if 0 <= s.index <= k and s.revertible]
        verified = [s for s in path if s.as_dict().get("verified") is True]
        return {
            "status": "verified" if path and len(verified) == len(path)
            else "unverified",
            "effectsOnPath": len(path),
            "verifiedOnPath": len(verified),
            "explanation": (
                "bisect trusts the recorded inverses. The state it bisects over "
                "is the one those inverses define at each step (what `inspect` "
                "reconstructs), not an independently checked state. `verified "
                "effect` (roadmap item 26) — which would confirm an inverse "
                "actually restores state — is not built, so every effect on the "
                "path to the found step reads as unverified. Whether the flip "
                "reflects true program state is the application's own "
                "equivalence, exactly the bound in docs/replay.md §4.1."),
        }

    # -- stepping back -----------------------------------------------------

    async def step_back(self, k: int, force: bool = False) -> dict:
        """Unwind to step ``k`` by running inverses from the top down to ``k``.

        The component is left **live**, not torn down: nothing here disposes a
        fiber.  ``k == -1`` unwinds everything that was recorded.

        Refuses (``IrreversibleStep``) if the range contains an emission with
        no compensation, unless ``force=True``.
        """
        k = self._bound(k)
        tail = [s for s in self.steps if s.index > k]
        blocking = [s for s in tail
                    if s.kind == KIND_EMISSION and s.compensation is None
                    and not s.crossed]
        if blocking and not force:
            raise IrreversibleStep(
                f"{self.component}: stepping back to {k} crosses "
                f"{len(blocking)} uncompensated emission(s) "
                f"({', '.join(s.label for s in blocking)}). An emission cannot "
                f"be undone — declare a `compensate` for it (A5), or pass "
                f"force=true to unwind the rest anyway and be told what was "
                f"crossed.",
                [s.as_dict() for s in blocking])

        ran, compensated, crossed, offset, failed = [], [], [], [], []
        for step in reversed(tail):
            if step.kind == KIND_EMISSION:
                step.crossed = True
                # a compensated crossing is reported separately from a bare
                # one: both were crossed, but only one had anything at all
                # done about it, and conflating them would overstate the first
                # and understate the second
                (offset if step.compensation is not None else crossed).append(
                    step.as_dict())
                continue
            if step.kind in (KIND_BOUNDARY, KIND_HINGE) or step.undo is None:
                continue
            if step.undone:
                continue
            try:
                result = step.undo(_by="step_back")
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 — an unwind reports, never stops
                # G7 teardown is best-effort and keeps going; so does this, or
                # one bad inverse would strand the whole tail of the
                # accumulator.
                step.error = f"{type(exc).__name__}: {exc}"
                failed.append(step.as_dict())
                continue
            (compensated if step.kind == KIND_COMPENSATION else ran).append(
                step.as_dict())

        return {
            "component": self.component,
            "to": k,
            "toLabel": self.steps[k].label if k >= 0 else "(before activation)",
            "inversesRan": ran,
            "compensationsRan": compensated,
            "emissionsCompensated": offset,
            "emissionsCrossed": crossed,
            "failed": failed,
            "live": True,
            "guarantee": GUARANTEE,
            **({"warning":
                "compensation is not inversion (paper §6.1) — the crossings "
                "above were offset by a further boundary crossing, not undone"}
               if compensated else {}),
            **({"warning_emissions":
                f"{len(crossed)} emission(s) were crossed with no compensation. "
                "Those effects are still out in the world."} if crossed else {}),
        }

    # -- replaying forward -------------------------------------------------

    def forward_plan(self, k: int) -> dict:
        """What re-running the tail after step ``k`` would involve.

        Only work that entered the accumulator through a re-invocable unit can
        be re-run — a provided-service call (`phase: "call"`) or a REPL line
        (`phase: "repl"`).  Activation-body steps cannot: the emitted body is a
        single generator whose head cannot be skipped, so "replay step 4
        onwards" is not expressible.  For those the honest remedy is a reload
        (`revl_swap`), and this says so rather than pretending.
        """
        k = self._bound(k)
        tail = [s for s in self.steps
                if s.index > k and (s.undone or s.crossed)]
        replay, seen, blocked = [], set(), []
        for step in tail:
            origin = step.origin or {}
            phase = origin.get("phase")
            if phase == "call":
                entry = {"kind": "call", "key": origin.get("key"),
                         "method": origin.get("method"),
                         "args": origin.get("args") or []}
            elif phase == "repl":
                entry = {"kind": "repl", "line": origin.get("line")}
            else:
                blocked.append({
                    "step": step.index, "label": step.label,
                    "reason": "activation-body step — the component body is one "
                              "generator; its tail cannot be re-entered without "
                              "re-running its head. Reload the component instead.",
                })
                continue
            token = repr(sorted(entry.items(), key=lambda kv: kv[0]))
            if token in seen:
                continue
            seen.add(token)
            replay.append(entry)
        return {
            "component": self.component,
            "from": k,
            "replay": replay,
            "notReplayable": blocked,
            "note": ("replaying re-executes the original invocations; it does "
                     "not restore the state they produced the first time"),
        }


def _keys(steps: list) -> list:
    return sorted(k for k in ((s.detail or {}).get("key") for s in steps)
                  if k is not None)


def _lbl(effect_label: Optional[str], what: str) -> str:
    return f"{effect_label or '?'}/{what}"


def _once(step: Step, fn: Callable) -> Callable:
    """A once-only inverse, shared between the fiber and the replay engine.

    Both the runtime's own teardown and :meth:`Timeline.step_back` hold this
    exact function, so whichever runs first wins and the other is a no-op —
    which is what keeps a replayed `store.drop()` from becoming a
    use-after-free when the session later unloads.
    """

    def undo(*args, _by: str = "runtime", **kwargs):
        if step.undone:
            return None
        step.undone = True
        step.undone_by = _by
        return fn(*args, **kwargs)

    undo.__name__ = getattr(fn, "__name__", "undo")
    undo.__doc__ = getattr(fn, "__doc__", None)
    return undo


def _describe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_describe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _describe(v) for k, v in value.items()}
    return repr(value)


# ---------------------------------------------------------------------------
# the recording context
# ---------------------------------------------------------------------------


class _ServiceProxy:
    """A required service, with its `emission` operations recorded.

    Which operations those are comes from the compiler (the IR's `emission`
    flag), not from a guess — that is the same trust direction
    `revl mcp` uses to derive MCP annotations.
    """

    __slots__ = ("_target", "_timeline", "_key", "_service", "_emissions")

    def __init__(self, target, timeline: Timeline, key: str,
                 service: Optional[str], emissions: set) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_timeline", timeline)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_emissions", emissions)

    def __getattr__(self, name: str):
        attr = getattr(self._target, name)
        if name not in self._emissions or not callable(attr):
            return attr
        timeline, key, service = self._timeline, self._key, self._service

        def emission(*args, **kwargs):
            frame = sys._getframe(1)
            timeline.record_emission(
                key, name, args, service,
                (frame.f_code.co_filename, frame.f_lineno))
            return attr(*args, **kwargs)

        return emission

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<recording {self._key}: {self._service}>"


class _RecordingContext:
    """Delegates everything to the real cordis context, recording the
    accumulator events on the way through.

    Only the *emitted component body* ever sees this object; every call is
    forwarded to the real bound method, so cordis itself only ever operates on
    its own `Context`.
    """

    def __init__(self, ctx, timeline: Timeline, requires: dict,
                 services: dict) -> None:
        object.__setattr__(self, "_revl_ctx", ctx)
        object.__setattr__(self, "_revl_timeline", timeline)
        object.__setattr__(self, "_revl_requires", dict(requires or {}))
        object.__setattr__(self, "_revl_services", services or {})

    # -- recorded surface --------------------------------------------------

    def effect(self, fn, *args, **kwargs):
        label = args[0] if args else kwargs.get("label")
        wrapped = _wrap_generator(fn, self._revl_timeline,
                                  label if isinstance(label, str) else None)
        return self._revl_ctx.effect(wrapped, *args, **kwargs)

    def provide(self, key, *args, **kwargs):
        disposer = self._revl_ctx.provide(key, *args, **kwargs)
        self._revl_timeline.note_provision(key, disposer)
        return disposer

    # -- transparent delegation -------------------------------------------

    def __getattr__(self, name: str):
        value = getattr(object.__getattribute__(self, "_revl_ctx"), name)
        requires = object.__getattribute__(self, "_revl_requires")
        if name not in requires or value is None:
            return value
        service = requires[name]
        methods = ((object.__getattribute__(self, "_revl_services")
                    .get(service) or {}).get("methods") or {})
        emissions = {m for m, spec in methods.items() if (spec or {}).get("emission")}
        if not emissions:
            return value
        return _ServiceProxy(value, object.__getattribute__(self, "_revl_timeline"),
                             name, service, emissions)

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover
        setattr(object.__getattribute__(self, "_revl_ctx"), name, value)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<recording ctx for {self._revl_timeline.component}>"


def _wrap_generator(fn, timeline: Timeline, label: Optional[str]):
    """Wrap an effect's generator function so each yield is recorded.

    Sync and async generator functions are wrapped as the *same* kind of
    function they were, and the inner generator is explicitly closed, so the
    A1 divert semantics (a close during an await skips the later steps) are
    unchanged.  Anything that is not a generator function is passed through
    untouched — the recorder does not understand it, so it does not pretend to.
    """
    if inspect.isasyncgenfunction(fn):

        async def recorded_async():
            inner = fn()
            try:
                async for value in inner:
                    _step, out = timeline.record_yield(value, label)
                    yield out
            finally:
                await inner.aclose()

        return recorded_async

    if inspect.isgeneratorfunction(fn):

        def recorded():
            inner = fn()
            try:
                for value in inner:
                    _step, out = timeline.record_yield(value, label)
                    yield out
            finally:
                inner.close()

        return recorded

    return fn


# ---------------------------------------------------------------------------
# the recorder
# ---------------------------------------------------------------------------


class Recorder:
    """Installs recording over an emitted module, and owns the timelines.

    One recorder spans a session; a swap re-instruments and starts fresh
    timelines for the new generation.
    """

    def __init__(self, ir: Optional[dict] = None) -> None:
        self.sources = Sources()
        self.timelines: dict = {}
        self._ir = ir or {}
        self._origin: dict = {"phase": "activation"}
        self.wal: Optional[WriteAheadLog] = None  # crash-recovery WAL (item 47)

    # -- setup -------------------------------------------------------------

    def register_source(self, filename: str, text: str) -> None:
        self.sources.register(filename, text)

    def instrument(self, module, ir: Optional[dict] = None) -> list:
        """Replace each component's ``apply`` with a recording one.

        The component dict is *copied*, never mutated, so an un-instrumented
        reference to the same emitted module keeps working.
        """
        if ir is not None:
            self._ir = ir
        services = self._ir.get("services") or {}
        names = []
        for comp in self._ir.get("components") or []:
            name = comp.get("name")
            plugin = getattr(module, name, None)
            if not isinstance(plugin, dict) or not callable(plugin.get("apply")):
                continue
            setattr(module, name,
                    dict(plugin, apply=self._wrap_apply(
                        name, plugin["apply"], comp.get("requires") or {}, services,
                        plugin.get("Config"))))
            names.append(name)
        return names

    def _wrap_apply(self, name: str, apply, requires: dict, services: dict,
                    config_schema=None):
        recorder = self

        def recording_apply(ctx, config):
            timeline = Timeline(name, recorder.sources)
            timeline.origin = dict(recorder._origin)
            if recorder.wal is not None:
                timeline.attach_wal(recorder.wal, recorder._ir)
            recorder.timelines[name] = timeline
            # The emitted apply no longer resolves config itself: the schema
            # rides on the plugin dict as 'Config' and cordis-py's
            # resolve_config runs before apply (fork commit 1c5e6f1). The
            # replay harness calls apply directly, so it applies the same
            # resolution here to keep the recorded body faithful.
            if config_schema is not None:
                config = config_schema.resolve(config)
            return apply(_RecordingContext(ctx, timeline, requires, services), config)

        recording_apply.__name__ = getattr(apply, "__name__", "apply")
        recording_apply.__doc__ = getattr(apply, "__doc__", None)
        return recording_apply

    # -- origin tagging ----------------------------------------------------

    def set_origin(self, origin: dict) -> None:
        """Tag everything recorded from now on with where it came from.

        The session sets this around a provided-service call, which is what
        makes forward replay expressible: a call is a re-invocable unit, an
        activation-body step is not.
        """
        self._origin = dict(origin)
        for timeline in self.timelines.values():
            timeline.origin = dict(origin)

    def activation_origin(self) -> None:
        self.set_origin({"phase": "activation"})

    # -- crash-recovery WAL (roadmap item 47) ------------------------------

    def open_wal(self, path: str, generation: Optional[int] = None) -> "WriteAheadLog":
        """Open a durable write-ahead log; every step recorded from now on is
        appended to it as it commits."""
        if self.wal is not None:  # a prior generation's log (e.g. --watch reload)
            self.wal.close()
        self.wal = WriteAheadLog(path, self._ir, generation).open()
        return self.wal

    def commit_wal(self, components: Optional[list] = None) -> None:
        """Mark activation complete and close the WAL. The presence of this
        marker is what a restart reads as roll-forward vs roll-back."""
        if self.wal is not None:
            self.wal.commit_activation(components)
            self.wal.close()

    # -- reading -----------------------------------------------------------

    def timeline(self, component: Optional[str] = None) -> Timeline:
        if component is None:
            if len(self.timelines) == 1:
                return next(iter(self.timelines.values()))
            raise ReplayError(
                "name a component — this composition recorded "
                f"{', '.join(sorted(self.timelines)) or 'nothing'}")
        try:
            return self.timelines[component]
        except KeyError:
            raise ReplayError(
                f"no recorded timeline for {component!r} "
                f"(recorded: {', '.join(sorted(self.timelines)) or 'none'})") from None

    def as_dict(self) -> dict:
        return {
            "components": [t.as_dict() for t in self.timelines.values()],
            "guarantee": GUARANTEE,
        }


# ---------------------------------------------------------------------------
# crash recovery: the accumulator as a write-ahead log (roadmap item 47)
# ---------------------------------------------------------------------------
#
# The accumulator already IS a write-ahead log: an ordered list of effects,
# each paired with the inverse that undoes it.  Held in process memory, it dies
# with the process — a `kill -9` mid-activation orphans whatever external state
# the half-run activation already touched, with no record of it.  This persists
# the accumulator, effect by effect *as each commits*, to a durable append-only
# log so a restart can prove its way back.
#
# The honest analysis is the design (docs/crash-recovery.md).  After a crash
# the process memory is gone, so an inverse that closes over an in-process
# object (a Map handle, a fiber's provision registry) is a no-op — there is
# nothing left for it to act on.  What the WAL's cargo really is, therefore, is
# **boundary state**: the crossings whose referent outlives the process.  Two
# kinds, both already classified by the frontend and the recorder:
#
#   * an **emission** already left the process (a row written, a message sent).
#     Its record carries whether it was crossed *bare* or with a *compensation*
#     declared for it (A5).
#   * an **acquire** whose returned resource outlives the process (a file on
#     disk persists; a socket died with the process).  The `extern acquire`
#     classification, and the resource type it returns, is what the WAL reads to
#     tell these apart.
#
# This forces one language-level requirement worth stating plainly: an inverse
# that must run *after* a restart has to be reconstructible **from a
# description** — a named boundary operation plus its captured concrete
# arguments — and NOT from a closure, because the closure's captured world no
# longer exists.  :func:`inverse_descriptor` marks each record accordingly, and
# where an inverse is only a closure it says so honestly rather than pretending
# a dead lambda can be re-run.

WAL_VERSION = 1

#: The single sentence recovery is allowed to claim.  Deliberately narrow, in
#: the same spirit as :data:`GUARANTEE`.
WAL_GUARANTEE = (
    "the WAL records each committed effect's step identity, boundary "
    "classification and inverse DESCRIPTOR (not its closure). On restart, "
    "recovery runs the reconstructible boundary inverses newest-first (LIFO); "
    "in-process inverses are moot (their captured memory died with the "
    "process) and closure-only boundary inverses are reported as residue, "
    "never silently claimed to have run."
)

# how the WAL classifies a step's referent lifetime relative to the process
REFERENT_PROCESS_CROSSING = "process-crossing"   # an emission — already left
REFERENT_OUTLIVES = "outlives-process"           # a file, a durable handle
REFERENT_IN_PROCESS = "in-process"               # a Map, a registry — dies with us
REFERENT_UNKNOWN = "unknown"                      # cannot be told; must be proven

#: A coarse, *stated* policy mapping an acquired resource's type name to whether
#: its referent outlives the process.  The language cannot know this in general
#: (that is the honest bound — docs/crash-recovery.md §"boundary is the cargo");
#: the policy is a host concern, defaulting to "unknown, therefore prove it".
_OUTLIVES_HINTS = {
    "outlives": ("File", "Path", "Dir", "Handle", "Db", "Table", "Row",
                 "Blob", "Object", "Lock", "Lease", "Pid"),
    "dies": ("Socket", "Conn", "Connection", "Stream", "Channel", "Fd",
             "Pipe", "Pool", "Session", "Client"),
}


def _referent_of_resource(resource: Optional[str]) -> str:
    if not resource:
        return REFERENT_UNKNOWN
    for hint in _OUTLIVES_HINTS["outlives"]:
        if hint.lower() in resource.lower():
            return REFERENT_OUTLIVES
    for hint in _OUTLIVES_HINTS["dies"]:
        if hint.lower() in resource.lower():
            return REFERENT_IN_PROCESS
    return REFERENT_UNKNOWN


def _acquire_resources(ir: Optional[dict]) -> dict:
    """extern name -> the resource type it acquires (its `returns`), for the
    `extern acquire` declarations. Read straight off the IR the frontend owns;
    never edited here."""
    out: dict = {}
    for ext in (ir or {}).get("externs") or []:
        if ext.get("class") == "acquire" and ext.get("returns"):
            out[ext["name"]] = ext["returns"]
    return out


def boundary_of(step: "Step", ir: Optional[dict] = None) -> dict:
    """Classify one recorded step's boundary state — what, if anything, of it
    outlives the process.

    This is the WAL's real payload.  A pure in-process effect (an author
    ``effect`` over a local ``Map``) classifies as ``in-process``: after a
    crash there is nothing of it left to undo.  An emission classifies as a
    ``process-crossing`` and carries whether a compensation was declared for
    it.  A provision is registry state, gone with the process.
    """
    kind = step.kind
    if kind == KIND_EMISSION:
        return {
            "class": "emission",
            "referent": REFERENT_PROCESS_CROSSING,
            "compensated": step.compensation is not None,
            "detail": step.detail or {},
        }
    if kind == KIND_COMPENSATION:
        return {"class": "compensation", "referent": REFERENT_PROCESS_CROSSING,
                "for": (step.detail or {}).get("for")}
    if kind == KIND_PROVISION:
        return {"class": "provision", "referent": REFERENT_IN_PROCESS,
                "key": (step.detail or {}).get("key")}
    if kind == KIND_EFFECT:
        # an author `effect X undo Y`. Is X an `extern acquire` whose referent
        # outlives us? The recorder does not capture the extern name for an
        # effect (its inverse is a closure), so absent an explicit descriptor
        # this is an in-process effect. When the step carries an explicit
        # acquire `resource`, decide by the stated policy.
        resource = (step.detail or {}).get("resource")
        if resource is not None:
            return {"class": "acquire", "resource": resource,
                    "referent": _referent_of_resource(resource)}
        return {"class": "in-process", "referent": REFERENT_IN_PROCESS}
    # boundary / hinge / opaque — nothing that outlives, nothing to reconstruct
    return {"class": kind, "referent": REFERENT_IN_PROCESS}


def inverse_descriptor(step: "Step", ir: Optional[dict] = None) -> dict:
    """The inverse of ``step`` as a DESCRIPTION that survives the process, or an
    honest flag that it is a closure and does not.

    Reconstructible ⟺ the inverse is a named boundary operation with captured
    concrete arguments — something a fresh process can re-issue against the
    world.  The recorder captures concrete arguments for exactly one thing: an
    **emission** (``key.method(args)`` recorded through the service proxy).
    Every other inverse the accumulator holds — a compensation lambda, a
    provision withdrawal (runtime-derived, R5), an author ``undo`` over a local
    handle — is a **closure** over process memory: the recorder has its source
    site but not a re-issuable call, so after a restart it cannot be run and
    this says so.

    A record may instead be appended with an *explicit* descriptor (see
    :meth:`WriteAheadLog.record_boundary`) when a boundary effect knows its own
    inverse call and arguments — that is the reconstructible path a durable
    resource (a file to unlink, a row to delete) takes.
    """
    explicit = getattr(step, "inverse_op", None)
    if explicit is not None:
        return {"reconstructible": True, "op": explicit,
                "why": "explicit boundary descriptor: a named call with "
                       "captured arguments, re-issuable in a fresh process"}
    if step.kind == KIND_EMISSION:
        # an emission has no inverse (one-way). Its record anchors the
        # boundary; a compensation, if any, is a *separate* step.
        return {"reconstructible": False,
                "reason": "an emission is a one-way crossing; it has no inverse"}
    if not step.revertible:
        return {"reconstructible": False,
                "reason": f"{step.kind}: nothing of the author's to undo"}
    return {
        "reconstructible": False,
        "reason": ("closure over in-process memory — the recorder holds this "
                   "inverse's source site but not a re-issuable call with "
                   "captured arguments, so a fresh process cannot run it"),
        "site": step.site,
        "source": step.source,
    }


def _wal_record(step: "Step", component: str, seq: int,
                ir: Optional[dict] = None) -> dict:
    return {
        "record": "effect",
        "seq": seq,
        "component": component,
        "stepIndex": step.index,
        "kind": step.kind,
        "label": step.label,
        "site": step.site,
        "source": step.source,
        "origin": step.origin or {},
        "boundary": boundary_of(step, ir),
        "inverse": inverse_descriptor(step, ir),
    }


def _next_seq(path: str) -> int:
    """The seq a reopened WAL must resume from: one past the highest seq
    already on disk, or 0 for a new or empty log.

    A session's WAL owns ONE strictly increasing seq space for its whole
    lifetime. A ``--watch`` reload reopens the same file in append mode
    (:meth:`Recorder.open_wal`), so a fresh :class:`WriteAheadLog` that reset
    ``_seq`` to 0 would collide in seq-space with the records the prior
    generation already wrote — and recover keys discharge descriptors by seq,
    so a duplicated seq lets a committed transaction's rollback be replayed or
    an aborted one's be skipped (item 325). Reopen therefore RESUMES the
    counter; it never resets over a non-empty log. A torn final record (a real
    ``kill -9`` can leave one) is skipped, exactly as :meth:`read` tolerates it.
    """
    highest = -1
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partial final record — the crash itself
                seq = entry.get("seq")
                if isinstance(seq, int) and seq > highest:
                    highest = seq
    except OSError:
        return 0  # no log yet: a new session starts its seq space at 0
    return highest + 1


class WriteAheadLog:
    """A durable, append-only log of committed effects and their inverse
    descriptors (roadmap item 47).

    Append-only and flushed+``fsync``'d per record, so a record that a caller
    saw acknowledged is on disk before the effect it describes is allowed to
    matter — the write-ahead discipline.  The on-disk format is JSON Lines: a
    header line, one line per committed effect, and a terminal marker
    (``activation-complete`` on a clean activation, absent after a crash).  That
    presence-or-absence is the whole roll-forward vs roll-back decision.

    A WAL never holds a live runtime object; it holds descriptions.  That is
    the point — it has to survive the process that produced it.
    """

    def __init__(self, path: str, ir: Optional[dict] = None,
                 generation: Optional[int] = None) -> None:
        self.path = path
        self._ir = ir
        self._generation = generation
        self._seq = 0
        self._handle = None

    # -- writing -----------------------------------------------------------

    def open(self) -> "WriteAheadLog":
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Resume this session's monotonic seq space from whatever is already on
        # disk. On the first open the log is new (or empty) and this is 0; on a
        # --watch reload the same file already carries the prior generation's
        # records, and resuming here — rather than the constructor's _seq = 0 —
        # is what keeps a reopened WAL from restarting seq at 0 and colliding
        # with them (item 325; see _next_seq).
        self._seq = _next_seq(self.path)
        self._handle = open(self.path, "a", encoding="utf-8")
        if self._handle.tell() == 0:
            self._write({"record": "header", "walVersion": WAL_VERSION,
                         "generation": self._generation,
                         "guarantee": WAL_GUARANTEE})
        return self

    def __enter__(self) -> "WriteAheadLog":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _write(self, record: dict) -> None:
        if self._handle is None:
            raise ReplayError("write-ahead log is not open — call open() first")
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()
        try:
            os.fsync(self._handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass

    def append_step(self, step: "Step", component: str) -> dict:
        """Write one committed effect ahead of it mattering."""
        record = _wal_record(step, component, self._seq, self._ir)
        self._seq += 1
        self._write(record)
        return record

    def record_boundary(self, component: str, label: str, *, resource: str,
                        inverse_op: dict, kind: str = KIND_EFFECT) -> dict:
        """Append a boundary effect with an EXPLICIT, reconstructible inverse.

        For a durable resource (a file, a row) whose inverse is a known named
        call with captured arguments — the reconstructible path.  ``inverse_op``
        is ``{"receiver","method","args"}``; recovery re-issues it against the
        world in a fresh process.
        """
        referent = _referent_of_resource(resource)
        record = {
            "record": "effect", "seq": self._seq, "component": component,
            "stepIndex": None, "kind": kind, "label": label,
            "site": None, "source": None, "origin": {"phase": "activation"},
            "boundary": {"class": "acquire", "resource": resource,
                         "referent": referent},
            "inverse": {"reconstructible": True, "op": inverse_op,
                        "why": "explicit boundary descriptor: a named call with "
                               "captured arguments, re-issuable in a fresh "
                               "process"},
        }
        self._seq += 1
        self._write(record)
        return record

    def record_discharge_descriptor(
            self, entry: str, *, receiver: str, method: str, args: list,
            origin: Optional[dict] = None, witness: Any = None,
            idempotency: Optional[str] = None) -> dict:
        """Append the WAL discharge-descriptor for one witnessed (`transactional`)
        inverse or one `compensation` (docs/design/teardown-contract.md, "WAL
        descriptor"; owned by the witnessed-wal-recover slice on the py tier).

        This is the record the Slice-2 teardown loop writes at REGISTRATION for
        an entry whose inverse/offset a fresh process must be able to re-issue
        after a crash. It is a NAMED-CALL descriptor with captured serializable
        arguments, never a closure (243 rule 4, 247 decision 5): a closure after
        a crash is residue, not recovery.

        The named-call field is ``call: {"receiver","method","args"}`` — the same
        re-issuable shape :meth:`record_boundary` writes and
        :meth:`revl.recovery.World.key`/``apply_inverse`` already key off. Writer
        and reader agree on ``receiver`` (the ``call.key`` spelling the contract
        first drafted is reconciled to ``receiver`` here so no adapter shim is
        needed). ``seq`` is the registration order on the shared stack; recover
        replays reverse-seq within a phase and SKIPS any seq named in a durable
        discharge record (:meth:`record_discharge`).
        """
        if entry not in ("transactional", "compensation"):
            raise ReplayError(
                f"discharge descriptor entry must be 'transactional' or "
                f"'compensation', got {entry!r}")
        record = {
            "record": "discharge-descriptor",
            "seq": self._seq,
            "entry": entry,
            "call": {"receiver": receiver, "method": method,
                     "args": list(args)},
            "origin": origin or {},
            "witness": witness,          # transactional only; durable data, not a handle
            "idempotency": idempotency,  # author-supplied key where present (item 309)
        }
        self._seq += 1
        self._write(record)
        return record

    def record_deferred_emission(self, *, receiver: str, method: str,
                                 args: list,
                                 idempotency: Optional[str] = None) -> dict:
        """Append the class-(b) deferral-queue descriptor at ENQUEUE (item 245,
        docs/design/245-session-commit.md, Decision 3). The intent is logged
        here, at the call site of the deferred emission; the OUTCOME is a later
        ``flushed`` record written after the host body fires, so the WAL never
        claims a crossing that has not happened.

        A NAMED-CALL descriptor with captured serializable arguments, never a
        closure (243 rule 4) — a crashed session's queue must be enumerable from
        the log alone. Draws from the session's single ``_seq`` counter, so a seq
        is a position in the session, not a per-kind index."""
        record = {
            "record": "deferred-emission",
            "seq": self._seq,
            "call": {"receiver": receiver, "method": method, "args": list(args)},
            "origin": {"key": receiver, "method": method},
            "idempotency": idempotency,
        }
        self._seq += 1
        self._write(record)
        return record

    def record_approval_granted(self, entry: dict) -> dict:
        """Append the ``approval-granted`` record for a human yes to a class-(c)
        crossing (item 246, docs/design/246-auto-approve.md, Decision 3). Names
        the ticket hash, the reach-closure candidate hash, the component, and the
        fields the human saw, so the audit can answer "which human decision
        authorized this crossing". Consumes no seq — like ``commit-approved`` it
        names facts, it is not an effect."""
        record = {"record": "approval-granted", **entry}
        self._write(record)
        return record

    def record_approval_consumed(self, request_id: str) -> dict:
        """Append the single-use SPEND, durably, BEFORE the extern body runs
        (item 246, Decision 3: consume-before-fire). A crash between this record
        and the emission leaves consumed-but-unfired — an owed action that needs a
        FRESH approval, which is fail-closed: the world saw at most one fire on
        this yes. The later emission record names the same ``requestId``; the
        audit joins the spend and the emission on it."""
        record = {"record": "approval-consumed", "requestId": request_id}
        self._write(record)
        return record

    def record_approval_revoked(self, request_id: str) -> dict:
        """Append the ``approval-revoked`` record when an operator retires a
        session-scoped standing grant EARLY (roadmap item 379), before its TTL or
        uses lapse. Names the grant's ``requestId`` so the audit reads the grant's
        whole life — granted, the crossings it auto-approved (each an
        ``approval-consumed`` on the same id), and this revoke — and can tell a
        grant that lapsed on its own from one an operator cut short. Consumes no
        seq: like ``approval-granted`` it names a consent fact, it is not an
        effect."""
        record = {"record": "approval-revoked", "requestId": request_id}
        self._write(record)
        return record

    def record_approval_emission(self, request_id: str, capability: str,
                                 component: str) -> dict:
        """Append the ``approval-emission`` record AFTER a typed-approval crossing
        fires (item 246, Decision 3), naming the same ``requestId`` the spend
        did. The audit joins the ``approval-consumed`` spend and this emission on
        ``requestId``: a spend with no matching emission is a visible owed action
        (crossing unverified), never a silent gap. Consumes no seq — it names a
        fact about a fire that already happened."""
        record = {"record": "approval-emission", "requestId": request_id,
                  "capability": capability, "component": component}
        self._write(record)
        return record

    def record_commit_approved(self, manifest_hash: str) -> dict:
        """Append the ``commit-approved`` marker (item 245, Decision 4). Its
        presence IS the commit verdict, written BEFORE the first flush fire and
        before the ``discharge`` record, so a crash anywhere in the
        approved-to-discharged window reads as a COMMITTED session on recover
        (the window rule, Decision 3). Binds the approved manifest hash: what the
        human approved is exactly what fires. Consumes no seq — like
        ``discharge`` it names facts about existing seqs, it is not one."""
        record = {"record": "commit-approved", "hash": manifest_hash}
        self._write(record)
        return record

    def record_flushed(self, seq: int) -> dict:
        """Append ``{"flushed": seq}`` after one deferred emission's host body
        has fired (item 245, Decision 3). Write-ahead honesty: the intent was
        logged at enqueue; the outcome is logged only after the fire, so a crash
        between the fire and this record leaves that one emission ``outcome:
        unknown`` for recover, which is the truth. Names the descriptor's own
        ``seq``; consumes no new one."""
        record = {"record": "flushed", "seq": seq}
        self._write(record)
        return record

    def record_flush_residue(self, seq: int, error: dict) -> dict:
        """Append ``flush-residue`` for a deferred emission whose host body
        RAISED at flush (item 245, Decision 3). Continue-and-record, the
        Phase-1 rule of the teardown contract: the remaining queue still flushes
        and the commit still completes, with this residue enumerated."""
        record = {"record": "flush-residue", "seq": seq, "error": error}
        self._write(record)
        return record

    def record_aborted(self, replayed: list) -> dict:
        """Append ``{"record":"aborted","replayed":[seq...]}`` after an
        in-process abort finishes its replay (item 245, Decision 3). For
        recover's benefit, not the verdict's: the ABSENCE of ``commit-approved``
        is the abort verdict. It lets recover tell a COMPLETED abort (redo
        nothing) from a CRASHED one (finish the replay); a crash before it costs
        only a redundant idempotent re-run, never a wrong one (243 rule 5)."""
        record = {"record": "aborted", "replayed": list(replayed)}
        self._write(record)
        return record

    def record_discharge(self, discharged: list) -> dict:
        """Append the WAL discharge record ``{"discharged": [seq...]}`` — the
        commit-path proof that these seqs' transactional inverses were COMMITTED
        (their mutation is the deliverable) or their compensations were discharged
        (never owed on a clean unload).

        Load-bearing (teardown-contract.md, "Commit path"): it must be DURABLE
        before the activation reports success, so that a post-commit crash before
        the terminal ``activation-complete`` marker still lets ``revl recover``
        skip the committed inverse instead of replaying a committed transaction's
        rollback. Writing goes through the same fsync'd append discipline as every
        other record."""
        record = {"record": "discharge", "discharged": list(discharged)}
        self._write(record)
        return record

    def append_timeline(self, timeline: "Timeline") -> list:
        """Write every committed step of one component's accumulator, in order."""
        return [self.append_step(step, timeline.component)
                for step in timeline.steps]

    def commit_activation(self, components: Optional[list] = None) -> dict:
        """Mark activation complete.  Its presence is roll-forward; its absence
        (the crash case) is roll-back."""
        record = {"record": "activation-complete", "generation": self._generation,
                  "components": components or []}
        self._write(record)
        return record

    # -- reading -----------------------------------------------------------

    @staticmethod
    def read(path: str) -> dict:
        """Load a WAL from disk into ``{header, records, complete, torn}``.

        ``complete`` is whether the terminal ``activation-complete`` marker is
        present.  A trailing half-written line (a genuine ``kill -9`` can leave
        one) is tolerated and reported as ``torn`` rather than crashing the
        recovery that exists to handle exactly that.
        """
        header: dict = {}
        records: list = []
        complete = False
        torn = False
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    torn = True   # a partial final record — the crash itself
                    continue
                kind = entry.get("record")
                if kind == "header":
                    header = entry
                elif kind == "activation-complete":
                    complete = True
                    records.append(entry)
                else:
                    records.append(entry)
        return {"header": header, "records": records,
                "complete": complete, "torn": torn}
