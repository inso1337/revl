# 478 (provisional): original native write receipts and expected-before filesystem guards

**Provisional roadmap id.** This note is filed against GitHub issue
[#523](https://github.com/inso1337/revl/issues/523) ("Support original native
write receipts and expected-before filesystem guards"). The number 478 is a
placeholder chosen so the file sorts after the last numbered design note; the
orchestrator assigns the real item number at merge and renames this file. Every
reference to "this item" below means issue #523.

Design plus a first opt-in slice. The public `stdlib/fs.rvl` surface is
deliberately NOT changed here: issue #523 asks that public API names be chosen
only after the contract is mapped onto the in-flight roots/cleanup (#500) and
witness/verdict (#498) work. What ships alongside this note is the smallest
tractable, opt-in, not-yet-wired guard-module piece
(`backends/python/revl_fs_workspace.py`: `original_receipt`, `expect_existing`,
`RECEIPT_FIELDS`, and the identity fields on `WriteHandle`), with unit coverage
in `tests/test_fs_write_receipts.py`.

Companion docs:
[243-witnessed-externs.md](243-witnessed-externs.md),
[245-session-commit.md](245-session-commit.md),
[273-witness-retention.md](273-witness-retention.md),
[309-idempotent-inverse.md](309-idempotent-inverse.md),
[../witnessed-fs.md](../witnessed-fs.md).

Source of record for the claims about today's code (as of the branch base):
`backends/python/revl_fs_workspace.py` (`open_confined_write`, `WriteHandle`,
`snapshot_preimage`, `write_through`, `confirm_landed`, `discard_write`,
`PATH_FAMILIES`), `stdlib/fs.rvl` (`write`/`restore` and the `WriteWitness`
type), `backends/typescript/revl_fs_ts.ts` (the ts peer), and
`src/revl/mcp/session.py` (effect emission/recording).

## 1. The gap, stated against the code

The workspace jail already does most of the hard part. `open_confined_write`
resolves the target (family 1), walks to the parent through directory fds with
`O_NOFOLLOW` (family 4), opens the leaf `O_NOFOLLOW`, and `fstat`s the fd it
holds, refusing a non-regular target (`ENOTFILE`) or a hardlinked one
(`EMULTILINK`). `snapshot_preimage` copies from that held fd, `write_through`
truncates and writes through it, and `confirm_landed` (roadmap 431(b)) re-walks
the name afterward and refuses (`ERACE`) unless it still resolves to the written
`(st_dev, st_ino)`. The fd is held across the whole op, so containment, type and
link count are established once and never re-derived from the name.

Two things the consumer (revl-harness) needs are nonetheless absent, and the
issue is precise about why the host cannot add them from outside:

1. **No expected-before content guard.** `write_through` truncates
   unconditionally. There is no way for a caller to say "overwrite this file
   *only if* it is still the file I recorded" (identity, size, or content
   digest), or "*only if* it is still absent". Host-side preflight cannot close
   this: anything the host checks by name before calling `write` is re-derived
   from the name, and a same-UID writer can swap the target between the host's
   check and the native open. The check has to happen on the *held descriptor*,
   inside the jail, before the snapshot and before the truncate.

2. **No original-handle receipt.** `WriteWitness` is `{path, preimage,
   created}`. It records where the preimage went, not which inode the write
   landed on. `confirm_landed` proves the *name still points at the written
   inode*, but nothing binds the effect to the *original* target's identity, so
   post-hoc host hashing cannot prove which inode participated: a snapshot or
   output path replaced with identical bytes before the first host observation
   yields the same digest, and the host has no original-inode fact to compare it
   against.

The issue also notes its own prototype's base has diverged from current main
(for example it describes "path-only APFS clone or a destination descriptor that
is closed before public return", whereas `snapshot_preimage` already clones from
the held fd via `fclonefileat`). That divergence is exactly why this note
re-grounds the contract against the code on this branch before proposing any
public name.

## 2. Mapping the contract onto existing surfaces

Issue #523 asks explicitly to reuse and coordinate rather than to grow a
parallel subsystem. The mapping:

- **#500 (pinned roots, sidecar cleanup).** Owns *where* the jail lives and how
  its sidecars are reclaimed. Receipts and expected-before checks are *content
  and identity* facts about a single target; they reuse #500's roots and add no
  new inventory. The preimage sidecar `snapshot_preimage` already produces is
  the snapshot descriptor whose original facts requirement 4 wants captured.
- **#498 (witness inspection, verdict APIs).** Owns *review* of serialized
  witnesses. A receipt is a new set of fields on the forward witness, not a new
  generic review API; #498 is the surface that would later *read* a receipt.
  This item must not add a competing verdict API.
- **#441 (recorded/raw-context Frame identity).** The receipt binds to the
  Frame/effect entry; preserve the #441 identity correction rather than
  reintroducing a raw-context frame.
- **#513 / #516 (Session, WAL surfaces).** The receipt is WAL-serializable data
  that rides the existing effect/WAL record; coordinate the record shape with
  these rather than opening a second WAL surface. Composition-surface CAS and
  model-decision records are not filesystem expected-before checks and do not
  satisfy requirement 1.
- **#422 (confinement families), #431(b) (`confirm_landed`).** The receipt is
  captured from the same held fd these already rely on; expected-before is a new
  family-4-time check on that fd. When wired, its entry points join
  `PATH_FAMILIES` and gain the table-driven totality (requirement, item 422 F6).

## 3. Proposed slice plan

1. **(this PR) Original receipt + expected-before, opt-in and unwired.**
   Capture `(dev, ino, size)` on `WriteHandle` from the open-time fstat; add
   `original_receipt(handle)` (identity/size/mode/mtime + a digest read through
   the held fd) and `expect_existing(handle, expected)` (refuse `EEXPECT` on
   drift, `EINVAL` on an empty expectation). Unit-tested against the guard
   module. No `stdlib/fs.rvl` change, so legacy execution is byte-identical
   (requirement 7). This is the piece the host provably cannot do for itself.
2. **Existing-required and absent-required open modes.** An
   `open_confined_existing` that opens without `O_CREAT`/`O_TRUNC` (a missing
   existing-required target stays missing, requirement 2) and an
   `open_confined_new` that creates with a single `O_CREAT|O_EXCL` and never
   retries into overwrite (requirement 3), distinguishing namespace mutation
   from content mutation.
3. **Public opt-in witnessed variant + witness fields.** With names chosen after
   coordinating the mapping above: a `write`-family variant that takes an
   explicit expectation and emits a receipt on the `WriteWitness`, versioned so
   an unsupported reader refuses it (requirement 7). Wire the new entry points
   into `PATH_FAMILIES` and the family scan; mirror on the ts tier.
4. **Bounds and partial-call reporting.** Size/write-count/handle bounds
   (requirement 6) and distinct-failure-vs-uncertainty reporting across a
   multi-write call (requirement 5). No automatic compensation or retry.

## 4. Explicit non-goals for this note

Per the issue's boundaries: this is compare-before-mutation, not an atomic
filesystem CAS (a same-UID writer racing the final check and syscall stays
outside the guarantee, exactly as `confirm_landed` already documents for the
post-write race). Multiple writes are not one atomic transaction. Async Session
teardown is a separate lifecycle item and is out of scope. No root reuse, no
whole-root restoration, no recovery replay is introduced here.
