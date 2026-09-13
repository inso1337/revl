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
construction with its own domain tag, and it says so by CALLING the same code:
`erasure_receipt._mac` MACs `attest._canonical_bytes(body)` and
`erasure_receipt.load_key` is `attest.load_key`. The bytes and the key file rule
have one implementation, which is what makes a verifier's reconstruction from
docs/revl-attest.md exact rather than merely similar. (Review of the first
revision of this note found the opposite: the receipt restated both rules, and
both restatements had drifted. See "Corrections" below.)

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
one the runtime actually proved.

The in-process row keeps the same discipline on the two readings that are NOT
`reclaimed`, because the proof is a tri-state and collapsing it would sign two
opposite facts as one word. `proven` true is `reclaimed`; `proven` false (the
teardown ran and the R4 proof found residue, the case `revl erase-report` exits
1 on) is `residue`; `proven` null (no proof was taken) is `unproven`, the honest
reading of "we did not measure it" and never of "it is still there". The row
carries the evidence its disposition summarises, inside the signed body: the
proof's `available`, its `reason` when it was never taken, and `failedChecks`,
the checks that did not hold. `_envelope` re-checks the disposition against
`proven`, so a body claiming `reclaimed` over `proven: false` is refused by the
checker rather than accepted on the strength of its MAC. The unsigned text
report distinguished these cases from the start; this makes the SIGNED artifact
carry the same distinction, which is what a verifier who holds only the receipt
needs.

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
verify as a receipt even under the same key. The canonical body is
`attest._canonical_bytes`'s bytes, `sort_keys=True` with compact separators and
`ensure_ascii=False`, so non-ASCII text (a `--receipt-signer` name, say) is
emitted as UTF-8 and a third-party verifier following docs/revl-attest.md
recomputes the identical bytes. The key file rule is `attest.load_key`'s, called
rather than copied: one trailing newline is stripped, so a key written with
`echo` resolves to the same bytes and the same receipt `key_id` in both
protocols; the erasure key fingerprint is separately domain tagged
(`KEY_ID_DOMAIN`), so the two protocols can never cross-read a fingerprint. The
key resolves from `--receipt-key PATH`, then `REVL_ERASURE_KEY_FILE` (a path),
then `REVL_ERASURE_KEY` (the secret). Deliberately not `REVL_ATTEST_KEY`: a
deploy-time attestation key must never silently become an erasure signing key.
A missing key is an error, never a hardcoded default. A run that supplies no key
gets the report this command always produced, byte-identical, so the receipt is
opt-in and nothing that consumes the report today is disturbed.

## Corrections after review

An independent review of the first revision found three defects in the receipt
and one in the CLI, all now fixed and each pinned by a test in
`tests/test_erasure_receipt.py`. They are recorded here because a design note
that describes a construction the code does not implement is worse than no
note.

1. **`proven: false` and `proven: null` signed the same disposition.** The
   unsigned text report distinguished "the teardown ran and left residue" (exit
   1) from "the proof was skipped", but the receipt signed both as `unproven`,
   and the distinguishing field sat outside the signed row. The disposition is
   now the tri-state above, the row carries the evidence it summarises, and
   `_envelope` checks the mapping from `proven` to the disposition in both
   directions (a row that claims `reclaimed` over `proven: null` is refused
   too). A receipt issued by the first revision whose proof failed therefore
   stops verifying, which is the intended reading of a document that cannot say
   which of two opposite facts it recorded.
   `RECEIPT_VERSION` stays `1.0` because nothing has been issued to be
   compatible with: `src/revl/erasure_receipt.py` is new in this change (no file
   of that name is reachable from `main`), so no first-revision receipt has
   escaped this tree and no external verifier holds a signed `1.0` document. The
   versioning rule stated above (`src/revl/erasure_receipt.py:66-68`: bump MINOR
   for an additive change, MAJOR for a breaking one) is the rule for a version
   that has landed, and read literally it would ask for a MINOR bump here, since
   this change adds a vocabulary value and adds row members. The reason not to
   mint a `1.1` is that `1.0` has never been observed by anyone, so there is no
   reader to protect by keeping an older spelling readable; once a receipt has
   been emitted, an additive change is the MINOR case and this reasoning stops
   applying.
2. **The key file rule had drifted from `attest`.** This module read the key
   verbatim while `attest.load_key` strips a trailing newline, so the same
   `cat`-created file was two different keys and the same receipt MACed and
   fingerprinted twice depending on the protocol that resolved it. It now calls
   `attest.load_key`.
3. **The canonical bytes had drifted from `attest`.** This module escaped
   non-ASCII (`ensure_ascii=True`) where `attest._canonical_bytes` emits UTF-8,
   so a receipt signed by a non-ASCII name failed third-party verification
   while self-verifying here. It now calls `attest._canonical_bytes`.
4. **A key path the process cannot read crashed the CLI.** `--receipt-key` with
   a missing file raised out of the receipt construction as a traceback, unlike
   every other verb. The construction is now inside the handler and answers with
   `error: ...` on stderr and exit 1.

The second review confirmed all four of these and found that the fourth was
necessary but not sufficient, in three ways. They are recorded as
"Corrections after the second review" below, together with the widened catch.

Not fixed here, and reported instead: `deploy._receipt_mac`
(`src/revl/deploy.py:473-489`) still canonicalizes with `ensure_ascii=True`, and
`deploy` reads its `--host-key` and far-host receipt key files without
`attest.load_key`'s newline rule (`src/revl/deploy.py:4195-4203`,
`src/revl/deploy.py:4783-4788`). That is a third, separately written reading of
both rules, and it is pre-existing and documented as deliberate in `deploy`'s
own docstrings rather than introduced by this item, so it is out of this
change's scope and deserves its own issue.

## Corrections after the second review

The second review confirmed the four corrections above, confirmed that the
`proven`/`disposition` machinery now says one thing to every reader, and found
three further ways these guarantees leaked. One of them was introduced by
correction 3 above, which is the reason it is recorded here at length.

1. **A `--receipt-signer` name with no UTF-8 spelling crashed the CLI.** The MAC
   is over `attest._canonical_bytes`, so once correction 3 started signing the
   canonical bytes, a signer that arrived as undecodable bytes
   (`--receipt-signer $'ops-\xff\xfe'`) had no MAC to compute at all, and
   `attest.NotCanonicalizable` is a `ValueError` rather than a `RevlError`, so
   the handler around the construction did not catch it: a traceback on stderr,
   exit 1, and nothing on stdout. The first revision had printed the whole
   report with a signature no conforming verifier could recompute, so correction
   3 traded an unverifiable receipt for a refusal that told an operator less.
   The refusal is now raised in `make_receipt` as a `RevlError` naming the
   flag, and the CLI handler catches `(RevlError, attest.NotCanonicalizable)`,
   the pair `deploy.py` already catches, so no path here can traceback.

   The decision is to refuse up front rather than sign without the name:
   `signer` is a field of the signed body, so quietly dropping it or sanitising
   it would put a name in the receipt that the operator did not write, and
   printing the report while silently skipping the signature would report a
   signing failure as a completed run. A receipt that was not produced is not
   printed as one: stdout stays empty and the exit is 1, which is the answer the
   missing-key path already gives, and the report-only form is one flag away
   (drop `--receipt-key`/`REVL_ERASURE_KEY*`). A signer that IS text is not this
   case, ASCII or not: a `José Müller` signer is signed as UTF-8 and
   verifies, which is the example this note advertises.

2. **`REVL_ERASURE_KEY` with bytes that are not UTF-8 text escaped the same
   handler.** The variable is documented as the secret bytes directly, but a
   string has to be encoded before it can be bytes, and `inline.encode("utf-8")`
   raises `UnicodeEncodeError` (again a `ValueError`, not a `RevlError`) for a
   value the shell handed over as raw bytes (`REVL_ERASURE_KEY=$'\xff\xfeabc'`).
   That is pre-existing rather than introduced, but it is the same class of
   answer and it sits at the call site the fourth correction touched, so the
   inline encode now raises a `RevlError` naming the file route, which carries
   any bytes. A value that is text is unchanged, and that includes a non-ASCII
   secret.

3. **`_envelope` did not check the row's new evidence against its disposition,
   or the rows against the tally that counts them.** Correction 1 added
   `available`, `reason` and `failedChecks` to the in-process row and a
   `summary.byDisposition` tally beside the rows, and checked neither against
   the rest of the document, so five bodies with an intact MAC were still valid
   receipts: `failedChecks` deleted, `failedChecks` naming a check when no proof
   was taken, `reclaimed` beside `available: false, reason: "runtime proof
   skipped"`, `reclaimed` beside a failed check, and a `residue` row counted as
   `summary: reclaimed 1`. `_envelope` now checks, on top of the
   `proven`/`disposition` pair it already checked:

     * the shape of the evidence (`available` a bool, `failedChecks` a list of
       strings, `reason` a string or null), so a dropped member is a refusal
       rather than a row that quietly reads as "nothing failed";
     * `unproven` with a non-empty `failedChecks` is refused, because no proof
       was taken and so no check can have been reported as failed;
     * `reclaimed` requires `proven: true`, `available: true`, no `reason` and
       no failed check, because `reclaimed` is the one reading whose evidence is
       a proof that both ran and held;
     * `summary.byDisposition` has to equal the tally of the receipt's own rows
       (the replica rows plus the in-process row, over the same closed
       vocabulary `build_body` counts), so the word a reader quotes cannot
       disagree with the rows printed beside it.

   Each check reads one member of the signed body against another, so none of
   them re-derives the report and none of them reads unsigned input: the report
   is not consulted, and the tally is computed from rows the MAC already covers.
   What they deliberately do not do is pin the check NAMES to a catalogue: the
   receipt does not own that vocabulary, since `revl run`'s teardown and the MCP
   session's teardown report different check sets, so a list pinned here would
   refuse an honest receipt from the other host. The line drawn is "evidence
   that contradicts the word the row carries", not "a second opinion on the
   proof", so a `residue` row naming a check that in fact held stays formally
   well formed here and is left to the report side.

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

## The retention half: the prerequisites as they were measured

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

## The retention half, landed (`src/revl/retention.py`)

That measurement stands, and the rule drawn from it stands too: a declaration
nothing enforces is worse than no declaration. What changed is that two of the
four prerequisites turn out to be reachable without a language redesign, and the
other two are reachable in a narrower form that can be stated honestly. The
qualifier ships with the refusal attached, so nothing below is an annotation
waiting for a checker.

**Prerequisite 1, a sink the type system owns: SUPPLIED, derived not invented.**
`retention.PERSISTENCE_SINK_SCOPES` is the durable-storage counterpart of
`taint._SINK_CLASS_SCOPES`, and it is read the same way: off the capability the
crossing DECLARES (`db`, `fs`, `store`, `kv`, `blob`, `archive`, `index`,
`cache`, `queue`, `wal`), never off an author qualifier, because the side that
owns the store is the side that knows it is a store. The note's objection was
that a `db` capability "is a resource scope, not a retention policy", and that is
right: the scope says where the bytes go, and the POLICY says how long they may
stay. The refusal needs both, and it now has both. `fs` is deliberately in this
set and in `_SOURCE_CLASS_SCOPES` at once, because a filesystem crossing
honestly is a provenance source on the way in and a persistence sink on the way
out.

**Prerequisite 2, a time fact: SUPPLIED at the policy's granularity, not the
value's.** The checker still has no clock and no per-record creation instant, and
nothing here invents one. What it has is ONE evaluation instant per compile
(`retention.evaluation_instant`: `REVL_RETENTION_AS_OF` when set, so a build is
reproducible and a test is deterministic, otherwise the compile's own UTC wall
clock), compared against the DECLARED deadline. That makes the refusal a property
of admission rather than of the source text: the same program is admitted before
the deadline and refused after it. It is the weaker of the two readings the note
contemplated, and it is the one a static checker can hold. It does not refuse an
individual record on its own age, and `revl.retention`'s module docstring says so
in those words.

**Prerequisite 3, the residence and legal-hold vocabulary: SUPPLIED as a
declaration.** `retention <name> { until, residence, hold, deleters,
derivatives }` is a contextual-keyword declaration (the `secret` / `composition`
/ `layer` discipline, so the self-hosted lexer's KEYWORDS table needs no sync),
and `Retained[T, <policy>]` names one. A declared `hold` OVERRIDES the deadline,
which is what a legal hold is: an instruction to keep data past the date it would
otherwise be disposed of. The override is a declaration, so it is visible in the
source, in the refusal's absence, and in every receipt issued under the policy,
where it turns every row into `withheld:legal-hold` rather than something
erasable. The residence is checked against the replica rows a caller supplies
(`residence-mismatch`) and nothing more: no placement fact is derived, and
nothing here can stop a host from writing bytes to a region it did not declare.

**Prerequisite 4, the derivative relation: NOT supplied, and reported as
declared rather than inferred.** The note is still correct that the taint
machinery tracks where a value came from and not what was made from it. A policy
therefore declares which CLASSES of derivative it covers, out of a CLOSED
vocabulary (`summary`, `index`, `embedding`, `backup`, `export`, `cache`), and
the receipt reports each derivative a caller enumerates against that
declaration. Nothing proves that a particular crossing wrote a summary of a
particular retained value. The vocabulary is closed on purpose: an open one would
let a policy "cover" a class no erasure path can enumerate, which reads as a
guarantee without being one.

### The two refusals, and why there are two

`G-RETAIN` fires in two places, off the same policy and the same evaluation
instant:

  * on the DECLARATION, when a crossing that declares a persistence capability
    also declares a `Retained[T, P]` parameter and P has expired. No call site is
    needed: the declaration is the statement that admitting this program admits
    writing this data to durable storage, and the checker is the admission gate.
  * on the FLOW, when a value carrying the `retained:P` origin reaches a
    persistence-scoped crossing whose own parameter carries no qualifier at all.
    The origin is minted where a `Secret[T]` origin is minted (an extern return,
    a parameter, an interface-only service operation return) and propagates
    through the walk that already exists, so the refusal follows the VALUE rather
    than the spelling at the sink.

The origin is `retained:<policy>`, kept disjoint from every `_ORIGIN_CLASSES`
member by its prefix, and it deliberately does NOT participate in G9. Retention
is a time-and-residence dimension orthogonal to 249's provenance dimension;
collapsing them would refuse a `Retained[Str, P]` value at every `Trusted[T]`
sink for a reason that has nothing to do with trust. A value that is both
untrusted and retained keeps its untrusted origin and is still refused by G9.

### The second receipt, and why it is not a version of the first

`retention.make_receipt` signs a POLICY and the replicas and derivatives a
caller can name under it; `erasure_receipt.make_receipt` signs a MEASUREMENT
`erase_report` took of a realm. They are siblings with separate domain tags, not
two revisions of one artifact, and they share `attest._canonical_bytes` and
`attest.load_key` by calling them for the reason correction 2 and 3 above give.

Neither is a proof of destruction. The retention receipt is an ENUMERATION plus a
signature: it establishes which replicas and derivatives the system knows about,
which policy they were held under, that the requester is a member of that
policy's own `deleters`, and that the document has not been altered. revl cannot
observe a remote replica's disks and nothing here asks a remote host whether it
complied. A derivative the policy does not cover is listed as `not-covered` and
carries, inside the signed body, the sentence that it is not claimed erased,
because the honest answer to "was the embedding index deleted?" under a policy
that never covered embeddings is "the policy does not reach it", never silence.
`_envelope` checks each row's `covered` against the policy's own `derivatives`
member, so an over-claim is refused even with an intact MAC.

### What is still open

  * No per-value age, so no refusal on an individual record's own lifetime
    (prerequisite 2, in the weaker form above).
  * No inferred derivative relation (prerequisite 4).
  * No derived residence: a replica's region is what the caller says it is.
  * The persistence refusal covers a crossing declared through an `extern` with
    a durable-storage capability. A service operation carries no capability of
    its own, so an operation that persists through a provider's extern is
    refused at that extern, which is where the capability is declared.
  * No CLI verb yet: the receipt is a library surface
    (`revl.retention.make_receipt` / `verify_receipt`), the way
    `erasure_receipt` was before `erase-report --receipt-key`.

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

The retention half is pinned by `tests/test_retention_472.py` and by the corpus
fixture `examples/rejections/gretain_expired_at_persistence_sink.rvl`, whose
deadline is absolutely past so it refuses under any clock. The fixture's twin
properties are tested beside it: the same program with a deadline that has not
passed compiles, and so does the same expired program with a `hold` declared.
`tools/build_gate_crate.py --check` reports in sync, because the qualifier is
added in `taint.py` and `parser.py` and touches neither `typecheck.py` nor
`lexer.py`, so no crate digest input moved and the emitted IR for a program
using `Retained[T, P]` is byte-identical to the same program without it.
