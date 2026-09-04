# The stdlib FS module: witnessed filesystem operations (roadmap item 244)

**The need:** an AI agent's tool layer is almost all filesystem mutation, and
every mutation today is an emission, so every one is irreversible by
declaration and prompts for approval. Item 243 added the language form that
fixes this: a `witnessed` extern is a host mutation whose declared inverse the
runtime auto-registers as a *transactional* teardown entry. On a clean commit
the mutation persists (it is the deliverable) and the inverse is discharged; on
an abort the inverse replays and the mutation reverts. `stdlib/fs.rvl` is the
first catalog of such effects: the point where "the agent works, fs ops stop
prompting because they are revertible, and the residue is exactly enumerable"
becomes a thing you can run. This is the H1 north-star demo.

This is the py reference tier. The ts tier carries the same catalog and the
same confinement pass (item 369 + item 422); the other four (rust, go, java,
wasm) land their `@py`-equivalent bodies in Slice 2b; the language form, the runtime
seam, and the WAL/recover foundation are already tier-shared (item 243,
docs/design/243-witnessed-externs.md, docs/design/teardown-contract.md).

## The catalog

Every op returns `Result[Witness, FsError]`. The witness is flat, serializable
data (paths and flags), never a host handle, so `revl recover` can rebuild the
inverse after a crash. A consumer imports only the mutation; the inverse is
auto-registered by the accumulator and is never named at the call site.

| op | mutation | witness | inverse |
|---|---|---|---|
| `write(path, contents)` | snapshot the preimage, then overwrite (or create) | `{ path, preimage, created }` | `restore`: put the preimage back, or delete the created file |
| `rm(path)` | rename the target into a session garbage dir | `{ path, garbage }` | `unrm`: rename it back |
| `move(src, dst)` | rename `src` into `dst` | `{ from, to }` | `unmove`: rename `to` back to `from` |
| `mkdir(path)` | create the directory | `{ path }` | `rmdir_if_empty`: remove it if still empty |

`write`'s preimage is captured with an APFS `clonefile()` copy-on-write clone
when the volume supports it (O(1), no bytes copied until one side diverges) and
a portable byte copy otherwise. That is the "clonefile preimage" decision: the
snapshot is cheap even for a large file because CoW shares storage with the
original until the subsequent write forces divergence.

Every inverse is a `pure` extern (host-local, non-emission, non-witnessed, as
item 243 rule 3 requires) and is idempotent on replay (rule 5): once the world
is already restored, a second run is a no-op, so an abort that crashes mid
replay and a later `revl recover` cannot clobber. Fallibility is real (rule 6):
`rm` on a missing path, `mkdir` over an existing one, and a confinement refusal
all return `Err` and register nothing, because a mutation that did not happen
must schedule no rollback (Ok-conditional registration).

## Workspace confinement

A witnessed fs effect is auto-approved (item 246) and discharged silently at
commit (item 245). Without a boundary, an agent holding `fs` authority could
witnessed-mutate anywhere the process can reach, permanently, invisible to the
single commit prompt, and item 246's claim that revl can say exactly which
actions escaped would be hollow because revl could not even say where. So a
minimal confinement slice lands with the catalog: one session **workspace
root**, and every op refused unless its target resolves inside it.

The root is configured, on the py tier, by the `REVL_FS_WORKSPACE` environment
variable that the `@py` bodies read through `backends/python/revl_fs_workspace.py`.
Keeping it in the process environment matches the toy witnessed fixture and
keeps the bodies a pure host-local read with no cordis config dependency. A
later tier, or item 294 (parameterized capabilities), can promote it to a typed
session capability; for this slice it is one env var.

The guard has three properties that make it real rather than decorative:

1. **Realpath before the check.** `resolve_within` realpaths the target, which
   resolves every symlink in the path, and only then tests root membership. A
   symlink placed inside the root that points outside is caught, not followed.
2. **The inverse path is guarded too, at BOTH endpoints.** `restore`, `unrm`,
   `unmove`, and `rmdir_if_empty` re-run the same check on the path they are
   about to write. A witness that (through tampering or a moved root) now
   points outside cannot make the reversal escape; a confinement failure on an
   inverse raises and is recorded as `restore-residue` rather than silently
   writing outside. This covers the SOURCE endpoint as much as the target:
   `restore`'s `preimage` and `unrm`'s `garbage` are the source of a rename,
   and a rename REMOVES what it names, so an unchecked source is not a weaker
   overwrite hole but a steal-and-destroy primitive for any file the process
   can reach. Those sources go through `resolve_sidecar`, which confines them
   and additionally requires them to be a sidecar this workspace itself
   produced. The inverses are `pure` externs, so they carry no capability and
   are callable from any pure position (and are reconstructed from WAL data by
   `revl recover` with no revl source involved at all). Item 243 rule 3 leaves
   no capability-scoped spelling for an inverse, so what makes that
   classification safe is how small their authority is: a sidecar we made,
   moved back inside the root we own.
3. **The reversal machinery lives inside the root.** The session garbage dir
   (`.revl-fs-garbage`) and the preimage snapshots (`.revl-fs-preimage`) are
   subdirectories of the workspace root, so an `rm` parks its target inside the
   boundary and a `write` snapshots inside the boundary. Reversal reads only
   from inside the root. The directories themselves are resolved and
   type-checked, not merely joined onto the root: a symlink planted under
   either name is refused, because `mkdir -p` semantics succeed on a
   pre-existing link and would otherwise redirect the whole reversal machinery
   outside the boundary.

Those are three of the FOUR path families the guard enumerates (`PATH_FAMILIES`
in `backends/python/revl_fs_workspace.py`). The fourth is the syscall itself: a
resolved path is a checked path, not yet a safe syscall, so every mutation runs
through a directory fd walked down from the root one component at a time with
`O_NOFOLLOW`, and a write goes through the verified fd rather than the name.
`tests/test_fs_confinement_families.py` scans the `@py` bodies of
`stdlib/fs.rvl` and fails if any path reaches a mutation by another route, so
the enumeration cannot quietly grow a fifth member.

## Observation: the read half, and the door it opens

The four ops above mutate. A consumer also needs to *look*: read a workspace
file, check that a write took, decide whether a path is a directory before
telling a model to create one. Asking "may I touch this path, and what is
there?" is the confinement decision, and a consumer must not answer it for
itself. A body that calls `os.path.exists` on a raw path has skipped the guard,
and the case it gets wrong is a symlink inside the root pointing out of it.

Three `pure` externs expose that half:

| observation | answers |
|---|---|
| `resolve_within(path) -> Result[Str, FsError]` | the confinement decision alone: the resolved absolute path, or the guard's own refusal |
| `lexists(path) -> Result[Bool, FsError]` | does it name something, once confined |
| `is_dir(path) -> Result[Bool, FsError]` | is it a directory, once confined |

They go through the same family-1 `resolve_within` guard every mutation and
every inverse uses, so the jail is unchanged: a path these refuse is a path no
op in the catalog would touch, and the refusal codes are the same ones
(`EOUTSIDE` for the boundary, `EWORKSPACE` for an unconfigured root, `EINVAL`
for a name no filesystem can hold). A path outside the root answers with a
refusal, never with a fact about what lives out there.

**Why they had to exist at the revl level.** A consumer's *host body* had no
supported door to the guard. Item 396(B) jails a user-origin `= @ts ref` to the
user compile-root tree, and `backends/typescript/revl_fs_ts.ts` lives in the
install tree. Item 410's second root (`__REVL_STDLIB_REF_ROOT__`) is reserved
for install-origin modules, which a consumer's file is not. And item 422 F1
removed the unconfined primitives the deprecated `globalThis.__revlFs` seam
published. That removal was correct: an exported unconfined rename *is* that
finding.
What was left was a relative path guess into the install tree
(`require("../../revl_fs_ts.ts")`, three candidates deep), which breaks whenever
revl moves; revl-harness hit that three times.

So the door is a revl one. The consumer asks revl for the decision and does its
own reading with the plain host filesystem module:

```revl sketch
// Elided host bodies, so this block is a sketch. The same consumer, whole and
// compiled, runs on both tiers in tests/test_fs_observation.py (`_CONSUMER`).
use "stdlib/fs.rvl" { resolve_within, lexists, is_dir }

extern pure fn read_confined(real: Str) -> Str
  = @py { ... open(real) ... }
  = @ts { ... process.getBuiltinModule("node:fs").readFileSync(real, "utf8") ... }

pub fn read_workspace_file(path: Str) -> Str {
  return match resolve_within(path) {
    Ok(real) => read_confined(real),
    Err(e) => "refused: " + e.code
  }
}
```

No import of any revl module on either tier, so there is nothing to guess and
nothing to break when the install moves.

**Observation only, deliberately.** The write primitives are not here and are
not coming back: their absence is the item 422 F1 fix. A caller that appears to
need one has two doors: a witnessed op from the catalog above, revertible by
construction and enumerated by the WAL, or a filed finding. `tests/
test_fs_observation.py` pins all of this, including a live py-vs-ts diff of one
case corpus run through the real emitted bodies on both tiers, and an end-to-end
consumer that reaches the jail on py and on ts importing nothing.

## Honest caveats

These are load-bearing limits, documented so the reversibility claim is not
oversold:

* **Reversal does not undo observation.** A concurrent process that read the
  intermediate state already saw it; restoring the bytes cannot unsee that.
  This is fine for a private workspace and weaker on a shared tree.
* **A hardlinked target is refused, not written.** `realpath` cannot see
  through a hardlink: an inside name and an outside name can be two directory
  entries for one inode, and every path check passes while the write lands
  outside the root. Writing through the fd does not help, because the fd names
  the same shared inode, so `write` `fstat`s the file it opened and returns
  `EMULTILINK` when the link count exceeds one. The cost is that a legitimately
  hardlinked file inside the workspace cannot be witnessed-written.
* **A `write` needs read access to its target.** The preimage is snapshotted
  out of the same fd the write uses, so a target the process may write but not
  read is refused. Without a preimage the write would not be reversible, so
  refusing is the honest outcome.
* **Confinement here is a runtime refusal, not physical containment.** A
  process that cannot even name a path outside the root is a wasm-tier property
  eventually. On this tier the guard holds only for ops that go through these
  bodies; it is not a sandbox around the whole process.
* **The ts tier has had this pass, with one thing node cannot express.**
  `backends/typescript/revl_fs_ts.ts` now carries the same four path families,
  the same `PATH_FAMILIES` enumeration, the same table-driven totality wrapper,
  the same sidecar-restricted inverse sources, the same `EMULTILINK` hardlink
  refusal, the same `(dev, ino)` landing check, and the same guard scan
  (`backends/typescript/tests/fs_confinement_families.test.ts`). What it cannot
  carry is the directory-fd walk: node's `fs` exposes no `*at()` syscall at all
  (no `openat`, `renameat`, `unlinkat`, `mkdirat`, `fstatat`), so a mutation
  cannot be performed relative to a verified directory fd. In its place the ts
  guard `lstat`-walks every component down from the root immediately before each
  syscall, opens the write leaf `O_NOFOLLOW`, re-checks the opened fd's
  `(dev, ino)` against the name, and writes only through that fd. That closes
  the leaf swap, which is the escape that was reproduced, and narrows without
  closing an intermediate-directory swap around a `renameSync`. A ts tier with
  the py tier's guarantee needs an `*at()` binding. The APFS `fclonefileat`
  snapshot is the other casualty: node's only clone is by name, and reopening
  the target by name is the race the fd exists to avoid, so the ts preimage is
  a copy through the fd rather than an O(1) clone.

## How it runs, and the one surface that is deferred

The runtime path is fully live: the fs bodies, the confinement guard, and the
transactional teardown all run on the cordis-py tier
(`tests/test_fs_stdlib.py`, `demo/witnessed_fs.py`). A component whose
activation calls `write` and `rm` in effect position persists both mutations on
a clean commit, reverts both residue-free on an abort, refuses an out-of-root
write, and its crossings are enumerated by the WAL discharge descriptors and
`revl recover`.

One surface is deferred to a later frontend slice: the cross-module call-site
syntax. lower.py admits `effect write(...)` when `write` is a witnessed extern
declared in the same file, but the parser recognizes a missing-`undo` witnessed
call from a per-file set of witnessed names (`parser.py`, `_witnessed_names`)
that is populated only from same-file `extern` declarations. An imported
witnessed extern is therefore unknown at parse time, so `use "stdlib/fs.rvl"
{ write }` followed by `effect write(...)` in a consumer does not yet parse.
Teaching the parser that an imported name is witnessed is a frontend change
that belongs with the lower.py / item 245 commit-UX work, not with this
runtime-consuming item. Until it lands, a consumer builds the witnessed call
site as the IR lower.py already emits (the demo and tests do exactly this over
the real module bodies), and every other part of the feature is exercised for
real.

## Running it

```
sh backends/python/setup.sh                         # build the cordis venv
backends/python/.venv/bin/python -m pytest tests/test_fs_stdlib.py -q
backends/python/.venv/bin/python demo/witnessed_fs.py
```
