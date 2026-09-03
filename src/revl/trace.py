"""`revl trace <run.jsonl>` — the causal trace with the model hop as a
first-class entry (roadmap item 121, docs/design/121-revl-trace.md).

Where `revl why` walks one component's cause chain and `revl metrics` rolls a
whole run into aggregate numbers, `revl trace` reads the run as a chain of
*hops* and surfaces the content of the one emission whose response the whole
system then executes: the **model completion**. For each model hop it reports
which model answered, the host-reported tokens and cost, the revl-measured
latency bracket, how many validation retries it burned against the item-257
static ``N + 1`` ceiling, which emission the answer produced (when a fiber-local
value-flow token proves it), and which G-rule verified the value.

This module is pure — it imports no runtime and reads only the JSONL trace
vocabulary from :mod:`revl.why_runtime`. It widens nothing: the ``llm`` payload
and ``activationId`` are written at record time (the item-257 ``validate_retry``
seam, ``backends/python/runtime.py``, and the driver's crossing arm, which
back-patches ``producedSeq``); this reader only projects them.

Two safety invariants are visible in the output shape, and are the whole point
of §4 of the design:

* Every host-reported field (``model``/``tokens``/``cost``) is marked
  ``host-reported`` (unverifiable, §4 attack 2); the ONE number revl can stand
  behind — the attempt count against the static ceiling — is marked
  ``revl-controlled`` and is the one the oracle (§3.2) cross-checks.
* ``produced`` prints ONLY when the edge was PROVED: the emitter saw the later
  emission's arguments read the completion's binding, the fiber-local token said
  which execution of that completion site produced the value, and the driver
  resolved it inside one activation. Anything short of all three OMITS it, never
  adjacency-guesses it (§4 attack 3).
  There is no ``prompt`` or ``response`` TEXT field anywhere — capturing the text
  is the exfiltration channel (§4 attack 1); only a salted, content-free digest
  may appear.

The CLI (`revl trace`, `--json`, `--component`, `--model`, `--otel`) always
exits 0 — reporting a run's shape is never a gate.
"""

from __future__ import annotations

from .why_runtime import EMIT, read_trace

# the capability a model completion emission is scoped to. A crossing is a model
# hop when it carries an `llm` payload (written only for a model completion at
# the validate_retry seam); the capability is a secondary, human-facing label.
MODEL_CAPABILITY = "model"


def _is_model_hop(event: dict) -> bool:
    """A trace event is a model hop when it is an `emit` carrying an `llm`
    payload. Detection is by DATA (the payload's presence), never by the
    capability string alone — the payload is written only for a model completion
    crossing, so its presence is the exact discriminator."""
    return event.get("event") == EMIT and isinstance(event.get("llm"), dict)


def _hop_document(event: dict) -> dict:
    """Project one model-hop `emit` event into the machine-readable hop shape
    (§1.2). Every field carries its provenance; a field the record does not
    carry (a suppressed digest, an omitted `producedSeq`) is simply absent."""
    llm = event.get("llm") or {}
    hop: dict = {
        "seq": event.get("seq"),
        "component": event.get("component"),
        "capability": event.get("capability"),
        "key": event.get("key"),
        "model": {"id": llm.get("model"),
                  "provenance": llm.get("modelProvenance") or "host-reported"},
        "tokens": {"in": llm.get("tokensIn"), "out": llm.get("tokensOut"),
                   "provenance": llm.get("usageProvenance") or "host-reported"},
        "latency": {"seconds": llm.get("latencySeconds"),
                    "provenance": llm.get("latencyProvenance")
                    or "revl-measured-bracket"},
        "attempts": {"count": llm.get("attempts"),
                     "ceiling": llm.get("attemptCeiling"),
                     "provenance": llm.get("attemptsProvenance")
                     or "revl-controlled"},
        "verifiedBy": list(llm.get("verifiedBy") or []),
    }
    if event.get("activationId") is not None:
        hop["activationId"] = event.get("activationId")
    cost = llm.get("cost")
    if isinstance(cost, dict):
        hop["cost"] = {"amount": cost.get("amount"),
                       "currency": cost.get("currency"),
                       "provenance": cost.get("provenance") or "host-reported"}
    produced = llm.get("producedSeq")
    if produced:
        hop["produced"] = _produced_edges(event, produced)
    digest = llm.get("promptDigest")
    if isinstance(digest, dict):
        hop["promptDigest"] = digest
    return hop


def _produced_edges(event: dict, produced_seqs: list) -> list[dict]:
    """Resolve each `producedSeq` back to the emission it names, for the human
    view. The seq is the value-flow edge the fiber token proved; a resolution
    that finds no matching emission still surfaces the seq (the token is the
    authority, not the reader's index)."""
    index = event.get("_producedIndex") or {}
    edges: list[dict] = []
    for seq in produced_seqs:
        target = index.get(seq)
        if target is not None:
            edges.append({"seq": seq, "capability": target.get("capability"),
                          "key": target.get("key")})
        else:
            edges.append({"seq": seq})
    return edges


def _attach_produced_index(events: list[dict]) -> None:
    """Give each model hop a private view of the emissions it can name, so the
    human render can label a `producedSeq` with its target's capability/key. The
    index is keyed by seq and is stripped from the JSON document (`_`-prefixed).
    """
    by_seq = {e.get("seq"): e for e in events if e.get("event") == EMIT}
    for ev in events:
        if _is_model_hop(ev):
            produced = (ev.get("llm") or {}).get("producedSeq") or []
            ev["_producedIndex"] = {s: by_seq.get(s) for s in produced}


def compute_trace(events: list[dict]) -> dict:
    """The whole projection, pure over a list of trace events. Returns the
    machine-readable trace document (`revl trace --json`) — the events count and
    the model hops, sorted by `seq` so a diff of two runs isolates the varying
    numbers from the fixed structure (§ determinism note)."""
    _attach_produced_index(events)
    hops = [_hop_document(e) for e in events if _is_model_hop(e)]
    hops.sort(key=lambda h: (h.get("seq") if h.get("seq") is not None else -1))
    return {"events": len(events), "modelHops": hops}


def trace_from_file(path: str) -> dict:
    """Read a JSONL trace from `path` and project its model hops."""
    return compute_trace(read_trace(path))


def filter_document(doc: dict, *, component: str | None = None,
                    model_only: bool = False) -> dict:
    """Apply the `--component`/`--model` view filters to a computed document.
    `--model` is a no-op here (the document is already model hops only); it is
    kept so the surface reads honestly and a future non-model projection can slot
    in. `--component` narrows to one component's hops."""
    hops = doc.get("modelHops") or []
    if component is not None:
        hops = [h for h in hops if h.get("component") == component]
    result = dict(doc)
    result["modelHops"] = hops
    return result


# --------------------------------------------------------------------------
# the oracle: recorded attempts vs the static N+1 ceiling (§3.2)
# --------------------------------------------------------------------------


def attempt_ceiling_defects(doc: dict) -> list[dict]:
    """The one model-hop number revl can check against a compile-time proof
    (§3.2): item 257 pins the retry budget as a static ``N + 1`` ceiling, so a
    recorded attempt count that EXCEEDS its ceiling is a real defect in one of
    the two — the runtime over-retried, or the static count under-counts. This is
    the differential-oracle move `why_runtime.oracle` already makes for the
    withdrawal cascade, applied to the retry loop."""
    defects: list[dict] = []
    for hop in doc.get("modelHops") or []:
        attempts = hop.get("attempts") or {}
        count, ceiling = attempts.get("count"), attempts.get("ceiling")
        if count is None or ceiling is None:
            continue
        if count > ceiling:
            defects.append({
                "kind": "attempt-ceiling-exceeded",
                "seq": hop.get("seq"),
                "component": hop.get("component"),
                "detail": (f"hop #{hop.get('seq')} recorded {count} attempt(s) "
                           f"but the static item-257 ceiling is {ceiling}; a "
                           f"differential oracle proves this is a real defect "
                           f"(the runtime over-retried, or the static count "
                           f"under-counts), not noise."),
            })
    return defects


# --------------------------------------------------------------------------
# human rendering
# --------------------------------------------------------------------------


def _fmt_tokens(tokens: dict) -> str:
    tin, tout = tokens.get("in"), tokens.get("out")
    if tin is None and tout is None:
        return "(host did not report usage)"
    return f"{tin if tin is not None else '?'} in / {tout if tout is not None else '?'} out"


def _fmt_cost(cost: dict | None) -> str | None:
    if not cost or cost.get("amount") is None:
        return None
    currency = cost.get("currency") or ""
    return f"${cost.get('amount')} {currency}".rstrip()


def render(doc: dict) -> str:
    """The human view: the run as a causal chain of model hops (§1.1). Every
    field prints its provenance inline, because it is the whole safety story:
    host-reported numbers are marked unverified, and `produced` prints only when
    the fiber token matched."""
    hops = doc.get("modelHops") or []
    lines = [f"trace: {len(hops)} model hop(s) over "
             f"{doc.get('events', 0)} trace event(s)"]
    if not hops:
        lines.append("")
        lines.append("  (no model hops recorded in this trace)")
        return "\n".join(lines)

    for hop in hops:
        lines.append("")
        lines.append(f"  #{hop.get('seq')}  {hop.get('component')}  "
                     f"emit[{hop.get('capability')}] {hop.get('key')}")
        model = hop.get("model") or {}
        lines.append(f"        model     {model.get('id')}"
                     f"        (host-reported - unverified)")
        lines.append(f"        tokens    {_fmt_tokens(hop.get('tokens') or {})}"
                     f"        (host-reported - unverified)")
        cost = _fmt_cost(hop.get("cost"))
        if cost is not None:
            lines.append(f"        cost      {cost}"
                         f"        (host-reported - unverified)")
        latency = hop.get("latency") or {}
        if latency.get("seconds") is not None:
            lines.append(f"        latency   {latency.get('seconds')}s"
                         f"        (revl-measured bracket; host owns its inside)")
        attempts = hop.get("attempts") or {}
        if attempts.get("count") is not None:
            lines.append(f"        attempts  {attempts.get('count')} of "
                         f"<= {attempts.get('ceiling')}"
                         f"        (revl-controlled; within the 257 ceiling)")
        produced = hop.get("produced")
        if produced:
            targets = ", ".join(
                (f"emit[{e.get('capability')}] {e.get('key')} (#{e.get('seq')})"
                 if e.get("capability") is not None else f"#{e.get('seq')}")
                for e in produced)
            lines.append(f"        produced  {targets}   (fiber token matched)")
        verified = hop.get("verifiedBy") or []
        if verified:
            lines.append(f"        verified  {', '.join(verified)}")
        digest = hop.get("promptDigest")
        if isinstance(digest, dict):
            lines.append(f"        prompt    {digest.get('salted')}  "
                         f"[{digest.get('bytesBucket')}]  "
                         f"(salted digest - no text captured)")

    defects = attempt_ceiling_defects(doc)
    if defects:
        lines.append("")
        lines.append(f"  ORACLE: {len(defects)} attempt-ceiling defect(s):")
        for d in defects:
            lines.append(f"    [{d['kind']}] {d['detail']}")
    return "\n".join(lines)
