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

This is the py reference tier. The other five tiers (ts, rust, go, java, wasm)
land their `@py`-equivalent bodies in Slice 2b; the language form, the runtime
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
2. **The inverse path is guarded too.** `restore`, `unrm`, `unmove`, and
   `rmdir_if_empty` re-run the same check on the path they are about to write.
   A witness that (through tampering or a moved root) now points outside cannot
   make the reversal escape; a confinement failure on an inverse raises and is
   recorded as `restore-residue` rather than silently writing outside.
3. **The reversal machinery lives inside the root.** The session garbage dir
   (`.revl-fs-garbage`) and the preimage snapshots (`.revl-fs-preimage`) are
   subdirectories of the workspace root, so an `rm` parks its target inside the
   boundary and a `write` snapshots inside the boundary. Reversal reads only
   from inside the root.

## Honest caveats

These are load-bearing limits, documented so the reversibility claim is not
oversold:

* **Reversal does not undo observation.** A concurrent process that read the
  intermediate state already saw it; restoring the bytes cannot unsee that.
  This is fine for a private workspace and weaker on a shared tree.
* **Concurrent-writer races are TOCTOU-bounded, not eliminated.** The realpath
  check narrows, but does not close, the window between the check and the
  syscall. A writer racing the same path can still interleave; the confinement
  guard applies to the same bound (property 1 above notes the residual window).
* **Confinement here is a runtime refusal, not physical containment.** A
  process that cannot even name a path outside the root is a wasm-tier property
  eventually. On this tier the guard holds only for ops that go through these
  bodies; it is not a sandbox around the whole process.

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
