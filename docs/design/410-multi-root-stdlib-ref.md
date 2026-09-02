# 410: multi-root stdlib host-ref imports

Design note for roadmap item 410 (`docs/v2.0-roadmap.md:4150`), the 396(B)
follow-up. The ask, from the harness lane (revl-harness Cam #1): let a shipped
stdlib module declare `= @ts ref` (or `= @py ref`) against a shipped runtime
helper, so `stdlib/fs.rvl` can drop the `globalThis.__revlFs` seam and the
harness can retire its `fs_host_install` workaround. The 396 note deferred this
deliberately: "a multi-root import story can be its own later item if the
stdlib ever needs one" (`docs/design/396-host-code-file-reference.md:351-356`).
The stdlib needs one. This note is design-first: it changes no compiler code;
it records the two-root scheme, the security separation between the roots, the
runner and bundle consequences, the self-host position, the end state for
`stdlib/fs.rvl`, a staged plan, and exit tests an implementation agent can pick
up. No new syntax is proposed; the surface is 396(B)'s `= @backend ref sym
from "path"`, unchanged.

## The problem (measured)

396(B) landed with ONE root: the user compile tree. `resolve_refs` receives
`_ModuleLoader._root_dirs()`, the directories of the composition's root compile
files (`src/revl/compiler.py:94-99`, wired at `compiler.py:174-175`), and
`_pick_root` refuses any ref whose realpath is not contained in one of them
(`src/revl/hostref.py:95-108`, refusal at `hostref.py:197-209`). The refusal
message itself names this item's gap: "A module resolved from the stdlib or
REVL_IMPORT_PATH may not declare a ref".

A `use "stdlib/fs.rvl"` resolves through the item-319 search path
(`compiler.py:121-133`): relative-to-importer first, then `REVL_IMPORT_PATH`
entries, then `stdlib_root().parent` as the standing default
(`compiler.py:26-44`). So `stdlib/fs.rvl` loads from the revl INSTALL tree, and
its would-be ref targets, `backends/python/revl_fs_workspace.py` and
`backends/typescript/revl_fs_ts.ts`, live there too, never under the user
root. A stdlib `= @ts ref` is therefore refused today, by design, and the
stdlib still reaches its shipped helpers through two invisible idioms:

- py: `import revl_fs_workspace as _ws` inside each `@py` body
  (`stdlib/fs.rvl:105`), resolving only because the CLI inserts
  `backends_root()/"python"` at `sys.path[0]` before importing the runtime
  (`src/revl/run.py:1174-1176`). An embedder that arranges `sys.path`
  differently gets an `ImportError` from an undeclared dependency.
- ts: `const _ws = (globalThis as any).__revlFs` (`stdlib/fs.rvl:127`), a
  global that `backends/typescript/revl_fs_ts.ts` installs at import time
  (`revl_fs_ts.ts:285`), because a verbatim `@ts` body cannot carry its own
  `import` (`revl_fs_ts.ts:1-22`). NOTHING imports that module automatically:
  "a node entrypoint imports this module for its install side effect before
  any witnessed fs body runs" (`revl_fs_ts.ts:20-22`). That entrypoint is the
  harness's `fs_host_install` workaround, Cam #1: every ts embedder must know
  to pre-import a file the language never declared.

396(B) built the declared, jailed, hash-pinned spelling of exactly this
dependency shape, and the stdlib, the one library every composition shares,
is the one place that cannot use it. Three subsystems must change together:
the jail (a), the runners (b), and bundle/verify (c). Each is a section below.

## Trust framing: first-party, not a 329 widening

The 396(B) jail exists to bound what an AUTHOR can reach: an untrusted author
(item 329) is refused externs outright, and a trusted author's refs are
contained so a compile cannot embed reach outside the reviewed tree. A stdlib
ref is a different trust object: the declaring module ships INSIDE the revl
install, next to the emitters and runtimes it would reference. Whoever can
tamper with `stdlib/fs.rvl` can already tamper with `backends/python/emit.py`
and own every artifact the toolchain produces. Letting a shipped module
reference a shipped helper adds no authority the install does not already
have; it converts an undeclared reach (a body-buried import, a global) into a
declared, hash-pinned, audit-printed one, which is the whole 396 thesis
(`396-host-code-file-reference.md:578-585`) applied to the stdlib itself.

What must NOT widen, and does not: an untrusted-author root module still gets
the no-extern refusal before any resolution (`compiler.py:152-161`); a USER
module gains no new reach (the exemption keys on the declaring module's
origin, next section, never on the ref target); and `REVL_IMPORT_PATH` gains
no new power (the trust argument is in the jail section).

## (a) The two-root scheme

### Origin classification

Every loaded module gets an ORIGIN, decided at load time by the loader, which
already knows everything needed:

- INSTALL-ORIGIN: the module was resolved through the item-319 search path
  (a `REVL_IMPORT_PATH` entry or the stdlib default, `compiler.py:129-132`),
  OR its realpath is contained in `stdlib_root()` (`src/revl/_paths.py:38-60`,
  canonical `_contained` from `hostfile.py:55`, never string prefix). The
  containment arm matters because a stdlib module that `use`s a sibling
  relatively (relative resolution is primary and wins, `compiler.py:121-127`)
  must classify the same as its importer.
- USER-ORIGIN: everything else, including every root compile file that is not
  itself inside `stdlib_root()`.

Origin picks the ref root SET, exclusively:

- A USER-ORIGIN module's refs resolve against `_root_dirs()` exactly as
  today. Byte-identical behaviour; this item changes nothing for them.
- An INSTALL-ORIGIN module's refs resolve against ONE root: the search-path
  entry that resolved the module (for the stdlib default that is
  `stdlib_root().parent`, the install tree), or `stdlib_root().parent` for
  the containment arm. `..` segments stay allowed, as in 396(B): a
  `stdlib/fs.rvl` legitimately refs `../backends/typescript/revl_fs_ts.ts`,
  and the containment check on the resolved realpath is what holds.

Origin decides; containment of the TARGET never does. When the user compiles
from inside the revl checkout (this repo's own tests do, constantly), the user
root and the install tree overlap; a user module's ref still resolves only
against the user root set, and a stdlib module's ref only against the install
root. No ambiguity, no deepest-root arbitration across kinds:
`_pick_root`'s deepest-root rule (`hostref.py:95-108`) keeps operating WITHIN
a kind, never across kinds.

The loader implementation seam: `resolve_use` is the only place that knows
which branch resolved a path (`compiler.py:121-133`), so it records
search-path-resolved abspaths mapped to their resolving entry, and `load`
passes the declaring module's origin (plus its install root, when
install-origin) into `_resolve_refs` alongside today's arguments
(`compiler.py:174-175`). The `stdlib_root()` containment arm is a realpath
check at the same seam.

### The `REVL_IMPORT_PATH` trust argument, stated

Granting install-origin to a `REVL_IMPORT_PATH`-resolved module means an
environment variable participates in choosing a ref root, which the 396(A)
jail called a supply-chain seam and refused. The difference here is what the
variable already controls: `REVL_IMPORT_PATH` decides WHICH `.rvl` SOURCE gets
compiled for a `use` (`compiler.py:34-40`), and that source may carry
arbitrary inline `@py` bodies. Whoever sets the variable already supplies
code; letting the same module also declare a ref jailed to the same entry's
tree adds no authority. It also keeps the feature testable: a test's stand-in
stdlib under a `REVL_IMPORT_PATH` entry exercises the whole path without
monkeypatching `_paths`. What is refused: an install-origin ref whose target
escapes ITS OWN entry's tree (each entry is its own jail; entries never pool),
and any install-origin classification for a module reached relatively from a
user module (relative resolution is user-origin unless the realpath lands in
`stdlib_root()`).

### The IR entry: a root KIND, never a root path

396 rejected per-ref absolute roots because they put machine-specific paths in
the IR document (`396-host-code-file-reference.md:354-356`). That constraint
holds. The ref entry (`src/revl/lower.py:2564-2567`) gains ONE additive key:

```
"refs": {"ts": {"symbol": "fsWrite",
                "path": "backends/typescript/revl_fs_ts.ts",
                "sha256": "...",
                "root": "stdlib"}}
```

`"root"` is `"stdlib"` for an install-origin ref and ABSENT for a user ref
(absent, not `"user"`, so every existing ref-carrying IR is byte-identical:
the 342/388 additivity discipline). `"path"` is relative to the kind's root:
for `"stdlib"` that is the install tree, and the layout makes that
machine-independent, which is the load-bearing fact of this design:
`_paths.py` supports exactly two layouts, source checkout
(`<repo>/stdlib`, `<repo>/backends`) and installed wheel
(`site-packages/revl/stdlib`, `site-packages/revl/backends`), and in BOTH the
stdlib and backends trees are siblings under `stdlib_root().parent`
(`_paths.py:19-60`). So `backends/typescript/revl_fs_ts.ts` is the same
root-relative path on every machine, every layout, and the IR carries kind +
relative path + content hash, nothing machine-specific. The hash pins the
SHIPPED helper's bytes at compile time, one file deep, with 396's stated
residual (the helper's own imports are unpinned; both fs helpers import only
platform builtins today, `revl_fs_workspace.py:52-55`, `revl_fs_ts.ts:24-26`,
and keeping them dependency-free is part of the migration contract below).

`revl audit` already prints `path#symbol` per ref tier
(`src/revl/__main__.py:216-218`, `:333-334`); it grows the kind
(`stdlib:backends/typescript/revl_fs_ts.ts#fsWrite`) so a reviewer sees at a
glance which trust domain a ref reaches into, and `audit --diff` flags a
user-to-stdlib kind change like any ref transition.

### Security separation: the invariants

1. A user ref NEVER resolves against the stdlib root. Compile: origin selects
   the root set, and a user-origin ref whose realpath lands only in the
   install tree is refused with today's outside-the-root message
   (`hostref.py:197-209`). Run: the emitted thunk's resolution root is picked
   by the IR kind (section b), and the py plug-time hash check binds the
   resolved file to the compile-time pin either way.
2. A stdlib ref NEVER resolves against the user root. Compile: install-origin
   refs are contained against the install entry only, so a shipped module
   cannot be tricked into loading composition-supplied code as first-party.
   Run: stdlib-kind thunks read only the stdlib root (ts), or import a dotted
   name whose file the plug check hashed against the install pin (py).
3. A local shadow never crosses domains. A composition tree carrying its own
   `stdlib/fs.rvl` resolves RELATIVELY (primary resolution wins,
   `compiler.py:121-127`), classifies user-origin, and its refs jail to the
   user root: shadowing the stdlib source never buys install-root reach.
4. The runner side trusts the IR document, as it always has. A handcrafted IR
   can claim `"root": "stdlib"` for any relative path; it can also carry
   arbitrary inline host bodies, so this is not a new hole, and the plug-time
   hash refusal still requires the resolved file to match the recorded pin.
   Stated, not claimed away.
5. The 329 surface is unchanged: the no-extern refusal fires before any ref
   resolution for root modules (`compiler.py:152-161`), and imported stdlib
   modules are pre-granted dependencies exactly as for inline bodies.

## (b) Deploy: a second runner-provided root

Today both runners provide ONE root. The ts thunk joins
`globalThis.__REVL_REF_ROOT__` with the recorded relative path
(`backends/typescript/emit.py:2955-2972`), the runner sets it from the spec's
`refRoot`, the user root file's directory (`src/revl/run_ts.py:136-146`,
`backends/typescript/placement_runner.ts:25-45`). The py driver appends the
user root dirs to `sys.path` and hash-checks at plug
(`hostref.py:319-367`, called from `run.py:571-585`, roots from
`run.py:1195`). The second root:

### py: no emit dispatch at all

The py thunk imports a DOTTED NAME derived from the root-relative path
(`backends/python/emit.py:2180-2186`), and the emitted text never contains a
root; the root is whatever `sys.path` makes the name resolve to. So the py
emitter needs NO change for stdlib refs: `backends/typescript/...` has no py
form, but `backends/python/revl_fs_workspace.py` becomes
`backends.python.revl_fs_workspace`, resolved as a namespace-package chain
from the install tree once it is on `sys.path`. The whole py change is in
`plug_refs` (`hostref.py:319-367`):

- `ir_refs` carries the kind through (`hostref.py:252-266`).
- When any py ref has kind `"stdlib"`, APPEND `stdlib_root().parent` to
  `sys.path` (append, never prepend, same shadowing rationale as 396: a
  first-party tree must not shadow an already-importable name, and the
  mirror rule cuts the other way too, an already-importable top-level
  `backends` package wins on path position, which the hash refusal below
  detects and names). A composition with no stdlib ref never touches it.
- The plug-time find-spec-plus-hash walk (`hostref.py:269-295`) and the pyc
  drop (`hostref.py:370-380`) run unchanged over the union search path; a
  version-skewed or shadowed helper is a refusal naming both paths.
- `_evict_ref` receives the union root set (`hostref.py:383-403`) so a dev
  editing a shipped helper mid-process (this repo's own loop) gets fresh
  code on replug; evicting the `backends`/`backends.python` namespace
  ancestors is harmless (namespace packages hold no state).

Named residual: `backends` is a generic top-level name. A site-packages
distribution owning it would win over the appended install root and fail the
hash check, a loud plug-time refusal rather than silent substitution. If that
refusal is ever hit in the field, the escape is scoping the wheel's dotted
prefix (importing as `revl.backends.python...` under the installed layout),
which would cost the layout-uniform relative path and is deliberately NOT
designed here; the refusal message should mention the collision possibility.

### ts: kind-dispatched resolution, and a root the runner can derive itself

The ts artifact resolves by PATH, not name, so it needs the second root
explicitly. The emit dispatch: `_emit_ts_ref_runtime` grows a second helper,
and the thunk emitter (`emit.py:2975-3018`, dispatch at `:3036-3041`) picks by
the ref's kind:

```
function _revl_ref_path(rel) {          // user refs, unchanged
  root = globalThis.__REVL_REF_ROOT__        // throw if unset
  ...
}
function _revl_ref_path_stdlib(rel) {   // stdlib refs, new
  root = globalThis.__REVL_STDLIB_REF_ROOT__ // throw if unset
  ...
}
```

No fallback in either direction, ever: a stdlib-kind thunk with no stdlib
root set fails loudly naming the missing knob; it never tries the user root
(invariant 2), and vice versa (invariant 1). The two globals are the two
domains, and the emitted text stays machine-independent.

Who sets `__REVL_STDLIB_REF_ROOT__`: the runner, from the spec, with a
self-derived default. `run_ts._spec` adds `stdlibRefRoot:
str(stdlib_root().parent)` next to `refRoot` (`run_ts.py:136-146`), and each
entry in the spec's `refs` hash-check list carries its kind so the runner
joins against the right root (`placement_runner.ts:31-45`). The default is
the elegant part: `placement_runner.ts` LIVES in the install tree, at
`backends/typescript/placement_runner.ts`, and in both supported layouts the
install root is exactly two directories up from it, so the runner derives
`path.resolve(dirname(fileURLToPath(import.meta.url)), "../..")` when the
spec omits the key. That makes multi-process placement work without touching
`src/revl/placement.py`'s spec at all for the stdlib root; `placement.py`
should still add both keys explicitly (it sets NEITHER today, a pre-existing
396 gap for user refs under placement, which this item fixes in passing for
the stdlib kind and records for the user kind). The runner's per-ref hash
check runs before the module is imported, per kind, unchanged in shape.

**Reachability (item 225).** For a long while none of that ran. The spec's
`refs` list is built from the externs the process's slice reaches, and
`placement.ts_safe_ir` classified an extern by its `bodies` alone — so a
`= @ts ref` extern (empty `bodies`, populated `refs`) counted as un-emittable
by the ts tier, and `tier_capability_gate` refused the node tier to every
component reaching one. The only compositions that could boot a node process
were the ones with no ref, so `spec.refs` was empty every time and the hash
check above walked nothing. The predicate now mirrors the emitter's own arms
(`placement._ts_unemittable_externs`), and the check is proven to refuse a
tampered module on a real node-placed spec rather than assumed correct because
it is present (`tests/test_ts_ref_node_tier_225.py`).

Sync colour holds: the fs externs are sync, the sync thunk loads through
`createRequire` (`emit.py:3010`), `revl_fs_ts.ts` is an ESM graph of node
builtins with no top-level await, and the bridge's node floor (>= 23.6,
require(esm) capable) already gates `run_ts`.

### The other drivers

`revl test`, fault, and the non-ts exec drivers get neither root nor hash
check, exactly as 396 recorded for user refs: a stdlib-ref program under them
fails with a loud first-call error, never silently. The migration section
below is therefore gated on the drivers the fs tests actually use; the
witnessed-fs test suites run under the py driver (which this item covers) and
the ts harness path (which this item exists to fix).

## (c) Bundle and verify: re-resolve from the install, carry nothing

`revl bundle` hard-refuses ANY ref extern today (`src/revl/bundle.py:324-341`,
the interim stage-4 refusal). The crux this item must answer: a stdlib ref
must be reproducible and verifiable across machines where the install PATH
differs, without machine paths in the IR.

Two candidate mechanisms:

- Travel: copy the ref'd helper bytes into the bundle under a new tree, and
  verify re-hashes the carried file. Rejected for the stdlib kind: the bundle
  would then carry a second copy of a file the RUNTIME still resolves from
  the install (the runner roots above), so a version skew between carried
  bytes and installed bytes is representable and invisible until run time,
  the exact class of quiet divergence verify exists to kill.
- Re-resolve: carry nothing; verify resolves the recorded relative path
  against the VERIFYING machine's install root and re-hashes against the pin.

Re-resolve is the design, because it is what the bundle already means for the
stdlib. `verify` RECOMPILES the bundled source through the normal pipeline
(`bundle.py:23-40`), and a bundled `use "stdlib/fs.rvl"` re-resolves through
the verifier's search path to the verifier's install; the stdlib source is
already a verify-time install dependency, alongside the compiler and emitters
themselves ("backend version" is already a check tier). A stdlib ref adds the
HELPER file to the same dependency set it is conceptually part of. Since
`resolve_refs` runs during that recompile, the recomputed IR carries the
verifier's hash and the existing IR byte-compare would already catch skew;
the design adds a TARGETED check so the report names the file:

- Lift the `bundle.py:324-341` refusal for refs whose kind is `"stdlib"`
  ONLY. A user ref still refuses with today's message: carrying user ref
  files is 396's own stage 4, a different mechanism (those files are not on
  the verifier's machine at all), and this item must not half-build it.
- `verify` grows one check tier, `stdlib refs`: for each recorded
  stdlib-kind ref, resolve `stdlib_root().parent / path` on the verifying
  machine, hash, compare to the recorded pin. OK / MISMATCH (both values
  printed, naming the file and both hashes) / SKIP (file absent: the install
  lacks the helper, named). This runs before the coarse IR compare so a
  version skew reads as "revl_fs_ts.ts changed", not "IR differs".
- Cross-machine reproducibility holds by the layout argument in (a): the
  recorded path is layout-uniform, so a bundle built on a checkout verifies
  on a wheel install and vice versa, provided the INSTALL VERSION matches,
  which is precisely the contract verify already enforces for the stdlib
  source and the emitters. A version mismatch is an honest MISMATCH, the
  correct verdict for "this bundle was reviewed against different first-party
  bytes".
- Stand-in caveat, stated: a bundle built with a `REVL_IMPORT_PATH` stand-in
  stdlib records paths relative to the stand-in entry; verifying without
  that environment resolves against the real install and mismatches
  honestly. Verify consults the same `_default_search_path()` order the
  compiler uses, so a reproducible stand-in verify sets the same variable.

## Self-host (391): reference-only now, port owed with 396(B)'s

The self-host emitters do not implement 396(B) refs at all: `selfhost/
emit_py.rvl` and `emit_ts.rvl` read only `bodies` (`emit_py.rvl:690`), and no
`refs` handling exists anywhere in `selfhost/`. Item 410 adds no new syntax
and no new IR stage of its own; it adds one IR key and emit dispatch INSIDE
396(B)'s feature. So the per-feature 391 question ("does this need a
self-host port?") answers: not separately. The 396(B) thunk emission and this
item's root dispatch are ONE port item when the self-host compiler grows ref
support toward the full-language v3 goal.

What keeps that honest today is the oracle corpus discipline: the emit
oracles run over a FIXED fixture list (`tests/test_selfhost_emit_ts.py:113-
124`), none of which uses `stdlib/fs` or any ref, and the corpus's own rule
is that a document must not enter until the self-host emitter covers it
(`test_selfhost_emit_ts.py:96`). The fs migration below therefore does not
red the oracles; the guard to add is the inverse: a ref-carrying IR (either
kind) handed to a self-host emitter must be a clean refusal in the self-host
emitters' own vocabulary, not a silently body-less extern, so divergence
stays impossible rather than unobserved.

## End state: `stdlib/fs.rvl` without `__revlFs`

The migration reshapes the two shipped helpers from "helper surface the
verbatim bodies reach" into "per-extern entry points the refs import", and
moves each body's logic into its helper:

- `backends/python/revl_fs_workspace.py`: grows one exported function per fs
  extern (`fs_write`, `fs_rm`, `fs_move`, `fs_mkdir`, `restore`, `unrm`,
  `unmove`, `rmdir_if_empty`), each the current `@py` body's logic calling
  the existing confinement/snapshot internals, same three-musts contract
  (`docs/witnessed-fs.md`), unit-testable by plain pytest for the first time.
- `backends/typescript/revl_fs_ts.ts`: the same per-extern exports over the
  existing `HOST` internals; the module STOPS being side-effect-loaded. The
  `globalThis.__revlFs` install line (`revl_fs_ts.ts:285`) survives one
  deprecation release for out-of-tree embedders that still pre-import it,
  then dies with the note.
- `stdlib/fs.rvl`: every `= @py { import revl_fs_workspace ... }` body and
  every `= @ts { const _ws = (globalThis as any).__revlFs ... }` body
  becomes a ref pair; classifications, witness types, and docs stay put:

```revl sketch
pub extern witnessed[fs] fn write(path: Str, contents: Str)
        -> Result[WriteWitness, FsError]
    = @py ref fs_write from "../backends/python/revl_fs_workspace.py"
    = @ts ref fsWrite from "../backends/typescript/revl_fs_ts.ts"

pub extern pure fn restore(w: WriteWitness) -> Unit
    = @py ref restore from "../backends/python/revl_fs_workspace.py"
    = @ts ref fsRestore from "../backends/typescript/revl_fs_ts.ts"
```

Witnessed machinery composes untouched: it is call-site keyed on the extern's
class (`backends/python/emit.py:674-681`), and a ref thunk is an ordinary
emitted function at the same name, so acquire/teardown/abort-replay wrap it
exactly as they wrap a body. The go/rust/java/wasm story for fs is unchanged
by this item (those tiers have no fs bodies today, `stdlib/fs.rvl:64`); the
ref tier gate stays `{"py", "ts"}` (`hostref.py:60`).

Downstream, the harness deletes `fs_host_install` (Cam #1): the ts runner
resolves and hash-checks `revl_fs_ts.ts` itself, and no embedder pre-import
remains. The py side sheds its silent dependence on `backends/python` sitting
at `sys.path[0]`: the declared ref, appended install root, and plug-time hash
check replace an arrangement that only the CLI happened to make true.

## Staged implementation plan

Additivity discipline throughout (342/388): a composition with no stdlib ref
is byte-identical at every stage, IR and artifacts and driver behaviour.

- Stage 1 (loader origin + jail exemption + IR). `resolve_use` records the
  resolving entry per search-path hit; `load` classifies origin (search-path
  arm plus `stdlib_root()` containment arm) and passes it to `_resolve_refs`;
  `hostref.resolve_refs` resolves install-origin refs against the single
  install entry (per-entry jail, `..` allowed, canonical containment) and
  stamps the node; `lower` emits the additive `"root": "stdlib"` key;
  `audit` prints the kind. Exit: a stdlib module's ref resolves and pins; a
  user ref into the install tree still refuses; existing ref IRs
  byte-identical.
- Stage 2 (py runtime). `ir_refs` carries kind; `plug_refs` appends
  `stdlib_root().parent` only when a stdlib ref exists, hash-checks and
  evicts over the union root set; refusal message mentions the top-level
  `backends` collision case. No emitter change. Exit: a py stdlib ref
  composition round-trips under `revl run`; edit-the-helper-then-replug runs
  the new code; a ref-free program never touches `sys.path`.
- Stage 3 (ts runtime). `_revl_ref_path_stdlib` + `__REVL_STDLIB_REF_ROOT__`
  in emit, kind dispatch at the thunk site; `run_ts._spec` gains
  `stdlibRefRoot` and per-ref kinds; `placement_runner.ts` sets both globals,
  self-derives the stdlib root when the spec omits it, and hash-checks per
  kind; `placement.py` adds both spec keys. Exit: ts golden shows the
  stdlib thunk resolving through the stdlib helper only; bridge `--once`
  boots and tears down clean; a spec with no stdlib root still runs via the
  self-derived default.
- Stage 4 (bundle/verify, scoped). Lift the ref refusal for stdlib-kind refs
  only; add the `stdlib refs` verify tier (resolve against the verifier's
  install, re-hash, OK/MISMATCH/SKIP naming the file). User refs keep
  today's refusal verbatim. Exit: bundle a stdlib-ref composition, verify
  green on the same install; verify against a doctored helper is a MISMATCH
  naming the file; a user-ref composition still refuses.
- Stage 5 (the fs migration). Reshape both helpers into exported entry
  points (helper-internal logic moves, contracts and tests move with it);
  rewrite `stdlib/fs.rvl` bodies as ref pairs; keep the `__revlFs` install
  for one deprecation release; update `docs/witnessed-fs.md`; add the
  self-host clean-refusal guard; coordinate the harness's `fs_host_install`
  deletion with the harness lane. Exit: the full witnessed-fs suites (py
  and ts) green with zero pre-import arrangements; the harness workaround
  deleted.

## Exit tests

- Additivity: a composition with no stdlib ref is byte-identical across IR
  and every backend golden at every stage; its py driver never appends the
  install root and its ts spec's stdlib root is inert.
- Headline resolve-and-run: a stdlib module's `= @ts ref` to a shipped
  helper compiles (pinned kind + relative path + hash in IR), and the ts
  bridge runs it end to end with no embedder pre-import; same on py.
- Separation, user side: a USER module's ref written to escape into the
  install tree (`../../../<install>/backends/...`, or a symlink realpathing
  there) is refused at compile with the outside-the-root message; a
  handcrafted user-kind IR entry naming a stdlib-relative path fails the
  runner hash check rather than resolving against the stdlib root.
- Separation, stdlib side: a stdlib-kind thunk with `__REVL_REF_ROOT__` set
  but `__REVL_STDLIB_REF_ROOT__` unset fails loudly naming the stdlib knob
  (never falls back); a composition-local `stdlib/fs.rvl` shadow classifies
  user-origin and gains no install reach.
- Overlap: compiling a user composition FROM INSIDE the revl checkout keeps
  user refs on the user root and stdlib refs on the install root, no
  cross-kind arbitration.
- Bundle round-trip across install paths: bundle on install A (checkout),
  verify on install B (wheel layout, different absolute path, same version):
  green. Same-version doctored helper on B: `stdlib refs` MISMATCH naming
  the file and both hashes. Missing helper: SKIP naming the file. A user-ref
  composition still gets the stage-4 refusal.
- Version skew honesty: bundling against stdlib helper X and verifying
  against helper Y reports the ref MISMATCH tier before the IR tier.
- Replug freshness (py): edit the shipped helper, replug in-process, first
  call runs the new bytes (eviction + pyc drop over the union roots).
- Stand-in: a `REVL_IMPORT_PATH` stand-in stdlib resolves, jails to its own
  entry, and round-trips run and verify when the variable is set for both.
- Self-host guard: a ref-carrying IR fed to `selfhost/emit_py.rvl` or
  `emit_ts.rvl` is a clean refusal, not a missing-body miscompile; the emit
  oracle corpus remains ref-free until the port.
- Migration: the witnessed-fs suites pass on py and ts with `__revlFs`
  unused; `revl audit` of an fs-using composition prints
  `stdlib:...#symbol` per fs extern.
- `test_doc_examples` stays green: the proposed block above is `sketch` and
  must not compile until stage 5 lands.

## The honest hard part

The two-root scheme is small; the hard part is that the second root is a
VERSIONED first-party dependency, not a tree the composition author controls.
Every guarantee here reduces to "the deployed install is the reviewed
install": the compile-time pin, the runner hash refusals, and the verify tier
all DETECT skew loudly, but none can reconcile it, and the design deliberately
refuses to carry helper bytes in artifacts or bundles because a second copy
makes skew representable instead of refusable. The residuals are inherited
from 396 and stated again rather than re-litigated: the pin is one file deep
(kept honest by the helpers-import-only-builtins contract, which nothing
machine-checks), the plug-to-first-call TOCTOU window remains, and a
handcrafted IR is trusted as it always was. The hardest open question is the
py namespace seam: importing shipped helpers as `backends.python.*` off an
appended install root is layout-uniform and shadow-safe by the hash refusal,
but it parks a generic top-level name (`backends`) on the embedder's
`sys.path` for the process lifetime, and the clean alternative (a `revl.`-
prefixed dotted path under the wheel layout) breaks the single
layout-uniform relative path the IR scheme rests on; this note chooses the
uniform path plus a named, detectable collision, and an implementer hitting
that refusal in the field should revisit the choice rather than widen the
fallback.
