# 1437. `emit` marks every emission crossing, whatever carries it

Issue #1437, on top of #1427 (PR #1436). This is a language change: it newly
refuses programs that compiled before. It is a tightening, and it is deliberate.

## The decision

Every emission crossing carries `emit` at its call site. A required service's
`emission fn`, a spawn handle's provision method, a host `emission` extern, and
a module `fn` that reaches one are the same kind of crossing, and the marker
rule does not depend on which of them carries it.

This was already the stated rule. `lower._is_emission_call` says an emission
extern "is a boundary crossing exactly as a service emission is, so `emit`
marks it too". The code did not do that. It held the `req` carrier and the
spawn-handle carrier to the marker in every setup-mode position, and it held
the extern carrier to it in exactly one position: inside an `emit` head's
argument list, which #1427 closed. Everywhere else an unmarked call to an
emission extern compiled. It is refused now:

```revl reject G4
extern emission fn charge(n: Int) -> Int = @py { return 1 }
service S { emission fn go(n: Int) -> Int }
component C provides s: S {
  provide s { fn go(n) { let r = charge(n) return r } }
}
```

The question #1437 asked was whether the comment or the code should change.
The answer is the code, for three reasons.

1. The marker exists to make a crossing legible where it is written. That
   purpose does not depend on the carrier.
2. An exemption for externs makes the most dangerous crossings the least
   visible ones. A host extern is the crossing with no service declaration in
   between and, for a plain `emission`, no inverse at all.
3. The code's own comment states the rule. An implementation that disagrees
   with its own stated invariant is the defect, not the invariant.

This decision was made by the project's product owner; this note records it
so it can be reviewed.

## What the rule is now

In a component body (activation, provide method, `every`/`subscribe` body, an
arrow written there), a named call to an `emission` extern, or to a function
that reaches one, must be marked `emit` in every position the `req` carrier
must be: a statement, a binding, a `return`, an expression-bodied method, a
condition, an argument, an arrow body, an `effect` bracket's acquisition. The
refusal is the `req` carrier's, tag and message verbatim:

```
call to emission `charge` must be marked `emit` (G4)
```

What it does not touch:

- **Teardown slots.** `undo` and `compensate` keep their documented
  bare-emission exception, exactly as they do for the `req` carrier. (An
  `undo` that reaches an emission is still refused, by G5.)
- **`witnessed` externs.** A witnessed extern is in the emission-reach set for
  G4's evidence and G8, but it is reversible by construction and legal only in
  effect position, where `effect` is its marker
  (docs/design/243-witnessed-externs.md). Treating it as an unmarked emission
  would refuse every correct witnessed program; measured below, that is 8
  corpus documents, every one of them a correct `effect stash(p)`.
- **Module `fn` bodies.** A pure `fn` has no `emit`. The function that reaches
  an emission becomes an emission call itself, and the marker goes on the
  component-body call to it.
- **`pure` and `acquire` externs.** Neither crosses in the emission sense.
- **Function values.** The rule judges named calls. A crossing reached through
  a function value is not a named call, and how it should be marked is a
  separate question this change does not answer.

## The implementation

`src/revl/lower.py::_refuse_unmarked_emission_call` drops the #1427
`_in_emit_args` condition and keeps `_expr_mode == "setup"`, the condition the
other two carriers read. It skips a witnessed extern by name.

`selfhost/lower.rvl::fn_call` drops the matching `cx.emitPos == "args"`
condition and keeps `!marked`, which is the gate's reading of the same mode.
`Ctx` gains a `witnessed` list, filled from `witnessed_extern_names(ts)`, for
the same exemption. The refusal is raised after the arguments are walked, as
the reference raises it after lowering them.

Seven generator sites that write provider methods delegating to an extern now
write `= emit <extern>(...)` when the extern is an emission: `revl import a2a`
(both operation shapes), `import cordis`, `import openapi`, `import wit`,
`revl mcp import`, and `synthesize`'s host and remote providers. Without that,
every file they generate would be refused.

The formal harness's marker rule saw only service crossings. The exporter
writes a `U` row on the pseudo-service `@host` for a host emission, in every
context but a bracket's `undo` (which the checker lowers in teardown mode and
leaves to G5), and `Oracle.g4OK` and the python reference read that row as an
emission.

## What it costs, measured

On the tree this change sits on (main `dd137a45` plus #1436, including its
arrow-argument fix), the census holds 906 programs with an empty baseline and
484 `agree-admit`. The arrow-argument fix moved none of the numbers below: no
corpus program was refused through the leak.

| change applied | `agree-admit` | `false-admit/G4` | `tag-mismatch/G4->A1` | `msg-mismatch/G4` | `tag-mismatch/G4->BAD` |
|---|---|---|---|---|---|
| reference only, witnessed included (the rule as first measured on `bb22be66`) | 467 | 18 | 7 | 4 | 1 |
| reference only, witnessed exempt | 475 | 10 | 7 | 4 | 1 |
| reference and gate | 475 | 1 | 0 | 0 | 1 |
| reference, gate, and the corpus markers below | 484 | 0 | 0 | 0 | 0 |

The last row adds this change's three rejection documents, so it is 909
programs: 484 `agree-admit`, 88 `agree-refuse/G4`, 40 `agree-refuse/A1`,
`--check` green and the baseline still empty. One case changes bucket without
diverging: `examples/rejections/g5_undo_fn_emission.rvl` moves from
`no-objection-out-of-slice` to `refuse-out-of-slice/G4`. The reference refuses
it with G5, which is outside the gate's slice; the gate now refuses it with the
marker message, because the gate walks an `undo` slot unmarked. The `req`
carrier already behaves that way in an `undo`, and the direction is the
fail-closed one.

Nine corpus programs were newly refused and got their marker, each a real
crossing: `backends/typescript/tests/fixtures/{a2a_agent, async_agent_loop,
async_fn_values, async_http}.rvl`, `tests/fixtures/emit_ts_corpus/component_edges.rvl`,
`tests/fixtures/query_mesh.rvl`, and the three self-host oracle programs
`async op admits an async body`, `async method reaching a rule-2-colored fn
admits` and `a coerced arrow nested in a sync arrow does not leak`.
`a2a_agent.rvl` is regenerated from the importer rather than edited.

**The census understates the cost.** Its corpus is the documents and oracle
programs above. The test suite writes its own programs inline, and about 130
distinct unmarked call sites across 44 test files were refused by this change,
along with six documentation code blocks. Every one was a real crossing, and
each now carries its marker; where the test was about an adjacent rule, the
marker keeps it testing that rule rather than tripping on this one. A green
census was not evidence that nothing a user wrote would change.

### Diagnostic order

The marker refusal is raised during lowering, so it now precedes the checks
that run on the lowered program: A1's async fences and the G4 provider upper
bound. That produced the seven `G4->A1` rows, four messages that changed from
the upper bound to the marker, and one `G4->BAD` (a G-RETAIN fixture the gate
cannot parse, whose reference verdict became the marker refusal).

The order is kept. It is the order the `req` carrier already has, and it is
the order the gate now computes too, so the two agree on every unmarked
program. What was decided per document is whether the program was about the
marker or about the adjacent rule:

- **Documents about an adjacent rule** mark their crossing, so they go on
  measuring what they were written for: the two A1 fixtures
  (`a1_async_extern_sync_method.rvl`, `a1_async_undo_suspends.rvl`), the five
  A1 oracle programs (two of which read those fixtures), the G5 arrow fixture,
  the G-RETAIN fixture, and the two upper-bound oracle programs. A test that asserted only the code `G4` would
  otherwise have passed on the marker refusal while no longer exercising the
  rule it names; those were marked too.
- **Documents about an unmarked emission** keep their verdict. The two
  `ecosystem-consumer-*/candidates/unmarked_emission_tool.rvl` are the only
  ones: their names say what they are, and their READMEs now state the marker
  refusal as the expected verdict.

## What the gate still cannot see

Two gaps predate this change and apply to every carrier, not only the extern
one. In both the gate answers `no_objection`, which is an escalation to the
reference, not an issued admission.

- A control-flow form the gate's statement reader does not model (`if`,
  `while` in a provide method) is skipped, so an unmarked crossing inside it
  is not seen.
- An `if`/`else` in an activation body hides the rest of the component from
  the gate. `tests/fixtures/emit_ts_corpus/component_edges.rvl` was the one
  corpus document this reached: with its crossing unmarked it was a
  `false-admit` after the port, and marking the crossing is what took it out.
