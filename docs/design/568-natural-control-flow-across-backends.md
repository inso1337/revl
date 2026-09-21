# 556. Natural control flow across backends: the census behind item 458

Roadmap item 458, issue #721.

## What the item asked for, and what was actually there

The item was filed from revl-harness feedback in 2026-09. The harness's web and
voice shells routed every HTTP request through one ternary chain, and every
conditional crossing in that chain was hoisted into a module `fn` that took the
emission as an arrow:

```revl
fn maybe_run(go: Bool, sid: Str, prompt: Str, run: (Str, Str) -> Async[Str]) -> Str {
  if (go) {
    return run(sid, prompt)
  }
  return ""
}
```

Fifteen functions of that exact shape existed across `web_shell.rvl`,
`voice_shell.rvl`, `guarded_toolbox.rvl` and `guarded_dynamic_toolbox.rvl`. The
exit clause names three of them.

The first thing this lane did was ask, for each one, what constraint it was
working around. The answers were written into the harness beside the helpers,
and all three had already expired:

| the comment in the harness | the constraint it names | state on `main` |
| --- | --- | --- |
| "Provide bodies allow no `if` statements (G6)" | the method grammar had no statement `if` | gone: items 548 and 681 gave it `if`/`while`/`for`/`break`/`continue` |
| "the py tier does not await ternary-nested emits (finding #38)" | a ternary arm's async emission leaked a coroutine | gone: item 141 awaits it; the sync-method case is refused by name (A1, `examples/rejections/a1_async_op_sync_ternary.rvl`) |
| "the ts tier does not await a sync method's returned promise" (finding #40) | same, ts side | gone, same refusal |

So the migration itself needed no new language feature. That is a finding, not
a shortcut: an item that reads as a language gap can be a documentation gap
that the harness paid for over months, and the only way to tell is to read the
real code rather than the item text.

## What was still in the way

Three things, measured rather than assumed.

### 1. The go tier drops a component silently (the reason the exit could not hold)

`backends/go/emit.py` routes any ir_version-3 document that carries a top-level
`fn`, `type`, `extern` or plain `test` to the pure typed-core path, and that
path renders none of the components it routes past. A module `fn` beside a
component with provide methods is the ordinary shape of real revl code — every
revl-harness component file is exactly it, and it is what a migrated dispatch
looks like the moment a route calls a plain helper from an `if` arm.

Emitting the migrated dispatch for go produced 6185 bytes: the stdlib preamble
and two free functions. No services, no component, no routes, and no error on
either side of the fork. 75 documents in the tree are in that state.

This was load-bearing for the exit: "byte-agreement across tiers" cannot hold
when one tier answers with a program that has none of the behaviour. It is also
the worst failure mode an emitter has, because the artifact compiles.

It refuses by name now (`_refuse_pure_path_component_drop`), in the same voice
as the stream diversion's `_refuse_stream_document_top_level` immediately above
it. The boundary is kept: a component with no activation body and no provide
method loses nothing when it is routed past, so the record and pure-fn corpus
cases are untouched.

Refusing is not the same as carrying it. Carrying it means the go tier emitting
declarations and a live stc-go component into one module, which the placement
path already does and `emit()` does not. That is the next slice, named below.

### 2. The byte-agreement oracle did not reach the shape

`tests/fixtures/emit_*_corpus/` is where the self-hosted emitters are held to
the reference ones byte-for-byte. Before this lane the corpus reached a
provide-method `if` exactly once — `shadowing_near_misses.rvl`, one arm, no
`else`, no emission, no loop — and reached a method `while`, `for`, `break` or
`continue` not at all. The migrated shape had no oracle over it.

`tests/fixtures/emit_py_corpus/services_control_flow.rvl` is that oracle: the
harness's dispatch reduced to the sync surface, with the if-chain, an `else`
arm, a nested `if`, a `var` assigned inside a branch (what replaces
`maybe_run`), a bare `emit` step in a branch, `while` with `break`, `for … of`
with `continue`, and a module `fn` called from an arm. It was added failing
first: the self-hosted py emitter answered `<<UNSUPPORTED-METHODSTEP:if>>`
three times and dropped every route, and ts answered
`<<UNSUPPORTED-METHOD-STEP:if>>`. Both carry `_method_control` now and both
agree byte-for-byte.

The document is deliberately SYNC. The self-hosted py and ts emitters carry no
provide-method `async` at all — an async document diverges on the method
signature and on the services table's `async` flag, which is a different gap
from the control flow this document is the oracle for, and mixing them would
make a red here unreadable.

### 3. Compound assignment stopped at the method grammar

`x += e` parsed in a module `fn` and not in a provide-method body, where it
failed as ``expected a statement (`let`, `effect`, `emit`, `fail`, `if`,
`return`), found 'i'`` — a message that reads as though assignment itself were
out of bounds, in a grammar that carries `var`, assignment, `if`, `while` and
`for`. The method grammar uses the same `_assign_ahead` lookahead the fn
grammar uses now, and lowering desugars `x += e` to `x = x + e` exactly as the
fn path does, so the IR carries one `assign` step either way and no emitter
learned a second shape.

## What the migration removed

`inso1337/revl-harness`, branch `agent/721-natural-control-flow`: fifteen
functions, 106 net lines deleted from the two shells.

* `web_shell.rvl`: `maybe_run`, `maybe_ship`, `maybe_run_pended`, `ws_claim`,
  `ws_begin_turn`, `ws_cancel_turn`, `ws_comp_ship`, `ws_comp_revert`
* `voice_shell.rvl`: `voice_maybe_ship`, `voice_comp_ship`,
  `voice_comp_revert`, `voice_claim`, `voice_begin_turn`, `voice_cancel_turn`,
  `voice_run_pended_answer`

Each crossing now sits at statement level in the branch that wants it, and the
`go: Bool` parameter every helper existed to carry is the branch condition.

Seven helpers in `voice_shell.rvl` stay, and they are not of that class:
`voice_run_answer`, `voice_look_answer`, `voice_self_answer`,
`voice_hide_answer`, `v_classify`, `v_dogfood` and `v_maybe_auto` hold real
routing logic. They take emitting arrows because a module `fn` cannot hold a
service at all ("records carry data, not methods"), which is a separate
constraint and a candidate for its own item.

One ordering note, stated rather than left to be re-derived: both shells read
the shared-route dispatch (`emit routes.dispatch(…)`) at the top of the method
now. In the voice shell it used to sit in the middle of the `let` chain.
`HarnessRoutes.dispatch` is a chain of route helpers that each answer `hr_nm()`
unless their own (method, path) matches, so for an interface route it crosses
nothing and the move is inert.

## Remaining slices

1. **go carries a component beside a top-level declaration.** The refusal above
   states the gap; it does not close it. `emit_placement` already renders
   types, functions, externs and components into one module, so the work is
   routing `emit()` through that shape without moving the bytes of the 19 go
   corpus documents and the frozen scenarios. Until then go is honest and
   unable, rather than quiet and wrong.
2. **The self-hosted java, rust and wasm emitters carry provide-method control
   flow.** Measured on the fixture: java drops the provide impl class entirely,
   rust drops its stdlib trait preamble, and wasm raises `IndexError: revl: Str
   index out of range`. The wasm crash is the one to take first — a port that
   raises cannot even report a divergence. go's self-host port is a different
   matter: the whole live stc-go path is unported by design, so a go entry in
   this corpus needs slice 1 first.
3. **Provide-method `async` in the self-hosted py and ts emitters.** Today the
   port drops the `async` flag from the services table and from the method
   signature. This is what stands between `services_control_flow.rvl` and an
   async twin that pins the harness's actual `async fn dispatch`.
4. **`selfhost/lower.rvl` lowers a method-body control-flow step.** The new
   corpus document is byte-exact through both self-hosted EMITTERS when fed the
   reference IR, and diverges through the fully-native chain, so it is recorded
   in `LOWER_GAP_DOCS["py"]` and `["ts"]` in `tests/test_selfhost_compile.py`.
   The self-hosted frontend is a separate port from the self-hosted emitters and
   this is a gap in the frontend one.
5. **A module `fn` reaching a service.** The seven surviving voice helpers take
   emitting arrows only because of this. It is the last structural reason a
   harness route reaches for an arrow, and it is not a control-flow question.
