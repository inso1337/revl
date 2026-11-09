"""Project a compiled IR's composition manifest onto the ambient-admission wire.

`admit_ambient(src, manifest)` in `selfhost/lower.rvl` (the native gate, item 186
/ issue #86) takes the running composition as a serialised wire, not as an IR:
rows joined by ``;``, the kind of each row set by its leading marker
(docs/design/186-ambient-admission-guarantees.md, "The wire"):

    C/k/r     provision:   component C provides key k in realm r ("" = shared)
    C<k       requirement: component C requires key k in the shared realm
    C<k/r     requirement: the same, resolved in realm r
    C<*k      requirement: the same, multi-realm bound (item 162)
    C>k/r,r   route:       the realms C binds k across (the legs of that bind)
    C=k:T     handoff:     C exports state of type T at key k (item 53)
    ~T~record~f:Ty~g:Ty
    ~T[P]~variant~A~B:Ty
              declaration: a running record or variant that a handoff spelling
                           reaches, so the gate compares the declarations
                           behind a nominal name and not only its spelling
    -C        replacing:   component C is withdrawn by this admission
    !halted   header:      the composition is halted; every admission refuses
    !services header:      the `:S` rows below ENUMERATE the running
                           composition's service declarations, exhaustively
    :S        service:     the running composition declares service S
    :S,a,b    service:     ... and its operations are exactly `a` and `b`
    :S,a(k:Str) service:   ... and `a`'s declared parameters are exactly those

`manifest_wire(ir)` renders `IR(M)` — the manifest of an already-compiled
composition `M` — into exactly that wire, so a differential oracle can construct
the SAME running manifest on both sides: the reference compiles `M` to `IR(M)`,
projects it here, and feeds the wire to the native gate. `replacing=` renders
the withdrawn set of a REPLACEMENT admission, so the wire says exactly what
``compile_files(paths, manifest=IR(M), replacing=R)`` says on the reference
side. The handoff row (``C=k:T``) carries the state shape each running provider
EXPORTS (item 53), which is what the gate compares a replacement's ACCEPTED
shape against — the port of `admission._admit_handoff_replacement`. The
declaration rows (``~T~record~…``) carry what the names in those shapes MEAN in
the running version, which `_handoff_compatible` reads each side's spelling
against; see `_decl_rows`.

The SERVICE BLOCK (issue #346) says which services the running composition
DECLARES, and its header is the load-bearing half. The service names are what
decide whether a candidate's ``service S { … }`` is a fresh interface (which the
reference admits into any composition) or a REDECLARATION of a running one
(which the reference gates on the §5 compatibility relation,
`revl.admission._admit_service_replacement`, and refuses when it breaks a
running toucher). A wire carrying no ``!services`` header makes no claim about
the running set, so a reader must treat it as UNKNOWN rather than empty: the
difference between "declares no service" and "does not say" is the difference
between a sound admission and a wave-through, which is why the claim is spelled
on the wire instead of inferred from the absence of rows.
"""

from __future__ import annotations

from collections.abc import Iterable

from .admission import _decl_refs, _spelling_names
from .model_answer import ANSWER_TYPES

SHARED_REALM = ""

#: The header row asserting that the `:S` rows beside it are the WHOLE running
#: service set. Without it a reader knows nothing about the running services.
SERVICES_HEADER = "!services"

#: The leading marker of a service-declaration row.
SERVICE_MARKER = ":"

#: The leading marker, and the separator, of a type-declaration row. No reader
#: from before this row has the kind, so every one of them refuses a wire that
#: carries it (fail closed). It never occurs in a type spelling, which is what
#: lets the members be joined by it without escaping.
DECL_MARKER = "~"


def _components(ir: dict) -> list[dict]:
    """The manifest entries of `ir` — accepts the whole IR (reads
    ``ir["manifest"]["components"]``) or a manifest dict (reads its
    ``components``). Entries carry ``provides``/``inject`` as key LISTS, an
    ``isolate`` key->realm map and a ``routes`` key->route map, the shape
    `_link` builds."""
    manifest = ir.get("manifest", ir) if isinstance(ir, dict) else {}
    comps = manifest.get("components") if isinstance(manifest, dict) else None
    return comps or []


def _declared_services(ir: dict) -> list[tuple[str, list[str]]] | None:
    """The running composition's services in declaration order as
    ``(name, operation names)`` pairs, or ``None`` when `ir` is not a whole
    compiled IR and the set is therefore UNKNOWN.

    A manifest DICT (the ``ir["manifest"]`` half on its own) carries components
    and no services, and an absent service table is not an empty one: returning
    ``None`` for it is what keeps the wire from claiming a composition declares
    no services when all that happened is that nobody asked.

    The operation names are the second half of the same claim, and the reason a
    candidate's ``store.get(key)`` can be resolved against the RUNNING `Store`
    rather than only against its name (docs/design/457 T4b). They are rendered
    for every service a known table holds, including one that declares none —
    ``:S,`` is the empty surface, which is a claim, while a bare ``:S`` is the
    absence of one."""
    if not isinstance(ir, dict) or "manifest" not in ir:
        return None
    services = ir.get("services")
    if not isinstance(services, dict):
        return None
    return [(name, _operations(entry)) for name, entry in services.items()]


#: The characters that carry STRUCTURE on the wire, and so may not appear
#: inside an operation signature's own text: the row separator, the operation
#: separator, the parameter separator, and the pair of brackets that opens and
#: closes a parameter list. A signature needing one of them is WITHHELD (the
#: bare operation name is rendered instead), never escaped: a reader that sees
#: no parameter list decides nothing about the operation's arguments, which is
#: the direction this wire is allowed to err in.
_WIRE_STRUCTURE = ";,|()"


def _wire_name(text: object) -> bool:
    """Is `text` a bare identifier — the same shape the gate's `bare_ident`
    accepts for every name on the wire?"""
    return isinstance(text, str) and text.isidentifier() and text.isascii()


def _signature(params: object) -> str | None:
    """``(name:Type|name:Type)`` for a method's declared parameter list, or
    None when the list cannot be spelled on the wire.

    The parameter TYPES are the half that makes a candidate's
    ``store.bump(key)`` decidable against the RUNNING `Store` rather than only
    against its operation names (docs/design/457 section 4.5, "its call sites
    are typed against the RUNNING signature"). The NAMES are rendered because
    the reference's own diagnostic names the parameter, and a gate that agrees
    on the refusal but not on its sentence is a `msg-mismatch`, not agreement.

    None is returned — and the caller then renders the bare operation name —
    for a parameter whose name is not an identifier, whose type is missing, or
    whose type spelling needs one of `_WIRE_STRUCTURE`. That last case is real
    (``Map[Str, Int]`` carries the operation separator) and it is why this
    returns an option rather than a best effort: a signature that lost a
    parameter, or gained one by splitting a type, would refuse calls the
    reference admits."""
    if not isinstance(params, list):
        return None
    rendered = []
    for param in params:
        if not isinstance(param, dict):
            return None
        name, ty = param.get("name"), param.get("type")
        if not _wire_name(name) or not isinstance(ty, str) or not ty:
            return None
        if any(ch in ty for ch in _WIRE_STRUCTURE):
            return None
        rendered.append(f"{name}:{ty}")
    return "(" + "|".join(rendered) + ")"


#: The exact key set a PLAIN service operation's IR entry carries: a parameter
#: list, a return type, and an `emission` flag that is False. Every other key in
#: `lower.py`'s service table is a MARKING — `async`, `capabilities`,
#: `commutative`, `idempotent`, `termination`, `cache`, `validated`, `route` —
#: and each of them changes what the reference decides about a call. The return
#: type is rendered only for an operation whose entry is exactly this set, so a
#: marking the wire has no spelling for WITHHOLDS the return rather than
#: shipping a signature that looks complete and is not. A marking added to
#: `lower.py` later withholds on arrival, without an edit here.
_PLAIN_METHOD_KEYS = frozenset({"params", "returns", "emission"})


def _return_type(method: object) -> str | None:
    """``Type`` for a plain operation's declared return, or None when the wire
    says nothing about it.

    The return is the half the wire withheld until issue #346's admission slice
    needed it: a candidate's ``fn lookup(key) = store.get(key)`` is checked by
    the reference against the RUNNING ``get``'s return, so a reader that cannot
    see that return cannot decide the method and must withhold. None is the
    silence, and it is returned for a marked operation, an operation with no
    declared return, and a return whose spelling needs one of
    `_WIRE_STRUCTURE`."""
    if not isinstance(method, dict):
        return None
    if set(method) != _PLAIN_METHOD_KEYS or method.get("emission"):
        return None
    returns = method.get("returns")
    if not isinstance(returns, str) or not returns:
        return None
    if any(ch in returns for ch in _WIRE_STRUCTURE + ":"):
        return None
    return returns


def _operation(name: str, method: object) -> str:
    """One operation token: the bare name, the name followed by its declared
    parameter list, or both followed by ``:Return``. Each half is a claim and
    its absence is silence, so a reader may believe what is there and must
    decide nothing about what is not."""
    if not _wire_name(name):
        return name
    signature = _signature(method.get("params")) if isinstance(method, dict) \
        else None
    if signature is None:
        return name
    returns = _return_type(method)
    return f"{name}{signature}" if returns is None \
        else f"{name}{signature}:{returns}"


def _operations(entry: object) -> list[str]:
    """One service's declared operation tokens, in declaration order. An entry
    with no readable ``methods`` table renders as the empty surface: the IR's
    service table always carries one, and a service with no operation is a
    legal (if idle) declaration.

    A service entry carrying anything beyond its ``methods`` table (today
    ``commutative``) has every return WITHHELD, for the reason a marked
    operation does: the entry-level marking is a fact about the operations that
    the wire has no spelling for."""
    if not isinstance(entry, dict):
        return []
    methods = entry.get("methods")
    plain = set(entry) == {"methods"}
    if isinstance(methods, dict):
        return [_operation(name, method if plain else {"params":
                           (method or {}).get("params")})
                for name, method in methods.items()]
    if isinstance(methods, list):
        return [_operation(m.get("name", ""),
                           m if plain else {"params": m.get("params")})
                for m in methods if isinstance(m, dict)]
    return []


def _route_rows(name: str, entry: dict) -> list[str]:
    """The route rows of `entry`: one per routed key, carrying the realms of the
    bind in declaration order.

    The requirement row's `*` marker says a key is routed; this says WHERE, and
    it is what lets the gate run item 162's per-realm provider check over a
    RUNNING consumer — the check the reference runs over every `_link` entry,
    ambient ones included, and the one the gate was blind to while the wire
    carried no legs (issue #1036). The strategy name is deliberately not
    rendered: no refusal on this surface names it, and a field nothing reads is
    a field that drifts.
    """
    rows = []
    for key, route in (entry.get("routes") or {}).items():
        rows.append(f"{name}>{key}/" + ",".join(route.get("realms") or []))
    return rows


def _handoffs(ir: dict) -> dict[str, dict]:
    """`component name -> {"key", "type"}` for every running provider that
    declares a `handoff` (item 53).

    Read off the WHOLE IR document's ``components``, whose ``handoff`` field
    survives lowering — the manifest projection ``_components`` reads drops it,
    which is why this is a second lookup rather than a field of the entry. The
    same place `compiler._running_handoffs` reads, so the wire carries exactly
    what the reference's own ambient view carries; a bare manifest dict has no
    such field anywhere and renders no handoff row, exactly as it gives the
    reference an empty hand-off table."""
    if not isinstance(ir, dict) or "manifest" not in ir:
        return {}
    out: dict[str, dict] = {}
    for comp in ir.get("components") or []:
        handoff = comp.get("handoff")
        if isinstance(handoff, dict) and handoff.get("key"):
            out[comp.get("name", "")] = handoff
    return out


def _presence_row(name: str) -> str:
    """The row naming a running component that renders no other row: its bare
    name, `C` (issue #2289). It contributes no edge, no provision and no
    requirement; it only puts `C` in the running composition's name set, which
    is what the reference's folded-name check reads."""
    return name


def _handoff_row(name: str, handoff: dict | None) -> list[str]:
    """The handoff row of one running component, or no row at all.

    ``C=k:T`` says component C exports state of shape T at the key k it
    provides. It is a per-component COMPOSITION row like the route row beside
    it, and it is the one the gate needs to run item 53's compatibility
    relation: a replacement that accepts a shape the predecessor's exported
    state does not fit is refused rather than admitted into a swap that drops
    the state. The type is rendered as the reference spells it in the IR (the
    declared annotation, alias unresolved), because that is the string
    `admission._handoff_compatible` compares."""
    if not handoff:
        return []
    return [f"{name}={handoff['key']}:{handoff.get('type') or ''}"]


def _decl_row(name: str, spec: dict) -> str | None:
    """``~Name[P,Q]~record~f:T~g:U`` or ``~Name~variant~A~B:T`` for one
    declaration, or None when it cannot be spelled on the wire.

    None is WITHHELD, never escaped, and it errs in the right direction: a name
    the running side does not declare reads there as undeclared, which the gate
    refuses against a successor that declares it, so a declaration this wire
    cannot carry refuses the swap and never admits it. A name, a parameter or a
    member name that is not a bare identifier, a type spelling that is empty or
    carries the row separator or the row terminator, or a record with no field
    is such a case."""
    params = list(spec.get("params") or ())
    if not _wire_name(name) or not all(_wire_name(p) for p in params):
        return None
    head = f"{name}[{','.join(params)}]" if params else name
    members: list[str] = []
    if spec.get("kind") == "variant":
        kind = "variant"
        for case in spec.get("cases") or []:
            cname, payload = case.get("name"), case.get("payload")
            if not _wire_name(cname):
                return None
            if payload is None:
                members.append(cname)
            elif _decl_type_ok(payload):
                members.append(f"{cname}:{payload}")
            else:
                return None
    else:
        kind = "record"
        for fname, ftype in (spec.get("fields") or {}).items():
            if not _wire_name(fname) or not _decl_type_ok(ftype):
                return None
            members.append(f"{fname}:{ftype}")
        if not members:
            return None
    return DECL_MARKER + DECL_MARKER.join([head, kind, *members])


def _decl_type_ok(text: object) -> bool:
    return (isinstance(text, str) and text != ""
            and not any(c in text for c in f";{DECL_MARKER}\n"))


def _decl_rows(types: dict | None, roots: Iterable[str | None]) -> list[str]:
    """The declaration rows for every record or variant that the ROOT type
    spellings reach, and only those, sorted by name.

    A spelling reaches a declared name by mentioning it, and a declaration
    reaches whatever its own fields or cases mention (minus its own type
    parameters), so a nested ``Map[Str, List[St]]``, a generic and a recursive
    type are all followed, each declaration once. A name the table does not
    declare (a builtin, an opaque nominal) reaches nothing and renders nothing,
    and so does a PROVIDED type (`model_answer.ANSWER_TYPES`), which every
    version seeds identically and no program can redeclare.

    `roots` is a set of spellings so a consumer other than the hand-off state
    can ask for the declarations behind its own spellings. With no roots, or
    roots that name no declared type, no row is rendered and the wire is
    byte-identical to one rendered before this row existed."""
    types = types or {}
    seen: set[str] = set()
    todo = [n for root in roots for n in _spelling_names(root)]
    while todo:
        name = todo.pop()
        spec = types.get(name)
        if name in seen or not isinstance(spec, dict) or name in ANSWER_TYPES:
            continue
        seen.add(name)
        params = set(spec.get("params") or ())
        todo += [n for t in _decl_refs(spec) for n in _spelling_names(t) - params]
    rows = (_decl_row(name, types[name]) for name in sorted(seen))
    return [row for row in rows if row is not None]


def _requirement_row(name: str, key: str, entry: dict) -> str:
    """One requirement row for `key`, carrying the realm the running consumer
    resolves it in — which is what makes the gate's per-(key, realm) reasoning
    (the G3 edge, and the unmet-consumer check of a replacement) match
    `_link`'s. A routed key is marked rather than realm-qualified: its legs
    resolve per-realm and the wire carries no route legs, so the gate must not
    resolve it through the single-realm table."""
    if key in (entry.get("routes") or {}):
        return f"{name}<*{key}"
    realm = (entry.get("isolate") or {}).get(key, SHARED_REALM)
    if realm == SHARED_REALM:
        return f"{name}<{key}"
    return f"{name}<{key}/{realm}"


def manifest_wire(ir: dict, replacing: Iterable[str] = ()) -> str:
    """The ambient-admission wire for the composition manifest of `ir`.

    Provision rows come first per component, then requirement rows, then the
    route rows of the same component, components in manifest (declaration)
    order — the node order the gate's G3 union graph
    seeds its DFS from, so a cross-manifest cycle is named identically to the
    single-source composition of the manifest ++ the incoming text. The
    withdrawal rows of `replacing` follow, after the composition they act on.

    A running component that renders none of those rows (it provides nothing,
    requires nothing, routes nothing and exports no state) renders a PRESENCE
    row instead: its bare name (issue #2289). Without it the wire did not name
    the component at all, so the gate could not refuse an incoming name that
    folds onto it, which the reference refuses. Every running component is
    therefore named by at least one row. A component that has any other row
    renders no presence row, so every wire that named all its components before
    is byte-identical. A reader from before this row refuses it as an unknown
    row kind, which fails closed: a consumer pinned to an older gate refuses
    every admission into a composition holding such a component. A bare
    manifest dict whose components all lack rows used to render the empty
    wire, which a reader takes as the empty composition with no services; it
    now renders the presence rows, and with no `!services` header the running
    services read as unknown, which is the truthful reading of a dict that
    carries no service table.

    `replacing` is the same set ``compile_files``' `replacing=` takes: the
    running components this admission withdraws. A name that is not running
    withdraws nothing, exactly as on the reference side. The components the
    incoming TEXT redeclares are NOT rendered here — the gate derives that
    implicit half from the text itself, as `compile_files` does.

    The DECLARATION rows (``~T~record~…``) follow the composition rows and
    precede the service block: they say what the type names in the handoff rows
    mean in the running version (`_decl_rows`), and only for the types those
    rows reach. A composition with no handoff, or whose handoffs name no
    declared type, renders none, so every such wire is byte-identical to before.
    A reader from before this row refuses it as an unknown row kind, which fails
    closed for the same reason as the presence row above.

    A route row is a COMPOSITION row — it says what one running component binds
    across which realms — so it sits with its component, after that component's
    requirement rows and ahead of the service block. The handoff row (``C=k:T``,
    item 53) is a per-component composition row too and sits last within its
    component, for the same reason and with the same consequence: it moves no
    provision or requirement position, so the `mnames` DFS seed order is
    unchanged and a composition declaring no `handoff` renders byte-identically
    to before. That keeps the per-component
    grouping the wire already has, keeps the running composition's routes in the
    order `_link` walks its entries (which fixes which realm a refusal names
    first), and leaves both the provision/requirement positions and the `mnames`
    DFS seed order untouched.

    The SERVICE BLOCK sits between the composition rows and the withdrawal rows:
    it describes the composition, and a withdrawal acts on what precedes it. So
    the withdrawal rows stay last, and a wire rendered with no `replacing` grows
    only a suffix — the provision/requirement rows keep their exact positions and
    order, which is what leaves the G3 DFS seed order (`mnames`, which a `:S` row
    does not touch) provably unchanged.
    """
    rows: list[str] = []
    handoffs = _handoffs(ir)
    for entry in _components(ir):
        name = entry.get("name", "")
        isolate = entry.get("isolate") or {}
        before = len(rows)
        for key in entry.get("provides") or []:
            realm = isolate.get(key, SHARED_REALM)
            rows.append(f"{name}/{key}/{realm}")
        for key in entry.get("inject") or []:
            rows.append(_requirement_row(name, key, entry))
        rows.extend(_route_rows(name, entry))
        rows.extend(_handoff_row(name, handoffs.get(name)))
        if len(rows) == before:
            rows.append(_presence_row(name))
    # the declarations the running hand-off spellings reach: after every
    # composition row, so none of their positions moves, and nothing at all for
    # a composition with no hand-off over a declared type
    rows.extend(_decl_rows(ir.get("types") if isinstance(ir, dict) else None,
                           [h.get("type") for h in handoffs.values()]))
    services = _declared_services(ir)
    if services is not None:
        rows.append(SERVICES_HEADER)
        rows.extend(f"{SERVICE_MARKER}{name}," + ",".join(ops)
                    for name, ops in services)
    rows.extend(f"-{name}" for name in replacing)
    return ";".join(rows)
