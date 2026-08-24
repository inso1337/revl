# truc — technical architecture (design pass, roadmap item 136)

**Status:** design only — nothing here is implemented. Companion doc:
`docs/truc.md` (identity and vocabulary, owned separately). This document is
the technical design: the component decomposition, the host boundary, the
gate-invocation path, the file formats, and the slice plan a multi-agent
fan-out builds from.

**The one-line thesis:** truc is a revl composition that manages revl
components, and its differentiator — every fetched component is admitted
through the G2 gate before it joins the assembly — is not a feature truc
implements but a host extern truc *calls*: `compile_files(files,
manifest=running)` (`src/revl/compiler.py:173`), the same gate everything
else in the system already passes. docs/registry.md §4 states the stance
truc inherits verbatim: *"Install is admission. There is no separate install
step to secure."*

Everything below cites the real machinery it builds on. truc adds **zero**
resolution or compatibility logic: the §5 predicate lives in
`src/revl/admission.py` (`_service_compatible`), the ranking and index in
`src/revl/registry.py`, and truc drives them from the other side of a G8
boundary.

---

## 1. Component decomposition

### 1.1 What a *truc* is, in truc's own vocabulary

A *truc* (a *petit bout* — docs/truc.md owns the naming) is **one registry
entry, vendored**: the triple the registry already defines in
docs/registry.md §1 and materializes under
`registry/components/<name>/{component.rvl, manifest.json, dossier.json}`.
Concretely a truc is:

- `component.rvl` — the source, verbatim;
- `manifest.json` — the item-28 interchange audit document, byte-reproducible
  from the source by the current compiler (`registry.py::_audit_document`);
- `dossier.json` — gauntlet evidence, when the entry has one
  (`RegistryEntry.evidence`, `registry.py:71`).

truc invents no new package format. A truc in `trucs/` is byte-identical to
the registry entry it was fetched from, and the lock proves it by hash
(§4.3). *Assembly* is then just `compile_files` over the project's own
sources plus every vendored `component.rvl` — the composition manifest the
compiler already produces (DESIGN.md §4) **is** the assembled artifact.

### 1.2 The components

truc's own composition — the dogfooding showcase — decomposes into seven
components across two strata: a **pure core** (planning and dispatch logic,
fully testable with `revl test`, no externs reachable) and a **boundary
rim** (thin components whose provide-methods emit through the G8 externs of
§2). The split is deliberate: the audit surface (`revl audit`) of truc
itself should read as "all authority lives in four rim components, the
brain is pure" — the exact property truc will later report about *other*
people's assemblies.

| Component        | provides            | requires (inject)                  | stratum |
|------------------|---------------------|------------------------------------|---------|
| `RegistryClient` | `index: Index`      | —                                  | rim     |
| `Fetcher`        | `fetch: Fetch`      | —                                  | rim     |
| `GateKeeper`     | `gate: Gate`        | —                                  | rim     |
| `Workspace`      | `ws: Workspace`     | —                                  | rim     |
| `Planner`        | `plan: Plan`        | —                                  | pure    |
| `Assembler`      | `asm: Assembler`    | `index`, `fetch`, `gate`, `ws`, `plan` | core |
| `Shipper`        | `ship: Shipper`     | `gate`, `ws`                       | rim+core|
| `CliDispatch`    | `cli: Cli`          | `asm`, `ship`, `ws`                | core    |

Service sketches (shapes, not final syntax — capability labels per
docs/capabilities.md, `emission[cap]`):

```revl
service Index {                                   // over registry/index.json
  emission[registry] fn load(registry: Str) -> Str      // index.json text
}
service Fetch {                                   // one entry, verbatim
  emission[registry] fn entry(registry: Str, name: Str) -> Str  // {source,manifest,dossier?} JSON
}
service Gate {                                    // THE differentiator (§3)
  emission[gate] fn admit(sourcesJson: Str, manifestJson: Str) -> Str  // verdict JSON
}
service Workspace {                               // truc.toml / truc.lock / trucs/
  emission[fs] fn read(path: Str) -> Str
  emission[fs] fn write(path: Str, text: Str)
  fn exists(path: Str) -> Bool
}
service Plan {                                    // PURE — the resolver brain
  fn resolution(tomlJson: Str, lockJson: Str, indexJson: Str) -> Str  // plan JSON
  fn lock_row(name: Str, indexRowJson: Str) -> Str
  fn drift(lockJson: Str, vendoredHashesJson: Str) -> Str   // hash mismatches
}
service Assembler {
  emission[registry, gate, fs] fn add(name: Str) -> Str     // report JSON
  emission[fs] fn rm(name: Str) -> Str
  emission[gate, fs] fn assemble() -> Str
}
service Cli {
  emission[registry, gate, fs] fn run(argv: List[Str]) -> Int
}
```

The provide/inject graph (arrows are `inject`):

```
CliDispatch ──► Assembler ──► RegistryClient
     │              ├───────► Fetcher
     │              ├───────► GateKeeper
     │              ├───────► Workspace
     │              └───────► Planner        (pure)
     └────────► Shipper ────► GateKeeper, Workspace
```

`Planner` is the component to be proudest of: resolution order, lock-row
construction, hash-drift detection are pure functions over JSON values
(`stdlib/json.rvl` `json_parse`/`json_stringify` — py and ts bodies exist
today), so `revl test` covers the whole brain with no mocks and no
emissions. Structured values cross service boundaries as `Str` JSON in v1
(the same choice `stdlib/json.rvl` documents for tool wires); a
record-typed surface is a later refinement (open decision §10.2).

## 2. The G8 host boundary

truc's externs are the *entire* list of things truc can do to the world.
This is the showcase inverted: run `revl audit` on truc and the report
enumerates exactly these, with capability labels. Precedent for the
extern-body mechanism: `examples/uxprobe2_jobs.rvl` (extern
pure/acquire/emission fns with `@py` bodies) and `stdlib/json.rvl`
(multi-tier bodies).

**Tier: py bodies only in v1.** The bodies call straight into the `revl`
package in-process (§3), and `revl run --backend py` boots in-process
(`src/revl/__main__.py:1679`, `src/revl/run.py::_Driver`) — no bridge seam,
no subprocess. Other tiers gain bodies only if truc is ever wanted
standalone-compiled (non-goal for now, §9).

```revl sketch
// -- hashing (pure: same classification as sha256_hex in examples/uxprobe2_jobs.rvl:11)
extern pure fn sha256_hex(data: Str) -> Str

// -- registry read path (emission[registry])
extern emission[registry] fn host_index_read(registry: Str) -> Str
  // @py: (Path(registry)/"index.json").read_text() — src/revl/registry.py INDEX_FILENAME
extern emission[registry] fn host_entry_read(registry: Str, name: Str) -> Str
  // @py: read component.rvl + manifest.json (+dossier.json) under
  //      registry/components/<name>/, return one JSON object

// -- filesystem (emission[fs]; scope = the project dir: truc.toml, truc.lock, trucs/)
extern pure       fn host_exists(path: Str) -> Bool
extern emission[fs] fn host_read(path: Str) -> Str
extern emission[fs] fn host_write(path: Str, text: Str)
extern emission[fs] fn host_toml_read(path: Str) -> Str   // tomllib.loads -> JSON text (§4.1)

// -- THE GATE (emission[gate]) — see §3
extern emission[gate] fn host_admit(sources_json: Str, manifest_json: Str) -> Str
extern emission[gate] fn host_compose(files_json: Str) -> Str

// -- publish (emission[registry]; ship only, slice 4)
extern emission[registry] fn host_publish(registry: Str, name: Str, source: Str) -> Str
  // @py: write registry/components/<name>/component.rvl, then
  //      revl.registry.build_index(registry) — the regenerate-or-red discipline
```

Notes:

- `host_toml_read` returns **JSON text** (host parses TOML with `tomllib`,
  re-serializes as JSON) so the composition needs only `stdlib/json.rvl` —
  there is no TOML parser in revl and writing one is not truc's job. The
  lock file is JSON precisely so the round-trip *write* path never needs a
  TOML serializer (§4.3).
- No `acquire` externs in v1: truc holds no handles across calls (each
  extern is open-read/write-close in its body), so there is nothing for G4
  undo to track. If a future slice holds a lock file open, that becomes an
  `acquire … undo` pair per `examples/durable_log.rvl:43`.
- The standing G8 caveat (docs/registry.md §4) applies to truc itself:
  these classifications are trusted, not verified — and that is fine,
  because truc's *bodies* are first-party code in this repo, reviewed like
  any other Python.

## 3. How `truc assemble` admits each fetched component (the differentiator)

### 3.1 Decision: in-process, through one extern, into `compile_files`

`host_admit`'s `@py` body calls the gate **in-process**:

```python
# @py body of host_admit (sketch)
import json
from revl.compiler import compile_files
from revl.errors import RevlError
req = json.loads(sources_json)          # {path: source text} — virtual sources
manifest = json.loads(manifest_json) if manifest_json else None
try:
    ir = compile_files(list(req), manifest=manifest, sources=req)
    return json.dumps({"ok": True, "manifest": ir["manifest"],
                       "services": ir.get("services") or {}})
except RevlError as e:
    return json.dumps({"ok": False, "diagnostic": str(e),
                       "why": e.why.to_dict() if getattr(e, "why", None) else None})
```

This is *exactly* the call docs/registry.md §4 names as the install step
("fetched source enters a composition only through `compile_files(files,
manifest=running)`"), and the in-memory `sources` parameter exists for
precisely this use (`src/revl/compiler.py:31` `_ModuleLoader`: "modules
that exist only in memory … before anything touches the disk"). A refusal
arrives as the structured `RevlError` with its why-trace — the same (G2,
"admission") classification every other refusal carries
(`src/revl/admission.py::_drift_error`).

**Rejected alternatives:**

- **Shell out to a `revl` subcommand.** There is no `revl admit` CLI today
  (`src/revl/__main__.py` has compile/audit/plan/apply/run/… but admission
  is an MCP verb), so this would mean building a CLI verb *and* parsing
  diagnostics back out of text, *and* paying a process spawn per candidate.
  All cost, no isolation benefit — admission never executes candidate code
  (docs/registry.md §4: "Resolve executes nothing"), so there is nothing to
  sandbox.
- **Go through MCP (`revl_admit`/`revl_ship`).** The MCP verbs
  (`src/revl/mcp/server.py:894`, `src/revl/mcp/ship.py`) target a *live
  session* — hot-swapping a running composition. truc assembles a *static*
  project on disk; there is no running composition to admit against, and
  standing up a stdio JSON-RPC server to reach a function in the same
  Python process inverts the token-economy logic that motivated `revl_ship`
  in the first place (one intent, one call).

The in-process extern keeps the property that matters for the dogfood
story: *the composition* decides when to admit and what refusal means (that
logic is revl code in `Assembler`), while the gate itself remains the
host's single, already-trusted implementation. truc cannot have a
"different opinion" from `revl` about admissibility — same process, same
`compile_files`, same version.

### 3.2 Data flow: fetched component → admit → compose or refuse

`Assembler.assemble()` admits **incrementally, in resolution order**, so a
refusal names the first candidate that breaks the assembly and G2/G3 span
everything admitted so far:

```
truc.toml + truc.lock ──ws.read──► Planner.resolution ──► ordered [name…]
running := admit(project entry sources, manifest=None)        // step 0
for name in order:
    entry   := trucs/<name>/component.rvl   (ws.read; hash-checked vs lock)
    verdict := gate.admit({<name>: entry.source}, running.manifest)
    ok  → running := verdict (manifest now includes <name>)
    no  → REFUSE: print diagnostic + why-trace, exit 1
          — nothing written, no partial assembly (all-or-nothing,
            same stance as admit_under_policy, src/revl/admission.py:455)
finally: host_compose(all files) → write build/assembly.json
         (the full IR document: what `revl run` / `revl audit` consume)
```

`truc add <name>` runs the same gate *before vendoring*: fetch → admit
against the current assembly → only on ok does anything touch `trucs/` or
the lock (§8.1). A component that would not join is never written to disk —
"admitted before it joins" is literal.

## 4. `truc.toml` + `truc.lock`

### 4.1 `truc.toml` — the intent (human-edited, TOML)

```toml
[assembly]
name    = "myapp"
entry   = ["src/main.rvl"]          # the project's own sources

[registries]
local = { path = "registry" }       # v1: a path. later: url = "https://…"

[trucs]
user_cache  = { registry = "local" }
pg_database = { registry = "local" }
```

No version field in v1 — deliberately. The registry has a single namespace
with first-come names and **no versioning until phase 2** (docs/registry.md
§7, gated on roadmap item 9); a version key that nothing resolves would be
a lie. Identity is pinned by *hash* in the lock instead. When registry
versioning lands, `user_cache = { registry = "local", version = "^1.2" }`
extends the table without breaking existing files.

### 4.2 Mapping to `registry/index.json`

The lock row is a verbatim projection of the index row
(`src/revl/registry.py::_entry_index_row`): `sourceHash`, `manifestHash`,
`capabilities`, `emissions`, `provides`, `requires` are copied, never
recomputed by truc (except `sourceHash`, which truc *re-verifies* against
the vendored bytes — that is the tamper check). `registry/index.json`
already carries every field the lock needs; truc adds only *when* and
*from-where*.

### 4.3 `truc.lock` — the proof (generated, committed, JSON)

JSON, not TOML: truc must *write* it from inside the composition, and
`stdlib/json.rvl` gives revl `json_stringify` today; a TOML serializer does
not exist and is not worth building for a machine-only file.

```json
{
  "lockVersion": 0,
  "registryIndexVersion": "0",
  "trucs": {
    "user_cache": {
      "registry": "local",
      "sourceHash":   "b782e70c…",
      "manifestHash": "d7d185fd…",
      "provides": {"cache": "Cache"},
      "requires": {"db": "Database"},
      "capabilities": ["*"],
      "emissions": 1,
      "admitted": {"at": "2026-08-24T…Z", "indexVersion": "0"}
    }
  }
}
```

- `sourceHash`/`manifestHash` — from the index row; `assemble` recomputes
  sha256 of `trucs/<name>/component.rvl` and refuses on mismatch (drift is
  a refusal, not a warning — the `registry.verify` regenerate-or-red
  discipline, `src/revl/registry.py:194`, applied to the vendor dir).
- `capabilities`/`emissions` — the authority the assembly accepted when it
  admitted this truc. `assemble` re-reports the totals so a lockfile diff
  in review *shows authority growth* — the least-authority ranking of
  `_Match.rank` (`src/revl/registry.py:393`) made reviewable.
- `admitted` — a record that the gate passed, and against which index
  generation. Not a signature (registry phase-2 non-goal); the hash is the
  integrity, this is provenance.

## 5. `trucs/` vendoring layout

```
myapp/
  truc.toml
  truc.lock
  src/main.rvl                # [assembly].entry
  trucs/
    user_cache/
      component.rvl           # byte-identical to the registry entry
      manifest.json
      dossier.json            # when the entry had one
    pg_database/
      component.rvl
      manifest.json
  build/
    assembly.json             # host_compose output (gitignored)
```

One directory per truc, mirroring `registry/components/<name>/` exactly —
a truc *is* a vendored registry entry (§1.1), so fetch is a copy and
verify is a hash. The assembled composition references vendored components
by nothing more clever than **the compile file list**:
`compile_files(entry + sorted(trucs/*/component.rvl))`. Cross-file service
redeclaration is already the compiler's linking model (every registry
entry redeclares `service Database` — see
`registry/components/user_cache/component.rvl` — and `compile_files`
links compositions across files, DESIGN §4); truc leans on it rather than
inventing an import graph. Vendoring (not a cache dir) is the deliberate
choice: the assembly is reviewable and buildable offline, and `truc.lock`
+ hashes make the vendor dir verifiable rather than trusted.

## 6. CLI and dual entry

**The CLI is a revl component; the launcher is a ~40-line Python shim.**
All dispatch, help text, and flow logic live in `CliDispatch`/`Assembler`
(revl); Python does only what revl cannot: be a console script.

The launcher (`src/revl/truc/_launcher.py`):

1. read truc's *own* lock (§7) for the file list of truc's composition;
2. `compile_files(files)`;
3. boot in-process on the py tier and call `cli.run(argv)` — precisely the
   mechanism `src/revl/mcp/session.py` uses (`Session.load` +
   `Session.call(key, method, args)`, the machinery behind `revl_call`,
   `src/revl/mcp/server.py:256`); `revl run --backend py` boots in-process
   the same way (`src/revl/run.py::_Driver`);
4. `sys.exit` with the returned `Int`.

**Dual entry (item 108 tie-in):** `pyproject.toml` gains a second console
script beside the existing one (`pyproject.toml:12` — `revl =
"revl.__main__:main"`):

```toml
[project.scripts]
revl = "revl.__main__:main"
truc = "revl.truc:main"
```

and `revl truc <verb> …` becomes a thin namespaced subcommand in
`src/revl/__main__.py::main` that forwards its tail to `revl.truc.main`
— one implementation, two spellings, zero drift. (The human settled this
as a `revl truc` subcommand group, deliberately *not* flat `revl add`
aliases, so truc's verbs never pollute revl's top-level namespace.) truc's `.rvl` sources
live *inside the package* (`src/revl/truc/components/*.rvl`) so the wheel
ships them for free under hatchling's package build — the exact lesson of
item 108 (docs/v2.0-roadmap.md:3005: the wheel must carry what the runtime
resolves; `src/revl/_paths.py` exists because `backends/` didn't). No new
`_paths`-style resolver needed: the launcher locates its sources relative
to `revl.truc.__file__`.

## 7. Bootstrapping — truc assembles truc

The chicken-and-egg dissolves because **the gate is host-side and always
present**: admitting truc's own components needs only the installed `revl`
package, never a prior truc.

- **Stage 0 (every boot).** The launcher compiles truc's composition from
  the file list in truc's own committed `src/revl/truc/truc.lock`. That
  compile *is* an admission (a cold-start `compile_files` runs G2/G3/A6
  over the whole composition) — so every boot of truc is truc passing its
  own gate. No pre-built artifact, no snapshot to trust.
- **Stage 1 (the fixpoint test) — as shipped (S5).** truc's own source tree
  carries `bootstrap/truc.toml` describing *itself*: truc's eight components
  are the `[assembly].entry`. The dogfood exit test
  (`tests/test_truc_bootstrap.py`): run truc's assemble path on that project
  and assert the regenerated composition is byte-identical to the committed
  `bootstrap/assembly.golden.json` — regenerate-or-red, the same discipline as
  `registry.verify` and the conformance baselines. CI runs it (the gate is
  `compile_files`, frontend Python, so the check runs in `pytest tests/` with
  no runtime and never skips); a truc change that breaks truc's ability to
  assemble truc is a red build, not a discovery.

  **Deviation from the sketch (honest note).** The sketch imagined truc's pure
  components *published to the local registry and vendored under `trucs/`*. In
  practice truc's components cross-import by relative `use` (`assembler.rvl`
  imports the `Plan` service from `planner.rvl`; `workspace.rvl` imports
  `../externs.rvl`), so they are not self-contained single-file registry
  entries the way `registry/components/*/component.rvl` are — vendoring one per
  `trucs/<name>/` dir would break those imports. The faithful expression is
  therefore *entry*, not *trucs/*: truc's components are named as the project's
  own `[assembly].entry`, in place, where their imports resolve. That is exactly
  what a truc project is — `entry` is the composition you author, `trucs/` is
  what you fetch — and truc authors truc. The gate, the incremental admit, and
  the regenerate-or-red pin are all still exercised on truc's own component set;
  only the vendoring dir is moot because truc fetches nothing to assemble
  itself. Rewriting the eight into self-contained registry-entry form (inline
  service redeclaration) so they could round-trip through `add`/`trucs/` is a
  possible later polish, not a requirement of the fixpoint.
- **No stage needs a running registry service** — the local registry is a
  directory (docs/registry.md §1), and stage 0 does not even need that.

## 8. The flows, end to end

### 8.1 `truc add <name>`

```
cli.run(["add", name])
→ asm.add(name)
   ├ index.load(reg)               host_index_read      emission[registry]
   ├ (name ∉ index)                → refuse, exit 1 (with a did-you-mean later)
   ├ fetch.entry(reg, name)        host_entry_read      emission[registry]
   ├ sha256_hex(source) vs index row sourceHash         pure
   │    mismatch → refuse ("registry entry disagrees with its own index")
   ├ current assembly manifest := gate.admit(entry sources ∪ vendored, …)
   ├ gate.admit({name: source}, current)                emission[gate]
   │    drift/G2 refusal → print diagnostic + why-trace, exit 1, disk untouched
   ├ ws.write(trucs/<name>/component.rvl, manifest.json[, dossier.json])
   ├ plan.lock_row(name, indexRow)  → ws.write(truc.lock)     pure → emission[fs]
   └ ws.write(truc.toml with the new [trucs] entry)
```

`truc rm <name>` is the inverse minus the gate: drop the toml entry, the
lock row, the vendor dir; then run the §8.2 admit chain once to prove the
remainder still assembles (a removal that strands a consumer is refused
the same way — the gate's `removed` drift, `src/revl/admission.py:172`).

### 8.2 `truc assemble`

The §3.2 chain: read intent + proof → pure plan → hash-verify vendor →
incremental `gate.admit` per truc in order → `host_compose` → write
`build/assembly.json` → print the report: components admitted, provides/
requires wiring, capability totals vs the lock, and — on refusal — the
single failing candidate with its drift reason.

### 8.3 `truc ship`

```
cli.run(["ship"])
→ ship.publish()
   ├ gate.admit(project component, manifest=None)   // it must at least compile clean
   ├ host_publish(reg, name, source)                emission[registry]
   │    @py body: write registry/components/<name>/ then
   │    revl.registry.build_index(reg)  — manifest.json is REGENERATED by the
   │    current compiler, never copied from the author (the §1 reproducibility
   │    invariant does the honesty work)
   └ report: index row (provides/requires/capabilities/emissions) as published
```

First-come name claim is just "the directory did not exist" (docs/
registry.md §1). Gauntlet evidence (`revl_gauntlet`,
`src/revl/mcp/gauntlet.py`) is *optional* at ship time in v1 — evidence
upgrades ranking, absence does not block publish (open decision §10.5).

## 9. Non-goals (v1)

HTTP registry transport (the `Fetch`/`Index` service shapes take a
registry name so the extern body can grow a URL arm without composition
changes); versioning and update flows (registry phase 2, item 9); non-py
extern bodies / standalone-compiled truc; signatures; a TOML serializer;
any new compatibility or ranking logic (that would defeat the point).

## 10. Decisions that are the human's call

1. **Source location and packaging**: `src/revl/truc/` inside the package
   (this doc's assumption — wheel-ships for free, per item 108) vs a
   top-level `truc/` tree like `selfhost/` (more visible as a showcase,
   but needs a force-include and a `_paths`-style resolver). §6.
2. **Str-JSON service surfaces vs record types** across truc's internal
   services (§1.2). JSON-as-Str is shippable today on py/ts; records are
   prettier but push on `Any`-typing gaps other tiers have
   (`stdlib/json.rvl` header). Recommend Str-JSON v1, records later.
3. **`revl truc` surface** (settled): a namespaced `revl truc <verb>`
   subcommand group that passes its tail through to `revl.truc.main`, so
   every verb (`add/rm/assemble/ship`) works without flat `revl add`
   aliases crowding revl's top level. §6.
4. **Lock as JSON** (this doc, §4.3) vs lock as TOML (symmetric with
   truc.toml but requires writing a TOML serializer in revl or moving lock
   writes host-side).
5. **Ship evidence policy**: audit-only publish allowed (this doc) vs
   gauntlet dossier required to ship. §8.3.
6. **Name claim**: registry/npm/PyPI/domain availability for "truc" is
   checked **at claim time only** (roadmap item 136) — that check is not
   part of any slice here and must not be run speculatively.

## 11. Slice plan and multi-agent collision map

Ordered; each slice lands independently green. File ownership is disjoint
by design — the collision map below is what the orchestrator fans out on.

### Slice 1 — `add` + `assemble` against the local registry ★ smallest first slice

The whole §8.1/§8.2 loop, local paths only, no publish, no alias.

- **Creates:** `src/revl/truc/__init__.py` (main), `_launcher.py`,
  `externs.rvl` (§2 minus `host_publish`), `components/registry_client.rvl`,
  `components/fetcher.rvl`, `components/gatekeeper.rvl`,
  `components/workspace.rvl`, `components/planner.rvl`,
  `components/assembler.rvl`, `components/cli.rvl`, plus truc's own
  stage-0 `truc.lock`; `tests/test_truc_add_assemble.py`.
- **Edits:** `pyproject.toml` (`truc` console script) — the only shared
  file this slice touches.
- **Exit test:** in a temp project with `[registries] local = {path =
  <repo>/registry}`: `truc add user_cache` vendors + locks it; `truc add
  pg_database` then `truc assemble` **refuses** when a second `db`
  provider joins (G2 — the differentiator observable from truc's CLI, the
  same scenario as registry exit test 2, docs/registry.md §6) and
  assembles clean in the compatible arrangement; hand-editing a vendored
  `component.rvl` turns `assemble` red on hash drift.

### Slice 2 — `revl truc <verb>` namespaced subcommand
- **Edits:** `src/revl/__main__.py` only (a REMAINDER-tail forwarder to
  `revl.truc.main`), + `tests/test_truc_revl_subcommand.py`.
- Independent of every other slice once slice 1's `revl.truc.main` exists.

### Slice 3 — `truc rm` + drift/`--check` hardening
- **Edits:** `components/planner.rvl`, `components/assembler.rvl`,
  `components/cli.rvl`; `tests/test_truc_rm_check.py`.
- Exit: `rm` of a truc a consumer still needs is refused with the drift
  why-trace; `assemble --check` verifies without writing.

### Slice 4 — `truc ship` (publish into the local registry)
- **Creates:** `components/shipper.rvl`; **edits:** `externs.rvl`
  (`host_publish`), `components/cli.rvl`; `tests/test_truc_ship.py`.
- Exit: ship a new component from a temp project; `registry.verify` stays
  green; re-shipping an existing name is refused (first-come).

### Slice 5 — the bootstrap fixpoint (truc assembles truc) ✅ landed
- **Creates:** `src/revl/truc/bootstrap/truc.toml` (truc-as-a-truc-project, its
  eight components as `[assembly].entry` — see the Stage-1 deviation note in §7),
  `src/revl/truc/bootstrap/assembly.golden.json` (the pinned composition), and
  `tests/test_truc_bootstrap.py`. No CI-file edit needed: the regenerate-or-red
  gate is frontend Python (`compile_files` / `_host.admit_all`), so it runs in
  the existing `pytest tests/` frontend job and never skips — the end-to-end
  `truc assemble` CLI proof is an added bonus that runs where the cordis runtime
  is present.
- Depended on slices 1 and 4 (truc's assemble loop + rim/core/pure split).

### Slice 6 (later, unblocked by nothing above) — HTTP fetch arm, versioning
- Gated on registry phase 2 (item 9); extern-body-only change by design.

### Collision map

| File | S1 | S2 | S3 | S4 | S5 |
|---|---|---|---|---|---|
| `pyproject.toml` | **E** | — | — | — | — |
| `src/revl/__main__.py` | — | **E** | — | — | — |
| `src/revl/truc/_launcher.py`, `__init__.py` | C | — | — | — | — |
| `src/revl/truc/externs.rvl` | C | — | — | E | — |
| `components/{registry_client,fetcher,gatekeeper,workspace}.rvl` | C | — | — | — | — |
| `components/{planner,assembler}.rvl` | C | — | E | — | — |
| `components/cli.rvl` | C | — | E | E | — |
| `components/shipper.rvl` | — | — | — | C | — |
| truc self-description (`truc.toml`, `trucs/`, stage-0 lock) | C(lock) | — | — | — | C |
| `tests/test_truc_*.py` | C | C | C | C | C (one file each, no overlap) |
| `registry/` | read-only everywhere; S4's exit test writes only in a temp copy | | | | |

C = creates, E = edits. **Safe parallel waves:** S1 alone first (it creates
the tree everything else edits). Then S2 ∥ S3 ∥ S4 — S2 is fully disjoint;
S3 and S4 collide only on `components/cli.rvl` (one adds `rm`/`--check`
arms, the other a `ship` arm — small file, rebase-trivial, or the
orchestrator sequences just that file). S5 last. `src/revl/registry.py`,
`admission.py`, `compiler.py` are **imported, never edited** by any slice —
a truc slice that wants to change the gate is out of scope by definition
(and per the runtime-ownership rule, a real gate defect is its own item,
fixed in the gate).
