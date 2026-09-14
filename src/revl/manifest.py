"""Project a compiled IR's composition manifest onto the ambient-admission wire.

`admit_ambient(src, manifest)` in `selfhost/lower.rvl` (the native gate, item 186
/ issue #86) takes the running composition as a serialised wire, not as an IR:
rows joined by ``;``, the kind of each row set by its leading marker
(docs/design/186-ambient-admission-guarantees.md, "The wire"):

    C/k/r    provision:   component C provides key k in realm r ("" = shared)
    C<k      requirement: component C requires key k in the shared realm
    C<k/r    requirement: the same, resolved in realm r
    C<*k     requirement: the same, multi-realm bound (item 162)
    -C       replacing:   component C is withdrawn by this admission
    !halted  header:      the composition is halted; every admission refuses

`manifest_wire(ir)` renders `IR(M)` — the manifest of an already-compiled
composition `M` — into exactly that wire, so a differential oracle can construct
the SAME running manifest on both sides: the reference compiles `M` to `IR(M)`,
projects it here, and feeds the wire to the native gate. `replacing=` renders
the withdrawn set of a REPLACEMENT admission, so the wire says exactly what
``compile_files(paths, manifest=IR(M), replacing=R)`` says on the reference
side. The handoff row (``C=k:T``) is the one kind still deferred, behind the
self-host type layer.
"""

from __future__ import annotations

from collections.abc import Iterable

SHARED_REALM = ""


def _components(ir: dict) -> list[dict]:
    """The manifest entries of `ir` — accepts the whole IR (reads
    ``ir["manifest"]["components"]``) or a manifest dict (reads its
    ``components``). Entries carry ``provides``/``inject`` as key LISTS, an
    ``isolate`` key->realm map and a ``routes`` key->route map, the shape
    `_link` builds."""
    manifest = ir.get("manifest", ir) if isinstance(ir, dict) else {}
    comps = manifest.get("components") if isinstance(manifest, dict) else None
    return comps or []


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

    Provision rows come first per component, then requirement rows, components
    in manifest (declaration) order — the node order the gate's G3 union graph
    seeds its DFS from, so a cross-manifest cycle is named identically to the
    single-source composition of the manifest ++ the incoming text. The
    withdrawal rows of `replacing` follow, after the composition they act on.

    `replacing` is the same set ``compile_files``' `replacing=` takes: the
    running components this admission withdraws. A name that is not running
    withdraws nothing, exactly as on the reference side. The components the
    incoming TEXT redeclares are NOT rendered here — the gate derives that
    implicit half from the text itself, as `compile_files` does.
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
    rows.extend(f"-{name}" for name in replacing)
    return ";".join(rows)
