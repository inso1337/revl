# Design: typed computer-use as a host-backed extern family

Design-doc id 532 (531 went to another design doc that landed in the same
wave; the roadmap item of the same number is unrelated, and 457 established
that convention). Roadmap item served: 521 (issue #1195), from the
2026-09-19 external review. Adjacent and deliberately not restated here: 522
(issue #1196, UI transactions and verified postconditions), 525 (issue
#1200, the one runnable demonstration), 539 (upstream
`inso1337/revl-harness#11`, the computer-use substrate and the ladder's
owner problem), 249 (taint and provenance), 257 (confinement), 294
(parameterized capabilities), 247/343/344 (a declared capability token is
the one spelling every authority surface reads).

Sources studied, all at `52fb8ef3`: `src/revl/parser.py`
(`_capability_list`, `_capability_params`, `extern_decl`, `secret_decl`,
`_clause_capability`), `src/revl/cap_order.py` (the closed parameter registry
and the `covers` relation), `src/revl/taint.py` (`_SINK_CLASS_SCOPES`,
`_SOURCE_CLASS_SCOPES`, `_sink_of`, `_origin_of`, `strip_qualifiers`),
`src/revl/boundary.py` and `src/revl/audit_diff.py` (the G8 reach report),
`src/revl/policy.py` (`component_reach`), `docs/capabilities.md`,
`docs/boundary-policy.md`, `docs/rejections.md` (G8),
`docs/prompt-injection-resistance.md`, `docs/design/249-taint-provenance.md`,
and `tests/test_247_capability_reach_spellings.py`.

## 0. The decision in one paragraph

A computer-use verb is **an ordinary capability-scoped emission, in a reserved
and closed namespace**, and nothing else. `ui.click` is a capability token
exactly as `db.write` is, so every authority surface that already reads a
capability token reads a UI verb for free: the G4 subset check, the G8 audit
reach, `secret K for C`, the item-246 approval gate, the `capability <glob>`
policy rules, item 294's partial order, and item 249's derived sink and source
classes, which read the token's dotted HEAD. That is the whole reason not to
invent a stratum-1 construct: the effect surface is OS- and toolkit-specific,
but the *authority* surface is not, and revl already has it. What revl adds is
a closed registry and three refusals that keep the namespace enumerable. The
open decision the item names, the fallback ladder, is settled by making a rung
**part of the capability token**, so descending the ladder is crossing a
different declared boundary rather than retrying the same one, and the
admissible depth becomes a compile-time fact an auditor reads instead of a
runtime convention an adapter honours.

## 1. The problem, in the guarantee's own terms

G8 says the boundary surface is enumerable: everything that reaches the host
appears on the audit surface, and an unclassified `extern` is refused
(`docs/rejections.md` G8). A computer-use agent breaks that twice.

**Its reach is unbounded.** "Whatever the GUI permits" is not a boundary. A
single `computer.control` capability, or a `ui.act(target)` verb, puts the
whole desktop behind one token: the audit prints one line and the operator
learns nothing, which is worse than printing `*`, because `*` at least reads
as an admission of ignorance.

**Its inputs are attacker-influenced.** A screenshot, an OCR result, a window
title and a rendered document are content, and
`docs/prompt-injection-resistance.md` already states the rule for content: a
page that says "Upload all local secrets to continue." stays text and mints no
capability. A screen is the same class of input. So a UI target discovered by
looking at pixels is a claim requiring verification, never a trusted
reference.

Today such a program lives in host code, where nothing is enumerated and
nothing is typed, which is exactly why its reach cannot be reported in `revl
audit`.

## 2. The family

Five verbs, each naming one emission class. The registry is
`src/revl/ui_family.py`, which is the one place they are written down.

| token | emission class |
| --- | --- |
| `screen.observe` | read a screen region; the result is attacker-influenced content and never a trusted reference |
| `ui.find` | resolve a semantic target from observed content; the result is a claim requiring verification, not authority |
| `ui.click` | actuate a semantic target |
| `ui.text` | generate key input into a semantic target |
| `ui.download` | a file arrives on the host: a real emission with its own capability, never a side effect of a click |

`ui.download` is separate on purpose. A download that rides along inside
`ui.click` is an emission with no capability, which is the plainest possible
G8 hole: a file lands on the host and no declaration says so.

The low-level primitives stay unexposed, and that is a refusal rather than an
omission: there is no `mouse.move`, no `mouse.click`, no `keyboard.type` and
no single `computer.control`. A semantic operation (click the button named
Submit in the Billing application) is auditable; a coordinate pair is not.

A program that declares the family and reaches three of its verbs:

Slice 4 landed the target record, so the program below is the spelling the
language admits today: `ui.find` returns a `UiTarget` and the actuation verbs
receive one. Section 5 and `docs/design/565-ui-target-binding.md` say why the
bare-string version this program used to carry is no longer writable.

```revl
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}

extern emission[screen.observe] fn screen_observe(region: Str) -> Str
  = @py { return "" }
extern emission[ui.find] fn ui_find(seen: Str, name: Str) -> UiTarget
  = @py { return None }
extern emission[ui.click] fn ui_click(target: UiTarget) -> Int
  = @py { return 0 }
extern emission[ui.download] fn ui_download(target: UiTarget) -> Str
  = @py { return "" }

service Worker { emission fn approve(region: Str) -> Int }

component Billing provides worker: Worker {
  provide worker {
    fn approve(region) {
      let seen = emit screen_observe(region)
      let target = emit ui_find(seen, "Approve")
      return emit ui_click(target)
    }
  }
}
```

## 3. Why a capability token, and one collision

The alternative spellings were weighed against one question: which surfaces
learn about a UI verb without being told?

A **new classification keyword** (`extern ui fn ...`) teaches nothing. Every
surface listed in section 0 keys on a capability token, so a new keyword means
a new namespace and a per-surface port, which is the shape item 247 had to
undo when a directly-emitted extern's reach was its NAME while every other
surface used its declared token.

A **new stratum-1 construct** is what the item rejects and the review rejects
with it: the effect surface is OS- and toolkit-specific.

A **capability token** already carries dotted structure (`gateway.send`,
`production.payment`, item 343), already funnels through one parse point at
the declaration site, already has a closed parameter registry to imitate
(`cap_order`, item 294), and is already read HEAD-first by item 249's derived
sink and source classes. `ui` as a namespace root is therefore not a naming
convention, it is the join key.

**The collision.** The issue's sketch names the verb `ui.type`. `type` is a
reserved keyword, and a dotted capability token's segments are parsed with
`expect("ident")` at the declaration site and in `secret K for C`, so
`emission[ui.type]` and `secret K for ui.type` both fail to parse today
(measured, not assumed). The verb is therefore `ui.text`. The alternative,
relaxing every dotted-capability parse point to accept a keyword segment, is a
grammar change with a self-host lexer sync cost and no user today, and the
failure it would prevent is a name. A verb that can be *declared* but not
*selected* by the surfaces that bound it would be worse than a verb with
another name: an operator's `capability ui.type requires approval` would
select nothing, silently.

## 4. The ladder decision

The item's open decision: the fallback ladder is typed action, then a DOM or
accessibility selector, then a pixel-level action, and the question is how far
down is admissible at run time under a declared policy, since "an unbounded
ladder is the same as no guarantee".

### 4.1 The three rungs and the failure direction of each

| rung | how the target is named | what binds it | failure direction when the binding is wrong |
| --- | --- | --- | --- |
| 0, semantic | the application's own identity for the control, an accessibility-tree node or a typed API handle | an identity the application publishes | the call fails; the target does not resolve |
| 1, selector | a structural query over a document the application renders | a derivation from content that may be attacker-influenced | the query matches a DIFFERENT control that satisfies it, and the action succeeds on the wrong thing |
| 2, pixel | a position | nothing; `(842, 611)` is not authority | the same pixel is Delete, Send or Approve after a layout change, and the action succeeds on the wrong thing |

Rung 0 fails closed by nature: an unresolved identity is an error. Rungs 1 and
2 fail *open* by nature: they always hit something. That asymmetry, not a
preference for typed APIs, is why the depth has to be bounded.

### 4.2 The decision

**A rung is part of the capability token. Descending is crossing a different
declared boundary, not retrying the same one.**

```
ui.click              rung 0, a semantic target
ui.click.selector     rung 1
ui.click.pixel        rung 2
```

Four consequences, each of which is a property revl already enforces on
capability tokens rather than new machinery:

1. **The admissible depth is declared and checked.** A component's depth is
   the maximum rung over its declared UI tokens. A component that declares
   only rung-0 tokens can never reach a pixel, and that is a compile-time
   fact, checked by the existing G4 subset rule with no new check.

2. **`revl audit` prints the depth for free**, because it prints declared
   capability tokens, and `capability ui.*.pixel requires approval` is an
   ordinary policy rule over an ordinary token.

3. **The descent cannot happen inside one call.** A `ui.click` that cannot
   bind its target FAILS. It does not become a `ui.click.pixel`. A ladder that
   silently falls through when the typed action fails is the fail-open shape
   this repository's worst findings share: the intent ("click Submit")
   outlives the binding that was meant to bound it. Making the descent a
   second call on a differently-named capability is what turns "the ladder
   descended" from an adapter's internal state into a fact on the audit
   surface.

4. **The declaration must be prefix-closed.** `ui.click.pixel` is admissible
   only in a component that also declares `ui.click`. A program that can reach
   pixels but not semantic targets has no ladder, it has a pixel driver, and
   the ordering claim would be vacuous for it. This is the strongest form of
   "never inverts that order" revl can honestly check: a property of the
   DECLARATION, not of the loop.

**Slice 1 admits rung 0 only.** `emission[ui.click.pixel]` is refused today,
by name, because the check that bounds it does not exist yet, and admitting
the spelling ahead of the check is the same fail-open mistake one level up.

```revl reject G8
extern emission[ui.click.pixel] fn click(target: Str) = @py { pass }
```

### 4.3 What this does not claim

It does not claim the loop tries rung 0 first. Item 539 states the reason
exactly: a route condition can only check the ordering **of a plan**, and what
walks the ladder is an agent loop, so a plan can name the ladder in order and
the loop can still take the third rung first because the first two returned
something it did not like. revl bounds the reach. It does not claim the order.
Saying so in the design is the point; an implied ordering guarantee revl does
not enforce would be worse than no ladder at all.

## 5. Taint discipline

Item 249's mechanism is the fit here, and it is a fit by construction rather
than by adaptation: `taint._sink_of` and `taint._origin_of` read a capability
token's dotted HEAD against `_SINK_CLASS_SCOPES` and `_SOURCE_CLASS_SCOPES`,
so a reserved root is exactly the granularity those tables want.

- `screen` joins the **source** classes: the return of a `screen.observe`
  crossing is `Untrusted` by derivation, not by an author's qualifier.
  Sink-ness and source-ness come from the granting side, never from the
  author, which is what keeps a hostile or careless author from opting out.
- `ui` joins the **sink** classes: a UI actuation is a position where a value
  *is* authority, in the same sense as a shell string. An `Untrusted` target
  reaching it is G9's refusal, and the declassifier has to be declared.

`ui.find` sits on both sides, and that is the honest reading: it consumes
observed content and produces a target, so it is a source whose output is a
claim. A target is `Untrusted[UiTarget]` because it was derived by looking at
pixels; endorsing it is the adapter's pre-execution verification (section 6),
and an endorsement revl cannot check is an endorsement it must not silently
perform.

### 5.1 Which arguments are sinks, settled in slice 2

The head rule above is right for four of the five verbs and wrong for one, so
the taint role is **registry-owned per verb** (`ui_family.TAINT_ROLES`),
exactly as the item-522 reversibility class is and for the same reason: a
classification an author can lower is a classification a careless author
lowers.

| token | role | why |
| --- | --- | --- |
| `screen.observe` | source | it reads pixels; this is the item's own premise |
| `ui.find` | source, **not** a sink | it is the family's declared consumer of observed content |
| `ui.click` | sink, every argument | the target IS the authority, the position a shell string occupies |
| `ui.text` | sink, every argument | key input is not inert text |
| `ui.download` | sink, every argument | the argument names what lands on the host |

**`ui.find` is the exception, and it is load-bearing rather than a
convenience.** If the head rule made its arguments sinks, the family's own
canonical program - observe, find, click - could not be written at all without
endorsing the observation *before anything had looked at it*. That inverts the
discipline: a target must stay `Untrusted` all the way to the actuation, and
the endorsement belongs at the actuation, where an operator can see what is
being claimed about it. `ui.find` is instead a source that mints `screen` on
its return, which is what stops a `ui_find` that ignores its arguments from
laundering the observation and handing a clean target to the click.

**`ui.text`'s value is a sink, and the reason is not that it resembles the
target.** It does not. Two arguments were weighed. Against: an untrusted
string typed into a field is data landing in an application's input, which is
the same class as an untrusted body passed to a `web` crossing, and revl does
not refuse that. For: `ui.text` generates KEY INPUT, and key input is not
inert - a newline submits, a tab moves focus, a shortcut is a command - so
observed content reaching it chooses *what happens* and not only what is
written. That is the shell-string shape, and it decides the question.

**The granularity is all-arguments, and that is a limit rather than a
choice.** A capability token carries no parameter roles: item 294's parameters
narrow the capability, they do not name the parameters, so revl cannot tell
`ui.text(target, value)` from `ui.text(value, target)` and has no way to mark
only the target. `shell`, `exec` and `terminal` already derive this way.
Per-parameter precision remains reachable only through an explicit `Trusted[T]`
annotation, which is author-side and therefore never the derivation.

**One consequence outside this module, found by reading rather than by a test
failing.** `policy.TAINT_FOLD_ORIGINS` is a hand-kept mirror of
`_SOURCE_CLASS_SCOPES`, and `mcp.approval.static_taint` INTERSECTS a
component's recorded taint with it. An origin missing from that mirror is an
origin silently dropped from the taint an auto-approve decision is made
against, so a screen-tainted crossing would have compared as clean and
auto-approved without an operator ever writing the rule. `screen` joins the
mirror too; every direction that reaches is fail-closed: the crossing prompts
unless a rule names it, the unknown-taint floor grows, and every existing
rule's `negative_guarantee` becomes truthfully wider.

The binding a verified target carries is item 521's own list and is recorded
here unchanged, because a design without it is a verb list: the window or
application identity, an accessibility-tree identity where one exists, a
screenshot/DOM/AX evidence hash, the allowed action type, the user/session/
task identity, a region or coordinate bound, an expiry, and whether a
confirmation is required.

Slice 4 turned that sentence into a checked record, `UiTarget`, whose field
set is registry-owned and whose absence is a G8 refusal.
`docs/design/565-ui-target-binding.md` is its note: which half of
`Untrusted[UiTarget]` each slice owns, why the field check is a floor rather
than a ceiling, and why a bare-string target is the thing that makes item
522's postcondition verdict positional.

## 6. What `revl audit` reports, measured

Run against the program in section 2:

```
component Billing
  requires: —
  provides: worker
  boundary: host code: screen_observe [screen.observe] (emission, py),
            ui_click [ui.click] (emission, py),
            ui_find [ui.find] (emission, py); cardinality: ... UNBOUNDED ...
```

and `policy.component_reach` gives exactly `{screen.observe, ui.find,
ui.click}`. Three things in that output are load-bearing, and all three are
pinned by `tests/test_ui_capability_namespace_521.py`:

- every reached UI emission class is enumerated, by its declared token;
- `ui.download` is DECLARED but never called, and is therefore not in the
  reach. The audit reports what a component can reach, not what its file
  mentions;
- an allow-list that omits `ui.click` refuses, and one that names it admits,
  so a UI verb is under the existing `capability <glob>` machinery with no new
  policy surface.

**This needed no new reporting code**, and the test that proves it passes on
`origin/main` as well. That is stated rather than hidden: the enumeration half
of the item's exit is a consequence of spelling a UI verb as a capability
token, and the work in slice 1 is the half that makes the spelling
*mandatory*.

Cardinality is honest and unflattering: an extern body's crossing multiplicity
is unchecked, so each verb reads UNBOUNDED until a `calls` ceiling is declared
(item 260). "How many times" is not answered by this item.

## 7. Revl's side of the line, and the substrate's

Item 539 records that the computer-use substrate is a separate project and
that this repository must not build it. The split, stated in both directions
so neither side can assume the other:

**revl decides, and checks:**

- which verbs exist at all (the closed registry);
- that the whole GUI surface is not spellable as one token;
- how deep the ladder a given program may reach goes, and that the declaration
  is prefix-closed;
- that a UI target is untrusted data and cannot create authority (G9);
- that every reached verb appears on the G8 audit surface, and is selectable
  by the existing policy, approval and secret-binding grammars.

**The substrate decides, and revl does not claim:**

- which rung an agent loop tries first, and in which order (item 539);
- whether a target still matches its expected identity at execution time: the
  pre-execution sequence (identity match, expected window, action inside the
  capability, target not a sensitive control, screen not untrusted content,
  budget and session deadline still holding);
- which desktop or VM runs, which applications are visible, which windows and
  targets are allowed, which keys may be generated, whether the clipboard,
  downloads, uploads or network are reachable, whether a human must confirm;
- the isolation the worker runs in, which inherits item 257's confinement
  argument rather than restating it.

The boundary between the two lists is the reason `ui.click` takes a target and
not a point: revl can type and enumerate a semantic target, and it can type
nothing useful about a coordinate.

## 8. Failure direction of every decision here

Stated one by one, because "fail-closed" is a claim and not a property until
the direction is written down.

| decision | fails | why that is the safe side |
| --- | --- | --- |
| the root token (`emission[ui]`) is refused | closed | an unenumerable boundary on the audit surface is a G8 hole whatever it is labelled |
| the verb set is closed | closed | an invented token narrows nothing and escapes every policy rule written against the real one; a typo would partition the authority cone silently |
| a rung token is refused until its check lands | closed | admitting a spelling ahead of the check that bounds it is the fail-open shape the ladder decision exists to prevent |
| the check runs at the declaration site only | open, deliberately | a `secret K for C` binding and a `capability <glob>` rule are SELECTORS over a token, not statements of reach. Refusing `capability ui.* requires approval` would remove an operator's ability to write a floor over the family, which is the opposite of the goal |
| source/sink classes are derived from the token head, not from an author qualifier | closed | sink-ness comes from the side that grants authority (item 249 Slice D); an author cannot opt out |
| `ui.download` is its own verb | closed | a download folded into a click is an emission with no capability |
| revl claims reach, not order | neither; it is a scope statement | the alternative is an implied guarantee revl does not enforce, which is how a ladder becomes advisory (item 538's own argument about a roster that degrades silently) |

## 9. Self-host

Does this need a self-host port? **Not in slice 1, and the reason is
structural rather than an exemption.**

`selfhost/lower.rvl`'s `admit_src` decides the composition and guarantee layer
and issues no admission at all: its verdicts are `Refused`, `NoObjection` and
`OutsideFrontier`, with `"admitted": false` on every arm (item 417). A program
carrying a reserved UI token that the self-host does not understand therefore
cannot be admitted by it. The gate is already fail-closed for this construct,
and slice 1 adds no arm the self-host could get wrong.

That is not a licence to leave it forever. The obligation is recorded here so
a later slice does not have to rediscover it: **when a slice of this item
starts deciding admission on a UI token, `selfhost/lower.rvl` must either port
the check or decline the program by a NAMED marker**, never by agreeing with
silence. The oracles catch divergence, not missing features, so a check the
self-host simply does not run leaves every oracle green (item 391's own
finding, restated by 417). The marker is the only thing that makes the absence
loud.

No `selfhost/*.rvl` file is touched by slice 1, and
`tools/build_gate_crate.py --check` reports in-sync, so no crate is
regenerated.

**Corrected in slice 4, by measurement.** The paragraph above places the
obligation at "a later slice" and slice 3's entry in section 10 repeats it.
Both are wrong about the date, and the error is the one this section warns
about. Slice 1 ALREADY decides admission on a UI token: it raises three G8
refusals over one. Run through the harness `tests/test_selfhost_lower.py`
uses, `admit_src` returns `""` on all three, and `""` is NO OBJECTION, which
is agreement by silence. It is not a false admission (the native gate has no
`admitted` arm, so the fail-closed claim above holds), but it is a gap nothing
reports: a G8 refusal classifies OUT-OF-SLICE in the differential corpus, so
every oracle stays green over a check the self-host does not run. The
obligation has been outstanding since slice 1. It is pinned by
`test_the_selfhost_gate_does_not_decide_a_ui_program` and discharged by slice
3, whose crate regeneration it shares.
`docs/design/565-ui-target-binding.md` §8 has the table and the mechanism (a
third frontier-table axis derived from `ui_family.ROOTS`).

## 10. Slice plan

Each slice is closable on its own and carries the oracle that makes it a
measurement. Slice 1 is landed; the rest are proposals.

**Slice 1: the reserved namespace and the unscoped-verb refusal. LANDED.**
`src/revl/ui_family.py` (the roots, the closed verb registry with one emission
class per verb, and the one refusal function) plus one hook in
`parser._capability_list`, which is the single declaration-site funnel for a
capability token. Three refusals, all `code="G8"`, `category="boundary"`: the
root alone, an undeclared verb, a deeper token. Oracle:
`tests/test_ui_capability_namespace_521.py`, 20 tests. Non-vacuity measured on
`origin/main` at `52fb8ef3` with `ui_family.py` present (it is data) and the
parser hook absent (it is the check): 7 fail, 13 pass. With the hook: 20 pass.
The 13 that pass on both are the controls, the registry assertions and the
audit enumeration.

**Slice 2: the taint classes. LANDED.** `screen` in `_SOURCE_CLASS_SCOPES`
and in `_ORIGIN_CLASSES` (an origin is resolved from the token's head, so a
source scope that is not an origin class mints the literal `screen.observe`,
which no source-class test, no `route model` arm and no `<origin>-taint` rule
matches), `ui` in `_SINK_CLASS_SCOPES`, the per-verb roles of §5.1 in
`ui_family.TAINT_ROLES`, and `screen` in `policy.TAINT_FOLD_ORIGINS` for the
reason §5.1 ends on. Oracle: `tests/test_ui_taint_classes_521.py`, 20 tests.
Failure direction: this WIDENS what is refused. A program that previously
compiled because its author did not write `Untrusted[Str]` now sees a G9
refusal naming the origin (`screen`) and the position (`a UI actuation`), and
there is no qualifier that turns it back off. Non-vacuity measured on
`dfecba2a`: 16 of the 20 fail there, 4 pass. The four are the controls -
`ui.find` is not a sink, the `endorse[screen]` repair admits, a `db.write`
crossing over the same shape is unaffected, and the landed §6 audit program
still compiles without `taint_strict` (the derived classes are profile-gated,
as every other Slice D class is). `tools/gate_reference_census.py --check`
reports no change from the baseline with `false-reject` still empty: the
widening refuses no program the reference corpus admits.

**Slice 3: the ladder rungs.** Admit `ui.<verb>.selector` and
`ui.<verb>.pixel`; check prefix-closure per component over the G8 reach.
Oracle: a component declaring `ui.click.pixel` without `ui.click` is refused
by name; one declaring both is admitted; `revl audit` prints the deepest rung;
and `capability ui.*.pixel requires approval` selects it. This is the slice
that replaces the "not admissible yet" refusal of slice 1, and the refusal
message is the thing that points an author at it.

Re-examined when slice 2 landed, and still deferred. The two reasons are
recorded so the next reader does not re-derive them. First, slice 1's hook is
`parser._capability_list`, which sees ONE token with no component context,
while prefix-closure is a per-component property over a SET of tokens: it
needs a refusal site in the reach/boundary layer that does not exist yet, and
nothing in the taint slice supplies one. Second, §9's obligation fires here
and did not fire for slices 1 or 2, because this is the first slice that
DECIDES ADMISSION on a UI token: `selfhost/lower.rvl` must then port the check
or decline the program by a named marker, which carries a crate regeneration.
Slice 2 needed neither, and `tools/build_gate_crate.py --check` reports
in-sync on it.

What slice 2 did leave for this slice: `ui_family.taint_roles` resolves by
longest registered PREFIX rather than by exact match, so `ui.click.pixel`
inherits `ui.click`'s sink role the moment the spelling becomes admissible. A
rung is a strictly weaker way to name the same target, and an exact-match
table would have given a rung no taint role at all - the fail-open direction,
reached by adding a spelling in a different file.

**Slice 4: the target type. LANDED.** `UiTarget` as a record carrying the
binding of section 5, with the field set registry-owned in
`ui_family.TARGET_FIELDS`; `ui.find` returns it and `ui.click`, `ui.text` and
`ui.download` each take one, checked in `lower._check_ui_target_binding`.
Three refusals, all `code="G8"`, `category="boundary"`: a target-carrying verb
with no record, a record missing or mis-typing a registry field, and a
signature that does not carry the target. Oracle:
`tests/test_ui_target_binding_521.py`, 44 tests. Non-vacuity measured on
`4cfc8f32` with `ui_family.py` present (it is data) and the `lower.py` pass
absent (it is the check): 21 fail, 23 pass. Here: 44 pass.
`docs/design/565-ui-target-binding.md` is the note.

Two things it deliberately does NOT do. It does not ask the author to write
`Untrusted[...]`: that half is slice 2's derivation and stays profile-gated on
`taint_strict`, while the `UiTarget` half is unconditional, and §4 of the note
has the separation. And it does not check a field's VALUE - an expiry in the
past is admitted - because a value check has no home in a compiler that never
sees a screen.

Failure direction: this WIDENS what is refused. The family's own canonical
program, as section 2 of this note spelled it before slice 4, no longer
compiles; section 2 now carries the post-slice-4 spelling. The fixtures in
`tests/test_ui_capability_namespace_521.py` and
`tests/test_ui_taint_classes_521.py` were moved to it in the same change, and
their verdicts are unmoved, because a taint role is read off the declared
capability token and never off an argument type.

**Slice 5: the receipt. LANDED.** `src/revl/ui_action.py`: one recorded
action carrying the crossing key (item 517's `component`/`step_index` pair,
reused rather than re-invented), the declared capability token, the outcome,
and the WHOLE target binding rather than the eight members this entry
originally listed. The eight are all there; carrying only them would
recreate, at audit time, the "which field got dropped" question slice 4
removed at compile time.

Oracle: `tests/test_ui_action_receipt_521.py`, 25 tests, with
`test_the_receipt_for_a_refused_step_is_as_complete_as_for_a_performed_one`
as the named one, generalised over all four arms by
`test_every_arm_is_as_complete_as_every_other`. `OUTCOMES` is
`performed | refused | unresolved | failed`, and `unresolved` is the arm that
matters most: §4.2 refuses the descent inside one call, so an `unresolved`
receipt is the POSITIVE evidence that a click which could not bind its target
did not become a `ui.click.pixel`.

Not signed, and the absence is pinned rather than left to be noticed. Item
517 carries a MAC because the producer and the consumer are both inside revl's
world; a UI step is performed by the substrate, which §7 and item 539 put
outside this repository, so revl holds no key for it. What the module bounds
is COMPLETENESS, which is a property of the record rather than of the
recorder. `docs/design/565-ui-target-binding.md` §12 has the argument, and
§12.5 the list of what nothing here verifies - starting with the fact that no
revl surface emits a receipt yet, which is where `revl.model_evidence` also
started.

**Not in this item:** the transaction phases, the five-way reversibility
classification and `uncompensated` are item 522's; the end-to-end
demonstration is item 525's; the substrate is upstream.

## 11. Non-goals

- A UI verb in the core language. The item rejects it and so does this design.
- A generic `ui.act(target)`. It cannot satisfy G8 and it is the shape that
  makes a computer-use agent unauditable.
- Driving a desktop. Nothing under `src/revl/` learns what a window is.
- Any claim about the order in which an agent loop walks the ladder.
- Any claim that a declared verb was actually performed on the control the
  author meant. That is a postcondition, and postconditions are item 522's.

## 12. What is not verified

Written down so a reader does not infer more than was measured.

- The ladder's rung tokens (section 4.2) are a design, not code. Nothing in
  the tree admits or checks them today; slice 1 refuses them. Still true.
- The taint classes of section 5 were a proposal when this was written and
  are not one now: slice 2 put `screen` in `_SOURCE_CLASS_SCOPES` and `ui` in
  `_SINK_CLASS_SCOPES`, and an observed value flowing into a click is a G9
  refusal under `taint_strict`. The bullet is corrected rather than deleted,
  because what it says about the pre-slice-2 tree is the measurement slice 2's
  non-vacuity is stated against.
- The audit output in section 6 was produced by running `revl audit` on the
  program in section 2 and is reproduced from that run. It enumerates DECLARED
  tokens. It does not and cannot verify that a host body reaches only the
  boundary its declaration names; G8 keeps host bodies opaque, and the
  cardinality line says UNBOUNDED for the same reason.
- The claim that every authority surface reads a UI verb for free was checked
  on two of them, the G8 reach and `component_reach`'s policy selection, by
  test. The approval gate, `secret K for C` and item 294's partial order were
  read rather than exercised.
