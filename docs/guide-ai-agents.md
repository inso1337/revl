# revl for AI agents

You are writing components in **revl 2.0**, a language for systems where
components load, unload, and hot-swap with *compile-time* safety. This guide is
written for you: the syntax was designed around what you already write well,
and around the mistakes you actually make.

> **Repo map, two lines.** The compiler is `src/revl/` (`parser.py` →
> `typecheck.py` → `lower.py` → `apply.py`). The **emitters are NOT under
> `src/`**: each tier lives in `backends/{python,typescript,rust,go,java,wasm}/`,
> its `emit.py` is the IR→host-language renderer, and most carry two or more
> expression dispatchers inside it (component vs fn-body; wasm has three),
> patch every dispatcher a change touches, and keep the `EXPR_DISPATCHERS` /
> `EXPR_REFUSED` tables in each emit.py in step (the conformance test
> `tests/test_expr_dispatcher_conformance.py` makes "did you patch both paths"
> a red test). Run `pytest tests/test_frontend.py tests/test_doc_examples.py`
> for the fast frontend loop; the full suite (~60s) before committing.

## The one rule

> **Same meaning → same syntax. Different meaning → different syntax.**

revl 2.0 is four strata. Know which one you're in before you write:

| stratum | allegiance | your job |
|---|---|---|
| 1. Pure expressions & functions | TypeScript subset | write it like TS; it is correct |
| 2. Types & data | revl's own | capitalized names, `[]` generics |
| 3. Components & effects | revl's own | slow down; this is the paradigm |
| 4. Host blocks | verbatim host language | label it `pure`/`acquire`/`emission` |

## Stratum 1: the TypeScript you already know

`fn`, `let`, `var`, `if`/`else`, ternary, `while`, `for (x of xs)`, arrow
lambdas, records `{a: 1}`, lists `[1, 2]`, indexing `xs[i]`, calls, `&&`/`||`,
`==`/`===` (identical), `!=`/`!==`, template strings `` `...${expr}...` ``.

```revl
fn fib(n: Int) -> Int {
  var a = 0
  var b = 1
  var i = 0
  while (i < n) {
    let t = a + b
    a = b
    b = t
    i += 1
  }
  return a
}
```

This compiles and runs as written. Write this stratum on autopilot.

## The traps (things that look like TS but aren't)

These are **excluded and named in diagnostics** ([syntax-2.0.md](syntax-2.0.md)
§3.3). Do not write them:

| you wrote | the problem | write instead |
|---|---|---|
| `n++` / `n--` | mutation; expressions are pure | `n += 1` on a `var` |
| `null` / `undefined` | no null in the type system | `None` (an `Opt[T]`), see below |
| `class`, `new`, `this`, `function`, `import`, `export`, `typeof`, `delete` | excluded | `type` / `fn` / `use` |
| `try { } catch { }` | failure is typed | `Result[T, E]`, or `fail` in a component body |
| `async` arrows in pure code, generators | excluded | `async fn` in a *service* |

## Small print (four things that are not TS)

- **`let`/`var` bind function-wide, not block-wide.** A `let` inside an `if`
  or `while` block is still in scope after the block ends. (Shadowing an outer
  name is refused, so this is safe.)
- **Statements are separated by newlines; `;` is not a separator.** One
  statement per line is the whole rule.
- **Strings have no escape sequences.** `"a\nb"` is four characters,
  backslash, `n` and all. Build multi-line output by concatenation and
  `` `${…}` `` templates.
- **A component activation body records effects only** (`let x = effect …
  undo …`, `emit`, `fail`, `if`, `return`), plain computation does not belong
  there. Put helper functions at top level and delegate from `provide { }`.

## Null safety (read this twice)

There is **no `null`**. Absence is `Opt[T]`, the ADT `Some(value) | None`.
`T?` is sugar for `Opt[T]`.

- `T` flows into `Opt[T]` automatically (you may pass `Some(5)`, or just `5`,
  where `Opt[Int]` is expected).
- `Opt[T]` does **not** flow back into `T`. If a function returns `Opt[Str]`,
  unwrap before using the value:

```revl
fn describe(name: Opt[Str]) -> Str {
  return match name {
    Some(n) => `hello ${n}`,
    None    => "anonymous",
  }
}
```

The diagnostic says: *`X` expects `T`, got `Opt[T]`, unwrap the optional
first: `match` on it, or use `??` to supply a fallback.*

## Types & match

```revl
type Row = { id: Int, name: Str }
type Outcome = Ok(Row) | NotFound | Invalid(Str)
```

- Records are structural; ADTs are nominal.
- `match` over an ADT is **exhaustiveness-checked**: omit a case and it won't
  compile, and the error names the missing case. Use `_` for "everything else".

```revl fragment
return match outcome {
  Ok(row)      => row.name,
  NotFound     => "-",
  Invalid(why) => why,
}
```

## Stratum 3: components & effects (slow down here)

This is the paradigm, deliberately **not** TS syntax. A component declares
what it requires and provides; its body is *revertible effects*.

```revl
service Database {
  emission fn execute(sql: Str) -> Int    // crosses the system boundary
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)   // its body emits (see "rules that bite")
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
```

Rules that will reject you if you forget them:

- **`effect E undo U`**, every mutation needs an inverse. A non-pure
  acquisition without `undo` is a compile error (G4).
- **`emit`**, calling a declared `emission` operation without the `emit`
  marker is an error (G4). An emission is irreversible; it must be *visible*.
- **Undeclared access**, a component reaches the world only through its
  `requires` (G1). Using an undeclared name won't compile.
- **No acquisition after `provide`** (A2); **no `await` in a provide-method
  body unless the operation is declared `async fn`** (A1).
- **`fail "msg"`**, deliberate L-Raise: reverts accumulated effects and lands
  the component FAILED.

## The guarantees are your safety net

The checker enforces, at compile time, the invariants a runtime can't
([DESIGN.md](../DESIGN.md) §4): G1 declared-only access · G2 provision
disjointness · G3 no dependency cycles · G4 inverse-or-emit · G5 no teardown
effects · G6 purity outside `effect` · G7 LIFO-complete teardown · G8
enumerable boundary. A program that compiles satisfies them. That is the
contract, and it is why a *generated* component is safe to self-deploy.

## Host blocks

When you need host power, say so honestly and classify it:

```revl
extern pure fn sha256(data: Bytes) -> Str
  = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
  = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
```

`pure` / `acquire` (needs `undo`) / `emission` (may have `compensate`).
Unclassified → no compile. The inverse/compensation slots sit on the
*declaration*, between the return type and the first host body:

```revl
extern pure fn close_ledger(h: Int) = @py { pass }

extern acquire fn open_ledger(path: Str) -> Int undo close_ledger(1)
  = @py { return abs(hash(path)) % 100000 }
```

| classification | `undo <expr>` | `compensate <expr>` |
|---|---|---|
| `pure` | refused (nothing to invert) | refused |
| `acquire` | **required** | optional |
| `emission` | refused (one-way crossing) | optional |

The same slots exist where you *call*: `effect E undo U` in a component body,
and an emission call site takes a best-effort cleanup after `compensate`:

```revl fragment
provide audit {
  fn record(line) {
    emit bus.send(line) compensate bus.send(`retract ${line}`)
  }
}
```

## Rules that bite (learned the hard way)

These are the refusals most likely to stop a generated component. Each was
added because real code got them wrong.

**A service declaration bounds its providers.** If a method's body reaches an
emission, directly, through an `emission` extern, or through a function that
does, the *service* must declare it:

```revl
service Cache {
  fn get(k: Str) -> Opt[Str]
  emission fn put(k: Str, v: Str)   // its body emits, so the interface says so
}
```

A provider may be purer than declared, never less pure. Skipping this is the
single most common rejection when a component writes anywhere.

**`emit` yields a value.** Use it in value position when you need the result:

```revl fragment
fn once(goal) = emit compiler.propose(emit assistant.complete(goal))
```

**Provide-methods take plain `fn`, no purity modifiers.** You cannot restate
`emission` (or `async`) on a provider: emission-ness is inherited from the
service declaration, which is the upper bound on every provider. Writing
`emission fn submit(…)` inside a `provide` block is refused before typechecking;
the modifier lives only in the service:

```revl fragment
provide queue {
  fn submit(id, payload) {        // plain `fn`; `emission` comes from JobQueue
    effect store.insert(id, payload)
    undo   store.remove(id)
    emit audit.record(`job ${id}`)
  }
}
```

**Method bodies take plain bindings.** Name intermediates instead of nesting:

```revl fragment
provide greet {
  fn hello(name) {
    let prefix = "hi, "        // plain value binding
    return prefix + name
  }
}
```

`let x = effect … undo …` still means the acquisition. In a component
*activation* body a plain binding is refused: that body records effects.

**`??` needs an optional on its left.** `a ?? b` on a non-optional is a type
error: the fallback would be dead.

**A void operation ends with a bare `return`** (or omits the return type and
returns nothing).

**`match` works in component and method bodies**, not only in `fn` bodies.

## Driving a running system (MCP)

`python -m revl mcp serve` exposes the compiler over MCP, and the loop it
enables is the point: a draft component never has to touch the filesystem. This
is your primary interface. The complete advertised verb set (36 verbs, from
`src/revl/mcp/server.py` and `query_tools.py`) is grouped below by what you
reach for; each verb's exact inputs and outputs are in
[mcp-reference.md](mcp-reference.md).

| verb(s) | use | detail |
|---|---|---|
| `revl_check` | does this compile? structured diagnostics — code, guarantee, and the `fix` rewrite — **and open holes' `fillSpec`** if not | [mcp-reference.md](mcp-reference.md#revl_check) |
| `revl_admit` | may it enter **this running composition**? | [mcp-reference.md](mcp-reference.md#revl_admit) |
| `revl_plan` | and then what? the delta a swap would produce, without applying it | [plan.md](plan.md) |
| `revl_ship` | check → admit → plan in one early-exit call; `apply:true` also swaps | [token-economy.md](token-economy.md) |
| `revl_resolve` | is there already an admission-compatible component to **import** instead of regenerating? | [registry.md](registry.md) |
| `revl_audit` · `revl_tools` · `revl_grammar` | the G8 boundary, the projected tool set, the prompt-sized language surface | [mcp-reference.md](mcp-reference.md#revl_audit) |
| `revl_load` · `revl_call` · `revl_state` | boot in memory, invoke a provided operation, inspect what is loaded | [mcp-reference.md](mcp-reference.md#revl_load) |
| `revl_edit` | patch the **server-side** source with a delta (hole-fill / range / anchor), you send the change, not the file | [mcp-reference.md](mcp-reference.md#revl_edit) |
| `revl_swap` · `revl_rollback` · `revl_undo` · `revl_unload` | replace a generation, undo the last swap, return to an earlier generation through the gate, tear down + prove no residue (R4) | [mcp-reference.md](mcp-reference.md#revl_swap) · [generation-history.md](generation-history.md) |
| `revl_gauntlet` · `revl_quarantine` | grade a candidate (proved / tested / claimed), or prove an untrusted one in the wasm sandbox | [gauntlet.md](gauntlet.md) · [quarantine-tier.md](quarantine-tier.md) |
| `revl_repair` · `revl_canary` | run the repair loop within policy, or canary a successor onto one realm slice and decide on evidence | [repair-loop.md](repair-loop.md) · [verified-canary.md](verified-canary.md) |
| `revl_lease` | claim/renew/release a TTL-bound lease on a component so co-agents do not replace it under you | [component-leases.md](component-leases.md) |
| `revl_snapshot` · `revl_restore` | capture / re-admit your evolved composition across a restart | [persistence.md](persistence.md) |
| `revl_timeline` · `revl_inspect_step` · `revl_step_back` · `revl_replay_bisect` · `revl_replay_forward` | walk, inspect, unwind, binary-search and re-run a recorded accumulator | [replay.md](replay.md) |
| `revl_query_{emitters,withdraw,dependents,reach,drift}` · `revl_live_query` | who emits to X? what breaks if I withdraw C? over source, or against the live session | [queries.md](queries.md) |
| `revl_history_emitted_between` · `revl_history_lifetime` | the same query envelope over a recorded run | [queries.md](queries.md) |

Two properties worth relying on: a **rejected candidate cannot deploy** (the
compile runs before the transition, so the running system keeps serving), and
`revl_unload` **proves** the component left nothing behind before you commit
it to disk. And every rejection carries its `fix` beside the guarantee, so the
diagnostic tells you the rewrite, not just the rule. See
[mcp-reference.md](mcp-reference.md) for the full verb set and
[docs/mcp-bridge.md](mcp-bridge.md) for the shapes.

MCP is your interface, but a human editing the same sources gets the same
checker through a language server. `python -m revl.lsp` speaks LSP over stdio:
diagnostics from the checker, hover from the diagnostic explanations, and
go-to-definition from the resolver (`src/revl/lsp/`). Same admission gate, a
different transport.

### The modern agent loop, scaffold → fill → resolve → admit

The verbs above compose into one workflow, and it is the discoverable path from
a vague need to an admitted component. Nothing touches the filesystem until the
last step:

1. **Scaffold with holes.** Write the component's shape and leave the parts you
   are unsure of as typed holes, `let cap: Int = hole "worker pool size"`. A
   hole type-checks, so the rest of the draft still checks ([holes.md](holes.md)).
2. **`revl_check` returns fill-specs.** A draft with holes compiles but can
   never admit; `revl_check` comes back with each open hole's `fillSpec`, its
   `line` and expected type. That is your worklist.
3. **Fill via `revl_edit` deltas.** Send a `{hole: <line>, expr: "<fill>"}` edit
   per hole, the server holds the source, so you transmit only the change, not
   the file. Deltas accumulate across calls; a refused patch advances nothing,
   so the working buffer never breaks. An edit that scaffolds a *new* hole comes
   back as another obligation with its own `fillSpec`, so you can iterate holes
   ([mcp-bridge.md §Deltas, not documents](mcp-bridge.md#deltas-not-documents--revl_edit)).
4. **`revl_resolve` before you regenerate.** For any hole or requirement that is
   a whole service, pass the need to `revl_resolve`, if an admission-compatible
   component already exists, importing it beats writing one, and the matched
   `source`/`manifest` ride back inline ([registry.md](registry.md)).
5. **`revl_gauntlet` / `revl_admit` to land it.** When the draft is hole-free,
   `revl_admit` answers "may this enter **this** composition?" mechanically, and
   `revl_gauntlet` upgrades that to a graded dossier (a boot/unload no-residue
   lifecycle in an isolated scratch session). Only what passes gets written out.

The whole loop runs server-side against the admission gate, so every
intermediate state is one the compiler already accepted.

Where that is checked: the tool surface, its annotations and its structured
rejections are gated by `tests/test_mcp.py`. Both properties above are gated
by `tests/test_mcp_session.py`
(`test_a_rejected_swap_leaves_the_running_system_serving`,
`test_load_call_and_unload_with_no_residue`), which needs the cordis-py
runtime and **skips in CI**. Run it yourself with
`sh backends/python/setup.sh` before relying on it.

## The check → run loop

Close the generate→check→run loop inside one file:

```revl
verified fn add(a: Int, b: Int) -> Int { return a + b }

test "add works" {
  assert add(1, 2) == 3
}
```

`verified` opts into the totality tier (no unbounded recursion/loops); `test`
blocks run under `revl test`.

## Driving a live composition: lifecycle and fault tests

`test` blocks check pure functions. Two more test kinds assert the paradigm's
own guarantees over a *live* composition (they need the cordis-py runtime;
`sh backends/python/setup.sh` installs it):

**`lifecycle test`**, load, call through provision keys, unload, and assert
nothing was left behind ([syntax-2.0.md](syntax-2.0.md) §7.1):

```revl
service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}

component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}

lifecycle test "a submitted job reverts cleanly" {
  load C
  call cache.put("k", "v")
  let hit = call cache.get("k")
  assert hit == Some("v")
  unload C
  assert no_residue
}
```

The statements are `load X` (optionally `with { field: value, … }` for the
component's config), `call key.op(args)` (bind its result with `let`),
`unload X`, and `assert no_residue`.

**`fault test "…" for Component`**, declares that activation *fails* at a
named step, then asserts the revert left nothing behind
([fault-tests.md](fault-tests.md)):

```revl
service P { fn ping(tag: Str) -> Str }

component Fragile provides p: P {
  let scratch = effect Map.new() undo scratch.drop()
  fail "deliberate L-Raise at activation"
  provide p { fn ping(tag) = tag }
}

fault test "mid-activation failure reverts its acquisition" for Fragile {
  fail at step 2
  assert no residue
}
```

`fail at step N` pairs with the Nth effect form (or `fail`) in the component's
activation body; the body may also say `assert effect <name>`. A fault test
body allows only `fail at …` and `assert …`, nothing else parses.

**Typed holes** round out the drafting loop: `let cap: Int = hole "worker pool
size"` compiles, the obligation is listed with its expected type, and admission
refuses the draft until you fill it ([holes.md](holes.md)).

## Reading error messages

Error messages are the interface, each names the guarantee and the fix.
Example:

> `` `missing` is not declared in this function ``, hint: declare it with
> `let`/`var` or add it as a parameter (G1).

When a compile fails, read the *hint*; it usually states the exact rewrite.

Three rejections are the verdict of a search over the whole composition, the
G4 emission fixed point, a G3 dependency cycle, a G2 provision conflict, and
those carry the derivation with them: the call chain, the cycle path, both
providers, each with a source location. It renders under the hint, and rides
in the structured diagnostic (and every MCP rejection) under `why`. See
[why-traces.md](why-traces.md). `revl explain <code>` turns any code back into
the guarantee it enforces and the rewrite that satisfies it.

## Common failure modes (check before you write)

1. Did you write `null`? → `None` / `Opt[T]` / `match`.
2. Did you mutate without `effect … undo …` (or `emit` for emissions)? → G4.
3. Did you use an undeclared requirement? → G1.
4. Is your `match` missing a case? → the error names it; add it or `_`.
5. Did you call an unknown method on a `List`/`Str`? → check the stdlib surface
   ([stdlib-2.0.md](stdlib-2.0.md)); note `push` is persistent
   (`out = out.push(v)`), never in-place.
6. Did you use 1.x `"$name"` interpolation? → `` `${name}` `` templates
   (`revl fmt --migrate` rewrites old sources).
7. Is your `extern` unclassified? → `pure`/`acquire`/`emission`.

## How you're measured

The 2.0 syntax ships only if you write it well ([syntax-2.0.md](syntax-2.0.md)
§10): the acceptance benchmark measures **first-pass compile rate** and
**iterations-to-green** across component specs written in {1.x, 2.0,
2.0+host-blocks}. First-pass compile rate is the metric. Treat the strata
boundary as the signal it is: autopilot in stratum 1, deliberate in stratum 3,
and let the exclusion diagnostics correct the rest in one shot.

The corpus is committed under `bench/results/`, and
`python3 bench/rescore.py --run all` recompiles it against the current
checker and prints a failure taxonomy. Read that taxonomy before writing:
its largest bucket has been "a `provide` method emits, but the service
declared the operation plain `fn`", the rule at the top of *Rules that
bite*.

