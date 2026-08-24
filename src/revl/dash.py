"""`revl dash` — the supervisor's cockpit (roadmap item 63).

Every human-in-the-loop feature the system grew — 21's boundary-widening ack,
33's policy exceptions, 55's operator actions, 61's leases, 62's interrupts —
ends a sentence with "and here a human decides." But the human was handed a
CLI and a wall of JSON. `revl dash` is the surface those features assume: a
**read-only** live view over a session or a recorded run, in one frame.

Three panes, sourced from surfaces that already exist and never mutated here:

  * the **dependency graph** as it actually is — components, the realms they
    are isolated into (`query.Composition`), and the service seams between
    them; in live mode each provision is marked *served* or *drifted* and each
    component carries its fiber state (the session's `live_state`).
  * the **causal trace** streaming — item 27's lifecycle trace
    (`why_runtime.Trace`): every load/withdraw with the cause behind it, and
    optionally the recorded effect timeline (`query`-side of a replay dump).
  * the **pending-decisions queue** — the boundary-widening additions awaiting
    an ack (item 21, `audit_diff`) and the policy exceptions awaiting a call
    (item 33, `policy`), each rendered *with its evidence attached*: the why-
    trace / dossier diff is the approval surface, not a footnote to it.

Read-only is the whole contract. The dash observes; it holds no handle that
can change the running system. It builds its model from a compiled IR (a copy),
a `live_state` dict, a trace, a timeline, and two audits — all pure reads — and
calls no session mutator. `build_model` treats its inputs as immutable, and the
tests pin that nothing it touches is changed.

Two ways to feed it, so it works with or without a runtime:

  * a **live session** — `Dashboard.from_session(session)` reads `session.ir`
    and `session.live_state()`, both read-only, and colors the graph as it
    stands now (generation, served-vs-drifted, fiber states);
  * a **recorded trace** — a `revl run --trace` JSONL (and optionally a replay
    timeline) renders the causal stream with no live runtime at all.

Machine first, human second (docs/queries.md §1): `build_model` returns the
structured view; `render_model` is the courtesy text rendering. A `--watch`
loop just rebuilds and reprints on an interval — the model is cheap and pure,
so a refresh is a re-read, never a re-run.
"""

from __future__ import annotations

import copy

# ---------------------------------------------------------------- vocabulary

MODE_STATIC = "static"      # a composition with no live state and no recording
MODE_LIVE = "live"          # a running session's post-swap state folded in
MODE_RECORDED = "recorded"  # a recorded trace/timeline, no live runtime

# decision-queue kinds (the two human gates the dash surfaces)
DECISION_WIDENING = "boundary-widening"  # item 21: an added G8 crossing to ack
DECISION_POLICY = "policy-exception"     # item 33: a boundary-policy violation

_SHARED_REALM = ""  # mirrors lower.SHARED_REALM: an un-isolated key is shared


# ---------------------------------------------------------------- model
#
# Everything below is a pure read. The one mutation-shaped risk is that
# `query.Composition` walks the IR and could, in principle, annotate it; it does
# not (it builds its own indices), but we hand it a deep copy anyway so the
# read-only contract holds regardless of what any reused surface does later.


def build_model(ir: dict, *, live_state: dict | None = None,
                trace=None, timeline=None,
                prev_audit: dict | None = None,
                accepted=None, accept_all: bool = False,
                policy=None, mcp_scope=None) -> dict:
    """The read-only cockpit model, assembled from the read surfaces.

    * ``ir`` — a compiled composition document (the dependency graph source).
    * ``live_state`` — a session's ``{generation, servedKeys, componentStates}``
      (`mcp.session.Session.live_state`); when present the graph is colored as
      it stands now and the mode becomes ``live``.
    * ``trace`` — an item-27 lifecycle trace: a ``why_runtime.Trace``, a list of
      event dicts, or JSONL text. Streams the causal pane.
    * ``timeline`` — a replay recording (a ``revl_timeline`` dump) for the
      effect/emission detail behind the lifecycle.
    * ``prev_audit`` — a previous ``audit`` document; diffing the current audit
      against it yields the boundary-widening queue (item 21). ``accepted`` /
      ``accept_all`` mark which additions have already been acked.
    * ``policy`` — a parsed ``policy.Policy``; its violations over the current
      audit are the policy-exception queue (item 33), each with its why-trace.
    * ``mcp_scope`` — components admitted through the MCP session, for the
      policy's ``mcp`` sandbox; ``"*"`` means every component.

    The input dicts are treated as immutable; nothing here writes back.
    """
    from . import query  # noqa: PLC0415 — lazy; query imports lower

    safe_ir = copy.deepcopy(ir or {})
    index = query.Composition(safe_ir)

    graph = _build_graph(index, live_state or {})
    trace_view = _build_trace(trace, timeline)
    decisions = _build_decisions(
        safe_ir, prev_audit=prev_audit, accepted=accepted,
        accept_all=accept_all, policy=policy, mcp_scope=mcp_scope)

    mode = MODE_STATIC
    if live_state:
        mode = MODE_LIVE
    elif trace_view is not None:
        mode = MODE_RECORDED

    return {
        "ok": True,
        "readOnly": True,
        "mode": mode,
        "generation": (live_state or {}).get("generation"),
        "graph": graph,
        "trace": trace_view,
        "decisions": decisions,
    }


# ---------------------------------------------------------------- graph pane


def _component_realms(index, name: str) -> list[str]:
    """The non-shared realms a component is isolated into — the values of its
    `isolate` map, read through `Composition.realm` so the dash can never
    disagree with the query layer about where a key lives."""
    entry = index.entries.get(name) or {}
    keys = list(entry.get("inject") or []) + list(entry.get("provides") or [])
    realms = {index.realm(name, key) for key in keys}
    return sorted(r for r in realms if r != _SHARED_REALM)


def _build_graph(index, live_state: dict) -> dict:
    """Components (with realms, provisions, requirements), the service seams
    between them, and a realm -> components grouping. All from the resolved
    `Composition`; live state only colors it."""
    served = set(live_state.get("servedKeys") or [])
    states = dict(live_state.get("componentStates") or {})
    has_live = bool(live_state)

    components = []
    realms: dict[str, list] = {}
    for name in sorted(index.entries):
        entry = index.entries[name]
        comp_realms = _component_realms(index, name)
        for realm in comp_realms:
            realms.setdefault(realm, []).append(name)

        provides = []
        for key in sorted(entry.get("provides") or []):
            realm = index.realm(name, key)
            prov = {"key": key, "realm": realm,
                    "service": index.service_of(name, key)}
            if has_live:
                prov["servedNow"] = key in served
            provides.append(prov)

        requires = []
        for key in sorted(entry.get("inject") or []):
            requires.append({"key": key, "realm": index.realm(name, key),
                             "service": index.service_of(name, key),
                             "provider": index.provider(name, key)})

        comp = {
            "name": name,
            "file": entry.get("file"),
            "realms": comp_realms,
            "provides": provides,
            "requires": requires,
            "unresolved": sorted(index.unresolved_injections(name)),
        }
        if has_live:
            comp["state"] = states.get(name)
        components.append(comp)

    seams = _build_seams(index)

    graph = {
        "components": components,
        "seams": seams,
        "realms": {r: sorted(set(cs)) for r, cs in sorted(realms.items())},
        "loadOrder": list(index.load_order),
    }
    if has_live:
        drifted = sorted(
            prov["key"]
            for comp in components for prov in comp["provides"]
            if prov.get("servedNow") is False)
        graph["driftedProvisions"] = drifted
    return graph


def _build_seams(index) -> list:
    """The service seams: an inter-component edge is a call on an injected key
    that lands in the provider's provide-method (`Composition.edges`). One
    seam per (from, to, key, method), deduped and ordered."""
    seen: set[tuple] = set()
    seams = []
    for scope_id, edges in index.edges.items():
        scope = index.scopes.get(scope_id) or {}
        frm = scope.get("component")
        for edge in edges:
            target = index.scopes.get(edge["scope"]) or {}
            to = target.get("component")
            mark = (frm, to, edge["key"], edge["method"])
            if mark in seen:
                continue
            seen.add(mark)
            seams.append({
                "from": frm, "to": to, "key": edge["key"],
                "method": edge["method"],
                "service": index.service_of(frm, edge["key"]),
            })
    seams.sort(key=lambda s: (s["from"] or "", s["to"] or "",
                              s["key"], s["method"]))
    return seams


# ---------------------------------------------------------------- trace pane


def _build_trace(trace, timeline) -> dict | None:
    """The causal stream: item-27 lifecycle events with their cause note, plus
    a compact view of any recorded effect timeline. Returns None when neither
    source is given (a static composition has no run to stream)."""
    if trace is None and timeline is None:
        return None

    from . import why_runtime as wr  # noqa: PLC0415 — read-only reuse of item 27

    events_view = []
    components: list[str] = []
    if trace is not None:
        tr = trace
        if not isinstance(tr, wr.Trace):
            events = (wr.parse_trace(tr) if isinstance(tr, str) else list(tr))
            tr = wr.Trace(events)
        components = tr.components()
        for event in tr.events:
            cause = event.get("cause") or {}
            events_view.append({
                "seq": event.get("seq"),
                "gen": event.get("gen"),
                "event": event.get("event"),
                "component": event.get("component"),
                "transition": event.get("transition"),
                "cause": cause.get("kind"),
                "note": wr._cause_note(cause),
            })
        events_view.sort(key=lambda e: (e["seq"] if e["seq"] is not None else 0))

    timeline_view = _timeline_view(timeline)
    return {
        "events": events_view,
        "components": components,
        "timeline": timeline_view,
        "eventCount": len(events_view),
    }


def _timeline_view(timeline) -> list | None:
    """A flat, ordered list of the recorded steps (a replay dump), enough to
    show what a component *did* alongside when it lived. Reuses query's own
    normaliser so the two never disagree on a recording's shape."""
    if timeline is None:
        return None
    from . import query  # noqa: PLC0415

    steps = []
    for tl in query._timelines_of(timeline):
        name = tl.get("component")
        for step in tl.get("steps") or []:
            detail = step.get("detail") or {}
            steps.append({
                "step": step.get("index"),
                "component": name,
                "kind": step.get("kind"),
                "label": step.get("label"),
                "key": detail.get("key"),
                "method": detail.get("method"),
                "compensated": step.get("compensatedBy") is not None,
            })
    steps.sort(key=lambda s: (s["step"] if s["step"] is not None else 0,
                              s["component"] or ""))
    return steps


# ------------------------------------------------------------ decisions pane
#
# The two human gates, as one queue. Each entry carries its evidence inline —
# the whole point of the pane is that the why-trace / dossier diff IS the
# approval surface, so a supervisor never has to leave the dash to see why a
# decision is being asked of them.


def _build_decisions(ir: dict, *, prev_audit, accepted, accept_all,
                     policy, mcp_scope) -> dict:
    """The pending-decisions queue: boundary-widening acks (item 21) and
    policy exceptions (item 33), each with evidence attached."""
    widening = _widening_queue(ir, prev_audit, accepted, accept_all) \
        if prev_audit is not None else []
    exceptions = _policy_queue(ir, policy, mcp_scope) \
        if policy is not None else []

    pending = ([w for w in widening if not w["acknowledged"]]
               + [e for e in exceptions])
    return {
        "widening": widening,
        "policy": exceptions,
        "pending": len(pending),
        "counts": {
            "widening": len(widening),
            "wideningPending": sum(1 for w in widening if not w["acknowledged"]),
            "policy": len(exceptions),
        },
    }


def _widening_queue(ir: dict, prev_audit: dict, accepted, accept_all: bool) \
        -> list:
    """Item 21's boundary-widening additions as a decision list. Each added G8
    crossing is one row; its evidence is the crossing token decoded to the
    component and the emission / host reach it names — the dossier diff the ack
    is a decision over."""
    from .audit_diff import audit_report, evaluate  # noqa: PLC0415

    new_audit = audit_report(ir)
    verdict = evaluate(prev_audit, new_audit, accepted=set(accepted or ()),
                       accept_all=accept_all)
    acked = set(verdict["acknowledged"])
    rows = []
    for token in verdict["added"]:
        rows.append({
            "kind": DECISION_WIDENING,
            "token": token,
            "acknowledged": token in acked,
            "evidence": _decode_crossing(token),
        })
    return rows


def _decode_crossing(token: str) -> dict:
    """`emit:<component>:<label>` / `host:<component>:<name>` -> its parts, so
    the queue shows *what* widened and *where*, not just the opaque token."""
    parts = token.split(":", 2)
    if len(parts) == 3 and parts[0] == "emit":
        return {"reach": "emission", "component": parts[1], "via": parts[2],
                "detail": f"{parts[1]} emits `{parts[2]}` — a new boundary "
                          f"crossing not in the previous generation"}
    if len(parts) == 3 and parts[0] == "host":
        return {"reach": "host", "component": parts[1], "via": parts[2],
                "detail": f"{parts[1]} reaches host code `{parts[2]}` — a new "
                          f"boundary crossing not in the previous generation"}
    return {"reach": "unknown", "component": None, "via": token,
            "detail": token}


def _policy_queue(ir: dict, policy, mcp_scope) -> list:
    """Item 33's policy exceptions as a decision list. Each violation is a row;
    its evidence is the violation's own why-trace — the offending chain that
    names which component reaches what it may not, and how."""
    from . import policy as policy_mod  # noqa: PLC0415
    from .audit_diff import audit_report  # noqa: PLC0415
    from .why import render as render_why  # noqa: PLC0415

    audit = audit_report(ir)
    scope = set(mcp_scope or ())
    mcp_components = (frozenset(audit.get("boundary") or {})
                      if "*" in scope else frozenset(scope))
    violations = policy_mod.evaluate(policy, audit, mcp_components=mcp_components)
    rows = []
    for v in violations:
        rows.append({
            "kind": DECISION_POLICY,
            "component": v.component,
            "token": v.token,
            "violation": v.kind,
            "message": v.message,
            "why": v.why.to_json() if hasattr(v.why, "to_json") else None,
            "evidence": {"detail": v.message,
                         "trace": render_why(v.why) if v.why else None},
        })
    return rows


# ---------------------------------------------------------------- rendering


class _Palette:
    """A tiny ANSI palette, disabled to empty strings when color is off (a
    pipe, a test, or `--no-color`). Kept std-lib only — no TUI dependency."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _c(self, code: str) -> str:
        return code if self.on else ""

    @property
    def dim(self): return self._c("\033[2m")
    @property
    def bold(self): return self._c("\033[1m")
    @property
    def red(self): return self._c("\033[31m")
    @property
    def green(self): return self._c("\033[32m")
    @property
    def yellow(self): return self._c("\033[33m")
    @property
    def blue(self): return self._c("\033[34m")
    @property
    def cyan(self): return self._c("\033[36m")
    @property
    def reset(self): return self._c("\033[0m")


def render_model(model: dict, *, color: bool = False) -> str:
    """The courtesy text render of a cockpit model — a plain periodic-refresh
    view, no heavy GUI. The structured model is the product; this is the
    human's window onto it."""
    if not model.get("ok"):
        return f"dash: {model.get('error', 'unavailable')}"

    p = _Palette(color)
    out: list[str] = []
    mode = model.get("mode", MODE_STATIC)
    gen = model.get("generation")
    head = f"{p.bold}revl dash{p.reset}  [{mode}]  (read-only)"
    if gen is not None:
        head += f"  generation {gen}"
    out.append(head)

    out.append("")
    out.extend(_render_graph(model["graph"], mode, p))
    if model.get("trace") is not None:
        out.append("")
        out.extend(_render_trace(model["trace"], p))
    out.append("")
    out.extend(_render_decisions(model["decisions"], p))
    return "\n".join(out)


def _render_graph(graph: dict, mode: str, p: _Palette) -> list:
    out = [f"{p.bold}DEPENDENCY GRAPH{p.reset}"]
    order = graph.get("loadOrder") or []
    if order:
        out.append(f"  {p.dim}load order (providers first):{p.reset} "
                   + " -> ".join(order))

    realms = graph.get("realms") or {}
    if realms:
        out.append(f"  {p.dim}realms:{p.reset} " + ", ".join(
            f"{p.cyan}{realm}{p.reset} [{', '.join(members)}]"
            for realm, members in realms.items()))

    for comp in graph.get("components") or []:
        realm_tag = ""
        if comp.get("realms"):
            realm_tag = f"  {p.cyan}@{','.join(comp['realms'])}{p.reset}"
        state = comp.get("state")
        state_tag = ""
        if state is not None:
            hue = p.green if state == "ACTIVE" else p.yellow
            state_tag = f"  {hue}{state}{p.reset}"
        out.append(f"\n  {p.bold}{comp['name']}{p.reset}{realm_tag}{state_tag}")

        for prov in comp.get("provides") or []:
            realm = f" @{prov['realm']}" if prov["realm"] else ""
            served = ""
            if "servedNow" in prov:
                if prov["servedNow"]:
                    served = f"  {p.green}(served){p.reset}"
                else:
                    served = f"  {p.red}(drifted — declared, not served now){p.reset}"
            out.append(f"    provides {p.green}{prov['key']}{realm}{p.reset}"
                       f": {prov['service'] or '?'}{served}")
        for req in comp.get("requires") or []:
            provider = req["provider"] or f"{p.red}— unresolved{p.reset}"
            realm = f" @{req['realm']}" if req["realm"] else ""
            out.append(f"    requires {p.yellow}{req['key']}{realm}{p.reset}"
                       f": {req['service'] or '?'}  <- {provider}")

    seams = graph.get("seams") or []
    if seams:
        out.append(f"\n  {p.bold}seams{p.reset} (service calls across components):")
        for seam in seams:
            out.append(f"    {seam['from']} {p.blue}--{seam['key']}."
                       f"{seam['method']}-->{p.reset} {seam['to']}")
    if graph.get("driftedProvisions"):
        out.append(f"\n  {p.red}drifted provisions (declared but not served "
                   f"now):{p.reset} " + ", ".join(graph["driftedProvisions"]))
    return out


def _render_trace(trace: dict, p: _Palette) -> list:
    out = [f"{p.bold}CAUSAL TRACE{p.reset}  "
           f"{p.dim}(item 27 — every transition carries its cause){p.reset}"]
    events = trace.get("events") or []
    if not events:
        out.append("  no lifecycle transitions recorded.")
    for event in events:
        verb = event.get("event") or "?"
        hue = p.green if verb == "load" else p.yellow
        out.append(f"  {p.dim}seq {event.get('seq')}{p.reset}  "
                   f"{hue}{event.get('component')}{p.reset} "
                   f"{event.get('transition')}  "
                   f"{p.dim}because{p.reset} {event.get('note')}")

    steps = trace.get("timeline")
    if steps:
        out.append(f"\n  {p.bold}effect timeline{p.reset} "
                   f"{p.dim}(recorded run){p.reset}")
        for step in steps:
            mark = f"  {p.dim}[compensated]{p.reset}" if step.get("compensated") else ""
            label = step.get("label") or (
                f"{step.get('key')}.{step.get('method')}"
                if step.get("key") else step.get("kind"))
            out.append(f"    step {step.get('step')}  "
                       f"{step.get('component')}: {step.get('kind')} "
                       f"{label}{mark}")
    return out


def _render_decisions(decisions: dict, p: _Palette) -> list:
    pending = decisions.get("pending", 0)
    hue = p.red if pending else p.green
    out = [f"{p.bold}PENDING DECISIONS{p.reset}  "
           f"{hue}{pending} awaiting a human{p.reset}  "
           f"{p.dim}(the evidence IS the approval surface){p.reset}"]

    widening = decisions.get("widening") or []
    if widening:
        out.append(f"\n  {p.bold}boundary widening{p.reset} "
                   f"{p.dim}(item 21 — ack to admit){p.reset}")
        for row in widening:
            ev = row["evidence"]
            if row["acknowledged"]:
                out.append(f"    {p.green}~ {row['token']} (acknowledged){p.reset}")
            else:
                out.append(f"    {p.red}+ {row['token']}{p.reset}")
            out.append(f"        {p.dim}{ev['detail']}{p.reset}")

    exceptions = decisions.get("policy") or []
    if exceptions:
        out.append(f"\n  {p.bold}policy exceptions{p.reset} "
                   f"{p.dim}(item 33 — admission refused, awaiting a call){p.reset}")
        for row in exceptions:
            out.append(f"    {p.red}! {row['component']} reaches "
                       f"`{row['token']}` [{row['violation']}]{p.reset}")
            out.append(f"        {p.dim}{row['message']}{p.reset}")
            trace_text = (row.get("evidence") or {}).get("trace")
            if trace_text:
                for line in trace_text.splitlines():
                    out.append(f"        {p.dim}{line}{p.reset}")

    if not widening and not exceptions:
        out.append(f"  {p.green}nothing pending — no widening to ack, "
                   f"no policy exception to rule on.{p.reset}")
    return out


# ---------------------------------------------------------------- dashboard


class Dashboard:
    """A bound view: it holds its read-only sources and rebuilds the model on
    demand. `snapshot()` returns a fresh model; `render()` the text of one. A
    `--watch` loop just calls `render()` on an interval — the sources are read
    the same way each time, and nothing is written back.

    The session, when given, is only ever *read*: `ir` (a property) and
    `live_state()` (which inspects fiber states). No mutator is called, so a
    dash can never move the running system."""

    def __init__(self, ir: dict, *, session=None, live_state: dict | None = None,
                 trace=None, timeline=None, prev_audit: dict | None = None,
                 accepted=None, accept_all: bool = False,
                 policy=None, mcp_scope=None) -> None:
        self._ir = ir
        self._session = session
        self._live_state = live_state
        self._trace = trace
        self._timeline = timeline
        self._prev_audit = prev_audit
        self._accepted = accepted
        self._accept_all = accept_all
        self._policy = policy
        self._mcp_scope = mcp_scope

    @classmethod
    def from_session(cls, session, **kwargs) -> "Dashboard":
        """Attach to a live `mcp.session.Session`, read-only. Reads the session's
        current IR and live state now; a later `snapshot()` re-reads the live
        state so the view tracks the running composition."""
        return cls(session.ir, session=session, **kwargs)

    def _current_live_state(self) -> dict | None:
        if self._session is not None:
            # read-only: live_state() inspects fiber states, mutates nothing
            return self._session.live_state()
        return self._live_state

    def _current_ir(self) -> dict:
        if self._session is not None:
            return self._session.ir or {}
        return self._ir or {}

    def snapshot(self) -> dict:
        """A fresh read-only model from the current state of the sources."""
        return build_model(
            self._current_ir(),
            live_state=self._current_live_state(),
            trace=self._trace, timeline=self._timeline,
            prev_audit=self._prev_audit, accepted=self._accepted,
            accept_all=self._accept_all, policy=self._policy,
            mcp_scope=self._mcp_scope)

    def render(self, *, color: bool = False) -> str:
        return render_model(self.snapshot(), color=color)
