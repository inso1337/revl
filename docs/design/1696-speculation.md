# Speculation by default, one explicit commit (issue #1696)

## The problem

revl can already try a change without the running system seeing it.
`revl_gauntlet` grades a candidate in a scratch session, `revl_canary` tries
one realm, `revl_fork` branches, `revl_undo` rewinds, and since #1690 the
session holds the source it swapped in. But the loop an agent actually runs
still mutates on the first step. `revl_edit` and `revl_change` (#1695) swap as
soon as a patch admits, so "try this" and "do this" are the same call. And the
only durable form of the work is a file the agent writes outside revl.

## The model

There are three states, and only one of them is shared.

1. **The running composition and its held source.** This is what the session
   booted or last swapped in, together with the exact text it was compiled
   from (`session.origin`, including `files_content` for files that edits
   changed). It is the source of truth. Every caller sees it: `revl_state`,
   `revl_call`, and `revl_source` by default.
2. **A proposal.** This is a speculative working copy of the held source, one
   per caller (keyed by the caller's operator identity, so two HTTP callers do
   not share one). A speculative change applies to the proposal and runs the
   whole verification: admission against the running composition, the lease
   and quarantine gates, and optionally the gauntlet. It then stops: nothing
   swaps. Successive speculative changes build on the same proposal. The
   proposal records the generation it was built on.
3. **Disk.** It is an export. `revl_export` writes the held source (state 1,
   never a proposal) to the files it was loaded from, or to a path the caller
   names inside the sanctioned roots. No other verb writes to disk.

The explicit durable step is the **commit**. It re-verifies the proposal
against the running composition as it is now, and swaps it in. If the running
composition moved since the proposal was built (another caller committed), the
commit is refused: replaying a stale full working set would silently undo the
other caller's change. The agent discards and re-proposes instead. A discard
drops the proposal, and the running composition never saw it.

## The verbs

- `revl_change` is the primary loop and is speculative by default:
  - `{intent}` proposes and verifies;
  - `{intent, commit: true}` proposes, verifies and commits in one call;
  - `{commit: true}` with no intent commits the held proposal;
  - `{discard: true}` drops it.

  On a draft (#1727) a speculative change advances the draft. Only
  `commit: true` boots it once it is hole-free.
- `revl_edit` gains `commit: false` to propose instead of swapping. Its default
  stays `commit: true` in this slice, because it is the verb existing clients
  and the benchmark harness call. Flipping it is slice 2, once those callers
  have moved to `revl_change` or pass `commit` explicitly. The two verbs share
  the one proposal.
- `revl_source {proposal: true}` reads the caller's proposal. Without it, the
  read is of the held source.
- `revl_export` writes the held source to disk. For a files-loaded composition
  it writes each loaded file whose held text differs from disk, back to its
  own path. For an inline composition it writes to a `path` inside the
  sanctioned roots, refusing to overwrite a file unless `overwrite: true`. It
  is authorized like `revl_snapshot`, which is the same read of the held
  source with a different destination.

## What is guaranteed

- **A speculative change never swaps and never writes.** The running
  composition and the disk are the same before and after.
- **A commit is the only state change another caller can see.** Before it,
  another caller's `revl_call`, `revl_state` and `revl_source` answer exactly
  as before the proposal existed.
- **A commit verifies again.** The proposal was verified against the
  composition of its own generation. The commit admits it against the current
  one, through the same gates, and refuses outright if the generation moved.
- **`revl_export` writes exactly the held source.** After an export,
  `revl_snapshot`'s `files_content` and the files on disk are byte-identical.

## Slices

1. **This PR:**
   - proposals for `revl_change` (speculative by default) and
     `revl_edit {commit: false}`;
   - commit and discard;
   - `revl_source {proposal: true}`;
   - `revl_export`;
   - the docs present propose, verify, commit as the loop.
2. **`revl_edit` speculative by default**, after its callers move.
3. **A proposal survives a reconnect** through `revl_snapshot`, and
   `revl_fork` can branch from a proposal.
