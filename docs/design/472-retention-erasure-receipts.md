# 472: typed data retention and erasure receipts

Design note for roadmap item 472 (issue #824). It records what the item's
premise looks like against the current tree, the one half that lands with this
note, and the measured reasons the other half stays design only with named
prerequisites.

The item has two clauses. The first asks for a `Retained[T]` type carrying a
retention deadline, a geographic residence, legal-hold exceptions, the set of
allowed deleters and whether derivatives are in scope, extending `Secret[T]`
and the taint machinery (items 249/256) with a time-and-residence dimension the
type system enforces at persistence sinks. The second asks that a deletion
request produce a signed erasure receipt naming each in-scope replica and
derivative. The exit criterion states both: a `Retained` value past its deadline
is refused at persistence sinks, and an erasure request emits a signed receipt
naming each in-scope replica and derivative.

The second clause is reachable now, because the tree already enumerates real
per-realm replicas and already has a signing story. The first is not, because
each of its nouns names something the tree does not have. A slice of the first
half would have to be an annotation nothing enforces, which is a stub, so this
note lands the second half and states the prerequisites of the first.

## The premise, measured

**`Secret[T]` has no time dimension.** `src/revl/taint.py` defines
`_QUALIFIERS = ("Untrusted", "Trusted", "Secret")` and treats each as a
qualifier orthogonal to the base type, stripped into the module's side table so
base typing and the emitted IR are unchanged. A `Secret[T]` is a
confidentiality fact about a value's origin. There is no member of that
machinery that says anything about how long a value may be kept, so
"extending `Secret[T]` with a time-and-residence dimension" is not an extension
of an existing dimension, it is a new dimension.

**The rest of the vocabulary does not exist.** `Retained` appears exactly once
in the whole tree, on the roadmap line that specifies it. There is no residence
type, no jurisdiction, no legal hold, no allowed-deleter set and no "derivative"
concept in `src/`. A grep for `legal.?hold|residence|jurisdiction|derivative`
across `src/revl/*.py` returns nothing.

**"Persistence sinks" do not exist as a refusal surface either.** The taint
machinery does have refusal sinks, and they are the closest thing in the tree to
the item's phrase: `taint.py` Decision 4 makes the refusal sinks the positions
where an untrusted value *is* authority, a `Trusted[T]` parameter, plus the
derived sink classes `_SINK_CLASS_SCOPES = {"shell", "exec", "terminal",
"policy"}` read off a crossing's capability scope. There is no persistence
member in that set and no capability scope that means "durable storage". A
`db` or `fs` capability says an extern reaches a database or a filesystem, which
is a resource scope, not a retention policy.

The nearest thing to a guarded write surface is `src/revl/fs.py`, where
`fs.write(path, data, *, expect=...)` and `fs.write_witnessed` refuse a drifted
or unexpected target with a typed `FsOpError` (`EEXPECT`, `ERACE`) and leave the
file untouched. That is a host-tier runtime API with runtime refusals, and it is
reachable only from a host body. The type checker never sees the store, so no
checker-level refusal can be attached to it. Item 472's "refused at persistence
sinks" presumes a sink the type system owns, and there is none.

**The three existing meanings of retention are all something else.**
`resources.retention_surface` (item 308, F10) enumerates the declared positions
where a resource handle leaves revl's sight, and its own docstring calls it an
audit and never a proof: it is about handle lifetime, not data age.
`docs/design/273-witness-retention.md` (item 273) is the undo horizon for
witnesses in the WAL, which is how long a rollback stays possible.
`liveness_confirm.py` and `why_runtime.py` (item 477) have a `deadline` and a
`LIVENESS_EXPIRED` cause, but that deadline is a provider heartbeat ceiling, not
a data-retention date. None of the three is a fact about how long a value may
be stored.

So the honest reading of the first clause is: it is a language change with
prerequisites, not a slice. Which prerequisites is spelled out below.

## The real seam

The second clause is cheap because two things already exist.

**The replicas.** `src/revl/erase_report.py` (item 29) already answers "what
does erasing this realm reach" against the G8 boundary surface
(`revl.query.Composition`), the same surface `revl audit` prints. Its
`_crossings` builds four buckets of real, per-realm crossing tokens:
`emit:<component>:<key>.<method>` for a service emission, `host:<component>:
<extern>` for a reached host extern, `witnessed:<component>:<extern>` for the
class-(a) revertible externs kept in their own bucket, and `widen:<component>:*`
for item 414's first-class-value widenings. Each irreversible crossing already
carries its compensation state (`compensated`, `bare`, or the item-247 third
state `unresolved` when a supplied residue says the offset did not land), and
each is read off the same IR the report was built from. That is an enumeration
of replicas the system actually knows about, produced by code that already
exists and is already tested.

**The signature.** `src/revl/attest.py` signs canonical JSON with a
domain-tagged HMAC-SHA256 (`SIGN_DOMAIN = b"revl.attestation/v2\x00"`,
`_sign`, `canonical_hash`, `key_id`), and `src/revl/deploy.py` does the same
for its admission receipt (`RECEIPT_DOMAIN = b"revl.deploy.receipt/v1\x00"`,
`_receipt_mac`). Two protocols, one construction, two domains. A third signing
story would be a third thing to get wrong, so the erasure receipt is the same
construction with its own domain tag.

## The slice that lands with this note

`src/revl/erasure_receipt.py` turns a report into a receipt: a document a
reader can check rather than one a reader has to decide to believe. The CLI
gains `--receipt-key PATH` and `--receipt-signer NAME` on `erase-report`, and
the receipt rides inside the report under `receipt`, which is the append-only
convention every other member this toolchain adds follows.

Five decisions carry the honesty of the artifact.

**A row per measured crossing, and no other source of rows.** `replicas()` reads
the four buckets above and nothing else, so the receipt can never be more
complete than the measurement it summarises and it never invents a recipient.
The realm comes from the report; a receipt for `vault` cannot name a crossing
`other` made.

**The report's own disposition vocabulary, not a second one.** A witnessed
crossing is `revertible`; a crossing whose token is in the report's
`unresolvedTokens` is `unresolved`; a crossing with a `compensate` clause
attached is `compensated`; anything else is `bare`. `reclaimed` is reserved for
the in-process row and is read only off the R4 no-residue proof
(`inProcessStateGone.proven`), so the one erasure claim in the document is the
one the runtime actually proved. An unavailable or skipped proof reads
`unproven`, which is the honest reading of "we did not measure it" and never of
"it is still there".

**A named channel only when the declaration names one.** `_inverse()` reads the
extern's `undo` clause for a revertible row and its `compensate` clause for a
compensated or unresolved one, through `resources._callee_name`, the same
accessor `resources.closing_ops` uses for the O1 double-close audit. A bare
crossing names nothing, because the declaration names nothing. With no IR in
hand the rows still carry their dispositions and name no inverse, so the receipt
is less dated than it could be and never wrong.

**The report hash goes inside the signed body.** The receipt carries the
canonical sha256 of the report it was issued over, computed over the report
with the `receipt` member removed so the binding is not self-referential. A
report edited after issue stops verifying against its own receipt, which is the
whole reason for the hash.

**The signature is domain separated, and the key is never assumed.** The MAC is
HMAC-SHA256 over `b"revl.erasure-receipt/v1\x00"` plus the canonical body, so an
erasure receipt does not verify as an attestation and an attestation does not
verify as a receipt even under the same key. The key resolves from
`--receipt-key PATH`, then `REVL_ERASURE_KEY_FILE` (a path), then
`REVL_ERASURE_KEY` (the secret). Deliberately not `REVL_ATTEST_KEY`: a
deploy-time attestation key must never silently become an erasure signing key.
A missing key is an error, never a hardcoded default. A run that supplies no key
gets the report this command always produced, byte-identical, so the receipt is
opt-in and nothing that consumes the report today is disturbed.

## What a receipt proves and what it does not

The scope is a member of the signed document rather than a note beside it, so
it travels with the receipt and cannot be detached from it. It proves that the
measurement is unaltered, that every replica the compiler can see is named with
the disposition the report assigned it, and that the receipt was issued by the
holder of the signing key. It does not prove that anything was erased, that an
offset landed, that any derivative was reached, or that no copy exists outside
the boundary. A replica made outside revl's boundary was never a crossing, so it
is never a row, and no signature can name it. That is the same limit item 472's
own wording draws when it says the scope cannot attest to copies made outside
its boundary.

On derivatives: the report has no derivative concept, and the receipt does not
invent one. What it can say is the closest honest thing, which is that an index
write or a summary write that crossed the boundary is a row like any other.
`emit:<component>:<key>.<method>` rows are where a derivative would appear if a
component wrote one through a service, and `host:` rows are where it would
appear through a host extern. Naming a derivative the way the item asks would
need a way to declare that a crossing *is* derived from a named value, which is
the same missing vocabulary as residence and legal hold.

## The retention half: prerequisites

A `Retained[T]` whose value past its deadline is refused at persistence sinks
needs four things that do not exist, and each one is a change larger than this
item.

1. **A sink the type system owns.** Either a capability scope that means
   durable storage, added to the refusal-sink set the way `shell` and `policy`
   are, or a checked write surface in the language rather than in a host body.
   Today the only guarded writes are `fs.py`'s runtime refusals.
2. **A time fact at the sink.** A deadline is a date and the checker has no
   clock. The refusal the item asks for needs an age the checker can compare,
   which means the age has to be a fact in the IR rather than something the
   runtime observes: a value's creation instant would have to be a declared,
   checkable property.
3. **The residence and legal-hold vocabulary.** A residence is a value the type
   system reasons about, and a legal hold is an exception that overrides a
   deadline. Both need declarations and both need to appear in the refusal the
   checker emits.
4. **The derivative relation.** "Whether derivatives are in scope" needs a way
   to say that one value is derived from another, which the taint machinery does
   not model: it tracks where a value came from, not what was made from it.

Landing any subset of these as a declaration nothing enforces would put a
retention guarantee in the language that the checker does not honour, which is
exactly the failure the checker-as-admission-gate promise exists to prevent. The
honest deliverable here is the note plus the half that is real.

## Verification

The receipt is pinned by `tests/test_erasure_receipt.py` against
`tests/fixtures/erase_receipt.rvl`, a composition with one realm `vault` that
crosses the boundary in every shape a row can carry (`stash` witnessed with a
registered `unstash` inverse, `put` carrying a declared `restore` compensate
slot, `leak` bare, `index.add` an in-process service emission) plus a realm
`other` whose crossing must never appear in a `vault` receipt. The file fails
before this change with an `ImportError` for `revl.erasure_receipt` and no
`--receipt-key` in the CLI, and passes after it. The remaining checks are
reported in the pull request with their literal output:

```
python3 -m pytest tests/ -q -k "erase or retention or secret or taint"
python3 tools/docgen.py --check
python3 tools/conformance.py --check-readme
```
