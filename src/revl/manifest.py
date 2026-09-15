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
    -C        replacing:   component C is withdrawn by this admission
    !halted   header:      the composition is halted; every admission refuses
    !services header:      the `:S` rows below ENUMERATE the running
                           composition's service declarations, exhaustively
    :S        service:     the running composition declares service S
    :S,a,b    service:     ... and its operations are exactly `a` and `b`

`manifest_wire(ir)` renders `IR(M)` — the manifest of an already-compiled
composition `M` — into exactly that wire, so a differential oracle can construct
the SAME running manifest on both sides: the reference compiles `M` to `IR(M)`,
projects it here, and feeds the wire to the native gate. `replacing=` renders
the withdrawn set of a REPLACEMENT admission, so the wire says exactly what
``compile_files(paths, manifest=IR(M), replacing=R)`` says on the reference
side. The handoff row (``C=k:T``) is the one kind still deferred, behind the
self-host type layer.

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

SHARED_REALM = ""

#: The header row asserting that the `:S` rows beside it are the WHOLE running
#: service set. Without it a reader knows nothing about the running services.
SERVICES_HEADER = "!services"

#: The leading marker of a service-declaration row.
SERVICE_MARKER = ":"


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


def _operations(entry: object) -> list[str]:
    """One service's declared operation names, in declaration order. An entry
    with no readable ``methods`` table renders as the empty surface: the IR's
    service table always carries one, and a service with no operation is a
    legal (if idle) declaration."""
    if not isinstance(entry, dict):
        return []
    methods = entry.get("methods")
    if isinstance(methods, dict):
        return list(methods)
    if isinstance(methods, list):
        return [m.get("name", "") for m in methods if isinstance(m, dict)]
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

    `replacing` is the same set ``compile_files``' `replacing=` takes: the
    running components this admission withdraws. A name that is not running
    withdraws nothing, exactly as on the reference side. The components the
    incoming TEXT redeclares are NOT rendered here — the gate derives that
    implicit half from the text itself, as `compile_files` does.

    A route row is a COMPOSITION row — it says what one running component binds
    across which realms — so it sits with its component, after that component's
    requirement rows and ahead of the service block. That keeps the per-component
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
    for entry in _components(ir):
        name = entry.get("name", "")
        isolate = entry.get("isolate") or {}
        for key in entry.get("provides") or []:
            realm = isolate.get(key, SHARED_REALM)
            rows.append(f"{name}/{key}/{realm}")
        for key in entry.get("inject") or []:
            rows.append(_requirement_row(name, key, entry))
        rows.extend(_route_rows(name, entry))
    services = _declared_services(ir)
    if services is not None:
        rows.append(SERVICES_HEADER)
        rows.extend(f"{SERVICE_MARKER}{name}," + ",".join(ops)
                    for name, ops in services)
    rows.extend(f"-{name}" for name in replacing)
    return ";".join(rows)
