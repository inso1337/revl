# 478: Component-author ergonomics — the language gaps behind #548

Provisional id. This note is filed under 478 pending a roadmap item number; the
issue it tracks is #548 ("language: component-author gaps"). Renumber the file
and this header when the roadmap assigns the item.

## Why this exists

A private full review (REVL-REVIEW-2026-09-06, summarised in #548) wrote eight
realistic components against the current surface and none compiled on the first
draft. Every failure was a language gap, not a mistake by the author. The
review ranked the walls it hit. This note takes that ranking, records which
gaps are bounded and which are design efforts, lands the one bounded scope bug
that had a clear root cause, and lays out the slice order for the rest so each
follow-up can be picked up on its own.

The gaps split cleanly into three groups: surface bugs (a feature exists but
misbehaves in one position), missing stdlib (a method an author reasonably
expects is absent), and new grammar (a statement form the provide/activation
body does not have). The first group is where the cheap wins are.

## What landed with this note

Group-1 bug, review item 8: a string template `${...}` inside a block-bodied
match arm could not see the arm binding or the enclosing fn parameters. The
same reference outside a template, or the same template in an expression-bodied
arm, compiled fine, so the wall was narrow and surprising.

Root cause: a block-bodied match arm is lambda-lifted into a synthetic helper
fn (`lower._lift_block_arm`), and the free-name collector that decides which
enclosing names become helper parameters (`lower._collect_arm_names`) walked
lists and dataclass nodes but not tuples. `Interp.parts` is a list of
`("text", str)` / `("expr", <ast>)` tuples, so the interpolated expression sat
in a tuple slot the walk never descended into. Names read only inside a
`${...}` were never collected, the lifted helper missed the parameter, and the
arm was refused at lowering with "`x` is not declared in this function (G1)".

The fix collects names from tuples exactly as from lists. It is a pure
front-end capture fix in shared lowering: no new IR node, no emitter change, no
gate-crate movement, and the self-host lowering was never affected because it
walks template parts through a typed AST (`walk_parts`/`parts_calls`) rather
than by dataclass reflection. No program that compiled before changes its
emitted bytes, so the golden and byte-agreement oracles are untouched. Tests:
`tests/test_548_template_block_match_arm_scope.py` (compile, capture-set, py
runtime on both arms, all-six-backend emit, arm-binding-only, and nested
templates).

## The ranking, with tractability

Ordered by value over cost. "Bounded" means a single reviewer can scope and
land it without a design decision that touches the effect calculus.

### Group 1 — surface bugs (bounded, do these next)

1. **Template scope in block match arms (review item 8).** DONE, this note.
   The template of the group: a walk that misses one node kind.

2. **Untyped arrow params in provide scope (review item, roadmap 77 ticked,
   gap untracked).** Item 77 landed arrow-parameter annotations
   (`docs/design/75a-arrow-parameter-annotations.md`), but the review found a
   position where an arrow parameter is still refused for want of an
   annotation the checker could infer. Bounded once the exact failing position
   is reduced to a repro. Likely a checker-inference gap, not new grammar.

3. **`match` arms are expression-only in some positions (review item).** Block
   arms lower everywhere a lift sink is in scope; the review hit a position
   with no sink. This is the same lambda-lift machinery as item 8. Bounded:
   either thread a sink into the position or give the clear refusal a fix-it
   that names the module-`fn` workaround.

### Group 2 — missing stdlib (bounded each, but each needs all six emitters)

4. **Str methods: `trim`/`upper`/`lower`/`replace`/`contains` (review item,
   roadmap 155 area).** Each is a `_BUILTIN_SIG` row plus a `_BUILTIN_METHODS`
   lowering plus a body on each of the six tiers plus a self-host row. The
   table edit is trivial; the cost is six runtime implementations and keeping
   their emitted bytes in agreement. Land as a batch, one method family per PR.

5. **List methods: `map`/`filter`/`fold`/`sort` (review item, undocumented,
   currently refuse on java/wasm).** Higher cost than Str because the callback
   is a function value crossing into each tier's iteration. The review notes
   they already half-exist and refuse inconsistently, so step one is to make
   the refusal uniform and documented before adding the four methods.

6. **Float methods: `to_str`/`to_int`/`floor`/`round`/`abs`/`min`/`max`/`pow`
   (review item 12).** Float is currently one-way: it participates in
   arithmetic but cannot render or convert back. Same shape as Str: a table row
   family plus six bodies plus a self-host row. `to_str` is the single highest
   value one (a Float that cannot print is the sharpest wall) and can go first
   alone.

7. **Opt/Result methods (review item 13).** `map`/`unwrap_or`/`ok_or` and
   friends. Function-value callbacks like the List methods, so it inherits
   their per-tier cost. Sequence it after List so the callback-crossing
   machinery is settled once.

### Group 3 — new grammar and semantics (design efforts, not bounded)

These change the provide/activation grammar, the lowering, all six emitters,
the self-host port, and the gate crates. Each deserves its own design note and
its own PR series. Listed in the review's own priority order.

8. **`if` statement in provide-method bodies (review item 2).** The parser's
   own "expected a statement" diagnostic lists `if` among the accepted forms,
   then the `in_method` guard refuses it and points at the pure `if`
   expression instead. Closing the gap means a conditional-effect statement in
   a method body, which is a real extension of the effect grammar (what does a
   half-emitting conditional mean for teardown), not a parser tweak. Highest
   author value of the whole issue; highest cost too.

9. **`while`/`for` in provide-method bodies (review item 1).** Sanctioned
   recursion is the current answer and it dies on rust/java/wasm and hits a
   ~1000-frame limit on py. A bounded loop form in method bodies is the fix,
   and it is the largest single effort in the issue: iteration boundaries,
   teardown accumulation across iterations, and six emitters.

10. **Expression-bodied top-level fns (review item 3).** `fn f(x) = expr` works
    in a provide method but not at module top level, so an author writing a
    small pure helper must spell the `{ return … }` block. Smaller than 8 or 9
    but still parser plus checker plus every emitter, and it interacts with the
    top-level fn grammar. Bounded-ish; a good first Group-3 slice.

11. **Variants carry one payload, no tuples (review item).** A user variant
    binds a single payload; `Pair(a, b)` is not expressible. This is a type-
    system extension (variant arity) touching the checker, lowering, and every
    emitter's pattern rendering.

12. **Opaque host `Map` values (review item).** A host `Map`'s value type is
    unmodelled, so reads off it stay on the G8 audit surface. Typing it is a
    host-frontier decision (`docs/contract-errata.md`), design-only here.

13. **Coeffects: `time`/`random` (review item).** No ambient clock or RNG in a
    provide body. A new coeffect family, design-only.

14. **Smaller semantics: `let` scoping (roadmap 155 blocked), a `Unit`
    literal, `Map` non-`Str` keys (review item).** Each is its own small design
    question; grouped here so they are not lost.

## Slice plan

The order that maximises author value per PR while keeping each PR bounded:

1. Group 1 items 2 and 3 (this note lands item 1). Same lift/inference
   machinery, no new surface.
2. `Float.to_str` alone (Group 2 item 6, first method), then the rest of the
   Float family. Highest-value stdlib wall, and it establishes the
   six-emitter-plus-self-host batch shape the other stdlib families reuse.
3. Str methods (Group 2 item 4) as one batch.
4. List then Opt/Result (Group 2 items 5, 7), sharing the callback-crossing
   work.
5. Expression-bodied top-level fns (Group 3 item 10) as the first grammar
   slice, because it is the most contained.
6. `if` in method bodies (Group 3 item 8), on its own design note, as the
   flagship author win.
7. Everything else in Group 3, each on its own note.

Each stdlib and grammar slice that adds a `_BUILTIN_SIG`/emitter row must regen
both gate crates and keep the self-host byte-agreement oracles green; the
Group-1 fixes in this note do not, because they change no previously-compiling
program's output.
