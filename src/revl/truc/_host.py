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
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib


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
    stays self-identifying (the lock is a list, not a name-keyed object)."""
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
    name = add["name"]
    reg_name = add["registry"]

    manifest = json.loads(toml_manifest(project_dir))
    reg_abs = (manifest.get("registries") or {}).get(reg_name) or manifest.get("registry")

    # fetch is a copy: vendor the registry entry dir verbatim (§5).
    src_dir = pathlib.Path(reg_abs, "components", name)
    dst_dir = pathlib.Path(project_dir, "trucs", name)
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


def _toml_add_truc(project_dir: str, name: str, registry: str) -> None:
    """Append `<name> = { registry = "<registry>" }` under `[trucs]`.

    A minimal, honest string edit — there is no revl TOML serializer, and
    writing one is not truc's job (docs/design/truc-architecture.md §4.3).
    Idempotent: a name already present is left as-is."""
    p = pathlib.Path(project_dir, "truc.toml")
    text = p.read_text(encoding="utf-8")
    line = f'{name} = {{ registry = "{registry}" }}'
    # already present? (a bare `name = ` at a line start under [trucs])
    for existing in text.splitlines():
        if existing.strip().startswith(f"{name} ") or existing.strip().startswith(f"{name}="):
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
    name = plan["name"]

    # un-vendor: the whole registry-entry mirror under trucs/<name>/ (§5).
    import shutil  # noqa: PLC0415 — stdlib, only needed on the rm path

    vendor = pathlib.Path(project_dir, "trucs", name)
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
    as-is (idempotent)."""
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
        if section == "[trucs]" and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == name:
                continue
        out.append(ln)
    new = "\n".join(out)
    if text.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    p.write_text(new, encoding="utf-8")


# -- clock + stdout (emission) ----------------------------------------------

def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def printout(text: str) -> None:
    print(text)
