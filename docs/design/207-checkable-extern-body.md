# 207: a checkable extern body, or why `shape-proven` should be deleted

Design note for roadmap item 207 and GitHub issue #207. Design only: no compiler
change, no `src/` change. One test file lands with it,
`tests/test_207_checkable_extern_body.py`: a falsification harness rather than a
prototype, in which every structural claim below is asserted mechanically, so
the recommendation can be re-checked by running the tests instead of by
rereading the argument. It also carries the reproducer for the one policy defect
this pass found.

Companion docs: [309-idempotent-inverse.md](309-idempotent-inverse.md),
[290-confidence-evidence-admission.md](290-confidence-evidence-admission.md),
[253-temporal-target.md](253-temporal-target.md),
[../crash-recovery.md](../crash-recovery.md).

**The recommendation is DELETE THE TIER, and do build the one small fix the
search found.** The issue states the problem correctly: `shape-proven` is
well-defined in the type and unreachable in the world. What the issue gets wrong
is the urgency, and the correction is the finding of this pass.

The issue says the tier's reachability is "a promise the code has already made
and must then keep", because `REDISPATCH_FREE` contains it. It is not. Every
consumer that grades `shape-proven` grades a value the tier's own provenance can
never carry: `REDISPATCH_FREE` is read at the owed-deferred-emission seam, and
`shape-proven` is an inverse-body register on a non-emission declaration. The
membership is inert. And in the one path that does read an inverse's register,
`recovery.py::_replay_tier`, a `shape-proven` inverse produces the verdict
`undo idempotent` already produces today. **The behavioural delta of the entire
feature, in recovery, is zero.** Section 2 measures this; the harness asserts it.

That leaves one surface where the tier would change a verdict, the 290/309
policy floor, and one real operator sentence behind it. Section 3 states that
sentence honestly, and section 6 shows it is already writable today with a
shipped rule that produces strictly better evidence than the proposed check.

---

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| Q1 | Is the tier worth having? | **No. Delete it.** | §2, §9 |
| Q2 | Is `REDISPATCH_FREE` a promise the code must keep? | **No, and this is the finding.** The membership is unreachable by the tier's own provenance, so removing it changes no producible verdict. The tripwire guards a door that opens onto a wall. | §2 |
| Q3 | Is there a real gap underneath? | **Yes, one sentence of it.** No local MUTATING inverse can reach a strong register floor, because `keyed` is emission-only and `read` requires the inverse to change nothing. | §3 |
| Q4 | Is `shape-proven` a fourth thing, or `keyed` with different provenance? | **Neither. It is `declared` at a different granularity.** The lattice's real axis is who enforces re-issue safety: nobody (`read`), the remote (`keyed`), the author (`declared`). A shape rule's enforcer is still the author, one level down. | §4 |
| Q5 | If it were built, what is the form? | Three candidates. Two collapse into things that already exist (`fn`, and the shipped `inverse-roundtrip` evidence facet); the third is real but produces a claim, not a proof. | §5 |
| Q6 | What could a shape rule actually prove? | Idempotence of a composition of leaf write-algebras, conditional on every leaf's declared algebra being true. It cannot see the syscall. And its own stated rule rejects all four named first adopters. | §6 |
| Q7 | What does it cost? | A second spelling of `extern`, or a migration of every extern body in the tree. Section 7 counts both. | §7 |
| Q8 | So what gets built? | The deletion (§9 S1), the doc recipe that answers the real operator sentence (§9 S2), and **F1**, a fail-open defect in the register floor found by walking every reader of the register. | §8, §9 |

---

## 1. What the tier is, and every place it is read

Measured on `afd0e144`. The register is one string per extern declaration,
produced by `lower.py::_idempotent_register` (`src/revl/lower.py:2609`) and
written onto the IR at `lower.py:3526`.

### 1.1 The producers

```
undo pure <inv>(result)      -> "read"      (item 440)
idempotent(key: p)           -> "keyed"     (item 309)
idempotent | undo idempotent -> "declared"  (item 309)
                             -> "shape-proven"   nothing
```

### 1.2 The consumers, all of them

| # | Site | Reads | What it decides |
|---|---|---|---|
| 1 | `recovery.py::_replay_tier` (`:91`) | an INVERSE's register | `read` / `free` / `fenced` replay of a transactional inverse |
| 2 | `recovery.py::_reissue_permitted` (`:532`) | an OWED DEFERRED EMISSION's register, against `REDISPATCH_FREE` | whether recover may auto-fire it |
| 3 | `recovery.py:638` | the same descriptor, against `REDISPATCH_FREE` | whether a spent re-issue fence may be crossed twice |
| 4 | `policy.py::_teardown_violations` (`:1577`) | every `recovery_surface` row | `requires idempotent-teardown(strength: F)` admission |
| 5 | `policy.py::_register_violations` (`:1612`) via `capability_registers` | per capability token | `capability G requires register F` admission |
| 6 | `__main__.py::_recovery_audit_view` (`:731`) | each surface row | the `revl audit --recovery` replay class |
| 7 | `backends/typescript/emit_temporal.py:113` | an emission's register, against `_RETRY_EARNING_REGISTERS` | whether a Temporal activity earns a `RetryPolicy` |

`shape-proven` is named at 2, 3, 6 and 7. It is **not** named at 1, which is the
only consumer whose input is an inverse register.

---

## 2. The finding: the tier is in the wrong set, and the set is inert

### 2.1 The register field is provenance-disjoint by classification

Two lowering rules, both shipped, partition the register vocabulary by which
kind of declaration can carry it.

- `idempotent` and `idempotent(key: p)` are **emission-only**
  (`lower.py:3123`): "`idempotent` is a delivery claim about a boundary
  crossing". So `keyed` implies `class == "emission"`.
- An **emission cannot declare `undo`** (`lower.py:3011`). So `undo idempotent`
  and `undo pure` imply `class != "emission"`.

The consequence is not a nuance. It means the one `register` field carries two
different kinds of claim depending on the declaration under it:

| family | classification | registers | what the claim is about |
|---|---|---|---|
| forward | `emission` | `keyed`, `declared` | re-FIRING the forward crossing |
| inverse | `witnessed`, `acquire` | `read`, `declared` | re-PLAYING the declared inverse |

`shape-proven` is designed as a syntactic check over an inverse **body** (309 §2:
"every write is `set(target, w.field)`, no reads of current state, no deltas, no
appends"). It is an inverse-family register, permanently.

### 2.2 `REDISPATCH_FREE` only ever sees forward-family registers

Both of its readers (consumers 2 and 3) grade a `deferred-emission` WAL
descriptor: an owed emission the commit approved whose `flushed` record never
landed. `deferred` is emission-only (`lower.py:2227`). So every register
`REDISPATCH_FREE` is ever asked about comes from an emission declaration, and an
emission declaration cannot produce an inverse-family register.

**`shape-proven` sits in a set its own provenance can never reach.** The same is
true of consumer 6's `owed-emission` branch (`__main__.py:751`) and of consumer
7, whose own comment already concedes the point.

The harness walks this end to end: `test_c3_no_declaration_can_put_an_inverse_
register_on_an_owed_emission` lowers a composition with one inverse and two
deferred emissions and asserts the `owed-emission` rows carry only
`{keyed, declared}` and the `inverse` rows only `{declared}`.

### 2.3 In the path that does read an inverse register, the tier buys nothing

A shape-proven inverse would still be spelled `undo idempotent <inv>(result)`,
with a body that happens to pass the check. So `decl.undo_idempotent` is true,
so `declared_idempotent` is true at both call sites of `_replay_tier`
(`recovery.py:934`, `:1033`), so:

```
_replay_tier("shape-proven", True) == "free"
_replay_tier("declared",     True) == "free"
_replay_tier(None,           True) == "free"
```

All three are asserted in `test_c4_shape_proven_and_declared_idempotent_replay_
identically`. The audit view agrees: `_recovery_audit_view` prints
`replay: free` for any inverse with any register (`__main__.py:745`), so a
shape-proven inverse would print the same line a declared one prints today.

**Item 309 slice 4, fully built, would change no recovery verdict and no audit
line.** That is the measurement the issue is missing, and it is the reason the
tripwire's alarm is quieter than it reads: nothing downstream is waiting on the
promise.

### 2.4 The shipped documentation already cannot state the tier's semantics

`docs/crash-recovery.md` §4b's tier table gives `keyed` and `shape-proven` one
shared row, and the "what makes a re-issue safe" cell reads "the remote dedups
on a stable key carried in the descriptor". That is `keyed`'s mechanism, and it
is not `shape-proven`'s: a last-writer-wins local inverse has no remote and no
key. The tier has been absorbed into its peer's row because there was never
anything to write in its own.

---

## 3. The gap underneath, stated so it can be judged

Delete the tier and something does go away, and this note is only honest if it
names it before recommending the deletion.

An inverse has exactly two routes above `declared` today:

- `keyed`, which is emission-only, so it is closed to every inverse; and
- `read` (`undo pure`), which requires the inverse to change nothing.

`stdlib/fs.rvl`'s four inverses (`restore`, `unrm`, `unmove`,
`rmdir_if_empty`) all mutate: they unlink, rename and rmdir real files. So they
are permanently `declared`, and no policy floor above `declared` can ever be
satisfied by a reversible filesystem mutation. The operator sentence this blocks
is a legitimate one:

> Auto-replay a local mutating inverse only if revl checked it, not merely
> because the author said so.

That sentence is written today as `requires idempotent-teardown(strength:
strong)` or `capability fs requires register strong`, and today it refuses every
witnessed fs mutation in the stdlib. `test_c5_no_mutating_local_inverse_can_
reach_a_strong_floor_today` pins it.

So the gap is real. What the rest of this note argues is that a syntactic body
check is the wrong answer to it, and that a better answer already ships.

---

## 4. Q4: is `shape-proven` a fourth thing?

Item 442's pass concluded that delegation collapses into item 294's leases and
recommended rescoping rather than building in parallel. The same question, asked
here, has a sharper answer, because the collapse is downward rather than
sideways.

Sort the lattice by **who enforces that a second application is safe**, which is
the only property recovery actually needs:

| register | enforcer | what revl holds |
|---|---|---|
| `read` | nobody. Nothing happens on the second run | an author claim, anchored to the `pure` classification |
| `keyed` | the remote, under its dedup contract and its retention window | a mechanical guarantee that the same key VALUE reaches every re-issue |
| `declared` | the author | the author's word, shape-checked |
| `shape-proven` | ? | ? |

Fill the row in. A shape rule over an inverse body proves the body is
last-writer-wins **only relative to the algebra of its leaves**, and revl cannot
see a leaf's algebra: a leaf is an extern, its body is host code, and its effect
on the world is exactly what G8 says revl cannot know. So a shape rule needs
every leaf's write-algebra declared (§5, form F2). The enforcer is then the
author of the leaf declarations.

**It is the same enforcer as `declared`, moved one level down.** Not a fourth
mechanism: the third mechanism at a different granularity. What changes is the
size and the reuse of the claim surface, which is a real engineering benefit and
a bad reason for a rank in a partial order whose entire discipline is "on any
ambiguity, fall back to the weaker claim".

The comparison with `keyed` is worth stating precisely, because it also shows
the peer ordering was never quite right. `keyed`'s guarantee **expires**: 309's
own honest caveat is that a recovery run days after the crash re-issues a key
the remote has already forgotten, and the remote then re-applies. A
last-writer-wins local inverse has no retention window at all. On that axis
`shape-proven` would be strictly stronger than `keyed`, not its peer, which the
rank-1 tie does not express. 290 §3.2 withdrew a total order for good reasons
(two rules must not grade one declaration differently), and the residue of that
withdrawal is a peer relation between one register that is a mechanism and one
that is a wish. Deleting the wish resolves it.

---

## 5. Q5: if it were built, what is the form?

Three candidates. Two collapse.

### F1: a revl-expressed extern body

`extern pure fn restore(w: WriteWitness) -> Unit = { ... revl statements ... }`.

This is the issue's literal reading, and it collapses on contact.

An extern is a declaration of something revl does not implement. An extern with
a revl body is a `fn`. revl has `fn`. So F1 is not a new extern form, it is the
removal of the requirement that an extern have a host body, and what it produces
is a function under an extern's name.

And it does not reach the goal. A revl `fn` cannot touch the world except
through externs, so the world-touching leaves of the new body are extern calls
again, with host bodies again, opaque again. The regress is exactly one step
long and ends where it started. `test_c1_the_undo_slot_is_an_expression_over_
extern_calls` asserts the shape of that step: the `undo` slot is a one-node call
expression naming another extern, and following the name reaches a `bodies` map
of host text and stops.

To terminate the regress you must bless a set of world primitives whose algebra
the checker knows without reading a body. That is F2.

### F2: declared write-algebras on the leaves

Not a body form at all. A new declaration axis on every extern that touches the
world, naming what its write does to what:

```
extern pure fn replace_confined(src: Str, dst: Str) -> Unit
    writes last(dst) consumes(src)  = @py { ... }
extern pure fn append_line(path: Str, line: Str) -> Unit
    writes delta(path)              = @py { ... }
extern pure fn lexists_confined(p: Str) -> Bool
    reads(p)                        = @py { ... }
```

The checker then composes: an inverse expression whose every write leaf is
`last`-shaped, over targets fixed by the witness rather than by current state,
is last-writer-wins and therefore idempotent. That composition is genuinely
mechanical, and it is the only one of the three forms that does what the item
asks.

What it buys is **a smaller, shared, reviewable claim surface**. Four inverses
in `stdlib/fs.rvl` share three primitives; declaring `last`/`delta` once on
`replace_confined` and reusing it four times is better than four independent
`undo idempotent` claims. That is worth something.

What it does not buy is a proof. `writes last(dst)` is an author claim about a
host body, checked for shape only, which is verbatim the trap items 440 and 309
already recorded twice (`pure` classification, `undo pure`). §4 is the
consequence: the tier it produces is `declared`, factored.

### F3: a declared effect signature the host body must satisfy

Declare the property, then verify it out of band against a real run rather than
against the syntax. This is the design's own fault sweep: 309's value-aware
double-undo re-applies the inverse's WAL descriptor to the post-rollback world
and asserts the value digest does not move.

F3 collapses because **it is already shipped, and it is already thresholdable in
policy.** §6 is that argument.

---

## 6. Q6: what could actually be proven, and the answer that already ships

### 6.1 The design's own rule rejects all four first adopters

309 §2's rule is "every write is `set(target, w.field)`, no reads of current
state, no deltas, no appends". Every one of `stdlib/fs.rvl`'s four inverses
branches on `lexists_confined(...)`:

```
if _ws.lexists_confined(parked):
    _ws.replace_confined(parked, target)
```

That is a read of current state, which the rule excludes. So the named
prerequisite adopters fail the check they were the prerequisite for.

The guard is not sloppiness. It is what makes the inverse idempotent: the second
replay finds the parked file gone and does nothing. Idempotence here comes FROM
the read, not despite it. So the rule has to be relaxed to admit a guarded
write, and the relaxed rule must say that the guard's falsity implies the write
is unnecessary. That is a program-logic obligation over a host predicate, not a
syntactic shape, and once the check needs it the check is no longer syntactic
and no longer cheap. Either the rule rejects its adopters or it stops being the
rule the design costed.

### 6.2 The evidence route is shipped, stronger, and already thresholdable

The operator sentence in §3 does not actually ask for a static proof. It asks
for verification instead of assertion. revl already produces that verification
and already grades it:

- `fault.py`'s value-aware double-undo sweep (item 309, landed) re-applies each
  inverse's descriptor and catches a falsely declared `undo idempotent` when the
  value digest diverges. It runs against the real host body, not against a
  restricted syntax.
- `registry.py` records it as evidence (`inverse-roundtrip`, `fault-sweep`).
- `policy.py::_EVIDENCE_STATUS_THRESHOLDS` (`:411`) makes both thresholdable:
  `inverse-roundtrip pass`, `fault-sweep full`, `fault-sweep N/N`.
- 290's rules are conjunction-only, fail-closed and worst-wins, so the operator
  sentence composes today:

```
capability fs requires register declared
capability fs requires evidence [inverse-roundtrip pass, attestation valid]
```

Read that as: the claim must be MADE (`register declared` refuses an inverse
with no claim at all), and it must have been TESTED, with the dossier rooted in
a signed attestation so it is not self-attested (290 §6.2).

Compare the two answers on what they actually establish about
`stdlib/fs.rvl`'s `unrm`:

| | F2 shape check | shipped `inverse-roundtrip pass` |
|---|---|---|
| what it inspects | a revl expression over declared leaf algebras | the real `@py` body, executed |
| catches a lying `undo idempotent` | no. A leaf whose `writes last` claim is false passes | yes. That is the sweep's headline test |
| catches `unrm` today | no. It is rejected by the read-guard rule (§6.1) | yes, it runs |
| trust root | the compiler, over author-declared leaf algebras | a signed dossier under `attestation valid` |
| needs a language change | yes | no |

The one place F2 is genuinely better is the trust root: a compiler-computed
verdict needs no attestation, while a dossier is self-attested unless rooted.
But 290 already solved rooting (the per-facet dossier sha256 is signed into the
attestation), so the advantage is a clause in a policy file, not a capability.

**A design pass that recommends building a weaker check than the one already
shipped, to fill a tier that changes no verdict, has answered its own question.**

---

## 7. Q7: what it costs

### 7.1 Building F2

- A new declaration axis (`writes last(x)` / `writes delta(x)` / `reads(x)`) on
  the extern grammar, parsed, lowered, carried on the IR, printed by `audit`,
  diffed by `audit --diff`, versioned by `revl version`, and ported to
  `selfhost/` under the byte-agreement oracles.
- A composition checker over inverse expressions, with the guarded-write
  semantics §6.1 forces on it.
- The axis is meaningless on an extern that does not appear in an inverse, so
  either it is optional (and the check silently degrades to `declared` for any
  inverse with one unannotated leaf, which is the honest fail-closed behaviour
  and also means the tier stays rare) or it is mandatory (a migration of every
  extern body in the tree and in every user program).
- Six backends emit extern bodies. None of them changes, which is the one
  genuinely cheap part.

### 7.2 Two spellings of one concept

This is the cost the brief names and it is the right thing to be wary of. F1
produces two spellings of `extern` outright. F2 does not, but it produces two
ways to state the same property about one inverse: `undo idempotent` (the
claim) and `writes last(...)` on its leaves (the derivation), which can
disagree. The design would have to decide which wins, and the fail-closed answer
(the weaker) makes the annotation optional and therefore skippable, which is how
a second spelling becomes permanent.

### 7.3 Deleting the tier

Counted on `afd0e144`:

| where | occurrences | kind |
|---|---|---|
| `src/revl/lower.py` | 10 | 2 constants (`_REGISTER_RANK`, `REDISPATCH_FREE`), 8 comment lines |
| `src/revl/recovery.py` | 4 | 1 constant, 3 docstring lines |
| `src/revl/policy.py` | 9 | 1 floor vocabulary entry (`_REGISTER_LEVELS`), 8 comment lines |
| `src/revl/__main__.py` | 2 | 1 audit class branch, 1 comment |
| `backends/typescript/emit_temporal.py` | 2 | 1 constant, 1 comment |
| `docs/` | 27 | prose, across 5 files |
| `tests/` | 17 | the 309 tripwire and two policy tests |

Three live constants, one audit branch, one floor spelling. The rest is prose.

The one behavioural change is the floor spelling: `requires register
shape-proven` and `idempotent-teardown(strength: shape-proven)` stop parsing.
`test_c6_the_only_floor_that_loses_a_spelling_is_the_named_peer` asserts that
over the whole producible register vocabulary that floor is an exact synonym for
`strong`, so no policy loses meaning. Follow 290's own precedent for closing a
vocabulary in time: reject it at parse with a distinct `PolicyError` naming the
replacement, rather than silently accepting a level that grades nothing.

---

## 8. F1: the defect this pass found

Not about `shape-proven`. Found by walking every reader of the register (§1.2),
which is the part of a design pass that pays for itself.

**`_capability_registers` folds a token's declarations with STRONGEST-wins;
290 §3.2 specifies WORST-wins.**

`audit_diff.py:235` builds the map the `capability G requires register F` rule
evaluates against, and its fold keeps the highest rank:

```python
if prior is None or _REGISTER_RANK.get(register, -1) \
        > _REGISTER_RANK.get(prior, -1):
    out[token] = register
```

with the docstring "a token declared by several externs takes the STRONGEST
register (the floor the policy can safely assert)". 290 §3.2 says the opposite,
in the section that specifies this evaluator:

> Worst register wins per capability. When a register rule selects a component,
> EVERY declaration lowered onto a matching reach token must meet the floor; the
> effective register of a capability is the WEAKEST among the declarations
> behind it. One bare `declared` inverse beside three keyed ones fails a `keyed`
> floor.

The reproducer, run on `afd0e144`:

```
extern emission[db] idempotent(key: k) fn push(k: Str) -> Unit = @py { pass }
extern emission[db] idempotent      fn ping(k: Str) -> Unit = @py { pass }
component Main { emit push("a")  emit ping("b") }
```

```
capability_registers: {'db': 'keyed'}
capability db requires register keyed  ->  ADMITTED
```

`ping` is a bare `idempotent`, register `declared`, and it reaches `db`. The
floor must refuse and it admits. **One keyed declaration launders every
bare-`declared` declaration on the same token past a `keyed` or `strong`
floor.** Fail-open, in a rule whose only job is to refuse.

The sibling rule does not have the bug: `_teardown_violations` iterates
`recovery_surface` row by row and refuses if any row is below the floor, which
is worst-wins by construction. So the two register rules currently grade the
same declarations differently, which is precisely the failure mode 290 §3.2
withdrew the total order to avoid.

**The fix** is one comparison and one docstring: fold with `<` instead of `>`,
and say WEAKEST. Blast radius: `audit_diff.py::diff_registers`
(`:536`) reads the map and its ordering assumption flips with it; the
`capability_registers` field in `revl audit --json` changes value for any token
with more than one register-bearing declaration, which is an audit-surface
change and needs a roadmap note; `tests/test_evidence_policy.py`'s register
tests use single-declaration compositions and are unaffected.

Filed as a strict-xfail reproducer in
`tests/test_207_checkable_extern_body.py::test_f1_a_register_floor_must_be_worst_wins_over_a_token`,
so it turns green loudly when fixed. It belongs on its own issue, not on 207.

---

## 9. Slices

**S1. Delete `shape-proven`. Build this.** Remove it from `_REGISTER_RANK`,
`REDISPATCH_FREE`, `_REGISTER_LEVELS`, `_RETRY_EARNING_REGISTERS` and the audit
branch; reject the floor spelling at parse with a `PolicyError` naming `strong`;
rewrite `docs/crash-recovery.md` §4b's shared row so `keyed` states its own
mechanism and the `shape-proven` paragraph becomes a one-line pointer here;
correct 290 §3.2, 309 §2 and 253's tier table; replace the 309 tripwire with
this note's C6. Small, mechanical, and it closes issue #207 with a negative
result rather than leaving a tier the partial order accepts and nothing
produces.

Keep `strong` as the "either verified form" floor. After the deletion it means
`keyed` or `read`, which is exactly what it means in practice today.

**S2. The doc recipe for the real sentence (§3, §6.2). Build this next.** In
`docs/crash-recovery.md`, next to where the `shape-proven` caveat lives today,
the two-clause policy that answers "verified, not merely claimed" for a local
mutating inverse:

```
capability fs requires register declared
capability fs requires evidence [inverse-roundtrip pass, attestation valid]
```

with the sentence that makes it honest: the register says the claim was made,
the evidence says it was tested against the real body, and the attestation roots
the dossier. Two paragraphs. This is the deliverable the operator actually
wanted, and it needs no code.

**S3. F1, on its own issue.** §8. One comparison, one docstring, one roadmap
note for the audit-surface change.

**Not in 207, and deliberately not filed as a follow-on:** F2's declared
write-algebras. If a trigger ever arrives (§11), it is a new item with its own
number, and the first question it must answer is why it is better than the
`inverse-roundtrip` evidence facet, not why it is better than nothing.

---

## 10. Exit tests

All in `tests/test_207_checkable_extern_body.py`, green on `afd0e144` except the
declared xfail.

1. **C1.** An extern with no `@backend` body is refused, and the `undo` slot
   lowers to a one-node call expression whose callee's IR is a `bodies` map and
   nothing else.
2. **C2.** `idempotent` is refused on a non-emission; `undo` is refused on an
   emission; a two-declaration program shows the two register families never
   meet.
3. **C3.** `deferred` is refused on a non-emission; a walked `recovery_surface`
   shows `owed-emission` rows carry only emission registers; `_reissue_permitted`
   is verdict-identical on `shape-proven` and `keyed` at every knob setting.
4. **C4.** `_replay_tier` returns `free` for `shape-proven`, `declared` and
   `None` alike when `undo idempotent` is declared, and `fenced` without it.
5. **C5.** A witnessed mutating inverse registers `declared` and satisfies
   neither `keyed`, `shape-proven` nor `strong`; `undo pure` is the only strong
   route and lower refuses it on a non-`pure` callee.
6. **C6.** The producible register vocabulary is exactly
   `{declared, keyed, read}`, and `REDISPATCH_FREE` and `_register_satisfies`
   give identical answers over it with `shape-proven` removed. Plus: the
   `shape-proven` floor is an exact synonym for `strong` over that vocabulary.
7. **F1** (strict-xfail). `capability db requires register keyed` refuses a token
   reached by one keyed and one bare-`declared` emission.

**What S1 must add when it lands.** The tripwire that replaces 309's: the
producible register set is exactly `{declared, keyed, read}` and the floor
vocabulary is exactly `{declared, keyed, strong}`, so a future tier cannot be
added to the type without a producer and a consumer landing with it. That is the
generalisation of the lesson, and it is the part worth keeping.

---

## 11. What would change this note's mind

Stated plainly, so the recommendation is falsifiable.

**The negative half**, that the tier is inert, would be overturned by any of:

- A declaration on `main` that puts an inverse-family register onto a
  `deferred-emission` descriptor, or an emission-family register onto an
  `inverse` row. That falsifies C2 or C3, and `REDISPATCH_FREE`'s membership
  becomes live.
- A change to `_replay_tier` that grades an inverse's register above
  `declared_idempotent`. That falsifies C4 and gives the tier a recovery-side
  delta for the first time.
- A consumer of the register not in §1.2's table. The search was every
  `register` read in `src/revl`, `backends/` and `selfhost/`; seven were found.

**The positive half**, that the evidence facet already answers §3's operator
sentence, would be overturned by:

- An operator requirement that a static verdict satisfies and an
  `inverse-roundtrip pass` dossier cannot. The candidate is an air-gapped
  admission where no dossier can be produced at all, and if that requirement is
  real it is the trigger, and it argues for F2 on its own terms rather than as a
  slice of 309.
- A demonstration that F2's leaf write-algebras can be checked rather than
  declared, on some tier. The wasm tier is where physical confinement is
  eventually supposed to become a property rather than a runtime refusal
  (`stdlib/fs.rvl`'s own caveat), and if a tier can ever observe a leaf's writes
  the whole argument in §4 changes, because the enforcer stops being the author.

Neither was found. The second is the one most worth a second reader.
