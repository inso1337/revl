"""Runtime "why" — causal lifecycle traces, and prediction-vs-actuality.

`revl why <component> --trace <run.jsonl>` is the runtime companion to the
compile-time why-traces in :mod:`revl.why`. Those explain a *rejection* — a
search the compiler ran and threw away. This explains a *transition* — a
lifecycle move the runtime actually made, and the cause chain behind it,
recursively to the root:

    UserCache deactivated  because  PgDatabase withdrew (provider of `db`)
    PgDatabase withdrew    because  the operator withdrew it   (root)

Two halves, and the second is the point.

1. **Causal trace.** Every lifecycle transition `revl run` streams is also
   written to a JSONL trace where the transition carries its *cause* — who
   caused it and why — so the run explains itself post hoc. This is possible
   here and nowhere an APM can reach it, because the dependency graph is a
   checked artifact (G2 makes each provision unique, G3 makes the graph
   acyclic), not a guess reconstructed from timing.

2. **The oracle.** The static `withdraw` query (`query.withdrawal`) is an
   *exact* prediction: given G2 + G3 it names the precise set of components
   that lose a provision when one is withdrawn, and the LIFO order the
   runtime will tear them down in. A recorded trace holds the *actual* set
   and order. Diffing them turns every real withdrawal into a free
   conformance check that the runtime did what the compiler computed — the
   differential-oracle move (docs/selfhost-findings.md) applied to the
   runtime. A disagreement is never swallowed: it is a real defect in one of
   the two, reported as such.

This module is pure — it imports no runtime. The run driver
(:mod:`revl.run`) calls the builders here to emit the trace; the CLI reads it
back. Both the prediction and the recording are expressed in the *same*
event vocabulary, so the oracle compares like with like.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2

# event kinds (the `event` field)
LOAD = "load"          # a component came up (… -> ACTIVE)
WITHDRAW = "withdraw"  # a component went down (ACTIVE -> DISPOSED/PENDING/FAILED)
EMIT = "emit"          # (v2) an emission crossed an irreversible boundary at runtime

# cause kinds (the `cause.kind` field)
BOOT = "boot"                          # root: the composition booted
TRIGGER = "trigger"                    # root: an external cause withdrew it
REQUIREMENTS = "requirements"          # loaded once its providers were up
PROVIDER_WITHDRAWN = "provider-withdrawn"  # went down because a provider did


# --------------------------------------------------------------------------
# event construction
# --------------------------------------------------------------------------


def make_event(seq: int, gen: int, event: str, component: str,
               transition: str, cause: dict, ts: float | None = None) -> dict:
    """One trace record. `transition` is the observed state move
    (``"ACTIVE -> DISPOSED"``); `cause` is one of the cause dicts below.

    `ts` (v2, additive) is a monotonic-clock reading in fractional seconds
    (:func:`time.monotonic`), stamped when the transition is recorded. It is
    meaningful only for *durations within one run* — differences between two
    events of the same run — never as a wall-clock time. Omitted (a v1 event)
    means the recorder did not stamp one; a consumer treats duration as
    unavailable rather than assuming zero."""
    record = {
        "v": SCHEMA_VERSION,
        "seq": seq,
        "gen": gen,
        "event": event,
        "component": component,
        "transition": transition,
        "cause": cause,
    }
    if ts is not None:
        record["ts"] = ts
    return record


def make_emit_event(seq: int, gen: int, component: str, capability: str,
                    key: str, cause: dict, ts: float | None = None) -> dict:
    """One ``emit`` trace record (v2). Recorded when an emission crosses an
    irreversible boundary at runtime (the driver's ``emissionsCrossed`` site).

    Unlike a load/withdraw, an emit carries no `transition` (nothing settled to
    a new fiber state — a boundary was crossed). It names the `capability` the
    emission is scoped to (the target service) and the `key` — the emission
    label ``"<key>.<method>"``. `cause` explains why the crossing happened.
    A v1 reader never sees this kind; a v2 reader that does not model emissions
    ignores it (it is neither a load nor a withdraw)."""
    record = {
        "v": SCHEMA_VERSION,
        "seq": seq,
        "gen": gen,
        "event": EMIT,
        "component": component,
        "cause": cause,
        "capability": capability,
        "key": key,
    }
    if ts is not None:
        record["ts"] = ts
    return record


def cause_boot() -> dict:
    return {"kind": BOOT}


def cause_trigger(detail: str, code: str | None = None) -> dict:
    """Root cause: an external trigger. `code` (v2, additive) is the failure's
    diagnostic code (from :func:`revl.diagnostics.classify`) when this trigger
    is the settle into a ``FAILED`` state; omitted when the failure carries no
    classifiable ``RevlError`` (a bare crash), so a consumer buckets it as
    unclassified rather than under a fabricated code."""
    cause = {"kind": TRIGGER, "detail": detail}
    if code is not None:
        cause["code"] = code
    return cause


def cause_requirements(providers: list[dict]) -> dict:
    """Loaded because these providers (``{"component", "key"}`` each) were up.
    Empty ``providers`` means it had no resolved injection — a root load,
    equivalent to boot for the purpose of the chain walk."""
    return {"kind": REQUIREMENTS, "providers": list(providers)}


def cause_provider_withdrawn(component: str, key: str,
                             code: str | None = None) -> dict:
    """Went down because the provider of injected `key` withdrew. `code` (v2,
    additive) is the diagnostic code when this dependent settled into ``FAILED``
    rather than cleanly ``PENDING``/``DISPOSED``; omitted otherwise. The causal
    *edge* is unchanged — the code is extra detail on *how* it went down, so the
    cause-chain walk and the oracle read it exactly as before."""
    cause = {"kind": PROVIDER_WITHDRAWN, "component": component, "key": key}
    if code is not None:
        cause["code"] = code
    return cause


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def write_trace(events: list[dict], path: str) -> None:
    """Write events as JSONL (one object per line)."""
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def parse_trace(text: str) -> list[dict]:
    """Parse JSONL text into events, tolerating blank lines."""
    events = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"trace line {lineno}: not JSON ({exc})") from None
        if not isinstance(record, dict) or "event" not in record:
            raise ValueError(f"trace line {lineno}: not a trace event")
        events.append(record)
    return events


def read_trace(path: str) -> list[dict]:
    return parse_trace(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# building causal events from the IR (used by the run driver, and to render
# the static prediction in the same vocabulary the runtime records)
# --------------------------------------------------------------------------


def _composition(ir: dict):
    from .query import Composition  # noqa: PLC0415 — lazy; query imports lower

    return Composition(ir)


def load_causes(ir: dict) -> dict[str, dict]:
    """component -> the `cause` its load carries: the resolved providers it
    injects, or `boot` when it injects nothing resolvable here."""
    index = _composition(ir)
    causes: dict[str, dict] = {}
    for name, entry in index.entries.items():
        providers = []
        for key in entry.get("inject") or []:
            provider = index.provider(name, key)
            if provider is not None:
                providers.append({"component": provider, "key": key})
        causes[name] = cause_requirements(providers) if providers else cause_boot()
    return causes


def withdrawal_causes(ir: dict, component: str) -> dict[str, dict]:
    """component -> the `cause` its withdrawal carries, for the cascade that
    withdrawing `component` sets off. The withdrawn component's cause is left
    to the caller (it is the trigger); every dependent's cause is the
    provider whose withdrawal took it down.

    Reads :func:`query.withdrawal` so the attribution can never disagree with
    the static prediction's own notion of who depends on whom."""
    from .query import withdrawal  # noqa: PLC0415

    result = withdrawal(ir, component)
    causes: dict[str, dict] = {}
    for item in result.get("cascade") or []:
        key = (item.get("lostKeys") or [item.get("provider")])[0]
        causes[item["component"]] = cause_provider_withdrawn(item["provider"], key)
    return causes


def predicted_cascade(ir: dict, component: str) -> dict:
    """The static prediction, as plain set + order.

    * ``broken`` — the components that lose a provision (the cascade), the
      exact set the query names; sorted for set comparison.
    * ``order`` — the LIFO teardown order the runtime is predicted to use,
      *including* the withdrawn component itself (it is the last to go).
    """
    from .query import withdrawal  # noqa: PLC0415

    result = withdrawal(ir, component)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"),
                "known": result.get("known")}
    broken = sorted(item["component"] for item in result.get("cascade") or [])
    return {
        "ok": True,
        "component": component,
        "broken": broken,
        "order": list(result.get("withdrawalOrder") or []),
        "survivors": list(result.get("survivors") or []),
    }


# --------------------------------------------------------------------------
# the trace, and the causal chain walk
# --------------------------------------------------------------------------


@dataclass
class Frame:
    """One hop of a cause chain: the transition, and why it happened."""

    component: str
    event: str
    transition: str
    cause: dict
    note: str = ""


class Trace:
    """A recorded run, queryable post hoc."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    @classmethod
    def load(cls, path: str) -> "Trace":
        return cls(read_trace(path))

    def components(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            name = event.get("component")
            if name and name not in seen:
                seen.append(name)
        return seen

    def transitions_of(self, component: str) -> list[dict]:
        return [e for e in self.events if e.get("component") == component]

    def last_transition(self, component: str) -> dict | None:
        found = self.transitions_of(component)
        return found[-1] if found else None

    # -- the causal chain --------------------------------------------------

    def _event_matching(self, component: str, prefer: str | None) -> dict | None:
        """The transition to explain for `component`: the last of the
        preferred kind if one exists, else the last transition recorded."""
        found = self.transitions_of(component)
        if not found:
            return None
        if prefer is not None:
            preferred = [e for e in found if e.get("event") == prefer]
            if preferred:
                return preferred[-1]
        return found[-1]

    def cause_chain(self, component: str, prefer: str | None = WITHDRAW) -> list[Frame]:
        """The cause chain for `component`'s transition, root last.

        Follows `provider-withdrawn` edges (a withdrawal cascade) or
        `requirements` edges (a load) up to a `boot`/`trigger` root. Robust
        to a missing link (a truncated trace) and — defensively, though G3
        forbids the cycle — to a repeat, which it stops at."""
        frames: list[Frame] = []
        seen: set[str] = set()
        current = component
        while current is not None and current not in seen:
            seen.add(current)
            event = self._event_matching(current, prefer)
            if event is None:
                frames.append(Frame(current, "?", "?",
                                    {"kind": "unrecorded"},
                                    "no transition recorded for this component"))
                break
            cause = event.get("cause") or {}
            frames.append(Frame(current, event.get("event", "?"),
                                event.get("transition", "?"), cause,
                                _cause_note(cause)))
            kind = cause.get("kind")
            if kind == PROVIDER_WITHDRAWN:
                current = cause.get("component")
            elif kind == REQUIREMENTS:
                providers = cause.get("providers") or []
                current = providers[0]["component"] if providers else None
            else:  # boot / trigger / unrecorded — a root
                current = None
        return frames


def _cause_note(cause: dict) -> str:
    kind = cause.get("kind")
    if kind == BOOT:
        return "the composition booted"
    if kind == TRIGGER:
        return cause.get("detail") or "external trigger"
    if kind == PROVIDER_WITHDRAWN:
        return (f"injects `{cause.get('key')}`, provided by "
                f"{cause.get('component')}, which withdrew")
    if kind == REQUIREMENTS:
        providers = cause.get("providers") or []
        if not providers:
            return "no unmet requirement — a root load"
        return "requires " + ", ".join(
            f"`{p['key']}` from {p['component']}" for p in providers)
    return kind or "unknown"


def render_chain(component: str, frames: list[Frame]) -> str:
    """Human rendering of a cause chain, mirroring `why.render`'s shape."""
    if not frames:
        return f"why {component}: nothing recorded for it in this trace"
    verb = {LOAD: "was loaded", WITHDRAW: "was withdrawn",
            EMIT: "crossed an emission boundary"}.get(
        frames[0].event, "transitioned")
    lines = [f"why {component} {verb}:"]
    width = max(len(f.component) for f in frames)
    for i, frame in enumerate(frames):
        arrow = "" if i == 0 else "-> "
        root = "   (root cause)" if i == len(frames) - 1 and \
            frame.cause.get("kind") in (BOOT, TRIGGER) else ""
        lines.append(f"  {arrow}{frame.component.ljust(width)}  "
                     f"{frame.transition}   because {frame.note}{root}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the oracle: static prediction vs recorded actuality
# --------------------------------------------------------------------------


@dataclass
class Defect:
    kind: str
    detail: str


def actual_cascade(trace: Trace, component: str) -> dict:
    """From a recorded trace, the withdrawal cascade actually observed when
    `component` was withdrawn: the set of dependents that went down, and the
    order every affected component (dependents plus `component`) settled in.

    A component counts as *broken* when its recorded withdrawal names, at the
    root of its cause chain, the withdrawal of `component` — i.e. it went down
    because of this withdrawal, not a coincidental one."""
    withdraws = [e for e in trace.events if e.get("event") == WITHDRAW]
    # the trigger event for the component under test
    root = None
    for event in withdraws:
        if event.get("component") == component \
                and (event.get("cause") or {}).get("kind") == TRIGGER:
            root = event
    order: list[str] = []
    broken: list[str] = []
    for event in withdraws:
        name = event["component"]
        if name == component:
            if root is not None and event is root:
                order.append(name)
            continue
        # does this withdrawal's cause chain root at `component`?
        chain = trace.cause_chain(name, prefer=WITHDRAW)
        roots_here = any(f.component == component for f in chain)
        if roots_here:
            order.append(name)
            broken.append(name)
    # the withdrawn component tears down last (LIFO); if it never reached a
    # settled trigger event we still surface what we saw
    if component in order:
        order = [n for n in order if n != component] + [component]
    return {"broken": sorted(broken), "order": order,
            "observed": root is not None}


def oracle(ir: dict, component: str, trace: Trace) -> dict:
    """Diff the static `withdraw` prediction against the recorded actuality.

    Returns a conformance report. ``conforms`` is true only when the actual
    broken *set* and teardown *order* both match the prediction. Every
    mismatch is a `Defect`: the runtime tore down something the compiler
    proved safe, skipped something the compiler proved doomed, or unwound in
    an order the LIFO prediction rules out. Each is a real defect in one of
    the two — the whole point of a differential oracle (a disagreement is
    never noise, because neither side is allowed to be wrong on its own
    terms)."""
    predicted = predicted_cascade(ir, component)
    if not predicted.get("ok"):
        return {"ok": False, "component": component,
                "error": predicted.get("error"), "known": predicted.get("known")}

    actual = actual_cascade(trace, component)
    if not actual["observed"]:
        return {
            "ok": True, "component": component, "conforms": None,
            "predicted": predicted, "actual": actual, "defects": [],
            "note": (f"no withdrawal of {component} is recorded in this trace "
                     f"— nothing to check the prediction against"),
        }

    pred_set = set(predicted["broken"])
    act_set = set(actual["broken"])
    defects: list[Defect] = []

    for extra in sorted(act_set - pred_set):
        defects.append(Defect(
            "unexpected-teardown",
            f"{extra} went down when {component} withdrew, but the prediction "
            f"proves it keeps its provisions (it survives). A component the "
            f"compiler proved safe was torn down: a defect in the runtime, or "
            f"in the prediction's provider graph."))
    for missing in sorted(pred_set - act_set):
        defects.append(Defect(
            "missing-teardown",
            f"the prediction proves {missing} loses a provision when "
            f"{component} withdraws, but the trace shows it did not go down. "
            f"Either the runtime failed to propagate the withdrawal, or the "
            f"prediction over-counts."))

    # order only when the sets agree (an order diff over different sets is
    # noise — the set defects above already name the real disagreement)
    if not defects:
        pred_order = [n for n in predicted["order"]
                      if n in pred_set or n == component]
        act_order = [n for n in actual["order"]
                     if n in act_set or n == component]
        if pred_order != act_order:
            defects.append(Defect(
                "order-mismatch",
                f"same set of teardowns, different order.\n"
                f"    predicted (LIFO): {' -> '.join(pred_order)}\n"
                f"    actual:           {' -> '.join(act_order)}\n"
                f"    G3 makes the graph acyclic, so the LIFO order is exact — "
                f"a divergence is a real runtime ordering defect."))

    return {
        "ok": True, "component": component,
        "conforms": not defects,
        "predicted": predicted, "actual": actual,
        "defects": [{"kind": d.kind, "detail": d.detail} for d in defects],
    }


def render_oracle(report: dict) -> str:
    if not report.get("ok"):
        lines = [f"error: {report.get('error')}"]
        if report.get("known"):
            lines.append("  known: " + ", ".join(report["known"]))
        return "\n".join(lines)

    component = report["component"]
    if report.get("conforms") is None:
        return f"oracle {component}: {report.get('note')}"

    predicted, actual = report["predicted"], report["actual"]
    head = (f"oracle: does the runtime's withdrawal of {component} match the "
            f"compiler's prediction?")
    lines = [head,
             f"  predicted teardown (LIFO): {' -> '.join(predicted['order']) or '—'}",
             f"  actual   teardown (LIFO): {' -> '.join(actual['order']) or '—'}"]
    if report["conforms"]:
        lines.append("  CONFORMS — the runtime did exactly what the compiler "
                     "computed (set and order).")
    else:
        lines.append(f"  DEFECT — {len(report['defects'])} disagreement(s); a "
                     f"differential oracle proves this is real, not noise:")
        for defect in report["defects"]:
            lines.append(f"    [{defect['kind']}] {defect['detail']}")
    return "\n".join(lines)
