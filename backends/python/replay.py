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
import sys
from typing import Any, Callable, Optional

__all__ = [
    "GUARANTEE", "IrreversibleStep", "KINDS", "Recorder", "ReplayError",
    "Step", "Timeline",
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
            return step, None

        if inspect.ismethod(value) and getattr(value, "__name__", "") == "drain":
            # `yield frame.drain`: the Frame's adopted-effect hinge.  Left
            # completely untouched — its ordering is load-bearing for R1 and
            # the adopted effects are already in this timeline individually.
            step = self._add(KIND_HINGE, _lbl(effect_label, "drain"), effect_label,
                             note="Frame.drain — runtime scaffolding, not an "
                                  "author step; step-back skips it")
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
                        name, plugin["apply"], comp.get("requires") or {}, services)))
            names.append(name)
        return names

    def _wrap_apply(self, name: str, apply, requires: dict, services: dict):
        recorder = self

        def recording_apply(ctx, config):
            timeline = Timeline(name, recorder.sources)
            timeline.origin = dict(recorder._origin)
            recorder.timelines[name] = timeline
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
