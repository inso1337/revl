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

### Python process-lifetime physical root binding (API 1)

Trusted Python hosts can opt into a **supported, separately versioned** bootstrap
API before `Session.load` or any filesystem guard use:

```python
from revl.fs_workspace import PINNED_ROOT_API_VERSION, bind_workspace_root

if PINNED_ROOT_API_VERSION != 1:
    raise RuntimeError("unsupported physical workspace binding API")
bind_workspace_root(root_fd, expected_dev, expected_ino, root_label=root_label)
```

`bind_workspace_root(root_fd: int, expected_dev: int, expected_ino: int, *,
root_label: str) -> None` duplicates the caller-owned directory descriptor,
checks its device/inode/type and checks that the label names that directory at
binding. The label must be an absolute normalized string. The caller may close
or reuse its descriptor afterward. `revl.fs_workspace` locates the Python
backend in either the checkout or wheel and binds the **same module state**
used by the real emitted stdlib bodies, not a second copy. A conflicting
already-imported backend installation is refused.

After binding, the label is an immutable namespace, **never authority to reopen
its pathname**. Every named-endpoint walk, sidecar directory, inverse source,
mutation and `READ_HELPERS` lookup starts from a duplicate of the pinned fd.
Preimages are read from the verified write fd and copied/cloned into the same
physical root. Renaming the root and replacing its old pathname therefore
continues on the admitted original directory, including rollback; it never
switches to the replacement. `REVL_FS_WORKSPACE` changes have no effect.

Bound mode accepts only absolute paths under the original label, with no `..`
components. It refuses **all symlinks below the root**, including internal
aliases; no-follow descriptor traversal re-establishes this at syscall time.
It does not add capabilities or broaden metadata access. Sidecar inverse
sources remain restricted to their matching runtime-reserved directories;
this API exposes no general file-reading or mutation helper to the host.
`resolve_within` returns a label, **not a pathname safe for a host `open()`**.
Hosts doing their own reads or classification must use their own admitted
directory fd and no-follow traversal, and retain their existing `.revl`/private
metadata exclusions. The observations below do not make arbitrary host reads
physically pinned.

The binding is process-lifetime and has no close, unbind, or rebind API: the
runtime cannot prove that all live handles, sessions and retained witnesses
have discharged. Its private fd is non-inheritable across exec and stays open
until process exit. Run one root per actor process; bind before starting work,
not after forking an already active runtime. Keep the actor alive while its
lifecycle remains owed. **Exit does not commit, abort, delete sidecars, or
recover witnesses.** Serialized witness paths do not encode physical identity:
post-crash recovery must independently re-establish the admitted physical root;
replaying under an unbound or newly granted replacement root is not protected
by this process-lifetime contract.

Failures raise the exported `FsOpError` / `ConfinementError` with explicit
codes: `EBOUND` for duplicate or late binding, `EIDENTITY` for identity/type
mismatch, `EINVAL` for malformed arguments, ordinary descriptor/path errno
codes where applicable, and `ENOTSUP` if directory-fd/no-follow primitives are
unavailable. A failed bind releases its duplicate and leaves no partial binding.
There is no unsafe platform fallback. Without binding, legacy relative paths,
symlink resolution and environment-based root selection remain unchanged;
root replacement is still outside that legacy mode's guarantee.

This is not a root-ownership registry, protection from arbitrary host Python,
or serialization against other writers. A descendant directory already held
open may itself be moved by another writer; pinning the workspace root is not a
filesystem-wide namespace lock. Hosts still own exclusive actor/root lifecycle
coordination and must not reload the guard or manipulate its private fd.

`tests/test_fs_pinned_root.py` runs fresh processes and real `Session.load(...,
record=True)` / `FsOpsC` methods, with barriers immediately before actual opens,
preimage creation/copy, writes, inverse classification and rename/unlink/rmdir
syscalls. The replacement tree stays unchanged across repeated write/abort
cycles, while original preimages and garbage are consumed by real inverses.

### Trusted committed-preimage cleanup (API 1)

The Python-only host facade separately exports
`COMMITTED_SIDECAR_API_VERSION = 1` and:

```python
finalize_committed_sidecar(
    path: str, expected_sha256: str, *, expected_dev: int, expected_ino: int
) -> None
```

This is **not** a Revl extern, agent/plugin tool, general deletion API, commit
receipt, or proof that a witness has discharged. Before invoking it, the trusted
host must capture authoritative ownership of the actual live witness's preimage
(original-label path, SHA-256, device, inode), durably prepare its ledger, obtain
and durably acknowledge a successful actual `Session.commit_confirm`, and retain
exclusive actor/root lifecycle ownership. It must drain all cooperative
filesystem actors and hold **exclusive sidecar-directory write ownership**
through capture, commit, and finalization: no concurrent forward captures,
inverses, or other sidecar mutations. No boolean argument can prove that premise.
Unknown ownership or missing/lost commit acknowledgment never authorizes cleanup.

Under that contract, the helper requires an active pinned root and accepts only
the exact normalized original-label path
`<root>/.revl-fs-preimage/pre-<32 lowercase hex digits>`. The directory must be
caller-owned and private (no group/other permissions), as when the runtime
creates it with mode `0700`; cleanup never repairs permissions. The leaf must
be regular, no-follow, singly linked, match the captured device/inode, and have
the supplied 64-lowercase-hex SHA-256 digest. Descriptor-relative traversal,
reading, and unlink stay on the admitted physical root even if its label is
renamed or replaced. No project paths, `.revl` metadata, garbage sidecars, nested
paths, symlinks, or recursive/directory cleanup are accepted.

Successful removal returns `None`. Refusals raise `FsOpError` (including
`ConfinementError`): `EWORKSPACE` without binding, `EINVAL` for malformed digest
or identity, `EOUTSIDE` for a forbidden target/type/directory, `EIDENTITY` for
identity/link-count/content changes, and ordinary OS codes where applicable.
**`ENOENT` is an error**, including a second call after successful removal.
The helper cannot distinguish interrupted successful cleanup from evidence
that went missing for another reason. Hosts must preserve that uncertainty,
freeze/quarantine the lifecycle, and reconcile their durable ledger explicitly,
not silently retry or clean unknown evidence after restart.

The helper checks the opened file and final named entry for detectable changes
before unlink; detected mismatch preserves evidence. **Portable POSIX unlink is
not inode-conditional.** Arbitrary same-UID/native writers violating exclusive
sidecar access can still replace the name between the final check and unlink.
This is outside the supported finalizer concurrency contract, not a race this
API claims to close. Exclusivity applies to reserved metadata cleanup only:
root pinning and no-follow project-path confinement do not rely on it.

`tests/test_fs_committed_sidecar.py` uses real files, identity/content corruption,
syscall barriers for root replacement, and actual recorded write/commit/reopen/
abort/reopen cycles. Sidecar-swap probes cover detectable stale evidence, not
adversarial safety across the final check/unlink window.

### Runtime-owned cleanup handle (API 1)

`finalize_committed_sidecar` proves none of its lifecycle premises: the host
reconstructs the sidecar's ownership, asserts elsewhere that the commit landed,
and passes the pieces in as loose arguments. The Python-only facade also exports
`COMMITTED_SIDECAR_CLEANUP_API_VERSION = 1` and a handle that folds ownership and
commit-acknowledgment into one opaque token instead:

```python
issue_committed_sidecar_receipt(
    path: str, expected_sha256: str, *, expected_dev: int, expected_ino: int
) -> CommittedSidecarReceipt
CommittedSidecarCleanup(receipt: CommittedSidecarReceipt)
CommittedSidecarCleanup.run() -> CleanupOutcome  # .completed / .state / .code / .message / .path
```

`issue_committed_sidecar_receipt` is the **only** way to obtain a
`CommittedSidecarReceipt`: its private grant never leaves the runtime, so a host
cannot forge one. The trusted commit path calls it *after* it holds the durable
acknowledgment of the actual `Session.commit_confirm` that the finalizer declines
to assume — that is where the acknowledgment is asserted, giving it one owner and
one carrier rather than scattered host state. Minting validates the same
argument shape the finalizer requires (64-lowercase-hex digest, nonnegative
device/inode), so a receipt can never name a target the finalizer would only
reject later. The runtime still cannot prove the host's storage flushed; this
widens nothing the finalizer promised.

A `CommittedSidecarCleanup` refuses to construct from anything that is not a
runtime receipt (`ERECEIPT`), and a receipt is single-use: binding it to a handle
consumes it, so one receipt authorizes cleanup of exactly the one sidecar it
named. `run()` performs the same `finalize_committed_sidecar` under the same
exclusivity contract — it makes **no** stronger atomicity claim against writers
violating that exclusivity — but *reports* the result rather than raising:
`CleanupOutcome(completed=True)` when the owned preimage was removed, or
`completed=False` with the finalizer's own `(code, message, path)` when the
sidecar is **unresolved** (missing evidence, a detectable mismatch, a lost root,
or an unbound process). An explicit missing/mismatch failure stays unresolved; it
is never silently reported as "already clean". Success is idempotent — a
completed handle re-reports completed without a second filesystem touch — and an
unresolved handle stays live, so the host can drain cooperative actors and call
`run()` again.

`tests/test_fs_committed_sidecar_cleanup.py` covers completed removal and its
idempotence, an unresolved-then-retried-to-completion cycle, unresolved
missing-evidence reporting, the receipt gate (no receipt, forged grant, malformed
ownership, single-use), and unbound reporting, all against real files.

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
own reading with the plain host filesystem module in **legacy, unbound mode**.
In pinned mode it must instead read through its admitted fd as described above:

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

## Guarded native writes: receipts and expected-before checks (`revl.fs`, issue #523)

The witnessed catalog above overwrites unconditionally: `write` truncates the
target whatever its current state, and the `WriteWitness` records where the
preimage went, not which inode the write landed on. A consumer (revl-harness)
that guards reversible writes against external drift needs two things the host
cannot add from outside, because anything it checks by name before the native
open is re-derived from the name and a same-UID writer can swap the target in
between: an *expected-before* content guard evaluated on the held descriptor, and
a *receipt* whose identity is captured from that original descriptor.

`revl.fs` is the supported, opt-in Python surface for exactly that. It exports
`WRITE_RECEIPT_API_VERSION = 1` and:

```python
from revl import fs

fs.write(path, data: str, *, expect=<prior_digest | fs.ABSENT | None>) -> fs.WriteReceipt
# WriteReceipt(path, prev_digest, new_digest, replaced)
```

`expect=` is checked on the descriptor `open_confined_write` verified, **before**
the snapshot and before the truncate, so a refusal never mutates the target —
there is no partial write:

- `fs.ABSENT` — refuse (`EEXPECT`) if the target already exists; the existing
  file is not overwritten.
- a `"sha256:..."` digest (a prior receipt's `new_digest`) — refuse (`EEXPECT`)
  unless the target currently exists and its content still hashes to it. A
  drifted or absent target leaves nothing changed.
- `None` — no guard.

`WriteReceipt.prev_digest` is the original content's digest read through the held
fd (or `None` for a created target), `new_digest` is the bytes just written, and
`replaced` distinguishes an overwrite from a create. Feeding one call's
`new_digest` back as the next call's `expect=` makes the second write conditional
on nothing having changed in between (a compare-before-mutation, **not** an atomic
CAS: a same-UID writer racing the final check and syscall stays outside the
guarantee, exactly as `confirm_landed` documents for the post-write race).

This is a thin shim over the confined-write machinery
(`backends/python/revl_fs_workspace.py`: `open_confined_write`,
`original_receipt`, `expect_existing`, `write_through`, `confirm_landed`,
`discard_write`). Every mutation still routes through the workspace boundary, so
confinement is retained: a write outside the root is `ConfinementError`
(`EOUTSIDE`), an unconfigured root is `EWORKSPACE`, and a lost race is `ERACE`
rather than a false success. Nothing here is wired into the witnessed
`stdlib/fs.rvl` `@py` bodies, so legacy execution is byte-identical for a caller
that does not use this surface (issue #523 requirement 7).
`tests/test_fs_public_write_api.py` pins the receipt fields, the three `expect=`
modes (match writes, mismatch/absent refuses with no partial write), the digest
round-trip, and the confinement refusals; the underlying identity-vs-content
distinction (a same-bytes inode swap cannot forge the original receipt) is pinned
in `tests/test_fs_write_receipts.py`.

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
