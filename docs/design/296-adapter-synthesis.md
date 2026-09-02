# 296: component contract negotiation, safe adapter synthesis

Design note for roadmap item 296 (`docs/v2.0-roadmap.md:3860`): when a
consumer's required interface and a candidate's provided interface differ but
are safely bridgeable, revl generates an adapter, but only under a strict,
complete, named safety predicate. This is design-first; the note itself
changes no compiler code, and it specifies exactly one gate change for the
implementation (alias token carry-over, section 2.1 S2). It records what
already exists and exactly where it stops, defines the
complete predicate (the crux), recommends where synthesis runs (proposed, not
silent), specifies the artifact and its wiring, states the refusal contract,
reconciles the design with 293 (evidence), 294 (parameterized capabilities),
and the 414 fold-completeness campaign, and ends with a staged plan and exit
tests an implementation agent can pick up.

The one-sentence design: an adapter is ordinary revl code, synthesized from a
closed catalogue of provably total transformations, declared at the consumer's
required classification, wired through the existing require seam, and admitted
by the same gate as hand-written code. The gate itself gains exactly one
general wiring feature to make that true, alias token carry-over (section 2.1,
S2), and hand-written wrappers use it identically. Nothing in the pipeline
learns what an adapter is; only the resolver and the `adapt` form do.

## 1. What already exists, and the exact residue 296 covers

The "does provider satisfy consumer" relation is one predicate used four ways:

* `admission._service_compatible(new, old, providers_retained)`
  (`src/revl/admission.py:126`, re-exported from `revl.lower`): in the
  consumer regime (`providers_retained=False`) a method may be added, a
  parameter may widen (contravariant, checked with `typecheck.compatible`),
  a return may narrow (covariant), an emission may be dropped but never
  introduced, capability scopes may narrow but never widen, and
  `async`/`commutative` are fixed.
* `version.diff_services` / `_classify_method` (`src/revl/version.py:134`,
  `:95`) reads the same predicate per operation for the derived semver bump.
* `federation.consumer_surface` / `check` (`src/revl/federation.py:73`,
  `:122`) pins a consumer's required shapes and asks the same predicate
  across a deployment boundary.
* `registry.resolve` (`src/revl/registry.py:861`) uses it as the hard filter:
  "search is admission, run backwards". An incompatible candidate is never
  returned.

`typecheck.compatible` (`src/revl/typecheck.py:596`) already grants a set of
implicit, total, value-directed coercions: `T -> Opt[T]` injection (`:667`),
`Int -> Float` (`:633`), `Int32 -> Int`/`Float` (`:639`), structural records
with equal field sets pairwise (`:615`), function types with contravariant
parameters (`:652`). So type widening and narrowing at unchanged arity already
satisfy the consumer regime with no adapter at all.

One caution up front: the bridge catalogue does not adopt `compatible`
wholesale. `compatible` contains rules that are deliberately permissive and
are not total coercions: `_is_wildcard` (`typecheck.py:590`) makes `Any`,
`Never`, poison, and inference type parameters compatible with everything,
the `Value` rule (`:611`) is compatible both ways, and the
structural-meets-nominal rule (`:624`) returns True precisely because no type
table is available at that call site to resolve the nominal. Section 2.2
defines `compatible_total`, the restricted, table-carrying subrelation the
bridges actually use.

The residue, the exact set of differences that fail `_service_compatible`
today but can carry a total, safe bridge, is:

1. arity: the candidate takes more (or fewer) parameters than the consumer
   requires;
2. head-mismatched but bridgeable types: `Result[V, E]` vs `Opt[V]` and its
   relatives (`compatible` refuses these on head mismatch, correctly);
3. structural record field-set differences (the equal-field-set rule at
   `typecheck.py:621`);
4. differences that are only bridgeable by discarding or fabricating a value,
   which are never safe silently and need explicit opt-in.

Everything else is either already compatible (no adapter needed) or never
bridgeable (section 2.4). Item 296 turns the registry's accept/refuse into
accept / compatible-with-adapter / refuse, with the adapter itself verified.

## 2. The safety predicate

Let `R` be the consumer's required service (its pinned `ServiceDecl`), `P`
the candidate's provided service. `ADAPT(R, P, D)` holds, where `D` is the
author-supplied `adapt` declaration (possibly empty), iff for every method
`m` in `R` there is a candidate method `p` with the same name (v1 rule;
renaming is a listed extension) and a bridge plan for `(m, p)` in which every
position uses one catalogue transformation whose condition holds, and the
five global clauses S1 to S5 hold for the whole method. One failing position
refuses the whole adapter, with the position named (section 5).

The predicate is decidable and syntax-directed: it reads only the two
declarations and `D`, never a body, and it either produces a plan (from which
synthesis is deterministic) or a refusal from a closed enum.

### 2.1 The five global clauses

**S1, totality (never stuck).** The synthesized body is built exclusively
from: parameter references, record construction and field projection,
constructor-complete `match` over `Opt` and `Result` (every constructor has
an arm), the implicit coercions `compatible` already grants, literal
construction of defaults from the absence-shaped table (S1a below), and
exactly one call to the candidate method. Excluded by construction: any
partial operation (indexing, faultable arithmetic, truncating conversion),
any extern, any `Value` boxing (a `Value`-typed position passes through only
as identity; `compatible`'s both-ways `Value` rule at `typecheck.py:611` must
not become a laundering channel). Consequence: for every input the consumer's
types admit, the adapter reaches the candidate call and maps every candidate
outcome to a consumer outcome. It cannot panic, fault, or get stuck.

**S2, effect class preserved.** The adapter method is declared with the
consumer's required classification verbatim: plain stays plain,
`emission` stays `emission`, `emission[caps]` keeps the same token list,
and the `async` and `commutative` flags are copied from `R`. Conditions on
the candidate:

* if `p` is an emission, `m` must be declared an emission (a plain
  requirement is never satisfied by an emitting candidate through a bridge;
  the adapter does not get to hide a boundary crossing);
* the candidate's capability reach must fit inside the consumer's declared
  bound, compared as **boundaries under the joint wiring**, never as
  literal token strings. Capability tokens are wiring keys
  (`docs/capabilities.md`), local to each composition's namespace: a
  candidate declared `emission[redis]` under a consumer declaring
  `emission[cache]` may name the very same store, and a string comparison
  would refuse that pair spuriously, or worse, admit two different
  boundaries that happen to share a spelling. The check resolves each
  side's tokens to the provisions they are bound to in the composed
  manifest and requires the candidate's resolved boundary set to be a
  subset of the consumer's (with 294 valuations intact, section 6.2). The
  unnameable `*` is never bridgeable;
* color and ordering promises are one-way, admissible exactly in the
  direction that *drops* a promise and refused in the direction that
  fabricates one. `async(p)` implies `async(m)`: an async candidate cannot
  hide behind a sync requirement, the consumer's call shape would change.
  But a sync candidate under an async requirement is safe: the adapter is
  declared `async` as required and its single candidate call is sync,
  purer than declared, the same sound direction the G4 capability bound
  already blesses. Symmetrically, `commutative(m)` implies
  `commutative(p)`: a commutative requirement is never satisfied by a
  candidate that made no reordering promise, but a consumer that never
  assumes reorderability may bind a commutative candidate, whose promise
  is simply dropped. `_service_compatible` currently fixes both flags as
  equalities (`admission.py:246`, `:251`); the bridge predicate uses the
  one-way form because the two relaxed directions are precisely the ones
  that discard a promise instead of inventing one, and refusing them would
  turn away safe candidates for no soundness gain.

Enforcement is *mostly* not new code, and the exception is load-bearing, so
it is stated exactly rather than waved through. The synthesized provide
body runs through the existing provider-body upper bound: a plain-declared
method whose body reaches an emission is refused at `src/revl/lower.py:6959`
(G4, emission-propagation), and an `emission[caps]` method whose body's
capability set exceeds the declaration is refused at `lower.py:7005` (G4,
emission-capability), both computed by `_method_emissions`
(`src/revl/emission_analysis.py:346`).

But today that bound **refuses the adapter itself** for every
capability-scoped requirement, including this design's own section 4
example. `_method_emissions` records the *local require key name* as the
capability a seam crossing reaches (`emission_analysis.py:398`,
`caps.add(target.get("name"))`). The adapter binds the candidate under a
fresh internal alias (section 4), say `backing`, so its body
`emit backing.get(...)` computes `used = {backing}` against
`decl.capabilities = (cache,)`, giving `extra = {backing}`, and the
emission-capability bound refuses at `lower.py:7011`. Naming the alias
`cache` instead collides with the adapter's own provided key `cache` and
trips the G3 cycle check. Both doors are closed: under the shipped
attribution rule, no admissible adapter for a capability-scoped
requirement exists at all.

The fix is a first-class **wiring feature, not an adapter special case**:
alias token carry-over. An aliased require binding carries the capability
tokens declared for the consumer-facing boundary it stands in for
(valuations included), which under the joint wiring are the candidate's
declared tokens resolved into the consumer's namespace, and
`_method_emissions` attributes a crossing through an aliased require by
those carried tokens, never by the alias's local key name. G4's subset
check itself is unchanged; only the attribution of an aliased crossing
changes, and it changes for **every** component, so a hand-written wrapper
that renames a require gets exactly the same treatment. That is what keeps
E5, the twin test, meaningful: the synthesized adapter and the
byte-identical hand-written wrapper both lean on carry-over, and both pass
or both fail together.

So the honest form of the "same gate" claim: the gate gains one general
attribution rule, specified in section 6.2, implemented in slice 2, tested
by E5/E6/E8, and after that single change the adapter passes the same
admission the same way hand-written code does. The predicate checks S2 up
front only to refuse early with a resolution-time message; the extended
gate would catch a violation regardless.

**S3, no authority added.** The adapter component requires exactly one key
(bound to the candidate's provision) and provides exactly the consumer's
required service. It declares no extern, imports nothing, and constructs
defaults from literals and constructors only (guaranteed by S1's catalogue,
which contains nothing that can emit). Its reach, as `_method_emissions`
computes it, is exactly the candidate seam: one `req` crossing, 414's
crossing kind 1. The consumer's declared reach is the ceiling (the G4 upper
bound): if the candidate's method reaches a capability outside the consumer's
required declaration, the answer is refusal, never a silent widening of the
consumer's declaration and never a reclassification of the adapter. The
item-66 spawn attenuation (`lower.py:8081` region) sees the adapter as an
ordinary component that holds one require and cannot create authority.

**S4, observation honesty.** Three rules, one asymmetry:

* (a) a value the consumer *supplies* is never silently discarded. Dropping
  a required-side argument, or a supplied record field the candidate does
  not accept, needs a per-position opt-in in `D`;
* (b) a value the consumer never *named* may be projected away: extra fields
  in the candidate's return record that the consumer's required record type
  does not mention are unobservable to the consumer and safe to drop;
* (c) distinct consumer-observable outcomes are never merged without opt-in,
  and the opt-in must be as fine-grained as the error type allows.
  `Result[V, E] -> Opt[V]` merges `Err(e)` with a legitimate `None`; that is
  the cache-outage-reads-as-a-miss bug, and it is exactly what the roadmap
  item calls "error semantics silently weakened". When `E` is a **closed
  variant type** (revl has these, `docs/syntax-2.0.md:84`), the opt-in must
  map every variant **by name**: `NotFound => None` admits, and each
  remaining variant is either mapped with its own honest arm or left
  unmapped, which refuses. A blanket `Err(_) => None` over a variant `E`
  is refused outright, because it is a fail-open trap. The revocation-cache
  shape makes it concrete: the consumer requires
  `revoked(t) -> Opt[Instant]` where `None` means proceed; the candidate
  provides `-> Result[Instant, Error]` where `Error` includes
  `Unavailable`. Under a blanket merge, a backend outage reads as "not
  revoked": fail-open, silent, permanent, and attested as reviewed. Under
  per-variant mapping the author must write an `Unavailable` arm, and
  there is no honest `Opt` arm to write for it, which is the refusal doing
  its job. Per-variant exhaustiveness also carries the right staleness
  behavior for free: a new candidate error variant breaks exhaustiveness,
  the derivation hash changes, re-derivation refuses, and the new error
  surfaces for a human decision instead of silently joining the merge.
  `Err(_) => None` remains available only when `E` is **opaque** (a `Str`,
  a record, anything without a closed constructor set), and it is then
  named for what it is: a **total waiver**. Every error the candidate can
  ever produce, present and future, will read as absence. The refusal hint
  says so in those words (section 5), and the plan records the two shapes
  distinctly (`merge-variant` per arm vs `merge-total`), so a reviewer and
  the 127 attestation can tell "merged NotFound" from "merged everything".

The asymmetry worth stating once: data flowing *toward the candidate*
(arguments) may be defaulted automatically when the default means absence;
data flowing *toward the consumer* (returns) is never fabricated
automatically, because the consumer will read it as truth.

And one structural conflict is named rather than papered over: at the
`Result -> Opt` position, S1 (the adapter is total) and S4 (outcomes are
not merged) genuinely pull against each other. A total adapter returning
`Opt[V]` has no honest destination for an error that must not be merged;
there is no third value to return. The only sound options at that position
are an explicit merge (per variant, or the total waiver on opaque `E`) or
refusal, and the design's job is to make that choice loud, human, and
recorded, never inferred.

**S5, lifecycle neutrality.** The roadmap item requires that "lifecycle
guarantees remain valid", and the adapter satisfies it by having no
lifecycle of its own: the S1 catalogue contains no `config` block, no
`effect` acquisition, no `spawn`, and no teardown-position code, so the
adapter registers no undo obligations and cannot reorder or drop anyone
else's. The candidate's lifecycle (its own `effect` brackets, its teardown
contract, its disposal) is untouched because the adapter only calls the
candidate's provided methods, exactly as any consumer would. The
service-level promises that shape call lifecycle, `async` and
`commutative`, are copied and checked under S2. A bridge that would need
state (memoization, dedup, retry) is outside the catalogue and therefore
refused: that is a component someone writes, not an adapter revl
synthesizes.

### 2.2 The transformation catalogue

Each row states the transformation, its exact admissibility condition, and
why it preserves totality, effect class, and the type contract. `auto` means
admissible with no opt-in; `opt-in` means admissible only when `D` names it;
`refused` means never synthesized (hand-write the wrapper instead).

**B1: argument added with a default** (consumer sends k arguments, candidate
wants k+n). Two conditions, and pairing comes first:

*Pairing.* Whenever arity differs, positional order alone is not evidence
of correspondence. The consumer's k parameters pair with candidate
parameters only where the **parameter names match**; an explicit mapping in
`D` overrides name matching; the unpaired candidate parameters are the ones
considered for defaulting. If more than one same-typed pairing would
satisfy the plan, the method is refused (`ambiguous-pairing`) rather than
resolved by position. The wrong bridge this rule exists to refuse: consumer
`log(message: Str)`, candidate `log(category: Str, message: Opt[Str])`.
Suffix-defaulting by position would pair consumer `message` with candidate
`category` (a B3 identity `Str -> Str`) and default candidate `message` to
`None`: every log line becomes a category with no message, fully
automatically, with every step individually admissible. Name pairing sends
`message` to `message` and then asks whether `category` is defaultable; it
is not (`Str` has no canonical default), which is the correct refusal. At
equal arity with all positions name-matched, positional and name pairing
agree and nothing changes; record fields were already protected because
structural records are name-keyed, and this rule extends the same
protection to parameters.

*Defaulting.* Each unpaired candidate parameter must have a canonical
inhabitant from the closed absence-shaped table:

* `Opt[T]` defaults to `None`;
* `List[T]` defaults to `[]`;
* `Map[K, V]` defaults to `{}`;
* a structural record is defaultable iff it is empty or every field is
  defaultable (so the item's `Options = {}` example is the empty-record
  case);
* `Str`, `Int`, `Int32`, `Float`, `Bool` have **no** canonical default and
  are refused in auto. A fabricated `0` or `""` flows into the candidate as
  if the consumer chose it; only absence-shaped values mean "not provided"
  to a well-behaved candidate.

With an explicit default expression in `D` (pure by S1: literals and
constructors only), any type is admissible; the value is then visible in the
adapter source and the diff. Totality: defaults are literal constructions.
Effect: pure by construction. Contract: the consumer's k parameters pass
through name-paired, each under B3.

One honesty note carried from adversarial review: an absence-shaped default
is a *convention*, not a checked property of the candidate. `delete(key)`
bridged to a candidate `delete(key, scope: Opt[Scope])` where `None` means
"all scopes" deletes strictly more than the consumer asked for, and every
step of that bridge sits inside the auto table. No type-level fact
distinguishes "None means not provided" from "None means everything". For
that reason B1 auto-defaulting stays **proposed-only** for as long as this
design stands: it is excluded from any future `--auto-adapt=safe` subset
(section 3), and the proposal renders the defaulted call in full, so a
reviewer sees `backing.delete(key, None)` and can ask what `None` means to
this candidate before committing it.

**B2: argument dropped** (the consumer's required method has parameters the
candidate lacks). Trivially total and pure. Refused in auto under S4(a):
no type-level fact distinguishes "the candidate ignores it" from "the
consumer believes it matters". Opt-in per parameter (`drop options` in `D`),
which makes the discard auditable.

**B3: parameter type bridge** (consumer passes `X` at a position where the
candidate expects `Y`). Auto iff `compatible_total(Y, X)` holds, where
`compatible_total` is a **named, restricted subrelation** of
`typecheck.compatible`, defined once here and used everywhere in this
predicate (B3, B4, B6):

* identity on equal resolved types;
* `Int -> Float`, `Int32 -> Int`, `Int32 -> Float`;
* `T -> Opt[T]` injection;
* structural records with equal field sets, elementwise `compatible_total`,
  with nominals on **either side resolved to their field sets first**: the
  predicate carries both declarations' type tables, so a nominal is
  expanded before comparison, and a nominal that cannot be resolved
  refuses rather than presumes;
* same-head containers, elementwise `compatible_total`;
* nothing else.

The restriction exists because `compatible` is deliberately permissive in
places that are not total coercions and must not become bridges (section
1). `_is_wildcard` (`typecheck.py:590`) makes `Any`, `Never`, poison, and
inference type parameters compatible with everything, and the `Value` rule
(`:611`) is compatible both ways; a wildcard or `Value` at a bridged
position would admit any candidate shape and prove nothing, so
`compatible_total` refuses these positions outright
(`non-total-conversion` naming the wildcard). And the
structural-meets-nominal rule (`typecheck.py:624`) returns True precisely
because that call site has no type table to resolve the nominal against;
under it, a candidate nominal `UserRecord` would auto-bridge to a consumer
structural `{name, age}` regardless of `UserRecord`'s actual fields. The
predicate has no such excuse: resolution holds both manifests, so it
carries both type tables into the relation and resolves, and refuses where
it cannot.

Anything the language requires an explicit spelling for is likewise
refused: `Int -> Int32` can lose bits and is spelled `.to_int32()`
(`docs/arithmetic.md`), `Float -> Int` likewise. The rule in one line: the
adapter may use exactly the *total* subset of the checker's implicit
coercions, resolved against real type tables, and nothing more. (At
unchanged arity, positions passing the permissive `compatible` already
pass `_service_compatible` with no adapter; B3's stricter relation matters
inside methods that also need B1/B2/B4, and extra strictness there can
only refuse a bridge, never a direct binding.)

**B4: return type bridge** (candidate returns `Y`, consumer requires `X`):

* auto iff `compatible_total(X, Y)` (identity, the numeric widenings,
  `V -> Opt[V]` injection, resolved structural equality; the same
  restricted relation as B3, with wildcard and unresolved-nominal
  positions refused);
* `Result[V, E] -> Opt[V]`: constructor-complete `match`, graded by the
  shape of `E` per S4(c). When `E` is a **closed variant type**, the
  opt-in is **per-variant**: `D` maps every variant by name
  (`NotFound => None`, each other variant with its own honest arm), every
  arm total and pure, and a missing variant refuses
  (`unmapped-error-variant` naming it), so a variant added to the
  candidate later breaks the plan at re-derivation and surfaces for a
  decision. When `E` is **opaque** (Str, a record, no closed constructor
  set), `Err(_) => None` is admissible as an explicit **total waiver**,
  opt-in under S4(c), with the waiver spelled out in the hint and the
  plan recording `merge-total` (section 5);
* `Opt[V] -> Result[V, E]`: requires fabricating an error for `None`. `E`
  has no canonical inhabitant, so this is **opt-in** with an explicit pure
  error expression (`None => Err({"code": "ENOENT", ...})`); refused
  otherwise;
* `Result[V, E1] -> Result[V, E2]`: auto iff `compatible_total(E2, E1)`.
  The permissive `compatible` is never consulted here, for the same reason
  as B3: a permissive True on the error types (a wildcard, an unresolved
  nominal) would silently change the error shape the consumer matches on.
  Otherwise opt-in with an explicit `Err` mapping, per-variant when `E1`
  is a closed variant;
* `Opt[V] -> V`: refused in auto (a `None` has nowhere honest to go);
  opt-in with an explicit value, same fabrication rule as B1's explicit
  form, and the same warning: this merges absence into data.

**B5: field reorder.** Structural records are name-keyed; order is not part
of the type (`typecheck.py:615` compares field *sets*), so reorder is the
identity bridge: no adapter code is generated and the plain check already
passes. If a target-tier representation is positional, that is the existing
seam canonicalization's job, not 296's. Listed only so the catalogue is
visibly complete.

**B6: structural record difference.**

* Return side: the candidate returns field set `F_p`, the consumer requires
  `F_r`. If `F_r` is a subset of `F_p` with each shared field bridgeable per
  B4: **auto** projection (S4(b): the consumer never named the dropped
  fields). If `F_r` has fields `F_p` lacks: refused in auto (fabricating
  data the consumer will read); opt-in per field with an explicit pure
  expression.
* Parameter side: the consumer sends `F_r`, the candidate wants `F_p`.
  Fields in `F_p` minus `F_r`: defaultable per B1's absence-shaped table,
  auto. Fields in `F_r` minus `F_p`: supplied-data discard, opt-in per
  field per S4(a).

### 2.3 The predicate, assembled

ADAPT(R, P, D) holds iff, for every method m of R:

1. P has a method p named m (v1);
2. there is a per-position assignment of catalogue transformations covering
   every parameter of m and p and the return, each condition satisfied;
3. every transformation graded opt-in is named in D at that position;
4. S1 holds (the plan uses only the closed body catalogue);
5. S2 holds (classification copied from R; emission implication, token
   subset on boundaries under the joint wiring, one-way color and
   commutativity against P);
6. S3 holds (one require, no externs, pure defaults, candidate reach inside
   the consumer's declared bound);
7. S4 holds (no un-opted discard, no un-opted fabrication toward the
   consumer, no un-opted outcome merge);
8. S5 holds (stateless by construction: no config, no effect acquisition,
   no spawn, no teardown obligations).

An adapter that could panic (S1), add authority or emission (S2/S3),
silently drop a needed error (S4c), or carry lifecycle of its own (S5) is
refused. There is no best-effort partial adapter: the plan admits whole or
refuses named.

### 2.4 Never bridgeable

For completeness, the differences no `D` can opt into, because no total,
effect-preserving bridge exists:

* an `async` or `commutative` change in the promise-fabricating direction:
  an async candidate behind a sync requirement (the consumer's call shape
  would change), or a commutative requirement over a non-commutative
  candidate (a reordering promise nobody made). The two opposite
  directions, sync candidate under async requirement and commutative
  candidate under a non-commutative requirement, drop a promise and are
  admissible under S2;
* an emitting candidate behind a plain requirement, or candidate capability
  tokens outside the consumer's declared scope (widening the consumer's
  declaration is an interface change, not an adapter);
* `*`, the unnameable reach (first-class emitting callables,
  `emission_analysis.py:152`): a bound that cannot be named cannot be
  checked, so it cannot be bridged;
* a method of R absent from P (v1: no rename mapping; listed extension);
* a conversion outside the implicit coercion set with no opt-in spelling
  above (for example `Float -> Int` anywhere).

## 3. Where it runs: proposed, not silent

Two candidate homes:

**(a) Silent, at resolve/link.** The resolver finds P, the predicate holds,
the adapter is synthesized and wired with no visible artifact. Maximum
convenience, and dangerous in exactly the way the item warns: an
`Err => None` merge admitted invisibly is a semantic decision nobody made.
It also breaks reproducibility (the composition is no longer derivable from
committed source alone) and hides the bridge from the 123 diff and the 127
attestation, which is the opposite of "included in the diff + attestation".

**(b) Explicit and proposed.** `revl resolve` reports a candidate as
`compatible-with-adapter`, returning the bridge plan, the generated `adapt`
text, and the near-miss refusals. The author (usually an agent) commits the
`adapt` declaration; the compiler synthesizes the component from it
deterministically; the whole composition re-runs the ordinary gate.

**Recommendation: (b), for v1 and as the default forever.** This is the 307
precedent applied unchanged: the minimal-capability repair patch is
"PROPOSED, never silently applied, then re-run through the gate"
(`docs/v2.0-roadmap.md:3885`), and an adapter is strictly more semantic than
a capability-list shrink. The registry loop already returns source to commit
in two calls (`registry.py` module docstring), so the proposal costs no
extra round trip in the agent workflow; the `adapt` block rides the same
commit as the import. The trade-off stated honestly: auto mode would make
some resolutions work with zero edits, and the no-opt-in subset is typed
end to end. But it is not semantically airtight, and the review's
counterexamples say why: B1's absence-shaped default is a convention the
type system cannot check (the `delete(key, scope: Opt[Scope])` widening,
B1), and pairing under arity change needed a name condition to exclude a
fully-auto wrong bridge (the `log` example, B1). A wrong silent bridge
ships a semantic bug with no reviewable artifact, and every 414 postmortem
is a fold that was airtight on paper. If soak time earns it, an explicit
`revl link --auto-adapt=safe` flag can later enable only B6 return
projection and B3/B4 `compatible_total` passthrough; B1 auto-defaulting is
permanently excluded from that subset, and the opt-in transformations are
never auto and never by default.

Either way the synthesized component passes the **same admission gate as
hand-written code**: G2 provision disjointness and G3 cycles over its wiring,
G4 both provider-body bounds (`lower.py:6959`, `:7005`) under the S2 alias
token carry-over rule, A6 completeness for the service it provides, the
item-66 attenuation fold, and the item-33 policy gate. The gate is extended
exactly once, by carry-over, and that extension is a general wiring feature
hand-written wrappers use identically (S2); beyond it, no gate learns what
an adapter is. That is the design's main safety property, and it is
testable (exit test E5).

## 4. The artifact

The synthesis output is an ordinary generated component in the target tier,
riding the existing seam:

```
// generated: revl adapt cache from vendor_cache
// derivation: sha256(R-surface, P-surface, P-sha, adapt-decl, catalogue-v1)
component CacheAdapter requires backing: VendorCache provides cache: Cache {
  provide cache {
    fn get(key: Str) -> Opt[Str] {
      // B1: options unpaired, defaulted to {}  (auto, absence-shaped)
      // B4: Result[Str, Error] -> Opt[Str]     (opt-in: Error is an opaque
      //     record here, so the total waiver Err(_) => None is admissible;
      //     a closed variant Error would demand per-variant arms instead)
      return match backing.get(key, {}) {
        Ok(v)  => Some(v),
        Err(_) => None,
      }
    }
  }
}
```

If `Cache.get` is declared `emission[cache]` by the consumer, the provide
method is declared the same and the body says `emit backing.get(key, {})`;
S2 already required the candidate's classification to fit inside it.

Properties:

* **Deterministic.** The derivation hash pins consumer surface, candidate
  surface and sha, the `adapt` declaration, and the catalogue version.
  Re-running synthesis is byte-stable; the hash is the adapter's identity
  for evidence (section 6.1) and staleness (either side changes, the hash
  changes, the stale adapter fails to re-derive).
* **Source-honest.** Synthesis is IR-level so every backend emits it through
  the normal pipeline (no per-backend codegen), but `revl adapt --emit`
  renders the .rvl source above, and that rendering is what the 123 diff
  and review show. The audit shows the bridge, not a hidden coercion: the
  G8 chain for a consumer call reads
  `consumer -> CacheAdapter.get -> backing.get (emission[...])`, two hops,
  real boundary named, exactly what `_method_emissions` produces for any
  wrapper.
* **Wiring.** The adapter provides the consumer's required key; the
  candidate's provision is bound under a fresh internal alias so G2 sees
  exactly one provider of the consumer-facing key. This aliasing is where
  S2's carry-over rule earns its keep: without it, `_method_emissions`
  attributes the body's crossing to the alias key and G4 refuses the
  adapter itself (S2 walks through the exact failure). The resolver's
  manifest wiring must rename the candidate's provided key, and the alias
  binding must carry the consumer-facing tokens, valuations included
  (section 6.2).
* **Placement.** The adapter lands in the candidate's placement unit, so
  the consumer-to-adapter seam is the same seam the consumer-to-candidate
  wiring would have had: no new process or tier crossing appears (414
  crossing kinds 5/6 unchanged).

## 5. Failure honesty

A refusal names which method, which position, which transformation, and
which clause failed, so an author can fix it by hand. The refusal record
mirrors `_Drift` (`admission.py:102`):

```
{method, position, transformation, clause, reason, hint}
```

where `position` is a parameter name, `return`, or a record field path, and
`clause` is one of a closed enum:

`no-canonical-default`, `ambiguous-pairing`, `supplied-value-dropped`,
`outcome-merge`, `unmapped-error-variant`, `fabricated-return`,
`non-total-conversion`, `effect-missing-declaration`,
`effect-exceeds-bound`, `color-mismatch`, `commutative-mismatch`,
`method-missing`, `unnameable-reach`.

On the admitting side, the plan records each opted-in outcome merge with
its shape: `merge-variant` per named arm, or `merge-total` for the opaque-E
waiver, so the 123 diff and the 127 attestation distinguish "merged
NotFound" from "merged everything" without reading the arms back out of
the source.

Message style follows the shipped G4 refusals (message plus repair hint):

```
adapter refused: `Cache.get` return merges outcomes
  candidate returns `Result[Str, Error]`; the requirement is `Opt[Str]`.
  Folding `Err` into `None` makes a failure indistinguishable from a miss.
  hint: `Error` is opaque, so the only opt-in is the total waiver
  `adapt cache { get: Err(_) => None }`: every error this candidate can
  ever produce, present and future, will read as absence. Opt in only if
  absence is a safe reading of any failure whatsoever, or require
  `Result` and handle the error at the call site.

adapter refused: `Revocation.revoked` leaves `Unavailable` unmapped
  candidate returns `Result[Instant, Error]` and `Error` is a closed
  variant; the plan maps `NotFound => None` but names no arm for
  `Unavailable`. A backend outage must not read as "not revoked".
  hint: map every variant honestly, or require `Result` and decide at
  the call site. `Err(_)` is not accepted for a closed variant type.

adapter refused: `Cache.get` exceeds the declared reach (G4)
  the candidate implementation is `emission[net, cache]`, but the
  requirement declares `emission[cache]` - `net` has no covering token.
  hint: widen the required declaration to `emission[cache, net]` if the
  consumer accepts that boundary, or pick a candidate that does not
  reach `net`. An adapter never adds authority.
```

Near-miss refusals ride the resolve response: a candidate that fails only
the predicate is reported with its refusal list, so "fix by hand" starts
from the exact positions, not from a diff hunt.

## 6. Interactions

### 6.1 Item 293, evidence through the bridge

The evidence bundle (`registry.py:47`, roadmap `:3845`) certifies the
candidate's own surface and behavior. Through an adapter:

* **capabilities.json does not transfer.** The pair's G8 surface is
  recomputed at synthesis (it is just compilation), and S2/S3 guarantee it
  is the candidate's surface bounded by the consumer's declaration.
* **fault-sweep, inverse-roundtrip, gauntlet, attestation, provenance
  still describe the candidate**, whose behavior the adapter calls into
  unchanged, so resolve ranks an adapted candidate by the candidate's
  evidence. But the ranking gains one bit ahead of evidence: at equal
  authority fit, a directly compatible candidate outranks an adapted one
  (the bridge is a cost, not a tie). The candidate row is annotated
  `via adapter` with the plan summary in its `why`.
* **One evidence class is discounted by the plan itself.** A fault-sweep
  conclusion of the shape "failures surface as `Err`, data is never
  corrupted" is *inverted* by a plan that merges outcomes: behind a
  `merge-total` waiver (or any merge arm folding an error into absence),
  those dutifully surfaced errors are exactly what the consumer stops
  seeing. So whenever the plan contains an outcome-merge opt-in, resolve
  discounts the candidate's error-semantics evidence class in ranking and
  flags the inversion in `why` ("fault sweep attests errors surface; this
  plan merges them into `None`"). Inverse-roundtrip and gauntlet evidence
  describe value behavior the bridge leaves untouched and rank at full
  weight.
* **Pair evidence accrues to the pair.** A gauntlet or fault sweep run
  through the adapted surface is recorded against the derivation hash
  (candidate sha plus adapter derivation), not against the bare candidate;
  either side changing invalidates it. The adapter itself needs no runtime
  evidence to be *safe* (the predicate plus the gate are that proof);
  evidence answers behavior questions types do not.

### 6.2 Item 294, parameterized capabilities survive unchanged

A bounded token like `fs.write(path="/tmp/job-42")` crosses the bridge
verbatim: the adapter copies the consumer's declared token list including
valuations (S2 copies the classification literally), and the S2 subset check
compares candidate tokens against consumer tokens under 294's extended
partial order (tighter-or-equal admits, wider refuses). The adapter neither
widens nor narrows a valuation; it has no spelling that could.

The load-bearing seam is the one the 294 note already names
(`docs/design/294-parameterized-capabilities.md`, "the elements the fold
compares are WIRING KEYS, not declared tokens"), and S2 shows it is not
merely load-bearing but currently *closed*: `_method_emissions` attributes
an aliased crossing to the bare alias key, so the G4 folds never see a
valuation and the emission-capability bound refuses the adapter outright.
The carry-over rule of S2 is the fix, restated here in fold terms: the
alias require binding carries the consumer-facing declared tokens,
valuations included, as its capability contribution, and every fold that
walks `_method_emissions` output (G4, the attenuation fold, `component_reach`,
the G8 audit) reads the carried tokens through the alias. Slice 2
implements exactly this, as a wiring feature any component's renamed
require gets, and the S2 subset check then compares valuations under 294's
extended partial order on the resolved boundaries, never on token
spellings.

### 6.3 The 414 folds see through the adapter

The adapter must not become an eleventh crossing kind. By construction it is
not: its only crossing is kind 1, a `req` seam call, which every
authority-derivation surface (A through G in
`docs/design/414-reach-completeness.md`) already visits. But "by
construction" is the exact phrase every 414 postmortem starts with, so the
claim is asserted, not assumed: the reach-completeness matrix gains
adapter-mediated variants of the crossings an adapter can front (a plain
`req` emission, a caps-scoped emission, a deferred class-(b) emission), and
every in-scope surface must attribute the *candidate's real* emission
through the adapter hop: the approval ClassMap fold classes the adapted call
by the real boundary, `policy.component_reach` includes the candidate's caps
in the consumer-side reach, the G8 audit chain shows both hops, and the
taint origin fold flows provenance through. A fold that reports the adapter
as the terminal boundary is a completeness bug, caught by these cells.

### 6.4 Adapters over adapters

A committed adapter is an ordinary component providing the consumer-facing
key, so a later resolve can find *it* as a candidate and propose a second
bridge in front of it. Two failure modes hide there. Composite lossiness:
each hop's plan is individually attested, but the composed loss (a merge
in hop one, a default in hop two) appears in no single artifact anyone
reviewed. Ranking inversion: the committed adapter provides the exact
required shape, so at equal authority fit it would outrank a fresh single
bridge to the original candidate, and chains would grow by default,
precisely backwards. Three rules close both:

* `resolve` reads the synthesized-adapter marking (the derivation-hash
  header of section 4 is machine-readable) and reports **chain depth** in
  the candidate's `why`;
* at equal authority fit, a chain ranks **below** a fresh single-bridge
  plan against the underlying candidate: one reviewed plan beats two
  stacked ones, and depth only ever ranks down;
* `revl adapt --check` flattens a chain and re-displays the **composite**
  plan end to end, every merge, default, and drop across all hops in one
  listing, so what gets attested is the actual composed loss, not the
  last hop's slice of it.

## 7. Staged plan and exit tests

### Slices

1. **The predicate, pure.** `src/revl/adapt.py`: `bridge_plan(required,
   provided, opt_ins)` over two `ServiceDecl`s returning a per-method plan
   or the refusal list; the catalogue and clause enum as data; the
   `compatible_total` relation carrying both type tables; the name-pairing
   rule for arity change. CLI `revl adapt --check` over a need and a
   candidate manifest. No synthesis, no IR. Unit tests exercise every
   catalogue row and every refusal enum member.
2. **Synthesis and the gate.** The `adapt` source form (parser), IR-level
   synthesis from a plan (deterministic, derivation-hashed), and the one
   deliberate gate change: alias token carry-over in `_method_emissions`
   attribution plus the joint-wiring boundary comparison (S2, 6.2),
   landed as a general wiring feature with its own tests over hand-written
   renamed requires. Admission then runs through the otherwise-unchanged
   `check_and_lower`. The twin test (E5) lands here.
3. **Registry, diff, federation.** (Resolver half LANDED: `registry.resolve`
   probes a §5-refused candidate with `bridge_plan`, reports
   `compatible-with-adapter` with the plan, the rendered artifact, the wiring
   rename and the derivation hash, ranks it below direct-compatible at equal
   authority, reads chain depth off the section-4 marking and ranks a chain
   below a fresh single bridge, discounts the error-semantics evidence class
   behind an outcome merge, and rides near-miss refusals out under
   `nearMisses`. `revl adapt --check` FLATTENING, `revl diff` and the
   federation pin remain.) `resolve` reports
   compatible-with-adapter below direct-compatible at equal authority, with
   plan, generated `adapt` text, and near-miss refusals inline; chain
   depth read from the derivation marking, chains ranked below fresh
   single bridges, `adapt --check` flattening (6.4); the outcome-merge
   evidence discount and `why` flag (6.1); `revl diff` (item 123) shows
   the adapter as an added bridge component; the attestation covers it as
   ordinary code, `merge-variant`/`merge-total` shapes included;
   `federation.check` records a satisfied-via-adapter verdict in the pin.
4. **Hardening.** The 414 matrix rows (6.3); a generative test over random
   declaration pairs asserting the dichotomy: every pair either yields an
   adapter that admits, or a refusal, never a synthesized artifact the gate
   rejects (any third outcome is a predicate/gate disagreement and a bug).
   The generated pairs must include nominal-vs-structural surfaces,
   wildcard (`Any`/`Never`/`Value`) positions, and closed-variant error
   types, the exact surfaces where the permissive `compatible` and the
   restricted `compatible_total` disagree; 293 pair-evidence plumbing.

### Exit tests

* **E1, the item's own pair adapts.** Consumer requires
  `get(key: Str) -> Opt[Str]`; candidate provides
  `get(key: Str, options: Options) -> Result[Str, Error]` with `Options`
  an empty-defaultable record and `Error` an opaque record type. With
  `adapt` opting into the total waiver `Err(_) => None`: resolution
  reports compatible-with-adapter, synthesis admits end-to-end under the
  carry-over rule, and `revl audit` shows the two-hop chain naming the
  candidate's real boundary. This test is red today without slice 2's
  attribution change; that is the point of E1 running through a
  capability-scoped `Cache`.
* **E2, error discard without opt-in refused.** Same pair, no `Err` arm in
  `D`: refused with `outcome-merge` naming `get`, position `return`, and
  the hint spelling the total waiver in waiver language. Nothing is
  synthesized.
* **E3, authority refused.** (a) Candidate `get` is `emission[net]`,
  consumer requires plain `get`: refused `effect-missing-declaration`
  naming `net`. (b) Consumer requires `emission[db]`, candidate reaches
  `[db, net]`: refused `effect-exceeds-bound` naming `net`. In both cases
  the differential check: hand-writing the same wrapper is refused by the
  same G4 bound (`lower.py:6959` / `:7005`).
* **E4, non-total refused.** Candidate wants `Int32` where the consumer
  sends `Int`: refused `non-total-conversion`, hint pointing at
  `.to_int32()` in a hand-written wrapper. `Float -> Int` likewise. Also
  the `compatible_total` fences: a candidate nominal `UserRecord` against
  a consumer structural `{name, age}` is admitted only when the carried
  type table resolves `UserRecord` to exactly those fields, refused on
  mismatch or when unresolvable; an `Any` or `Value` at a bridged
  position is refused, never auto-passed.
* **E5, the twin test (same gate as hand-written).** The synthesized
  adapter and a byte-identical hand-written component produce the same
  admission verdict, the same G8 surface, and the same audit chains, over
  both an admitting and a refusing composition. This is the "same gate"
  claim, tested rather than asserted.
* **E6, folds see through.** The 6.3 matrix cells: an adapted composition's
  emission is attributed to the candidate's boundary by the approval fold,
  `component_reach`, the G8 audit, and the taint fold.
* **E7, defaults discipline.** Unpaired absence-shaped parameters (`Opt`,
  `List`, empty record) auto-admit; an unpaired `Int` parameter is refused
  `no-canonical-default`; an explicit default in `D` admits it and shows
  the value in the emitted source.
* **E8, 294 pass-through.** A candidate scoped
  `emission[fs.write(path="/tmp/job-42")]` bridges under a consumer
  requirement with the same or wider valuation and is refused under a
  tighter one; the attenuation fold sees the valuation through the alias.
* **E9, pairing under arity change.** Consumer `log(message: Str)`,
  candidate `log(category: Str, message: Opt[Str])`: refused, never the
  positional bridge (consumer `message` must pair with candidate
  `message`, and `category: Str` has no canonical default). An explicit
  `D` mapping naming the pairing and a default admits, and the emitted
  source shows both. A pair with two same-typed candidates for one
  consumer parameter refuses `ambiguous-pairing`.
* **E10, closed-variant error mapping.** The revocation pair: consumer
  `revoked(t) -> Opt[Instant]`, candidate `-> Result[Instant, Error]`
  with `Error = NotFound | Unavailable`. Blanket `Err(_) => None` is
  refused (closed variant). A plan mapping only `NotFound => None` is
  refused `unmapped-error-variant` naming `Unavailable`. Adding a new
  variant to the candidate's `Error` breaks re-derivation of a previously
  admitted plan. The same pair with an opaque `Error` admits under the
  total waiver and the attestation records `merge-total`.
* **E11, one-way promises.** A sync candidate under an async requirement
  admits (adapter declared async, purer body); an async candidate under a
  sync requirement refuses `color-mismatch`. A commutative candidate
  under a non-commutative requirement admits; the reverse refuses
  `commutative-mismatch`.
* **E12, chains.** An adapter proposed in front of a committed adapter is
  reported with chain depth 2 in `why` and ranks below a fresh
  single-bridge plan against the underlying candidate at equal authority;
  `revl adapt --check` on the chain displays the flattened composite plan
  listing every hop's merges, defaults, and drops.
* **E13, evidence discount.** A candidate whose fault-sweep evidence
  attests errors-surface-as-`Err`, resolved through a plan containing an
  outcome merge, has that evidence class discounted and the inversion
  flagged in `why`; the same candidate through a merge-free plan ranks at
  full weight.

## 8. Honest hard parts

* **Alias token carry-over is the one deliberate gate change.** S2 shows
  the shipped attribution rule refuses every capability-scoped adapter
  (both alias spellings lose, to G4 and to G3), so carry-over is not a
  wiring nicety but the load-bearing enabling change, and the design says
  so in S2 rather than claiming zero gate code and contradicting itself
  here. It must land as a general wiring feature: a shortcut that
  special-cased synthesized components would break the E5 twin and the
  capability folds at once. The resolver's manifest rename and the carried
  tokens on the alias binding are the two places to get exactly right;
  both are named in slices 2 and 3 and tested in E5/E6/E8.
* **Name-only matching in v1.** A candidate whose method is named `fetch`
  cannot satisfy `get` yet. Rename mapping in `D` is a clean extension, but
  it multiplies the audit surface and is deliberately out of scope.
* **The defaults table will feel strict.** Expect pressure to default `Str`
  to `""` and `Int` to `0`. The line is principled (absence-shaped only)
  and the escape hatch is explicit and auditable; hold the line.
* **`Value` is excluded from the catalogue on purpose.** It would bridge
  anything to anything and prove nothing; an adapter that type-checks only
  through `Value` is a hidden coercion, the thing this design exists to
  refuse.
* **The predicate and the gate must never disagree.** The predicate is a
  fast, pointed pre-image of checks the gate performs anyway. The
  generative dichotomy test (slice 4) is the standing proof; any
  divergence is fixed in the predicate, never by special-casing the gate.

## 9. Scoped out

* **The verified adapter marketplace** (external proposal #15, folded into
  the item at `docs/v2.0-roadmap.md:3863`): PostgresV1-to-Database style
  adapters across heterogeneous runtimes, each carrying its interface
  transform, capability transform, lifecycle mapping, evidence, and version
  bounds. Out of scope here, but designed for: the derivation hash, the
  catalogue version, the plan, the refusal enum, and the pair-evidence
  identity of section 6.1 are exactly the fields a marketplace entry would
  carry, and the predicate is the compatibility proof it would ship. What
  the marketplace adds (hand-written adapters with richer bodies than the
  S1 catalogue, WIT/MCP/cordis-imported surfaces, version ranges) rides on
  top without changing the predicate for synthesized adapters.
* **Rename mapping** (`fetch` satisfying `get`): a `D` extension, deferred
  (section 8).
* **Auto mode**: revisit only for the no-opt-in subset, behind an explicit
  flag, after soak (section 3).
* **The 293 trust graph and trusted-mediator marking** (folded into item
  293 at `roadmap:3848`): an adapter in front of a trusted mediator must
  count as replacing it for the stronger-evidence rule; noted here so the
  293 design owns it, since the mediator marking does not exist yet either.
