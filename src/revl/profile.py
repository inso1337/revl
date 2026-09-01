"""Capability/emission profiling — declared surface vs runtime usage.

`revl profile <composition> <run.jsonl>` compares what a component's code *may*
emit (the static emission surface, from the IR) against what a recorded run
*did* emit (the `emit` events in a schema-v2 trace), and flags
**over-declaration**: a declared emission the run never exercised. That is the
least-privilege companion to `revl audit` — audit says which boundaries a
component may reach, `revl metrics` counts which it did, and this pairs the two
per component so the *unused* reach is named, not just totalled.

The motivating shape is authority hygiene: a component whose code can emit
through both `bus` and `db`, but whose run only ever crossed `db`, is
over-privileged on `bus` for that run — the declaration is wider than the
behaviour. Narrowing the surface to what was used is a real attack-surface
reduction the compiler can then re-prove.

### Where the two sides come from

* **Declared (static).** The same G8 boundary walk `revl audit` runs
  (`__main__._boundary`, which itself reads `lower._emitting_capabilities` and
  each service method's `capabilities`): per component it yields the emission
  **labels** it can cross (``key.method``) and, per label, the capability
  **scopes** the called operation declares. We reuse it read-only. A composition
  handed to us as an ``audit --json`` document already carries that walk as its
  ``boundary`` key; a full compiled IR (or ``.rvl`` source) does not, so we run
  the walk on it. Either input form works, exactly like `revl diff`.

* **Used (runtime).** The v2 `emit` event (`revl.why_runtime`, docs/why-runtime.md)
  records one crossing as ``{event:"emit", component, capability, key}`` — the
  `key` is the same ``key.method`` label the static walk uses, and its first
  segment is the **required key** the crossing went through (docs/capabilities.md:
  "called through required key ``db`` -> capability ``db``"). We aggregate the
  used labels (and their required keys) per component straight from the trace.

Both sides therefore speak the *same* label vocabulary, so the set difference is
apples-to-apples: no field the trace lacks is needed (v2's `emit` carries
everything), and nothing is fabricated when it is absent — a trace with no
`emit` events simply used nothing, so every declared emission reads as
over-declared, which is the honest answer for a run that crossed no boundary.

### The two grains

Over-declaration is reported at two grains, both a plain declared-minus-used set:

* **keys** — the emission **label** (``bus.publish``): the finest unit, exactly
  what both the static walk and the trace name.
* **capabilities** — the **required key** the label goes through
  (``bus.publish`` -> ``bus``): the least-privilege unit, the boundary you would
  revoke. This is the component's *local* capability, per docs/capabilities.md
  ("calling ``emission[db] fn put`` through key ``cache`` contributes ``cache``")
  — derived identically on both sides as the label's first segment, so it never
  disagrees with itself.

**Under-declaration** (used − declared) is the inverse: an emission the trace
records that the static surface does not name. The checker forbids emitting
through an undeclared boundary, so a non-empty under-declared set is *not* a
normal profile — it almost always means the composition and the trace are not
the same system (a mismatched pair). It is surfaced as a warning, never merged
into the over-declaration count, and never silently dropped.

This module is pure: `compute_profile(ir, events)` is the whole computation over
an IR document (or audit doc) plus the `why_runtime` event list; `render` is the
human table; the loaders read the two files. The CLI defaults to exit 0 — a
profile describes a run, it is not a gate — with an opt-in `--strict` that exits
nonzero when anything is over-declared.
"""

from __future__ import annotations


from .why_runtime import EMIT, read_trace

# the required key a bare/unscoped emission (`*`) or an unparseable label maps
# to — kept explicit so it can never collide with a real key name
_UNKNOWN_KEY = "*"


# --------------------------------------------------------------------------
# declared side (static): reuse the audit boundary walk, read-only
# --------------------------------------------------------------------------


def _boundary_of(ir: dict) -> dict:
    """The per-component G8 emission surface for `ir`, as
    ``{component: {"emissions": [label...], "capabilities": {label: [scope...]}}}``.

    Accepts both composition input forms, mirroring `revl diff`:

    * an ``audit --json`` document already carries the walk under ``boundary`` —
      use it directly (a degraded doc with no `components`/`services` cannot be
      re-walked, and does not need to be);
    * a full compiled IR (or the result of compiling ``.rvl`` source) carries
      `components`/`services` but no `boundary`, so run the same walk `revl
      audit` runs — imported lazily to avoid a construction-time import cycle
      (`__main__` imports this module when dispatching the subcommand).
    """
    boundary = ir.get("boundary")
    if isinstance(boundary, dict):
        return boundary
    if ir.get("components") is not None:
        from .__main__ import _boundary  # noqa: PLC0415 — lazy: breaks the cycle

        return _boundary(ir)
    raise ValueError(
        "composition carries neither a `boundary` (an `audit --json` document) "
        "nor `components` (a compiled IR or `.rvl` source) — cannot determine "
        "the declared emission surface")


def _required_key(label: str) -> str:
    """The required key a ``key.method`` emission label crosses — its first
    segment (docs/capabilities.md). A label with no ``.`` names no key we can
    attribute, so it buckets under `*` rather than being invented."""
    head = label.split(".", 1)[0]
    return head or _UNKNOWN_KEY


def _requires_index(ir: dict) -> dict[str, dict[str, str]]:
    """``{component: {required-key: service}}`` from a full IR, for the
    descriptive service annotation. Empty when the input is an audit document
    (which does not carry the key->service wiring) — the service is optional
    context, never part of the set difference, so its absence degrades cleanly."""
    index: dict[str, dict[str, str]] = {}
    for comp in ir.get("components") or []:
        index[comp["name"]] = dict(comp.get("requires") or {})
    return index


# --------------------------------------------------------------------------
# used side (runtime): aggregate emit crossings from the trace
# --------------------------------------------------------------------------


def _used_emissions(events: list[dict]) -> dict[str, set[str]]:
    """``{component: {emission label...}}`` actually crossed, from the v2 `emit`
    events. A trace with no `emit` events yields an empty map — nothing was
    used, which is a valid (if maximally over-declared) profile, not an error."""
    used: dict[str, set[str]] = {}
    for event in events:
        if event.get("event") != EMIT:
            continue
        component = event.get("component")
        key = event.get("key")
        if component is None or key is None:
            # a malformed emit names neither what emitted nor what it crossed;
            # it cannot be attributed to a declared surface, so skip it rather
            # than fabricate a component or label for it
            continue
        used.setdefault(component, set()).add(key)
    return used


# --------------------------------------------------------------------------
# the profile: declared vs used, per component
# --------------------------------------------------------------------------


def compute_profile(ir: dict, events: list[dict]) -> dict:
    """Compare the declared emission surface of `ir` against the emissions the
    `events` trace records, per component. Pure and unit-testable: `ir` is a
    composition document (a full compiled IR, or an ``audit --json`` doc with a
    `boundary`) and `events` is the `why_runtime` event list.

    Returns the document `revl profile --json` prints and `render` formats.
    """
    boundary = _boundary_of(ir)
    requires = _requires_index(ir)
    used = _used_emissions(events)

    # every component that has a declared emission surface, plus any that emitted
    # in the trace (an emitter absent from the surface is an anomaly we must not
    # drop — see `unknownComponents` below)
    declared_components = {
        name: entry for name, entry in boundary.items()
        if entry.get("emissions")
    }
    names = sorted(set(declared_components) | set(used))

    components: dict[str, dict] = {}
    unknown_components: list[str] = []
    for name in names:
        entry = declared_components.get(name) or {}
        declared_keys = set(entry.get("emissions") or [])
        used_keys = set(used.get(name) or set())

        if not declared_keys and used_keys:
            # emitted at runtime but has no declared emission surface at all —
            # the composition and the trace almost certainly disagree
            unknown_components.append(name)

        declared_caps = {_required_key(k) for k in declared_keys}
        used_caps = {_required_key(k) for k in used_keys}

        over_keys = sorted(declared_keys - used_keys)
        over_caps = sorted(declared_caps - used_caps)
        under_keys = sorted(used_keys - declared_keys)
        under_caps = sorted(used_caps - declared_caps)

        req = requires.get(name) or {}
        # descriptive context (never diffed): the declared downstream scopes per
        # label, and the service each required key resolves to when the input
        # carries the wiring
        scopes = {
            label: list(caps)
            for label, caps in (entry.get("capabilities") or {}).items()
        }
        services = {
            cap: req[cap] for cap in sorted(declared_caps | used_caps)
            if cap in req
        }

        components[name] = {
            "declared": {
                "keys": sorted(declared_keys),
                "capabilities": sorted(declared_caps),
            },
            "used": {
                "keys": sorted(used_keys),
                "capabilities": sorted(used_caps),
            },
            "overDeclared": {"keys": over_keys, "capabilities": over_caps},
            "underDeclared": {"keys": under_keys, "capabilities": under_caps},
            "scopes": scopes,
            "services": services,
        }

    over_key_total = sum(len(c["overDeclared"]["keys"]) for c in components.values())
    over_cap_total = sum(
        len(c["overDeclared"]["capabilities"]) for c in components.values())
    under_key_total = sum(
        len(c["underDeclared"]["keys"]) for c in components.values())
    over_components = sum(
        1 for c in components.values() if c["overDeclared"]["keys"])

    return {
        "components": components,
        "unknownComponents": sorted(unknown_components),
        "summary": {
            "components": len(components),
            "overDeclaredComponents": over_components,
            "overDeclaredKeys": over_key_total,
            "overDeclaredCapabilities": over_cap_total,
            "underDeclaredKeys": under_key_total,
            # clean = nothing over-declared AND no under-declaration/unknown
            # anomaly: the run exercised exactly the declared surface
            "clean": (over_key_total == 0 and under_key_total == 0
                      and not unknown_components),
        },
    }


# --------------------------------------------------------------------------
# repair patch: the least-authority emission a component should declare (item 307)
# --------------------------------------------------------------------------
#
# `revl profile` (item 124) NAMES the over-declared reach; the repair patch turns
# that into an actionable, least-authority SUGGESTION: the narrowed `emission[...]`
# an author should write so the component declares only the authority its run
# actually exercised. The suggestion is PROPOSED (printed, emitted as JSON) and
# NEVER silently applied. The author (or an agent harness) applies it and re-runs
# the admission gate, which is what turns an over-broad, agent-generated
# declaration into a tight one without the agent understanding the whole
# capability model.
#
# ### Why this is sound
#
# The declared side is a *sound over-approximation* of reach (the G8 static walk);
# the observed side is one run, an *under-approximation* of reach. Narrowing to
# the observed set is therefore a SUGGESTION to re-verify, not a proof, so the
# output says exactly that, and the gate re-run is the backstop (if the code can
# still reach a narrowed-away emission, admission rejects the patch and the author
# widens back). Within that honest framing the patch is conservative in three
# concrete ways, so it never proposes dropping a *real* reach:
#
# * **Never past `*`.** A `*` in the declared surface is an emission whose target
#   is unnameable: a first-class emitting callable, or an unknown dispatch (see
#   `_boundary`'s `*` widening). The trace can never *observe* a `*`, so a naive
#   narrow would always drop it. The suggestion keeps every `*` that is declared;
#   the least-authority set is `observed` plus the declared `*`, never less.
# * **Never on a mismatch.** A component with under-declaration (used not declared)
#   or one that emitted with no declared surface at all (`unknownComponents`) is a
#   composition/trace *disagreement*, not an over-declaration, so narrowing it
#   would narrow the wrong system. Such a component is passed through unnarrowed.
# * **Only when strictly over-declared.** A component whose run exercised exactly
#   its declared surface gets no patch: there is nothing to narrow.


def _emission_decl(tokens: list[str]) -> str:
    """The `emission[...]` declaration string for a suggested token set: the
    exact spelling an author would write. An empty set (a component that emitted
    nothing observable and declares no `*`) is a bare `emission` with no scope,
    the least authority a declaration can name."""
    inside = ", ".join(tokens)
    return f"emission[{inside}]" if inside else "emission"


def compute_repair_patch(profile: dict) -> dict:
    """Turn a `compute_profile` document into a proposed least-authority patch:
    per over-declaring component, the narrowed emission surface it should declare.

    Pure over the profile document (so it is unit-testable without a runtime and
    re-uses item 124's declared-vs-observed computation verbatim, never
    recomputing either side). Returns the document `revl profile --patch [--json]`
    prints. The patch is a SUGGESTION: `applied` is always `false`, and every
    entry carries the `emission[...]` string plus the machine-readable token sets
    an agent harness consumes.
    """
    components = profile.get("components") or {}
    unknown = set(profile.get("unknownComponents") or [])

    patch: dict[str, dict] = {}
    caps_removed_total = 0
    keys_removed_total = 0
    minimizable = 0

    for name in sorted(components):
        entry = components[name]
        declared_keys = list(entry["declared"]["keys"])
        declared_caps = list(entry["declared"]["capabilities"])
        observed_keys = set(entry["used"]["keys"])
        observed_caps = set(entry["used"]["capabilities"])
        over_keys = entry["overDeclared"]["keys"]
        under_keys = entry["underDeclared"]["keys"]

        # a `*` anywhere in the declared surface is an unnameable reach the trace
        # cannot observe; it must survive every narrowing (never past `*`)
        wildcard_keys = [k for k in declared_keys if _required_key(k) == _UNKNOWN_KEY]
        wildcard_caps = [c for c in declared_caps if c == _UNKNOWN_KEY]
        keeps_wildcard = bool(wildcard_keys or wildcard_caps)

        # a composition/trace disagreement is not an over-declaration; do not
        # narrow the wrong system
        mismatch = name in unknown or bool(under_keys)

        if mismatch or not over_keys:
            # nothing to soundly propose: pass the declared surface through
            # unchanged so a consumer sees "no narrowing" explicitly
            reason = (
                "composition and trace disagree (under-declared or unknown "
                "emitter); not narrowed" if mismatch else
                "run exercised exactly the declared surface; nothing to narrow")
            patch[name] = {
                "minimizable": False,
                "reason": reason,
                "declared": {"keys": declared_keys, "capabilities": declared_caps},
                "suggested": {
                    "keys": declared_keys, "capabilities": declared_caps},
                "removed": {"keys": [], "capabilities": []},
                "emission": _emission_decl(declared_keys),
                "keepsWildcard": keeps_wildcard,
            }
            continue

        # the least-authority set: what the run observed, plus every declared `*`
        # (unnameable reach the trace cannot see), never less than that
        suggested_keys = sorted(observed_keys | set(wildcard_keys))
        suggested_caps = sorted(observed_caps | set(wildcard_caps))
        removed_keys = sorted(set(declared_keys) - set(suggested_keys))
        removed_caps = sorted(set(declared_caps) - set(suggested_caps))

        keys_removed_total += len(removed_keys)
        caps_removed_total += len(removed_caps)
        minimizable += 1

        note = None
        if keeps_wildcard:
            note = ("kept `*` (a first-class or unknown-dispatch emission the "
                    "trace cannot observe); narrowing never drops it")

        patch[name] = {
            "minimizable": True,
            "declared": {"keys": declared_keys, "capabilities": declared_caps},
            "observed": {"keys": sorted(observed_keys),
                         "capabilities": sorted(observed_caps)},
            "suggested": {"keys": suggested_keys, "capabilities": suggested_caps},
            "removed": {"keys": removed_keys, "capabilities": removed_caps},
            "emission": _emission_decl(suggested_keys),
            "keepsWildcard": keeps_wildcard,
            **({"note": note} if note else {}),
        }

    return {
        "patch": patch,
        "summary": {
            "components": len(patch),
            "minimizable": minimizable,
            "keysRemoved": keys_removed_total,
            "capabilitiesRemoved": caps_removed_total,
        },
        # applied is ALWAYS false: the patch is proposed, never written. The
        # author/agent applies it and re-runs the gate; observation is one run
        # (an under-approximation of reach), so the gate re-run is the backstop.
        "applied": False,
        "advisory": (
            "PROPOSED least-authority patch: printed, never applied. Each "
            "`emission[...]` narrows a component to the authority its run "
            "exercised (plus any `*` the trace cannot observe). It is one run, an "
            "under-approximation of reach: apply it, then re-run `revl admit` or "
            "`revl profile --strict` to confirm the narrowed component still "
            "admits. If admission rejects, the emission was reachable, so widen "
            "back."),
    }


def repair_patch_from_files(composition: str, trace: str) -> dict:
    """Load a composition and a trace and compute the proposed repair patch."""
    return compute_repair_patch(profile_from_files(composition, trace))


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------


def load_composition(path: str) -> dict:
    """Load the composition side (declarations). Auto-detects the input form the
    way `revl diff` / `revl metrics` do: a ``.rvl`` source is compiled; an IR /
    interchange / ``audit --json`` JSON document is taken as-is. Delegates to the
    shared `composition_diff.load_composition` so the detection can never drift."""
    from .composition_diff import load_composition as _load  # noqa: PLC0415

    return _load(path)


def profile_from_files(composition: str, trace: str) -> dict:
    """Load a composition and a trace from disk and compute their profile."""
    ir = load_composition(composition)
    events = read_trace(trace)
    return compute_profile(ir, events)


# --------------------------------------------------------------------------
# human rendering
# --------------------------------------------------------------------------


def _fmt_set(values: list[str]) -> str:
    return ", ".join(values) if values else "-"


def render(profile: dict) -> str:
    """A compact human table: per component, its declared vs used emission
    surface and the over-declared remainder. `--json` prints the full document
    `compute_profile` returns."""
    summary = profile.get("summary") or {}
    components = profile.get("components") or {}

    lines = [
        f"emission profile over {summary.get('components', 0)} component(s) "
        f"with a declared emission surface"
    ]

    if not components:
        lines.append("  (no component declares an emission — nothing to profile)")
        return "\n".join(lines)

    for name in sorted(components):
        entry = components[name]
        declared = entry["declared"]
        used = entry["used"]
        over = entry["overDeclared"]
        under = entry["underDeclared"]

        lines.append("")
        lines.append(f"component {name}")
        lines.append(f"  declared : {_fmt_set(declared['keys'])}")
        lines.append(f"  used     : {_fmt_set(used['keys'])}")
        if over["keys"]:
            caps = _fmt_set(over["capabilities"])
            lines.append(f"  OVER-DECLARED (never used): {_fmt_set(over['keys'])} "
                         f"[capabilities: {caps}]")
        else:
            lines.append("  over-declared: none — every declared emission was used")
        if under["keys"]:
            lines.append(f"  WARNING under-declared (used, not declared): "
                         f"{_fmt_set(under['keys'])}")

    unknown = profile.get("unknownComponents") or []
    if unknown:
        lines.append("")
        lines.append("WARNING: these components emitted in the trace but declare "
                     "no emission surface — the composition and trace may not be "
                     "the same system:")
        lines.append(f"  {_fmt_set(unknown)}")

    lines.append("")
    if summary.get("clean"):
        lines.append("summary: clean — the run exercised exactly the declared "
                     "emission surface (no over-declaration)")
    else:
        lines.append(
            f"summary: {summary.get('overDeclaredKeys', 0)} over-declared "
            f"emission(s) across {summary.get('overDeclaredComponents', 0)} "
            f"component(s); "
            f"{summary.get('overDeclaredCapabilities', 0)} over-declared "
            f"capability(ies)")
    return "\n".join(lines)


def render_patch(patch: dict) -> str:
    """The human view of a proposed repair patch: per over-declaring component,
    the narrowed `emission[...]` to write and the authority it drops. Machine
    consumers take `--json` instead."""
    entries = patch.get("patch") or {}
    summary = patch.get("summary") or {}
    minimizable = summary.get("minimizable", 0)

    lines = ["proposed minimal-capability repair patch (least authority)"]
    lines.append("  PROPOSED ONLY: nothing is written; apply, then re-run the "
                 "gate to confirm the narrowed component still admits")

    proposed = [n for n, e in sorted(entries.items()) if e.get("minimizable")]
    if not proposed:
        lines.append("")
        lines.append("  no component over-declares; nothing to narrow")
        return "\n".join(lines)

    for name in proposed:
        entry = entries[name]
        lines.append("")
        lines.append(f"component {name}")
        lines.append(f"  declared  : {_fmt_set(entry['declared']['keys'])}")
        lines.append(f"  observed  : {_fmt_set(entry['observed']['keys'])}")
        lines.append(f"  SUGGEST   : {entry['emission']}")
        lines.append(f"  drops     : {_fmt_set(entry['removed']['keys'])} "
                     f"[capabilities: {_fmt_set(entry['removed']['capabilities'])}]")
        if entry.get("note"):
            lines.append(f"  note      : {entry['note']}")

    lines.append("")
    lines.append(
        f"summary: {minimizable} component(s) narrowable; "
        f"{summary.get('keysRemoved', 0)} emission(s) / "
        f"{summary.get('capabilitiesRemoved', 0)} capability(ies) would be dropped")
    lines.append(patch.get("advisory", ""))
    return "\n".join(lines)
