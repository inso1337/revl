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

from .. import cap_order
from ..policy import TAINT_FOLD_ORIGINS, component_realms
from ..query import Composition, SHARED_REALM
from ..taint import REDACTED_SECRET

# worst-class ordering: (c) > (b) > (a) > none. One prompt covers the whole call
# or none of it — the same all-or-nothing rule admission and the operator gate
# already use (Decision 1).
_ORDER = {None: 0, "a": 1, "b": 2, "c": 3}


def _cap_covers(wide: str, narrow: str) -> bool:
    """Whether the declared capability spelling `wide` COVERS `narrow` in the
    item-294 partial order (narrow at or below wide). The single point where the
    class map speaks the order; fails closed and additive (an identical spelling
    or an unparseable one reduces to string equality, so a parameter-free class
    map is bit-for-bit unchanged)."""
    if wide == narrow:
        return True
    try:
        return cap_order.covers(cap_order.parse_cap(wide),
                                cap_order.parse_cap(narrow))
    except cap_order.CapError:
        return False


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
        # the CLASS-(c) subset of `caps`: the capabilities this scope reaches via
        # a class-(c) crossing specifically, kept apart from the worst-class fold
        # `caps` (which also holds class-(a)/(b) capabilities). The standing-grant
        # gate reads THIS set, not `caps`, so a grant can only auto-approve the
        # class-(c) capabilities it actually covers and never a distinct un-granted
        # one the same call reaches (245/246-F1). Resolved here, where the extern
        # token resolution happens, because a crossing dict alone does not carry
        # its resolved capability (an `emission[token]` extern's cap is its token,
        # not its name).
        class_c: set[str] = set()
        comp = scope["component"]

        # direct service-op emissions fire AT the call: class (c). Deferral is an
        # extern-declaration property, not spellable on a service method, so a
        # service emission is class (c) regardless of `compensate` (247), exactly
        # as `erase_report._crossings` tags it.
        for fact in facts["emissions"]:
            cls = worse(cls, "c")
            caps.add(fact["key"])
            class_c.add(fact["key"])
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
                # item 343: a capability-scoped `emission[gateway.send]` is keyed
                # on its declared TOKEN, exactly as a witnessed extern is (below),
                # so a `capability C requires approval` rule and a 344 standing
                # grant target the crossing by token. A bare `emission` declares
                # no scope, so `capabilities` is absent and we fall back to the
                # extern name — the pre-343 behaviour, byte-for-byte.
                for cap in (self.index.externs.get(name) or {}).get(
                        "capabilities") or [name]:
                    caps.add(cap)
                    if c == "c":
                        class_c.add(cap)  # a deferred (b) emission needs no grant
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
        # `_crossings` in the erase report consults the SAME detection
        # (`Composition.value_widens`), so the two class folds can never disagree
        # about whether a scope widens (item 414).
        if self.index.value_widens(scope["nodes"]):
            cls = worse(cls, "c")
            caps.add("*")
            class_c.add("*")
            crossings.append({
                "kind": "widening", "component": comp, "scope": scope["kind"],
                "capability": "*", "actionClass": "c",
                "note": "an emitting callable is handed on as a value; the "
                        "boundary it may reach cannot be named (`*`), so it "
                        "cannot be proven reversible"})

        return {"class": cls, "crossings": crossings, "capabilities": caps,
                "classC": class_c}

    # -- reach-closure fold -------------------------------------------------

    def _fold_closure(self, sid: str) -> dict:
        direct = self._direct[sid]
        cls = direct["class"]
        crossings = list(direct["crossings"])
        caps = set(direct["capabilities"])
        class_c = set(direct["classC"])
        comps = {self.index.scopes[sid]["component"]}
        for reached in self.index.closure(sid):
            rsid = reached["scope"]
            rdirect = self._direct[rsid]
            cls = worse(cls, rdirect["class"])
            crossings += rdirect["crossings"]
            caps |= rdirect["capabilities"]
            class_c |= rdirect["classC"]
            comps.add(self.index.scopes[rsid]["component"])
        return {"class": cls, "crossings": crossings, "capabilities": caps,
                "classC": class_c, "closureComponents": comps}

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

    def crossings_for_capability(self, capability: str) -> list[dict]:
        """Every LIVE class-(c) crossing whose declared reach COVERS `capability`,
        deduplicated by (component, reach-closure candidate hash). The
        standing-grant path (roadmap item 344, fork b) reads this to mint a
        capability-scoped grant against the semantic identity of the crossing —
        not one ticket hash — and the operator verb gate reads it to scope the
        `approve` verb when a grant is minted proactively (no outstanding
        ticket). A capability reachable only via distinct closures yields one
        entry per closure, so a proactive mint over an ambiguous capability is
        visible to the caller as more than one target.

        Item 294 Slice 2: a declared crossing capability matches `capability` when
        it COVERS it in the one partial order (`capability` at or below a declared
        `(T, P)`), not by string equality. So a narrow mint spelling
        `fs.write(path="/tmp")` resolves against a crossing declared bare
        `fs.write` (or any wider `path=`) that covers it — mint-narrow works
        instead of being refused dead on arrival — while a mint WIDER than every
        declared crossing matches nothing and is refused (a grant may only narrow
        the declaration, never widen it). Bare-token capabilities match exactly as
        before (`covers` on empty valuations is string identity)."""
        seen: dict = {}
        for sid, reach in self._reach.items():
            if reach["class"] != "c":
                continue
            caps = reach["capabilities"]
            if not any(_cap_covers(declared, capability) for declared in caps):
                continue
            component = self.index.scopes[sid]["component"]
            chash = self.candidate_hash(reach["closureComponents"])
            key = (component, chash)
            if key not in seen:
                seen[key] = {"component": component, "candidateHash": chash,
                             "capabilities": sorted(caps)}
        return list(seen.values())

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

    # -- item 251 Slice 2: the resource / realm / taint projections ---------

    def _declaring_extern(self, token: str) -> dict | None:
        """The extern that declares a class-(c) capability `token`, so its
        `params` name the resource dimensions (host/path/table) the crossing
        binds. A capability-scoped emission (`emission[gateway.send]`) is declared
        by an extern whose `capabilities` list carries the token; a bare emission
        keys on the extern name itself. None for a service-op key (no extern), so
        it keys bare and a resource-scoped rule cannot cover it (fail-closed)."""
        ext = self.index.externs.get(token)
        if ext is not None:
            return ext
        for ext in self.index.externs.values():
            if token in (ext.get("capabilities") or []):
                return ext
        return None

    def bind_resource_scope(self, token: str, args) -> str | None:
        """Bind the RUNTIME resource args into the crossing capability `token`,
        turning bare `gateway.send` into `gateway.send(host="api.stripe.com")` at
        ticket time (design §6 N1). The registered-resource projection: for each
        of the declaring extern's parameters whose NAME is a `cap_order._REGISTRY`
        resource kind (host/path/table, never a ceiling), bind the positional arg
        at that parameter's index. Returns the canonical spelling, or None when the
        token exposes no resource param or no arg is bound (it then keys bare, byte
        for byte as before). Only the registered-resource projection is bound, NOT
        the whole argsDigest, so the ledger carries a STRUCTURED target.

        Item 416c: a resource dimension the author declared `Secret[T]` is bound
        to the REDACTED placeholder, never the runtime value. This spelling is
        the ticket body, and the ticket is what `record_approval_granted` writes
        to the durable WAL and what `replay_forward` echoes back into a later
        response — both externalization sinks item 256 Slice 3 already fences
        for an ordinary crossing argument. An UNDECLARED resource param (the
        common case — `host="api.stripe.com"` is not a secret) is bound exactly
        as before: only a param the program itself marked confidential is
        touched, so the N1 ledger and the distiller keep reading real targets."""
        ext = self._declaring_extern(token)
        if ext is None or not args:
            return None
        params = ext.get("params") or []
        pairs: list[tuple[str, object]] = []
        for index, param in enumerate(params):
            name = param.get("name")
            if name is None or not cap_order.is_registered(name) \
                    or cap_order.is_ceiling(name):
                continue
            if index >= len(args):
                continue
            value = args[index]
            if not isinstance(value, str):
                continue        # a resource value is a string literal; skip else
            if isinstance(param, dict) and param.get("secret"):
                value = REDACTED_SECRET
            pairs.append((name, value))
        if not pairs:
            return None
        try:
            return cap_order.make_cap(token, pairs).to_str()
        except cap_order.CapError:
            return None

    def component_realm(self, component: str) -> str:
        """The single item-33 realm the crossing component is isolated into, or
        the shared realm (`""`) as its own bucket (design §1.2). A component
        isolated into exactly one realm keys on it; the shared / multi-realm case
        keys shared, exactly as the distiller's single-string shape key expects."""
        manifest = (self.ir or {}).get("manifest") or {}
        realms = component_realms(manifest, component) - {SHARED_REALM}
        return next(iter(realms)) if len(realms) == 1 else SHARED_REALM

    def static_taint(self, component: str) -> frozenset[str]:
        """The static item-249 taint over-approximation for a crossing component:
        the post-endorsement origins (declassification already folded into
        `comp["taint"]["reaches"]`) that reach a sink, intersected with the five
        taint-fold origins (design §2.2). This is the honest recorded taint on a
        tier with the static audit and the admission floor on a tier without
        runtime value taint. Empty when the component touches no taint."""
        for comp in self.ir.get("components") or []:
            if comp.get("name") == component:
                reaches = (comp.get("taint") or {}).get("reaches") or []
                return frozenset(reaches) & TAINT_FOLD_ORIGINS
        return frozenset()

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
        # the class-(c) capability subset, added AFTER the hash so the ticket hash
        # (the outstanding-ticket key and ledger binding) is byte-identical to
        # before this field existed. `_find_standing_grant` reads it to require
        # EVERY class-(c) capability covered, never the worst-class `capabilities`
        # fold — the 245/246-F1 fix.
        #
        # item 251 Slice 2 (§6 N1): bind the runtime resource args into each
        # class-(c) capability, so `gateway.send` carries the destination it
        # crossed (`gateway.send(host="api.stripe.com")`). The bound spelling
        # replaces the bare token in `classCCapabilities` (so the resource-scope
        # `covers` check the auto-approve gate runs sees a STRUCTURED target, and a
        # host-scoped rule can never admit a send to another host), and
        # `resourceScopes` maps token -> spelling for the ledger's structured
        # target. A token with no resource param keys bare, byte for byte as
        # before, so a composition that crosses no registered resource is
        # unchanged. All three fields land AFTER the hash, additive.
        resource_scopes: dict[str, str] = {}
        bound_class_c: list[str] = []
        for token in sorted(reach.get("classC") or []):
            spelling = self.bind_resource_scope(token, args)
            if spelling is not None:
                resource_scopes[token] = spelling
                bound_class_c.append(spelling)
            else:
                bound_class_c.append(token)
        body["classCCapabilities"] = sorted(bound_class_c)
        if resource_scopes:
            body["resourceScopes"] = resource_scopes
        # item 251 Slice 2: the crossing's realm and its post-endorsement taint,
        # for the ledger's shape key (the distiller reads these; a recorded taint
        # set is KNOWN, closing the Slice-1 "taint-unknown" fail-close) and for the
        # auto-approve realm scope and taint-subset gate. Taint is recorded ONLY
        # when non-empty: a clean crossing OMITS `taintOrigins` so the distiller
        # reads it as known-clean, NOT as the empty-but-relevant set the H2 floor
        # deliberately treats as all five (an empty recorded set is reserved for
        # the enforcement-tier red flag, design §3.3).
        component = reach.get("component")
        if component is not None:
            body["realm"] = self.component_realm(component)
            taint = self.static_taint(component)
            if taint:
                body["taintOrigins"] = sorted(taint)
        return body
