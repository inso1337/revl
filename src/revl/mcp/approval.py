"""The auto-approve-unless-irreversible policy core (roadmap item 246, Slice 1).

The policy has no vocabulary for naming actions. Its only input is the CHECKED
effect class of a call, and its output is one of three postures: proceed
silently (class a, witnessed-revertible), proceed and enumerate at commit
(class b, deferred emission), or stop for a human (class c, an immediate
emission with no checked inverse). Derivation is the whole product — the moment
the policy could say "rm is fine" it would be an allowlist with extra steps
(docs/design/246-auto-approve.md, "The one thing to get right").

This module builds the per-generation **class map** the decision reads. The map
is derived from the CHECKED effect facts — the `query.Composition` per-scope
facts and the `_emitting_capabilities` fixed point over them, the same surface
`_crossings` aggregates and `revl audit` prints — never the advisory
`schema._method_effects` walk, which misses the `*` first-class-value widening a
callback-smuggled emission needs (Decision 2, Fix 10). Two things the review
sharpened live here:

  * **the map classifies ACTIVATION reach as well as provided operations**
    (Fix 1): an emission moved into a component's activation body must not dodge
    the per-call prompt, so `load`/`swap` answer for their own crossings;
  * **a call's class is the worst class over its whole reach CLOSURE** (Fix 3):
    the target's provider plus every provider it transitively reaches across the
    service seam, so a swap of a transitively-reached provider invalidates a
    standing approval for an untouched caller.

The ticket two-step reuses 245's hash-bound consent shape: the candidate hash is
over the reach-closure semantic entries, and the outstanding-ticket table
(keyed by the ticket hash) refuses a hash the server never issued.
"""

from __future__ import annotations

import hashlib
import json

from ..query import Composition, SHARED_REALM
from ..emission_analysis import _calls_in

# worst-class ordering: (c) > (b) > (a) > none. One prompt covers the whole call
# or none of it — the same all-or-nothing rule admission and the operator gate
# already use (Decision 1).
_ORDER = {None: 0, "a": 1, "b": 2, "c": 3}


def worse(x: str | None, y: str | None) -> str | None:
    """The worse (more-irreversible) of two action classes."""
    return x if _ORDER[x] >= _ORDER[y] else y


def _semantic(entry: dict) -> dict:
    """A component IR entry without provenance — the semantic content a swap
    actually touches (the same equality `operator._semantic` and
    `operator._changed_targets` use). Two compiles of the same component compare
    equal, so the reach-closure hash is stable across an edit elsewhere."""
    return {k: v for k, v in entry.items() if k != "source"}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _args_digest(args) -> str:
    return _sha(_canon(list(args or [])))


class ApprovalRequired(Exception):
    """A class-(c) crossing that no standing approval covers. Carries the ticket
    the two-step relays to the human. Raised from the single chokepoint
    (`Session.call`) and from the activation gate (`load`/`swap`) so every
    internal re-invocation — `replay_forward` included — passes through it."""

    def __init__(self, ticket: dict) -> None:
        super().__init__("approval required for a class-(c) crossing")
        self.ticket = ticket


class ClassMap:
    """The per-generation call classifier (Decision 2). Built at load/swap over
    EVERY scope of every component — each provide-method AND each activation
    body — from the checked reach facts. The per-call decision is then a
    dictionary lookup; nothing is compiled or re-derived on the hot path."""

    def __init__(self, ir: dict) -> None:
        self.ir = ir
        self.index = Composition(ir)
        self._semantic = {c["name"]: _semantic(c)
                          for c in ir.get("components") or []}
        self._emitting = self.index.emitting_fns
        # direct (non-transitive) classification per scope
        self._direct: dict[str, dict] = {}
        for sid, scope in self.index.scopes.items():
            self._direct[sid] = self._classify_direct(scope)
        # the reach-closure fold: worst class over the scope AND every scope it
        # reaches across the service seam (Decision 1's worst-class-over-reach).
        self._reach: dict[str, dict] = {}
        for sid in self.index.scopes:
            self._reach[sid] = self._fold_closure(sid)

    # -- per-scope direct classification -----------------------------------

    def _classify_direct(self, scope: dict) -> dict:
        facts = scope["facts"]
        cls: str | None = None
        crossings: list[dict] = []
        caps: set[str] = set()
        comp = scope["component"]

        # direct service-op emissions fire AT the call: class (c). Deferral is an
        # extern-declaration property, not spellable on a service method, so a
        # service emission is class (c) regardless of `compensate` (247), exactly
        # as `erase_report._crossings` tags it.
        for fact in facts["emissions"]:
            cls = worse(cls, "c")
            caps.add(fact["key"])
            crossings.append({
                "kind": "emission", "component": comp, "scope": scope["kind"],
                "key": fact["key"], "method": fact["method"], "actionClass": "c",
                "compensated": bool(fact.get("compensated"))})

        # reached host externs, off the checked classification.
        for fact in facts["externs"]:
            klass = fact.get("class")
            name = fact["name"]
            if klass == "emission":
                c = "b" if fact.get("deferred") else "c"
                cls = worse(cls, c)
                caps.add(name)
                crossings.append({
                    "kind": "extern", "component": comp, "scope": scope["kind"],
                    "name": name, "class": klass, "actionClass": c})
            elif klass == "witnessed":
                # a witnessed extern crosses the boundary but is revertible by a
                # registered inverse (243): class (a), auto-approved silently.
                cls = worse(cls, "a")
                for cap in (self.index.externs.get(name) or {}).get(
                        "capabilities") or [name]:
                    caps.add(cap)
                crossings.append({
                    "kind": "extern", "component": comp, "scope": scope["kind"],
                    "name": name, "class": klass, "actionClass": "a"})
            # pure / acquire externs are not boundary crossings.

        # the `*` first-class-value widening (Fix 10): an emitting callable
        # referenced in VALUE position escapes this scope and may be dispatched
        # by whoever receives it. `*` is the unnameable capability no
        # `emission[...]` list can name, so it can never be proven reversible —
        # class (c). This is exactly the reach the advisory `_method_effects`
        # walk misses and the `_emitting_capabilities` fixed point catches.
        if self._value_widens(scope["nodes"]):
            cls = worse(cls, "c")
            caps.add("*")
            crossings.append({
                "kind": "widening", "component": comp, "scope": scope["kind"],
                "capability": "*", "actionClass": "c",
                "note": "an emitting callable is handed on as a value; the "
                        "boundary it may reach cannot be named (`*`), so it "
                        "cannot be proven reversible"})

        return {"class": cls, "crossings": crossings, "capabilities": caps}

    def _value_widens(self, nodes) -> bool:
        """Whether this scope references an emitting callable in value position
        (an arrow-typed argument, a stored binding) — the `*` widening the G4
        fixed point applies (`emission_analysis._emitting_capabilities`)."""
        found: set = set()
        values: set = set()
        _calls_in(nodes, found, values=values)
        return bool(values & self._emitting)

    # -- reach-closure fold -------------------------------------------------

    def _fold_closure(self, sid: str) -> dict:
        direct = self._direct[sid]
        cls = direct["class"]
        crossings = list(direct["crossings"])
        caps = set(direct["capabilities"])
        comps = {self.index.scopes[sid]["component"]}
        for reached in self.index.closure(sid):
            rsid = reached["scope"]
            rdirect = self._direct[rsid]
            cls = worse(cls, rdirect["class"])
            crossings += rdirect["crossings"]
            caps |= rdirect["capabilities"]
            comps.add(self.index.scopes[rsid]["component"])
        return {"class": cls, "crossings": crossings, "capabilities": caps,
                "closureComponents": comps}

    # -- lookups ------------------------------------------------------------

    def _provider_of(self, key: str) -> str | None:
        provider = self.index.provider_of.get((key, SHARED_REALM))
        if provider is not None:
            return provider
        for (k, _realm), p in self.index.provider_of.items():
            if k == key:
                return p
        return None

    def classify_call(self, key: str, method: str) -> dict | None:
        """The reach of a `revl_call(key, method)`, or None when the key/method
        resolves to no scope (the handler will report that; the gate defers)."""
        provider = self._provider_of(key)
        if provider is None:
            return None
        sid = f"{provider}:{key}.{method}"
        reach = self._reach.get(sid)
        if reach is None:
            return None
        return {**reach, "component": provider, "key": key, "method": method}

    def activation_reaches(self) -> list[dict]:
        """Every component's activation-body reach, in load order. The activation
        gate reads this so a candidate whose activation body reaches a class-(c)
        emission answers for it before boot (Fix 1)."""
        out = []
        order = self.index.load_order or list(self.index.entries)
        for name in order:
            sid = f"{name}:activation"
            reach = self._reach.get(sid)
            if reach is None:
                continue
            out.append({**reach, "component": name, "key": None, "method": None})
        return out

    # -- the reach-closure candidate hash (Fix 3) ---------------------------

    def candidate_hash(self, closure_components) -> str:
        """A sha256 over the canonical JSON of the semantic entries of the
        call's REACH CLOSURE — the target's provider plus the providers of every
        service on its checked reach path. A swap of ANY of those (changed
        behaviour, same names) recomputes a different hash, so every standing
        token whose closure includes the changed provider fails the check,
        including one held by an untouched caller (invariant 4)."""
        entries = [self._semantic[c] for c in sorted(closure_components)
                   if c in self._semantic]
        return _sha(_canon(entries))

    # -- the ticket ---------------------------------------------------------

    def build_ticket(self, reach: dict, args=None) -> dict:
        """The class-(c) ticket: what a yes would mean. Names the component, key,
        method, an args digest, the capabilities reached, the crossing list, the
        reach-closure candidate hash, and `hash` — a sha256 over all of it, the
        outstanding-ticket table's key and the ledger binding."""
        closure = reach["closureComponents"]
        chash = self.candidate_hash(closure)
        crossings = sorted(reach["crossings"],
                           key=lambda c: _canon(c))
        activation = reach.get("method") is None and reach.get("key") is None
        body = {
            "component": reach["component"],
            "key": reach.get("key"),
            "method": reach.get("method"),
            "kind": "activation" if activation else "call",
            "argsDigest": _args_digest(args),
            "capabilities": sorted(reach["capabilities"]),
            "crossings": crossings,
            "closureComponents": sorted(closure),
            "candidateHash": chash,
        }
        body["hash"] = _sha(_canon(body))
        return body
