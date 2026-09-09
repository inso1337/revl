"""Project a compiled IR's composition manifest onto the ambient-admission wire.

`admit_ambient(src, manifest)` in `selfhost/lower.rvl` (the native gate, item 186
/ issue #86) takes the running composition as a serialised wire, not as an IR:
rows joined by ``;``, the kind of each row set by its leading marker
(docs/design/186-ambient-admission-guarantees.md, "The wire"):

    C/k/r    provision:   component C provides key k in realm r ("" = shared)
    C<k      requirement: component C requires key k
    !halted  header:      the composition is halted; every admission refuses

`manifest_wire(ir)` renders `IR(M)` — the manifest of an already-compiled
composition `M` — into exactly that wire, so a differential oracle can construct
the SAME running manifest on both sides: the reference compiles `M` to `IR(M)`,
projects it here, and feeds the wire to the native gate. Slice 3 lands the
projection over the two landed row kinds (provisions and requirements); the
replacement (`-C`) and handoff (`C=k:T`) rows are the deferred wave.
"""

from __future__ import annotations

SHARED_REALM = ""


def _components(ir: dict) -> list[dict]:
    """The manifest entries of `ir` — accepts the whole IR (reads
    ``ir["manifest"]["components"]``) or a manifest dict (reads its
    ``components``). Entries carry ``provides``/``inject`` as key LISTS and an
    ``isolate`` key->realm map, the shape `_link` builds."""
    manifest = ir.get("manifest", ir) if isinstance(ir, dict) else {}
    comps = manifest.get("components") if isinstance(manifest, dict) else None
    return comps or []


def manifest_wire(ir: dict) -> str:
    """The ambient-admission wire for the composition manifest of `ir`.

    Provision rows come first per component, then requirement rows, components
    in manifest (declaration) order — the node order the gate's G3 union graph
    seeds its DFS from, so a cross-manifest cycle is named identically to the
    single-source composition of the manifest ++ the incoming text.
    """
    rows: list[str] = []
    for entry in _components(ir):
        name = entry.get("name", "")
        isolate = entry.get("isolate") or {}
        for key in entry.get("provides") or []:
            realm = isolate.get(key, SHARED_REALM)
            rows.append(f"{name}/{key}/{realm}")
        for key in entry.get("inject") or []:
            rows.append(f"{name}<{key}")
    return ";".join(rows)
