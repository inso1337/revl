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
from ..query import Composition, SHARED_REALM, _walk
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


def _called_names(nodes) -> set:
    """Callable names a lowered tree references, both call encodings (the same
    walk `query._called_names` uses). Used to close the candidate hash over the
    host bodies a component actually reaches (item 427 F4)."""
    from ..lower import _calls_in  # noqa: PLC0415

    found: set = set()
    _calls_in(nodes, found)
    return found


def _rebound_names(nodes) -> set:
    """Every name a lowered scope BINDS after entry: `let`/`assign` targets and
    `for`/`effect` binders. A method parameter that appears here no longer names
    the caller's argument at the extern call site, so the resource-scope dataflow
    refuses to bind it (item 427 F2)."""
    names: set = set()
    for node in _walk(nodes):
        if not isinstance(node, dict):
            continue
        step = node.get("step")
        if step in ("let", "assign") and isinstance(node.get("name"), str):
            names.add(node["name"])
        elif step in ("for", "effect") and isinstance(node.get("bind"), str):
            names.add(node["bind"])
    return names


def _call_arg_lists(nodes, callee: str) -> list:
    """Every positional argument list at a DIRECT call to `callee` inside
    `nodes`. Both lowered encodings: the component `{kind: fn, name}` form and
    the pure `{kind: call, callee: {kind: var, name}}` form."""
    out: list = []
    for node in _walk(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "fn" and node.get("name") == callee:
            out.append(list(node.get("args") or []))
        elif node.get("kind") == "call":
            target = node.get("callee")
            if isinstance(target, dict) and target.get("kind") == "var" \
                    and target.get("name") == callee:
                out.append(list(node.get("args") or []))
    return out


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


def two_step_payload(ticket: dict, *, how_to_approve: str) -> dict:
    """Shape an `ApprovalRequired` into the ticket two-step RESULT (item 246).

    Lives here, beside the exception, because more than one MCP surface can
    raise it and the two have already drifted apart once: the compiler server
    renders the ticket, while the composed server (`composed.py`) caught it in a
    broad `except Exception` and returned an opaque `"ApprovalRequired: ..."`
    runtime diagnostic with no ticket and no hash (roadmap 425 F4's shape, on
    its second surface). Both fail CLOSED — the crossing does not fire either
    way — but a caller that never receives a hash can never get the crossing
    APPROVED, so the refusal is a dead end instead of a step.

    `how_to_approve` is the surface's own answer to "what can I do instead"
    (item 274): the two servers mint a yes through different channels.
    """
    return {
        "ok": False,
        "approvalRequired": True,
        "note": ("a class-(c) crossing (an irreversible emission with no checked "
                 "inverse) needs a human yes — nothing fired. " + how_to_approve),
        "ticket": ticket,
    }


# What a yes ALSO does when the crossing's resource target came from a caller
# argument (roadmap 425 F3 / 427 F5). The asymmetry this states is otherwise
# invisible: a call's arguments are only ever HASHED into the ticket
# (`argsDigest`), except for a parameter whose NAME happens to sit in
# `cap_order._REGISTRY` (`path`, `host`, `table`), whose runtime VALUE is lifted
# out and bound into the capability spelling — which `record_approval_granted`
# then writes verbatim to a durable, cross-session, plaintext log. Nothing told
# the author that naming a parameter `path` changes where its values end up, and
# nothing told the operator that a yes persists this one. Same move item 425 F1
# made with `unreviewedHostCode`: the ticket must not understate what a yes means.
_CALLER_VALUE_DURABILITY = (
    "this crossing's resource target is a value the CALLER passed in, not a "
    "literal the author wrote. Approving binds that value into the durable "
    "approval log (`record_approval_granted` -> the cross-session WAL, plaintext "
    "at rest), where the rest of this call's arguments only ever appear as the "
    "`argsDigest` hash. To keep a sensitive value out of it, declare the "
    "parameter `Secret[Str]` — it then binds to the redacted placeholder "
    "everywhere (item 416c), at the cost of the resource fold, since every "
    "secret-valued crossing binds to the SAME placeholder and a standing grant "
    "scoped to it is refused. An operator can withhold caller values from the log "
    "for the whole session instead with `revl mcp serve "
    "--approval-record-values withheld`.")


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
        # class-(c) capability -> the scopes that raise it DIRECTLY. The
        # resource-scope dataflow (item 427 F2) may only bind a token whose sole
        # origin inside the reach closure is the scope the caller's args land in.
        self._classc_scopes: dict[str, set] = {}
        for sid, direct in self._direct.items():
            for cap in direct["classC"]:
                self._classc_scopes.setdefault(cap, set()).add(sid)
        # component -> (externs, pure fns) its scopes reach. The candidate hash
        # closes over these (item 427 F4): the `@py` host bodies ARE the crossing,
        # so a swap that rewrites only a body must invalidate the standing token.
        self._reached_host: dict[str, tuple] = {}
        for name in self.index.components:
            self._reached_host[name] = self._reached_host_code(name)
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

    def _reached_host_code(self, component: str) -> tuple:
        """The externs and pure functions this component's scopes reach, directly
        or through the pure-fn call graph. The candidate hash folds their SEMANTIC
        entries in (item 427 F4) so a swap that changes only an `@py` host body -
        the thing that actually crosses the boundary - recomputes a different hash
        and every standing token pinned to the old closure fails closed."""
        work: list = []
        for sid in self.index.scopes_of.get(component) or []:
            scope = self.index.scopes.get(sid)
            if scope is not None:
                work += sorted(_called_names(scope["nodes"]))
        externs: set = set()
        fns: set = set()
        while work:
            name = work.pop()
            if name in self.index.externs:
                externs.add(name)
            elif name in self.index.functions and name not in fns:
                fns.add(name)
                body = self.index.functions[name].get("body") or []
                work += sorted(_called_names(body))
        return frozenset(externs), frozenset(fns)

    def _fold_closure(self, sid: str) -> dict:
        direct = self._direct[sid]
        cls = direct["class"]
        crossings = list(direct["crossings"])
        caps = set(direct["capabilities"])
        class_c = set(direct["classC"])
        comps = {self.index.scopes[sid]["component"]}
        scopes = {sid}
        for reached in self.index.closure(sid):
            rsid = reached["scope"]
            rdirect = self._direct[rsid]
            cls = worse(cls, rdirect["class"])
            crossings += rdirect["crossings"]
            caps |= rdirect["capabilities"]
            class_c |= rdirect["classC"]
            comps.add(self.index.scopes[rsid]["component"])
            scopes.add(rsid)
        return {"class": cls, "crossings": crossings, "capabilities": caps,
                "classC": class_c, "closureComponents": comps,
                "closureScopes": frozenset(scopes)}

    # -- lookups ------------------------------------------------------------

    def _provider_of(self, key: str) -> str | None:
        """The one component providing `key`, or None when the key does not
        resolve to exactly one.

        A shared-realm provision answers directly. Otherwise the key is
        realm-partitioned, and the fold may only answer when every realm's
        provider is the SAME component. Returning the first arbitrary match over
        an AMBIGUOUS key (the same key provided in two realms by two different
        components) would classify the call against a provider the caller may
        not even reach — the r2 provider's class-(c) crossing then reports as
        the r1 provider's class none, and the gate defers on a boundary call.
        No single answer exists, so refuse: None, which the per-call decision
        now treats as unresolved-and-refused rather than not-a-boundary-call."""
        provider = self.index.provider_of.get((key, SHARED_REALM))
        if provider is not None:
            return provider
        providers = {p for (k, _realm), p in self.index.provider_of.items()
                     if k == key}
        if len(providers) == 1:
            return next(iter(providers))
        return None

    def classify_call(self, key: str, method: str) -> dict | None:
        """The reach of a `revl_call(key, method)`, or None when the key/method
        resolves to no scope — an unresolved classification, which the per-call
        decision refuses (it is never read as "not a boundary call")."""
        provider = self._provider_of(key)
        if provider is None:
            return None
        sid = f"{provider}:{key}.{method}"
        reach = self._reach.get(sid)
        if reach is None:
            return None
        return {**reach, "component": provider, "key": key, "method": method,
                "scopeId": sid}

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
            out.append({**reach, "component": name, "key": None, "method": None,
                        "scopeId": sid})
        return out

    # -- the reach-closure candidate hash (Fix 3) ---------------------------

    def candidate_hash(self, closure_components) -> str:
        """A sha256 over the canonical JSON of the semantic entries of the
        call's REACH CLOSURE — the target's provider plus the providers of every
        service on its checked reach path, AND the host code those components
        reach. A swap of ANY of those (changed behaviour, same names) recomputes
        a different hash, so every standing token whose closure includes the
        changed provider fails the check, including one held by an untouched
        caller (invariant 4).

        Item 427 F4: the closure is not the component entries alone. A component
        entry names the extern it calls; the `@py` BODY of that extern is where
        the crossing actually happens, and a swap that rewrites only the body
        (same extern name, same declared class and capabilities, different
        destination) left the hash bit-identical and carried every standing grant
        across untouched. The reached externs and the reached pure functions are
        therefore hashed alongside the components. Reach is per-component over all
        its scopes, the same granularity the component entry itself has, so the
        hash never depends on which method of a component the call entered."""
        comps = [c for c in sorted(closure_components) if c in self._semantic]
        externs: set = set()
        fns: set = set()
        for c in comps:
            reached_ext, reached_fns = self._reached_host.get(
                c, (frozenset(), frozenset()))
            externs |= reached_ext
            fns |= reached_fns
        body = {
            "components": [self._semantic[c] for c in comps],
            "externs": [_semantic(self.index.externs[n])
                        for n in sorted(externs) if n in self.index.externs],
            "functions": [_semantic(self.index.functions[n])
                          for n in sorted(fns) if n in self.index.functions],
        }
        return _sha(_canon(body))

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

    def _resource_params(self, ext: dict) -> list:
        """`(index, name, secret)` for each of the extern's parameters naming a
        `cap_order._REGISTRY` RESOURCE kind (host/path/table, never a ceiling).
        These are the dimensions a crossing capability can be scoped on."""
        out: list = []
        for index, param in enumerate(ext.get("params") or []):
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            if name is None or not cap_order.is_registered(name) \
                    or cap_order.is_ceiling(name):
                continue
            out.append((index, name, bool(param.get("secret"))))
        return out

    def _scope_params(self, sid: str) -> list:
        """The provide-method parameter names of scope `sid`, in the order the
        caller's positional args arrive in. Empty for an activation scope (no
        caller and no args) and for a scope that is not a provide-method."""
        scope = self.index.scopes.get(sid)
        if scope is None or scope.get("kind") != "provide-method":
            return []
        comp = self.index.components.get(scope["component"]) or {}
        for step in comp.get("body") or []:
            if not isinstance(step, dict) or step.get("step") != "provide":
                continue
            if step.get("name") != scope.get("key"):
                continue
            for method in step.get("methods") or []:
                if method.get("name") == scope.get("method"):
                    return [p for p in (method.get("params") or [])
                            if isinstance(p, str)]
        return []

    def binding_scope(self, reach: dict, token: str) -> str | None:
        """The one scope whose args may be bound into `token`'s resource scope,
        or None when no scope may be (item 427 F2).

        The caller's positional args land in exactly ONE scope: the provide-method
        the call names. A class-(c) token raised anywhere ELSE on the reach closure
        (a downstream provider across the service seam, a spawned instance, a
        second method of the same component) is crossed with arguments this call
        never supplied, so binding this call's args into it would state a resource
        target that is not the one the crossing uses. Refused: the token then keys
        bare and the operator is shown the whole cone, which is wide but true."""
        root = reach.get("scopeId")
        if root is None:
            return None
        origins = set(self._classc_scopes.get(token) or ())
        origins &= set(reach.get("closureScopes") or {root})
        return root if origins == {root} else None

    _NO_DATAFLOW_HINT = (
        "revl could not prove which value reaches this parameter, so the "
        "crossing is shown UNSCOPED rather than with a resource scope that "
        "might be wrong. To get a resource-scoped prompt (and a standing grant "
        "narrowed to one target), forward the provide-method's own parameter "
        "straight into the extern's resource parameter (`fn send(host, body) "
        "{ emit http_post(host, body) }`), or pass a string literal. A value "
        "that is computed, rebound by a `let`, reached through a helper "
        "function, or crossed by a different component cannot be bound.")

    def bind_resource_scope(self, token: str, args,
                            scope_id: str | None = None) -> tuple:
        """Bind the resource args into the crossing capability `token`, turning
        bare `gateway.send` into `gateway.send(host="api.stripe.com")` at ticket
        time (design §6 N1). Returns `(spelling, refusal)`: the canonical spelling
        or None, and a refusal sentence naming the unbindable parameters or None.
        A token with no resource parameter binds nothing and refuses nothing (it
        keys bare, byte for byte as before).

        Item 427 F2: the binding is now a DATAFLOW fact, not a positional
        coincidence. The old projection indexed the CALLER's positional args by
        the DECLARING EXTERN's parameter list, which agree only when the
        provide-method forwards its parameters straight through in the same
        positions: nothing checked that, so a body that ignored its `host`
        argument and posted somewhere else still produced a ticket, a ledger entry
        and a distilled rule reading the caller's host. The operator narrowed a
        grant to a target the call never used. A resource scope that MIGHT be
        wrong is worse than none, because the operator reads it as a fact.

        So each resource parameter is bound only from a proven source, at the one
        call site (or several agreeing sites) inside the scope the caller's args
        land in:

          * a `{kind: lit}` string argument binds to that literal: it is what
            executes, whatever the caller passed;
          * a `{kind: name/var}` argument naming a provide-method PARAMETER that
            the scope never rebinds binds to the caller's arg at that parameter's
            index.

        Anything else leaves the parameter unbound and returns a refusal naming
        the fix the author can enact (item 274): a computed expression, a rebound
        name, an extern reached THROUGH a pure function
        (`facts["externs"][...]["through"]`), call sites that disagree, or a token
        raised by another scope on the reach closure.

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
        if ext is None:
            return None, None, False   # a service-op key: no extern, keys bare
        resource = self._resource_params(ext)
        if not resource:
            return None, None, False   # no resource dimension to scope on
        dimensions = ", ".join(f"`{name}`" for _i, name, _s in resource)
        if scope_id is None or scope_id not in self.index.scopes:
            return None, (
                f"`{token}` was not resource-scoped: the crossing has no single "
                f"calling scope to trace {dimensions} from. "
                + self._NO_DATAFLOW_HINT), False
        scope = self.index.scopes[scope_id]
        # an extern reached THROUGH a pure fn is not called with arguments this
        # scope wrote, so no site in this scope proves the value (fail closed).
        for fact in scope["facts"]["externs"]:
            if fact["name"] == ext.get("name") and fact.get("through"):
                helpers = ", ".join(f"`{h}`" for h in fact["through"])
                return None, (
                    f"`{token}` was not resource-scoped: `{ext.get('name')}` is "
                    f"reached through {helpers}, so revl cannot trace "
                    f"{dimensions} to this call's arguments. "
                    + self._NO_DATAFLOW_HINT), False
        sites = _call_arg_lists(scope["nodes"], ext.get("name"))
        if not sites:
            return None, (
                f"`{token}` was not resource-scoped: no direct call to "
                f"`{ext.get('name')}` in this scope binds {dimensions}. "
                + self._NO_DATAFLOW_HINT), False
        params = self._scope_params(scope_id)
        rebound = _rebound_names(scope["nodes"])
        pairs: list[tuple[str, object]] = []
        unbound: list[str] = []
        # roadmap 425 F3 / 427 F5: whether any bound dimension's value came from
        # a CALLER argument rather than a literal the author wrote. A literal is
        # already in the source and discloses nothing about the caller; an
        # argument is the caller's runtime data.
        from_caller = False
        for index, name, secret in resource:
            sources = {self._arg_source(site, index, params, rebound)
                       for site in sites}
            if len(sources) != 1:
                unbound.append(name)        # sites disagree: no single target
                continue
            source = next(iter(sources))
            value = self._arg_value(source, args)
            if value is None:
                unbound.append(name)
                continue
            if source[0] == "param" and not secret:
                from_caller = True
            pairs.append((name, REDACTED_SECRET if secret else value))
        refusal = None
        if unbound:
            named = ", ".join(f"`{n}`" for n in unbound)
            refusal = (f"`{token}` was not resource-scoped on {named}: "
                       + self._NO_DATAFLOW_HINT)
        if not pairs:
            return None, refusal, False
        try:
            return cap_order.make_cap(token, pairs).to_str(), refusal, from_caller
        except cap_order.CapError:
            return None, refusal, False

    @staticmethod
    def _arg_source(site: list, index: int, params: list, rebound: set):
        """The PROVEN source of the argument at `index` of one extern call site:
        `("lit", value)` for a string literal, `("param", i)` for an unrebound
        provide-method parameter, or None when nothing proves it. Hashable, so
        several call sites can be compared for agreement."""
        if index >= len(site):
            return None
        arg = site[index]
        if not isinstance(arg, dict):
            return None
        if arg.get("kind") == "lit" and isinstance(arg.get("value"), str):
            return ("lit", arg["value"])
        if arg.get("kind") in ("name", "var"):
            name = arg.get("id") if arg.get("kind") == "name" else arg.get("name")
            if isinstance(name, str) and name in params and name not in rebound:
                return ("param", params.index(name))
        return None

    @staticmethod
    def _arg_value(source, args) -> str | None:
        """The runtime string a proven source resolves to, or None."""
        if source is None:
            return None
        kind, payload = source
        if kind == "lit":
            return payload
        value = (args or [])[payload] if payload < len(args or []) else None
        return value if isinstance(value, str) else None

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
        #
        # item 427 F2: the binding is only made where DATAFLOW proves it. A token
        # whose resource dimension cannot be traced from this call's arguments
        # keys bare and lands in `resourceScopeRefusals`, so the operator reads
        # the wide-but-true cone plus the sentence naming what the author must
        # change to get a narrow one: never a scope that might be wrong.
        resource_scopes: dict[str, str] = {}
        refusals: dict[str, str] = {}
        bound_class_c: list[str] = []
        # roadmap 425 F3 / 427 F5: the tokens whose bound valuation came from a
        # CALLER ARGUMENT rather than a literal the author wrote. The ticket is
        # the live decision and keeps the real value either way — an operator who
        # cannot see the target cannot answer, and for a fresh call the value is
        # the caller's own, just sent. What this marks is DURABILITY: the ledger
        # copy (`_distillation_ledger_fields`) must not write a caller-supplied
        # value into the cross-session WAL. See `Session._distillation_ledger_fields`.
        caller_valued: list[str] = []
        for token in sorted(reach.get("classC") or []):
            sid = self.binding_scope(reach, token)
            spelling, refusal, from_caller = self.bind_resource_scope(
                token, args, sid)
            if spelling is not None:
                resource_scopes[token] = spelling
                bound_class_c.append(spelling)
                if from_caller:
                    caller_valued.append(token)
            else:
                bound_class_c.append(token)
            if refusal is not None:
                refusals[token] = refusal
        body["classCCapabilities"] = sorted(bound_class_c)
        if resource_scopes:
            body["resourceScopes"] = resource_scopes
        if refusals:
            body["resourceScopeRefusals"] = refusals
        if caller_valued:
            body["resourceScopesFromCallerArgs"] = sorted(caller_valued)
            body["resourceScopeDurability"] = _CALLER_VALUE_DURABILITY
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


# ------------------------------------------------------- item 310, surface H
# The cache APPLICABILITY fold: "is this callee's reach cacheable?".
#
# The 414 matrix gives it a column of its own because it is itself an
# authority-derivation surface, and "plausibly correct if implemented over the
# ClassMap closure" is exactly the hand-wave the matrix exists to eliminate. So
# it is a worst-over-reach fold over the SAME provider closure the ClassMap
# folds (`Composition.closure`), which is what makes it follow the
# spawn/instance-get seam (crossing kind 2), the transitive service closure
# (kind 4) and the `*` first-class-value widening (kind 8) without a second
# reach walk that could disagree with the class fold.
#
# It runs at LOAD, not at compile: the compile-time admission checks in
# `lower._check_cache_declarations` see only the DECLARING method's declared
# reach shape (`emission` or not), and a service method's cache clause is an
# interface contract every provider inherits. Which component actually provides
# the key — and therefore what the cached reach really crosses — is a fact about
# the composition, known only once the manifest is linked.


def _cache_walk_kinds(node, kinds: set) -> bool:
    """Whether any node in `node` carries one of `kinds` in its `kind` slot."""
    for inner in _walk(node):
        if isinstance(inner, dict) and inner.get("kind") in kinds:
            return True
    return False


def _component_state_names(comp: dict) -> set:
    """The component-level bindings that are STATE — a name whose value can
    differ between two calls with equal arguments.

    A `let-effect` binding is the component's effect-created world (the
    `examples/handoff_cache.rvl` `Map`), a `let mut` is mutable by spelling, and
    a `let` of a `spawn` is a live instance. Everything else at component level
    is a closed constant expression evaluated once at activation, so reading it
    keeps a method a function of its arguments.

    Read only by the `pure_fn` arm of the fold below: `cache capability` /
    `cache external` name a BOUNDARY read, and the connection handle that read
    goes through is exactly such a binding — refusing those would refuse the
    feature. A purity claim is the one that a state read falsifies."""
    names: set = set()
    for step in comp.get("body") or []:
        if not isinstance(step, dict):
            continue
        kind = step.get("step")
        if kind == "let-effect" and isinstance(step.get("bind"), str):
            names.add(step["bind"])
        elif kind == "let" and isinstance(step.get("name"), str):
            if step.get("mutable") or _cache_walk_kinds(step.get("value"),
                                                        {"spawn", "host"}):
                names.add(step["name"])
    return names


def _cache_referenced_names(nodes) -> set:
    """Every name a lowered scope mentions. Deliberately does not model
    shadowing: a method parameter that reuses a state binding's name reads here
    as a state reference and REFUSES. That is the conservative direction (a
    refusal, never a silent cache over state), and the fold says so."""
    out: set = set()
    for node in _walk(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "name" and isinstance(node.get("id"), str):
            out.add(node["id"])
        elif node.get("kind") == "var" and isinstance(node.get("name"), str):
            out.add(node["name"])
    return out


def _cache_scope_findings(index, sid: str, cls: str):
    """Every reason this ONE scope makes a reach uncacheable, as
    `(token, what, why)` triples.

    ALL of them, not the first: the refusal below reports one, but the 414
    per-kind differential (tests/test_reach_completeness.py) needs the fold's
    whole visit set — a fold that stopped visiting one crossing kind while
    another in the same scope still reported would look identical from the
    outside, which is exactly the blind spot the matrix exists to catch.

    `token` names the crossing machine-readably (the extern name, the emitted
    `key.method`, `*` for the widening, the state binding); `what`/`why` are the
    human halves of the diagnostic."""
    scope = index.scopes.get(sid)
    if scope is None:
        return
    facts = scope["facts"]
    label = scope.get("label") or sid
    pure = cls == "pure_fn"

    for fact in facts["externs"]:
        name = fact["name"]
        klass = fact.get("class")
        decl = index.externs.get(name) or {}
        via = f" (through `{'`, `'.join(fact['through'])}`)" if fact.get("through") else ""
        if klass == "witnessed":
            yield (name, f"{label} reaches the `witnessed` extern `{name}`{via}",
                   "a witnessed crossing registers its inverse in the escrow at "
                   "fire time; a hit skips the firing and therefore the "
                   "registration, so an abort would replay an incomplete "
                   "history (G7)")
        elif klass == "acquire":
            yield (name, f"{label} reaches the `acquire` extern `{name}`{via}",
                   "an acquisition registers an undo in the escrow at fire "
                   "time; a hit skips the firing and therefore the "
                   "registration, so teardown would leak the resource (G7)")
        elif klass == "emission" and fact.get("deferred"):
            yield (name,
                   f"{label} reaches the `deferred` emission extern `{name}`{via}",
                   "a deferred emission is a QUEUED write; a hit that skips the "
                   "firing skips the enqueue, so the commit would flush an "
                   "incomplete history (G7)")
        elif klass == "emission" and decl.get("compensate") is not None:
            yield (name,
                   f"{label} reaches the `compensate`-declaring emission extern "
                   f"`{name}`{via}",
                   "a compensated emission registers a compensation escrow "
                   "entry at fire time; a hit skips the registration, so an "
                   "abort after it replays an incomplete offset history (G7)")
        elif klass == "emission" and pure:
            yield (name, f"{label} reaches the emission extern `{name}`{via}",
                   "`cache pure` claims the result is a function of the "
                   "arguments alone, and a boundary crossing is not")

    for fact in facts["emissions"]:
        token = f"{fact['key']}.{fact['method']}"
        if fact.get("compensated"):
            yield (token, f"{label} emits `{token}` under a `compensate`",
                   "the compensation escrow entry is registered at fire time; "
                   "a hit skips the registration, so an abort after it replays "
                   "an incomplete offset history (G7)")
        elif pure:
            yield (token, f"{label} emits `{token}`",
                   "`cache pure` claims the result is a function of the "
                   "arguments alone, and a boundary crossing is not")

    if index.value_widens(scope["nodes"]):
        yield ("*", f"{label} hands an emitting callable on as a VALUE",
               "the boundary it may reach cannot be named (`*`), so an entry "
               "cannot be scoped to the authority that covered its miss and a "
               "hit could re-deliver a result the caller was never authorized "
               "to obtain")

    if pure:
        comp = index.components.get(scope["component"]) or {}
        for name in sorted(_component_state_names(comp)
                           & _cache_referenced_names(scope["nodes"])):
            yield (name, f"{label} reads the component state `{name}`",
                   "`cache pure` claims the result is a function of the "
                   "arguments alone; a method over mutable component state is "
                   "not, so an entry would serve a value the state has since "
                   "moved past")


def cache_applicability_findings(class_map, cache_index):
    """Every `(key, method, class, token, what, why)` the applicability fold
    finds over the whole cache index, in a deterministic order: by declaring
    method, then by closure depth, then by scope id. The refusal below reports
    the first; the 414 differential reads them all."""
    index = class_map.index
    for key, method in sorted(cache_index):
        cls = (cache_index[(key, method)] or {}).get("class")
        reach = class_map.classify_call(key, method)
        if reach is None:
            # an unresolved classification, which the per-call decision refuses
            # outright: the call never runs, so no entry can ever be born and
            # there is nothing to fold over.
            continue
        sid = reach["scopeId"]
        order = [sid] + [hop["scope"] for hop in
                         sorted(index.closure(sid),
                                key=lambda hop: (hop["depth"], hop["scope"]))]
        for scope_id in order:
            for token, what, why in _cache_scope_findings(index, scope_id, cls):
                yield (key, method, cls, token, what, why)


def cache_applicability_refusal(class_map, cache_index) -> str | None:
    """Surface H: refuse a `cache`-declaring seam method whose PROVIDER CLOSURE
    is not cacheable, or None. Runs at load, over the linked composition.

    Worst-over-reach: the declaring scope plus every scope it reaches across the
    service and spawn seams, in a deterministic order, so the same composition
    always names the same crossing. Inert for a composition that declares no
    `cache` (the index is empty)."""
    for key, method, cls, _token, what, why in cache_applicability_findings(
            class_map, cache_index):
        spelling = {"pure_fn": "cache pure",
                    "capability_result": "cache capability",
                    "external_effect": "cache external"}.get(cls, "cache")
        return (
            f"`{spelling}` on `{key}.{method}` is refused: {what}; {why}. "
            f"The applicability fold is worst-over-reach over the provider "
            f"closure `{key}` resolves to, not over the declaration alone — "
            f"the clause is an interface contract every provider inherits, "
            f"so the reach that actually runs is what decides (item 310, "
            f"surface H). Move the clause to a method whose whole reach is "
            f"cacheable, or drop it.")
    return None
