# 1437. `emit` marks every emission crossing, and every marked crossing meets the approval floor

Issue #1437, with the two approval gaps that live in the same functions of
`src/revl/lower.py` (`_is_emission_call`, `_lower_emit_step`,
`_lower_emit_approval`). This is a language change: it newly refuses programs
that compiled before. It is a tightening, and it is deliberate.

## 1. The marker is required on every emission crossing

### The decision

Every emission crossing carries `emit` at its call site. A required service's
`emission fn`, a spawn handle's provision method, a host `emission` extern, and
a module `fn` that reaches one are the same kind of crossing, and the marker
rule does not depend on which of them carries it.

This was already the stated rule. `lower._is_emission_call` says an emission
extern "is a boundary crossing exactly as a service emission is, so `emit`
marks it too". The code held the `req` carrier and the spawn-handle carrier to
the marker in every setup-mode position, and the extern carrier in exactly one
position: inside an `emit` head's argument list (issue #1427, PR #1436).
Everywhere else an unmarked call to an emission extern compiled. It is refused
now:

```revl reject G4
extern emission fn charge(n: Int) -> Int = @py { return 1 }
service S { emission fn go(n: Int) -> Int }
component C provides s: S {
  provide s { fn go(n) { let r = charge(n) return r } }
}
```

```
call to emission `charge` must be marked `emit` (G4)
```

Issue #1437 asked whether the comment or the code should change. The product
owner decided the code, for three reasons:

1. The marker exists to make a crossing legible where it is written. That
   purpose does not depend on the carrier.
2. An exemption for externs makes the most dangerous crossings the least
   visible ones. A host extern is the crossing with no service declaration in
   between and, for a plain `emission`, no inverse at all.
3. The code's own comment states the rule. An implementation that disagrees
   with its own stated invariant is the defect.

### What the rule is now

In a component body (activation, provide method, an arrow written there), a
named call to an `emission` extern, or to a module `fn` that reaches one, must
be marked `emit` in every position the `req` carrier must be: a statement, a
binding, a `return`, an expression-bodied method, an operand, a condition, an
argument, an arrow body, an `effect` bracket's acquisition. The refusal is the
`req` carrier's, tag and message verbatim.

What it does not touch:

- **Teardown slots.** `undo` and `compensate` keep their documented
  bare-emission exception, as they do for the `req` carrier. An `undo` that
  reaches an emission is still refused, by G5.
- **`witnessed` externs.** A witnessed extern is in the emission-reach set for
  G4's evidence and for G8, but it is reversible by construction and legal
  only in effect position, where `effect` is its marker
  (docs/design/243-witnessed-externs.md). Without this exemption 8 correct
  corpus programs were refused, every one an `effect stash(p)`.
- **Module `fn` bodies.** A module `fn` has no `emit`. The function that
  reaches an emission becomes an emission call itself, and the marker goes on
  the component-body call to it.
- **`pure` and `acquire` externs.** Neither crosses in the emission sense.

The rule follows the emission fixed point (`emitting_fns`), and that fixed
point counts a function VALUE that escapes as reach. So a `fn` that only
returns an emitting function (`fn getship() -> (Str) -> Str { return ship }`)
is an emission call, and `dispatch(emit getship(), a)` is how the call is
written. That is the fixed point's existing judgment, which the provider upper
bound already applied; the marker rule only makes it visible at the call.

### Diagnostic order

The marker refusal is raised during lowering, so it precedes the checks that
run on the lowered program: A1's async fences and the G4 provider upper
bound. This is the order the `req` carrier already had. A program that is
about an adjacent rule marks its crossing, so it goes on measuring that rule;
marked, a plain-declared method reaching the extern is refused by the upper
bound exactly as before.

### What it costs, measured

On `origin/main` `7880a6a2` the census holds 931 programs, 495 `agree-admit`,
and an empty baseline. With the reference change alone:

| change applied | `agree-admit` | `false-admit/G4` | `tag-mismatch/G4->A1` | `msg-mismatch/G4` | `tag-mismatch/G4->BAD` |
|---|---|---|---|---|---|
| reference only, witnessed included | 478 | 18 | 7 | 8 | 1 |
| reference only, witnessed exempt | 486 | 10 | 7 | 8 | 1 |
| reference, witnessed exempt, corpus markers | 495 | 0 | 0 | 2 | 0 |

The two remaining `msg-mismatch/G4` are the consumer candidates below, and
they close when the gate carries the same rule.

Nine corpus programs were newly refused and got their marker, each a real
crossing: `backends/typescript/tests/fixtures/{a2a_agent, async_agent_loop,
async_fn_values, async_http}.rvl`, `tests/fixtures/emit_ts_corpus/component_edges.rvl`,
`tests/fixtures/query_mesh.rvl`, and three self-host oracle programs.
`a2a_agent.rvl` is regenerated from the importer rather than edited. The
documents about an adjacent rule (two A1 fixtures, five A1 oracle programs
(two of which read those fixtures),
the G5 arrow fixture, the G-RETAIN fixture, and six upper-bound oracle
programs) mark their crossing, and each keeps the exact verdict, line and
message it had on main.

The two `examples/ecosystem-consumer-{rs,js}/candidates/unmarked_emission_tool.rvl`
are about an unmarked emission, so they keep the marker refusal, and their
READMEs state it.

**The census understates the cost.** The test suite writes its own programs
inline, and 43 test files, six documentation code blocks and seven generator
sites wrote unmarked extern calls. Every one was a real crossing and carries
its marker now. The generators are `revl import a2a` (both operation shapes),
`import cordis`, `import openapi`, `import wit`, `revl mcp import`, and
`synthesize`'s host and remote providers. A green census is not evidence that
nothing a user wrote changes.

## 2. The approval floor is keyed by capability token, and every marked crossing meets it

An extern that declares `requires approval` makes its capability unreachable
without an `Approval[C]` edge on the crossing (item 246, Decision 3). Two
defects let a crossing skip that floor.

### A scoped extern was never required

`_approval_index` built its `required` set from extern NAMES, while
`_emit_crossed_caps` resolves a crossing to its capability TOKEN: the declared
scope when there is one. So `extern emission[production.payment] fn charge(..)
requires approval` crossed with no edge, while the same declaration without the
scope was refused. The scoped spelling is the one `src/revl/parser.py`'s own
docstring uses for the clause.

The set is keyed by token now, the rule every other fold over the same
crossing follows (docs/capabilities.md section 2: "a scope replaces the name;
it does not join it"). The requirement therefore belongs to the capability, as
a policy `capability C requires approval` rule does: a sibling extern that
declares the same scope, or a service operation declared `emission[C]` for the
same token, needs the edge too. The unscoped spelling already behaved this
way, because there the name is the token.

### Only the `emit` step was checked

`_lower_emit_approval` runs from `_lower_emit_step`, the `emit` STEP. The
`emit` VALUE form (`let r = emit charge(n)`, `return emit charge(n)`, an
expression-bodied method `= emit charge(n)`) is lowered by the expression
path, which never consulted the floor. In a provide method, where the value
form is the usual way to write a returning crossing, an approval-required
extern crossed with no edge in any spelling but the bare statement.

The value form has no `with` clause, so it can never carry the edge. It meets
the floor now with no edge, and an approval-required crossing written that way
is refused, with a hint to write it as an `emit … with a` step.

A marked call to a module `fn` that reaches the extern (`emit bill(1)`) was
also resolved to the head's own name, found no requirement on `bill`, and
crossed. The floor now reads what the `fn` reaches from the emission fixed
point (`env.emitting_caps`). `_emit_crossed_caps` itself is unchanged, because
item 470's intent refinement reads it and refuses a crossing it cannot name.

### Measured

On `7880a6a2`, all admitted before and refused after, with
``crossing capability `<token>` requires approval, but this `emit` carries no
covering `with` edge``:

- a scoped extern, `emit charge(1)` in an activation body, and in a provide
  method;
- a scoped extern with an edge for another scope (`Approval[staging.payment]`);
- `let r = emit charge(1)`, `return emit charge(1)`, `fn m() = emit charge(1)`
  in a provide method, scoped and unscoped;
- `emit bill(1)` where `bill` reaches `charge`.

The corpus holds no program that declares `requires approval` on an extern, so
the census moves only by this change's own documents.

## 3. The nested approval crossing

`emit log_line(charge(1))`, with `charge` requiring approval and nested
unmarked in another emit's arguments, had no step to bind an edge to. It is
refused on `7880a6a2` by the marker rule of issue #1427, in an activation body
and in a provide method, scoped and unscoped, by the reference and the gate
alike. `examples/rejections/g4_nested_approval_emission.rvl` pins the shape.

## What is not covered

- **Teardown slots and approval.** A `compensate` slot that calls an
  approval-required extern (`emit notify(1) compensate charge(2)`) is admitted:
  the slot keeps its bare-emission exception and has no place for a `with`
  edge. Whether a compensation may cross an approval-required capability is a
  question about the teardown contract, left open here.
- **Spawn-handle and service-typed-local carriers.** `_emit_crossed_caps`
  resolves only a `req` target and a direct extern, so a crossing through a
  spawn handle meets the floor only if its token is reached some other way.
- **The formal model** carries no fact about approvals. The three approval
  documents sit in the ratcheted `out-of-fragment-approval` bucket
  (`formal/out_of_fragment_ledger.json`) rather than being judged against the
  marker rule's `G` row.
