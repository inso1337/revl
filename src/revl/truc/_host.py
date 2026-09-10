"""Host-side bodies for truc's G8 externs (slice S1).

truc is a revl composition; its `.rvl` components decide *what* to do, and
these functions are the *entire* set of things truc does to the world — the
same list `revl audit` enumerates from `externs.rvl`. They are deliberately
dumb I/O executors: every decision (which trucs to read, whether to admit,
what a refusal means, what to write) is made in revl and arrives here as
already-computed data. In particular `commit_add`/`compose_write` write
nothing when handed an empty plan, so "disk untouched on refusal" is a pure
revl decision (the planner returns an empty plan), not host policy.

The one exception that must live here is the gate itself (`admit`): it calls
`revl.compiler.compile_files(sources, manifest=running)` in-process — the
same call docs/registry.md §4 names as the install step ("fetched source
enters a composition only through compile_files"). truc cannot hold a
different opinion from revl about admissibility: same process, same compiler.

There is a second exception, and it is the same kind of exception: the NAME a
truc is added or removed under. A truc name becomes a directory
(`trucs/<name>`) and a `truc.toml` key, so the jail the whole project rests on
is the name itself — and every other check on the add/rm path (index
membership, source hash, the gate, the planner's empty-guard) is *content*
based, which means `trucs/../../..` is a perfectly well-formed plan for a
perfectly well-formed registry row. `_vendor_dir` is where a name stops being
a string and has to be a directory *inside* `trucs/`, so that is where the
shape is checked. The plan is still the whole decision — the check only
refuses to let a name mean something other than what "this truc" means.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re

#: The names TOML lets us write bare. Anything else is quoted, which is what
#: keeps `pg_database = { registry = "local" }` spelled the way the docs and
#: every hand-written project already spell it.
_TOML_BARE_KEY = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class TrucNameRefusal(ValueError):
    """A name that cannot name a directory inside the project's `trucs/`.

    A `ValueError` rather than a silent no-op because there is nothing sensible
    to do with `../../etc`: the name never meant a truc, so the only honest
    answer is to leave the disk alone and say why. Nothing on the add/rm path
    duplicates this check, which is exactly why it is here (and not only in the
    planner, which a caller can bypass): vendoring or deleting
    `trucs/<name>` is the write, so the write is where the shape is enforced.
    """


def _name_refusal(name: object, what: str = "truc") -> str:
    """Why `name` cannot be a truc (or registry) name, or "" when it can. Never
    raises, so a caller that wants to REPORT rather than refuse can reuse it.

    The rule is the registry's own (`registry._unsafe_name_reason`), called
    rather than copied: a truc name IS a component name — a truc is a component
    vendored into a project — so a second, stricter grammar here would silently
    refuse a component the registry legitimately published, and a second, looser
    one would let `trucs/<name>` mean something the registry would never have
    written. Both grammars are "one path segment in a flat namespace", so
    reusing the registry's is also what makes the reading side safe: the path
    this check guards is one the same rule produced.
    """
    if not isinstance(name, str) or not name:
        return f"a {what} name must be a non-empty string, not {name!r}"
    from ..registry import _unsafe_name_reason  # noqa: PLC0415, the registry's rule verbatim
    reason = _unsafe_name_reason(name)
    if not reason:
        return ""
    if what != "truc":
        return (f"refusing the {what} name {name!r}: {reason}. A {what} name is "
                f"one key in `[registries]` of `truc.toml`.")
    return (
        f"refusing the truc name {name!r}: {reason}. A truc name is one "
        f"directory under the project's `trucs/` and one key in `truc.toml`, "
        f"so `trucs/{name}` is not a directory this project owns — and a truc "
        f"is only ever a truc inside it.")


def _check_name(name: object, what: str = "truc") -> str:
    """`name`, or raise `TrucNameRefusal`. Called before ANY path is derived
    from a name, on both the reading and the writing side."""
    reason = _name_refusal(name, what)
    if reason:
        raise TrucNameRefusal(reason)
    return name  # type: ignore[return-value] — narrowed by the check above


def _vendor_dir(project_dir: str, name: str) -> pathlib.Path:
    """The one directory `name` is allowed to name: `<project>/trucs/<name>`.

    Two checks, in the order the jail needs them: the name must be one plain
    segment (above), and neither it nor `trucs/` itself may be a symlink. The
    link check exists for the same reason `_launcher._contained_path` walks
    every segment before compiling a stage-0 component — `mkdir(exist_ok=True)`
    and `write_text` both FOLLOW a link, so `trucs/evil -> /etc` would turn
    "vendor a truc" into a write outside the project, and `rmtree` would at
    best die on it. The trust claim is that the bytes truc owns are the bytes
    under `trucs/`; a link is bytes truc does not own.
    """
    _check_name(name)
    root = pathlib.Path(project_dir, "trucs")
    for rel, path in (("trucs", root), (f"trucs/{name}", root / name)):
        if path.is_symlink():
            raise TrucNameRefusal(
                f"refusing to touch `{rel}`: it is a symlink to "
                f"{os.readlink(path)!r}, and a truc's bytes are the ones under "
                f"the project's own `trucs/` directory. Remove the link (or "
                f"vendor the truc into the project) and re-run.")
    return root / name


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def exists(path: str) -> bool:
    return os.path.exists(path)


def wiring(manifest_json: str) -> str:
    """Reshape the gate's composition manifest (admit_all's `manifest`) into
    wiring the pure Planner can name: `{provided: [key...], needs: [{name,
    key}...]}`. The raw manifest rows carry `provides`/`inject`, but `provides`
    (and `requires`) are revl reserved words and cannot be parsed into a record
    field — so this mechanical rename is the only way the brain can read the
    gate's own output. No effect, no decision: `rm` makes the call about an
    unmet requirement in the Planner, over exactly this data."""
    mf = json.loads(manifest_json) if manifest_json else {}
    provided: list[str] = []
    needs: list[dict[str, str]] = []
    for comp in mf.get("components") or []:
        for key in comp.get("provides") or []:
            provided.append(key)
        for key in comp.get("inject") or []:
            needs.append({"name": comp.get("name", ""), "key": key})
    return json.dumps({"provided": provided, "needs": needs})


# -- registry read path (emission[registry]) --------------------------------

def index_read(registry: str) -> str:
    """The registry index verbatim (docs/registry.md §1: the local registry
    is a directory; the index is a file)."""
    return pathlib.Path(registry, "index.json").read_text(encoding="utf-8")


def index_row(registry: str, name: str) -> str:
    """One component's row from the registry index, flattened for the pure
    planner (which navigates a list/record cleanly but has no Map `.get`).
    "" when the name is not in the index — the planner reads that as "unknown
    component" and refuses. `name` is echoed into the row so a list of rows
    stays self-identifying (the lock is a list, not a name-keyed object).

    The name is checked before it is used as a key AND before `entry_read` /
    `commit_add` are ever reached with it: `components/<name>` is a path, and a
    registry row naming a path is not the row a truc could be vendored from.
    """
    _check_name(name)
    idx = json.loads(pathlib.Path(registry, "index.json").read_text(encoding="utf-8"))
    row = (idx.get("components") or {}).get(name)
    if row is None:
        return ""
    out = dict(row)
    out["name"] = name
    out["indexVersion"] = idx.get("indexVersion", "0")
    return json.dumps(out)


def entry_read(registry: str, name: str) -> str:
    """One registry entry, bundled as JSON: `component.rvl` + `manifest.json`
    (+ `dossier.json` when present). A truc *is* this triple, vendored."""
    _check_name(name)
    base = pathlib.Path(registry, "components", name)
    src = base / "component.rvl"
    if not src.exists():
        # Not an error to raise here: the planner guards on the empty index row
        # first and refuses "unknown component" before touching this. Returning
        # empty keeps the boundary total (a host body never crashes the loop).
        return json.dumps({"name": name, "source": "", "manifest": ""})
    out = {
        "name": name,
        "source": src.read_text(encoding="utf-8"),
        "manifest": (base / "manifest.json").read_text(encoding="utf-8"),
    }
    dossier = base / "dossier.json"
    if dossier.exists():
        out["dossier"] = dossier.read_text(encoding="utf-8")
    return json.dumps(out)


# -- filesystem (emission[fs]; scope = the project dir) ---------------------

def toml_manifest(project_dir: str) -> str:
    """`truc.toml` parsed (host owns TOML — there is no revl TOML parser) and
    flattened to the JSON shape the pure planner reads: the registry paths
    resolved to absolute, the `[trucs]` table flattened to an ordered list."""
    import tomllib  # noqa: PLC0415 — stdlib, py3.11+

    p = pathlib.Path(project_dir, "truc.toml")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assembly = data.get("assembly") or {}
    registries = data.get("registries") or {}
    resolved: dict[str, str] = {}
    for rname, r in registries.items():
        path = (r or {}).get("path")
        if path:
            resolved[rname] = os.path.abspath(os.path.join(project_dir, path))
        else:
            resolved[rname] = (r or {}).get("url", "")
    trucs = [
        {"name": tname, "registry": (t or {}).get("registry", "local")}
        for tname, t in (data.get("trucs") or {}).items()
    ]
    default_reg = resolved.get("local") or next(iter(resolved.values()), "")
    return json.dumps({
        "name": assembly.get("name", ""),
        "entry": [os.path.abspath(os.path.join(project_dir, e))
                  for e in (assembly.get("entry") or [])],
        "registries": resolved,
        "registry": default_reg,
        "trucs": trucs,
    })


def read_file(path: str) -> str:
    """A single file's text, or "" when absent (a fresh project has no lock)."""
    p = pathlib.Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_sources(project_dir: str, spec_json: str) -> str:
    """Gather the composition's source files for admission/composition.

    `spec` names the entry files (absolute) and the vendored truc names; the
    loop is host-side because a provide method cannot iterate. The decision of
    *which* names to read was made by the pure planner and arrives in `spec`.
    Returns `{sources: {abspath: text}, vendored: [{name, path, source}]}`.
    """
    spec = json.loads(spec_json)
    entry = []
    for e in spec.get("entry") or []:
        ap = os.path.abspath(e)
        txt = pathlib.Path(ap).read_text(encoding="utf-8")
        entry.append({"path": ap, "source": txt})
    vendored = []
    for name in spec.get("trucs") or []:
        _check_name(name)
        ap = os.path.abspath(os.path.join(project_dir, "trucs", name, "component.rvl"))
        src = pathlib.Path(ap).read_text(encoding="utf-8")
        vendored.append({"name": name, "path": ap, "source": src})
    return json.dumps({"entry": entry, "vendored": vendored})


# -- THE GATE (emission[gate]) ----------------------------------------------

def admit_all(ordered_json: str) -> str:
    """Admit an ordered list of `{path, source, name}` through revl's own gate,
    *incrementally*, exactly as docs/design/truc-architecture.md §3.2 describes:
    each candidate is linked against the running composition
    (`compile_files(files, manifest=running)`) so ambient services are in scope
    without redeclaration and G2/G3 span everything admitted so far. The first
    candidate that breaks the assembly stops the loop and its diagnostic (with
    the why-trace `str()` already carries) is the refusal.

    The gate is unchanged — this is the same `compile_files` the whole system
    passes. Only the *stepping* runs here, because a revl provide method cannot
    iterate; the resolution *order* was decided by the pure planner and arrives
    in `ordered_json`. truc cannot hold a different opinion from revl about
    admissibility: same process, same compiler, same version.
    """
    from revl.compiler import compile_files  # noqa: PLC0415 — in-process gate
    from revl.errors import RevlError  # noqa: PLC0415

    items = json.loads(ordered_json)
    running: dict | None = None
    admitted: list[str] = []
    for item in items:
        path = item["path"]
        label = item.get("name") or path
        try:
            ir = compile_files([path], manifest=running, sources={path: item["source"]})
            running = {"manifest": ir.get("manifest") or {},
                       "services": ir.get("services") or {}}
            admitted.append(label)
        except RevlError as error:
            return json.dumps({
                "ok": False,
                "diagnostic": str(error),
                "failed": label,
                "admitted": admitted,
                "manifest": (running or {}).get("manifest", {}),
            })
    return json.dumps({
        "ok": True,
        "diagnostic": "",
        "failed": "",
        "admitted": admitted,
        "manifest": (running or {}).get("manifest", {}),
    })


def write_assembly(project_dir: str, manifest_json: str) -> str:
    """Write the admitted composition manifest to `build/assembly.json` — what
    a reviewer reads to see the wiring the gate accepted. An empty
    `manifest_json` is a no-op: on a refusal the planner passes "", so `build/`
    is untouched (all-or-nothing)."""
    if not manifest_json:
        return "skipped"
    out = pathlib.Path(project_dir, "build")
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads(manifest_json)
    (out / "assembly.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "written"


# -- composition layers: truc's distribution front doors (426 S6) -----------

def _entry_composition(project_dir: str) -> str:
    """The composition document a truc project applies: the single entry file
    named in truc.toml `[assembly].entry`.

    426 decision 7 (§7): distribution is truc's and SEMANTICS are the
    composition's. truc owns *which* file this is, the vendored trucs beside it
    and the truc.lock that pins them; `revl.composition` owns resolution and
    admission. Both `apply` and `stack check` hand the entry document to the
    composition engine and report its verdict verbatim — truc holds no opinion
    about layer resolution that revl does not (the same premise `admit` rests
    on: same process, same compiler)."""
    manifest = json.loads(toml_manifest(project_dir))
    entry = manifest.get("entry") or []
    if not entry:
        raise ValueError("truc.toml [assembly].entry names no composition "
                         "document to apply")
    return entry[0]


def _render_stack(table: object) -> str:
    """A header-only rendering of a resolved row table: the rows with their
    provenance trail and the wiring, no component body lowered (426 §3.3). The
    format follows `revl layer check`; truc adds only the leading line naming
    the composition it resolved."""
    lines = [f"truc: stack resolves — {table.name} "  # type: ignore[attr-defined]
             f"(origin `{table.origin}`, {len(table.rows)} rows)",
             "ROWS"]
    for row in table.rows:  # type: ignore[attr-defined]
        lines.append(f"  {row.qualified:<24} {row.component}  ({row.source})")
        if any(level for level, _, _ in row.provenance):
            trail = " -> ".join(f"{op} by `{layer}` (L{level})"
                                for level, layer, op in row.provenance)
            lines.append(f"  {'':<24}   {trail}")
    lines.append("WIRING")
    for label, edges in table.wiring().items():  # type: ignore[attr-defined]
        claims = ", ".join(edges["claims"]) or "nothing"
        needs = ", ".join(f"`{k}`" for k in edges["requires"]) or "nothing"
        lines.append(f"  {label:<24} claims {claims}; requires {needs}")
    lines.append("RESOLVED     header-only: no component body was lowered "
                 "(run 'truc apply' to admit the rows)")
    return "\n".join(lines)


def stack_check(project_dir: str) -> str:
    """`truc stack check` — resolve the entry composition's declared layer
    stack HEADER-ONLY and report a collision before anything is admitted
    (426 §8, S6: "report a collision at edit time before anything is fetched").

    The pure fold never calls the gate (§3.3), so this can only over-refuse: a
    peer conflict (exit test 6), an address that resolves to nothing (exit test
    5), the vendored-dir jail (exit test 15) and the mandatory truc.lock pin
    (exit test 17) are the resolution-time refusals, each naming the layer. A
    clean stack prints the resolved wiring and writes nothing. The report shape
    is the planner's `Report` {code, message, sources, commit}."""
    from revl.composition import resolve_file  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    try:
        path = _entry_composition(project_dir)
    except (OSError, ValueError) as error:
        return json.dumps({"code": 1, "message": f"truc: {error}",
                           "sources": "", "commit": ""})
    try:
        table = resolve_file(path, os.path.abspath(project_dir))
    except RevlError as error:
        return json.dumps({
            "code": 1,
            "message": f"truc: refused — the layer stack does not resolve:\n"
                       f"{error}",
            "sources": "", "commit": ""})
    return json.dumps({"code": 0, "message": _render_stack(table),
                       "sources": "", "commit": ""})


def apply(project_dir: str, trust_host_code: bool) -> str:
    """`truc apply` — resolve the entry composition's layer stack and ADMIT it
    (426 §8, S6). Every gate fires inside `revl.composition`, unchanged: the
    mandatory truc.lock pin (exit test 17) and the vendored-dir jail (exit test
    15) at resolution, and the untrusted-author confinement profile (exit test
    13, CRITICAL A) at admission, so a stack layer's declared-`pure` `@py` body
    that would exfiltrate has no reachable spelling. A layer shipping a host
    body is refused by default; `--trust-host-code` lifts that (the §8.8 shape
    change), exactly as `revl composition --admit --trust-host-code`.

    All-or-nothing: on a clean admit the applied composition manifest is written
    to build/assembly.json (via the planner's `sources` slot); on any refusal
    the slot is "" and build/ is untouched. truc adds no admission opinion of
    its own — it reports the composition's verdict."""
    from revl.composition import admit_composition  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    try:
        path = _entry_composition(project_dir)
    except (OSError, ValueError) as error:
        return json.dumps({"code": 1, "message": f"truc: {error}",
                           "sources": "", "commit": ""})
    thc = bool(trust_host_code)
    try:
        document = admit_composition(path, os.path.abspath(project_dir),
                                     confine=True, trust_host_code=thc)
    except RevlError as error:
        return json.dumps({
            "code": 1,
            "message": f"truc: refused — the layer stack would not admit:\n"
                       f"{error}",
            "sources": "", "commit": ""})
    manifest = document.get("manifest") or {}
    order = " -> ".join(manifest.get("loadOrder") or [])
    basis = ("CLAIMED (host code trusted by --trust-host-code)" if thc
             else "MEASURED (non-first-party rows confined)")
    msg = (f"truc: applied — the layer stack admitted through the gate "
           f"[{basis}]; load order {order}; wrote build/assembly.json")
    return json.dumps({"code": 0, "message": msg,
                       "sources": json.dumps(manifest), "commit": ""})


def commit_add(project_dir: str, plan_json: str) -> str:
    """Execute an `add` commit plan: vendor the registry entry, write the
    lock row, append the `[trucs]` entry to `truc.toml`.

    An empty plan is a no-op: on a refusal the planner returns "", so nothing
    is vendored and nothing is recorded ("admitted before it joins" is
    literal — a truc that would not join is never written to disk). All the
    file I/O here is mechanical; the *decision* to add arrived as the plan.

    The vendor copy is byte-for-byte from `registry/components/<name>/` (a
    truc *is* a vendored registry entry, §1.1) and the lock row is a verbatim
    projection of the index row (§4.2: copied, never recomputed by truc),
    with the sha256 already re-verified against the fetched bytes by the
    planner before this runs, plus an `admitted` provenance stamp."""
    if not plan_json:
        return "skipped"
    plan = json.loads(plan_json)
    add = plan["lockAdd"]
    name = _check_name(add["name"])
    reg_name = _check_name(add["registry"], "registry")

    manifest = json.loads(toml_manifest(project_dir))
    reg_abs = (manifest.get("registries") or {}).get(reg_name) or manifest.get("registry")

    # fetch is a copy: vendor the registry entry dir verbatim (§5). `_vendor_dir`
    # is the jail: the destination is inside this project's `trucs/`, or the
    # plan does not get to write at all.
    src_dir = pathlib.Path(reg_abs, "components", name)
    dst_dir = _vendor_dir(project_dir, name)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("component.rvl", "manifest.json", "dossier.json"):
        sp = src_dir / fname
        if sp.exists():
            (dst_dir / fname).write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")

    # verbatim projection of the index row into the lock (§4.2).
    row = json.loads(index_row(reg_abs, name))
    index_version = str(row.get("indexVersion", "0"))
    lock_row = {
        "name": name,
        "registry": reg_name,
        # the registry's declared version, projected verbatim so the project
        # pins WHICH release it admitted, not just which bytes (428 F12). An
        # unversioned entry records "" and stays honestly unpinnable.
        "version": row.get("version", ""),
        "sourceHash": row.get("sourceHash", ""),
        "manifestHash": row.get("manifestHash", ""),
        "provides": row.get("provides") or {},
        "requires": row.get("requires") or {},
        "capabilities": row.get("capabilities") or [],
        "emissions": row.get("emissions", 0),
        "admitted": {"at": now(), "indexVersion": index_version},
    }
    lock_path = pathlib.Path(project_dir, "truc.lock")
    if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    else:
        lock = {"lockVersion": 0, "registryIndexVersion": index_version, "trucs": []}
    lock["trucs"] = [r for r in (lock.get("trucs") or [])
                     if r.get("name") != name] + [lock_row]
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")

    _toml_add_truc(project_dir, name, reg_name)
    return "committed"


#: TOML's single-character basic-string escapes, and their inverses. Only these
#: are spelled out; every other character TOML forbids raw (the rest of C0 and
#: DEL) is written `\uXXXX`, and everything else is written verbatim.
_TOML_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
                 "\r": "\\r", '"': '\\"', "\\": "\\\\"}
_TOML_UNESCAPES = {escaped[1:]: raw for raw, escaped in _TOML_ESCAPES.items()}


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string, escapes and all.

    Written out rather than borrowed from `json.dumps`, which is a *near* miss:
    JSON and TOML agree on every character except the astral planes, where JSON
    emits a surrogate pair (`\\ud83d\\ude00`) and TOML forbids the escape
    outright. An emoji in a name is enough to make the difference load-bearing,
    and the failure mode is the one this file exists to prevent — a
    `truc.toml` that no longer parses.
    """
    out: list[str] = []
    for ch in text:
        escaped = _TOML_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_unstring(literal: str) -> str:
    """The text a TOML basic string was written from — `\\uXXXX`, `\\n` and the
    rest read back. Tolerant on purpose: this reads a `truc.toml` the project
    does not fully control, so an escape it did not write is kept verbatim
    rather than raised on, and no input can make it fall over."""
    body = literal[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out.append(ch)
            i += 1
            continue
        marker = body[i + 1]
        if marker in _TOML_UNESCAPES:
            out.append(_TOML_UNESCAPES[marker])
            i += 2
        elif marker in ("u", "U"):
            width = 4 if marker == "u" else 8
            digits = body[i + 2:i + 2 + width]
            if len(digits) != width:
                out.append(ch)
                i += 1
                continue
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                out.append(body[i:i + 2 + width])
            i += 2 + width
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _toml_key(name: str) -> str:
    """`name` spelled as a TOML key: bare when it can be, quoted when it must.

    A bare TOML key admits `A-Za-z0-9_-` and nothing else, so a name carrying a
    dot — legal as a component name, illegal bare — is written quoted. Quoting
    is not cosmetic: interpolating a name bare is what produced a `truc.toml`
    that no longer parses, which bricked the project — neither `assemble` nor
    even the remediating `rm` could read it back.
    """
    return name if _TOML_BARE_KEY.match(name) else _toml_string(name)


def _toml_key_token(text: str) -> str:
    """The key text at the head of a `key = value` line — quotes included — or
    "" when the line is not an assignment.

    A quoted key is delimited by its CLOSING quote rather than by the first `=`,
    because a quoted key is allowed to contain both an `=` and the other quote
    character. Splitting on `=` instead is what makes a name like `a=b` an
    un-removable key: the line reads `"a=b" = { … }`, the split yields `"a`, and
    the `rm` that should drop the key never recognizes it."""
    if text[:1] == "#":
        # A comment is not an assignment, however much it reads like one. A
        # commented-out `# pg_database = 1` would otherwise report the key
        # `# pg_database` — a name truc never wrote, and one that shadows a real
        # `# pg_database` truc, whose key IS spellable and so is quoted.
        return ""
    if text[:1] in ("'", '"'):
        quote = text[0]
        i = 1
        while i < len(text):
            if quote == '"' and text[i] == "\\":
                i += 2  # an escaped character inside a basic string
                continue
            if text[i] == quote:
                break
            i += 1
        else:
            return ""  # unterminated: not an assignment we can read
        if text[i + 1:].lstrip()[:1] != "=":
            return ""
        return text[:i + 1]
    head = text.split("=", 1)
    return head[0].strip() if len(head) == 2 else ""


def _toml_key_of(line: str) -> str:
    """The name of one `key = value` line, unquoted and unescaped. "" when the
    line is not an assignment. Every spelling is read back — bare, basic and
    literal — because a `truc.toml` written by an older truc (or by hand) is
    still a project someone has to be able to `rm` their way out of."""
    token = _toml_key_token(line.strip())
    if not token:
        return ""
    if token[0] == '"':
        return _toml_unstring(token)
    if token[0] == "'":
        return token[1:-1]
    return token


def _toml_add_truc(project_dir: str, name: str, registry: str) -> None:
    """Append `<name> = { registry = "<registry>" }` under `[trucs]`.

    A minimal, honest string edit — there is no revl TOML serializer, and
    writing one is not truc's job (docs/design/truc-architecture.md §4.3).
    Idempotent: a name already present under `[trucs]` is left as-is. Both the
    key and the registry VALUE are escaped by `_toml_key`/`_toml_string` rather
    than interpolated raw: this runs after the write to `trucs/` and after the
    lock row, so a name that came out of here as invalid TOML would leave the
    project half-updated and unreadable — the failure mode is not "the edit
    looked wrong", it is "no `truc` verb can read truc.toml any more"."""
    p = pathlib.Path(project_dir, "truc.toml")
    text = p.read_text(encoding="utf-8")
    line = f'{_toml_key(name)} = {{ registry = {_toml_string(registry)} }}'
    # already present? (the name as a key under [trucs], in either spelling)
    section = ""
    for existing in text.splitlines():
        stripped = existing.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section == "[trucs]" and _toml_key_of(stripped) == name:
            return
    if "[trucs]" in text:
        lines = text.splitlines()
        out: list[str] = []
        for ln in lines:
            out.append(ln)
            if ln.strip() == "[trucs]":
                out.append(line)
        new = "\n".join(out)
        if text.endswith("\n"):
            new += "\n"
        text = new
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n[trucs]\n{line}\n"
    p.write_text(text, encoding="utf-8")


def commit_rm(project_dir: str, plan_json: str) -> str:
    """Execute a `rm` commit plan: un-vendor `trucs/<name>/`, drop the lock
    row, remove the `[trucs]` entry from `truc.toml`.

    The exact inverse of `commit_add`, and guarded the same way: an empty plan
    is a no-op, so a `rm` the planner refused (name absent, remainder would
    not admit, or removal strands a consumer) leaves the disk untouched —
    "disk untouched on refusal" is the pure planner's decision, not host
    policy. The *decision* to remove arrived as the plan; the file I/O here is
    mechanical."""
    if not plan_json:
        return "skipped"
    plan = json.loads(plan_json)
    name = _check_name(plan["name"])

    # un-vendor: the whole registry-entry mirror under trucs/<name>/ (§5).
    import shutil  # noqa: PLC0415 — stdlib, only needed on the rm path

    vendor = _vendor_dir(project_dir, name)
    if vendor.exists():
        shutil.rmtree(vendor)

    # drop the lock row (the pin goes with the bytes it pinned).
    lock_path = pathlib.Path(project_dir, "truc.lock")
    if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["trucs"] = [r for r in (lock.get("trucs") or [])
                         if r.get("name") != name]
        lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")

    _toml_rm_truc(project_dir, name)
    return "removed"


def _toml_rm_truc(project_dir: str, name: str) -> None:
    """Remove `<name> = { … }` from under `[trucs]` in `truc.toml`.

    The inverse of `_toml_add_truc`: a minimal, honest string edit (there is no
    revl TOML serializer, §4.3). Scoped to the `[trucs]` section and matched on
    the exact key so a name that is a prefix of another — or a same-named key in
    a different table — is never touched. A name that is not present is left
    as-is (idempotent). The key is unquoted before it is compared, so the
    `rm` of a dotted name finds the quoted key its `add` wrote."""
    p = pathlib.Path(project_dir, "truc.toml")
    text = p.read_text(encoding="utf-8")
    out: list[str] = []
    section = ""
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            out.append(ln)
            continue
        if section == "[trucs]" and _toml_key_of(stripped) == name:
            continue
        out.append(ln)
    new = "\n".join(out)
    if text.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    p.write_text(new, encoding="utf-8")


# -- ship / publish (emission[registry]; slice S4) --------------------------

def _ship_toml(project_dir: str) -> dict:
    """`truc.toml` parsed to the [assembly] + [ship] + [registries] a ship
    needs. Host owns TOML (there is no revl TOML parser)."""
    import tomllib  # noqa: PLC0415 — stdlib, py3.11+

    p = pathlib.Path(project_dir, "truc.toml")
    return tomllib.loads(p.read_text(encoding="utf-8"))


def _read_dossier_facts(path: pathlib.Path) -> tuple[bool, str, str]:
    """The (present, verdict, lifecycle) of a supplied gauntlet dossier — the
    author's proof they ran the proving ground (item 31). Absent or unreadable
    reads as "no evidence supplied"; ship re-runs the gauntlet to VERIFY these
    against the current source before trusting them."""
    if not path.exists():
        return (False, "", "")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (False, "", "")
    verdict = str(d.get("verdict", ""))
    lifecycle = str((((d.get("tested") or {}).get("lifecycle")) or {}).get("status", ""))
    return (True, verdict, lifecycle)


def _registry_policy(registry_dir: str) -> str:
    """A registry declares its ship policy in a registry-side `policy.json`
    (`{"ship": "gauntlet" | "audit" | "none"}`) — separate from index.json so
    build_index / registry.verify never touch it. The official registry sets
    "gauntlet" (audit + gauntlet evidence); additional registries set their
    own. Absent → "audit": a component must at least compile clean to publish,
    the safe middle (docs/design/truc-architecture.md decision §10.5)."""
    p = pathlib.Path(registry_dir, "policy.json")
    if not p.exists():
        return "audit"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "audit"
    policy = str(data.get("ship", "audit"))
    return policy if policy in ("gauntlet", "audit", "none") else "audit"


def _ship_target(project_dir: str) -> tuple[dict, str, str, str]:
    """The `[ship]` table, the source being shipped, and the target registry
    (name + absolute path). Shared by `ship_context` and `release_facts` so the
    two can never disagree about WHICH registry or WHICH source is in play."""
    data = _ship_toml(project_dir)
    assembly = data.get("assembly") or {}
    ship = data.get("ship") or {}
    registries = data.get("registries") or {}

    # the component being shipped is the project's own entry, verbatim.
    entry = assembly.get("entry") or []
    source = ""
    if entry:
        src_path = pathlib.Path(project_dir, entry[0])
        if src_path.exists():
            source = src_path.read_text(encoding="utf-8")

    # the target registry: [ship].registry, else the conventional "local", else
    # the first declared registry.
    reg_name = ship.get("registry") or ("local" if "local" in registries
                                        else next(iter(registries), "local"))
    reg = registries.get(reg_name) or {}
    reg_path = reg.get("path")
    reg_abs = (os.path.abspath(os.path.join(project_dir, reg_path))
               if reg_path else reg.get("url", ""))
    return ship, source, reg_name, reg_abs


def release_facts(project_dir: str, name: str) -> str:
    """The registry's own verdict on publishing `name` from this project, in the
    shape the pure Shipper already reads a gate verdict in (`{ok, diagnostic}`).

    Registry semantics belong to the registry: whether a name already published
    may be REPUBLISHED, whether the declared release follows the one it
    replaces, and whether it satisfies the bump item 64 computes from the
    interface diff are all `revl.registry.release_facts`. truc cannot hold a
    different opinion from revl about that any more than it can about admission
    (`admit_all`) — same process, same module, same version.

    The publisher's declarations come from `[ship]`: `version` (the release this
    is), `version_scheme` (`semver`, the default, or `opaque` to publish
    date/build-id versions with the bump check recorded UNVERIFIED), and
    `publisher` (the label whose continuity an update must preserve).
    """
    from revl.registry import (  # noqa: PLC0415 — the registry owns this rule
        BUMP_UNVERIFIABLE, SCHEME_SEMVER)
    from revl.registry import release_facts as registry_release_facts

    ship, source, _reg_name, reg_abs = _ship_target(project_dir)
    version = str(ship.get("version", "") or "")
    scheme = str(ship.get("version_scheme", SCHEME_SEMVER) or SCHEME_SEMVER)
    publisher = str(ship.get("publisher", "") or "")

    if not reg_abs or not os.path.isdir(reg_abs):
        # no registry on disk yet: nothing is published, so nothing is being
        # replaced. `publish` creates it, exactly as it did before.
        return json.dumps({"ok": True, "diagnostic": "", "version": version,
                           "scheme": scheme, "publisher": publisher,
                           "previousVersion": "", "note": ""})

    facts = registry_release_facts(reg_abs, name, source, version,
                                   scheme=scheme, publisher=publisher)
    note = ""
    if not facts["isUpdate"] and not version:
        note = (" Published UNVERSIONED: nothing pins which release this is, and "
                "an unversioned entry can never be updated (there is no release "
                "to bump from). Declare `[ship] version` in truc.toml.")
    elif facts["bumpCheck"] == BUMP_UNVERIFIABLE and not facts["refusals"]:
        note = (f" Bump NOT VERIFIED ({facts['bumpCheckReason']}); the release "
                "records it as unverified for every consumer to see.")
    return json.dumps({
        "ok": not facts["refusals"],
        "diagnostic": "; ".join(facts["refusals"]),
        "version": version,
        "scheme": scheme,
        "publisher": publisher,
        "previousVersion": facts["previousVersion"],
        "note": note,
    })


def ship_context(project_dir: str) -> str:
    """Everything the pure Shipper decides from: the component source (the
    project's own entry, verbatim), its author-supplied discoverability
    (description + tags), the TARGET registry (name + absolute path) and its
    declared evidence policy, and the facts of any supplied gauntlet dossier.

    Whether the NAME is free, and what may replace it if it is not, is not here:
    that is registry semantics and it arrives through `release_facts`.
    """
    ship, source, reg_name, reg_abs = _ship_target(project_dir)

    policy = _registry_policy(reg_abs) if reg_abs and os.path.isdir(reg_abs) else "audit"

    # supplied evidence: [ship].evidence names the dossier the author produced.
    present, verdict, lifecycle = (False, "", "")
    ev_rel = ship.get("evidence")
    if ev_rel:
        present, verdict, lifecycle = _read_dossier_facts(
            pathlib.Path(project_dir, ev_rel))

    tags = ship.get("tags") or []
    return json.dumps({
        "source": source,
        "description": str(ship.get("description", "")),
        "tags": [str(t) for t in tags],
        "registryName": reg_name,
        "registryPath": reg_abs,
        "policy": policy,
        "evidencePresent": present,
        "evidenceVerdict": verdict,
        "evidenceLifecycle": lifecycle,
    })


def gauntlet_evidence(source: str) -> str:
    """Run the proving ground (item 31, `revl.mcp.gauntlet`) cold on the shipped
    source and return the facts the pure Shipper checks, plus the full dossier
    text to stamp. Isolated: the gauntlet boots the candidate in a throwaway
    Session and grades it — it never raises, so a broken source comes back as
    verdict "rejected", not an exception. This is the re-run that VERIFIES the
    author's supplied evidence is current for the source being shipped."""
    import concurrent.futures  # noqa: PLC0415

    from revl.mcp import gauntlet  # noqa: PLC0415 — in-process, like the gate
    from revl.mcp.session import Session  # noqa: PLC0415

    # The gauntlet boots the candidate in a scratch Session, which drives its
    # own asyncio loop. ship runs this from *inside* truc's own running Session
    # loop, so the scratch boot must happen on a thread with no running loop —
    # otherwise asyncio refuses ("another loop is running"). One worker thread,
    # joined synchronously: the extern stays a plain call from revl's view.
    def _grade() -> dict:
        # truc runs on the operator's own machine over the package it is
        # shipping: the human is the author, so no MCP authoring trust applies
        # (see `server.compile_under_authoring`).
        return gauntlet.run(Session(), {"source": source},
                            over_the_transport=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        dossier = pool.submit(_grade).result()
    lifecycle = ((dossier.get("tested") or {}).get("lifecycle")) or {}
    admission = ((dossier.get("proved") or {}).get("admission")) or {}
    return json.dumps({
        "verdict": str(dossier.get("verdict", "")),
        "lifecycle": str(lifecycle.get("status", "")),
        "admission": str(admission.get("status", "")),
        "dossierText": json.dumps(dossier, indent=2, sort_keys=True) + "\n",
    })


def publish(plan_json: str) -> str:
    """Execute a publish plan through `revl.registry.publish_release`: freeze the
    release being replaced, install the component, record the declared version,
    REGENERATE the index (manifest.json is produced by the current compiler,
    never copied from the author — the reproducibility invariant does the
    honesty work, docs/registry.md §1), attach the derived changelog, and record
    the discoverability fields the compiler cannot derive (description + tags) in
    the entry's own `meta.json`, from which the regenerated index row copies them
    so revl_resolve / registry search can find it. They go in a file rather than
    straight into the row because a key nothing regenerates is a key `verify`
    can never certify (docs/registry.md §1.3).

    The registry re-runs its own release checks here rather than trusting the
    plan, so the write path is not safe only because the Shipper looked first.

    An empty plan is a no-op: on any refusal the Shipper returns "", so the
    registry is untouched (all-or-nothing — the empty-guard, mirroring
    commit_add). This is the ONLY registry-mutating body in truc."""
    if not plan_json:
        return "skipped"
    from revl.registry import publish_release  # noqa: PLC0415 — the write path

    plan = json.loads(plan_json)
    reg = plan["registryPath"]
    pathlib.Path(reg, "components").mkdir(parents=True, exist_ok=True)
    publish_release(
        reg, plan["name"], plan["source"],
        version=plan.get("version") or None,
        scheme=plan.get("scheme") or "semver",
        publisher=plan.get("publisher") or None,
        description=plan.get("description", ""),
        tags=plan.get("tags") or [],
        dossier_text=(plan["dossierText"] if plan.get("stampDossier")
                      and plan.get("dossierText") else None))
    return "published"


# -- clock + stdout (emission) ----------------------------------------------

def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def printout(text: str) -> None:
    print(text)
