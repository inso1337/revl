# Typed holes

**Status:** implemented (2026-08-18). Successor discussion to
docs/syntax-2.0.md §3 — this document owns the `hole` construct.

Agents, and humans, write top-down. Without a placeholder you must write a
whole plausible file before the checker says anything useful, and by then the
one diagnostic you care about is buried under errors about the parts you
haven't written yet. A **hole** is the fix: a placeholder that

* **has a type**, so the code around it is checked for real;
* **satisfies the checker**, so a draft compiles;
* **is recorded as an unmet obligation**, so nobody has to remember it; and
* **can never run** — admission and every backend refuse it, loudly.

```revl
service Cache { fn get(key: Str) -> Str }

component C provides c: Cache {
  let pool = effect hole[Db] "a pooled connection" undo pool.close()
  provide c {
    fn get(key) = hole "look up in the store"     // : Str, from the service
  }
}
```

```
$ revl compile draft.rvl > draft.json
2 open holes — this is a draft: it compiles, admission will refuse it (docs/holes.md)
  draft.rvl:4: expects `Db` — "a pooled connection"
  draft.rvl:6: expects `Str` — "look up in the store"
```

## 1. Syntax

```
hole                      // type from context
hole "why"                // type from context, with a message
hole[T]                   // explicit type
hole[T] "why"             // explicit type and a message
```

`hole` is a reserved word (it joins the list in `lexer.KEYWORDS`), so it can
no longer be used as an identifier or a record field name. Nothing in the
corpus used it.

### Why `[T]` and not `: T`

Two reasons, both from syntax-2.0's governing principle.

1. **`[]` is already revl's type-application bracket** (`List[Row]`,
   `Map[Str, Int]`, §2). `hole[Db]` reads as a type position on sight, in
   exactly the notation the language uses everywhere else a type appears in
   expression-adjacent syntax. `<Db>` was never an option — §2 chose `[]`
   precisely to avoid the `<` ambiguity.
2. **`: T` would be genuinely ambiguous.** revl admits the TypeScript
   ternary verbatim (§3.2), so `c ? hole "x" : y` has two readings: a hole
   ascribed to `y`-the-type, or the ternary's else-branch. A grammar whose
   ambiguity depends on what the author meant is exactly the uncanny valley
   §0 warns about. With `[]` the construct is LL(1) and `c ? hole "x" : y`
   parses the one way it reads.

### Why a juxtaposed string for the message

`test "name" { … }` and `realm("label")` are the precedents; `test` won
because the message is prose *about* the hole, not an argument to it. No
other production in the grammar juxtaposes a string literal after an
expression, so `hole "why"` cannot be misread, and `hole` followed by
anything else is a bare hole.

The message is optional and free text. It is the note you would otherwise
leave in a `// TODO`, except the compiler holds onto it.

## 2. Checking

A hole checks as its **expected type**, and the rest of the body is checked
normally. That is the whole point: surrounding code still produces real
diagnostics.

```revl
fn score(r: Row) -> Int {
  let n = hole[Int] "count the active columns"
  return n.length            // `Int` has no `length` — a real error, today
}
```

The expected type comes from context wherever context has one:

| position | supplies |
|---|---|
| `return` in a `fn` with `-> T` | `T` |
| `return` (or `= expr`) in a provide method | the **service's** declared return (A6) |
| argument of a call to a declared `fn`/`extern` | the declared parameter type |
| element of a list checked against `List[T]` | `T` |
| field of a record literal checked against a record type | the field's type |
| arm of a `match` in check position | the expectation flowing in |
| `if` / `while` / `assert` / component-guard condition | `Bool` |

Where context has **no** type, the hole must say so itself:

```revl reject
fn f() -> Int {
  let x = hole "…"       // rejected: nothing here says what `x` must be
  let y = hole[Str] "…"  // fine
  return 1
}
```

The rejection is deliberate. Inventing a type for a hole would hand the
author an obligation the compiler made up, which is the same
drowning-in-noise failure holes exist to remove.

An **annotated** hole is still checked against its context, so
`hole[Str]` in an `Int` position is a normal type mismatch — a hole carries a
type it must eventually meet, and disagreeing with the context is a real
disagreement.

Holes are a stratum-1 pure expression, so they are available in `fn` bodies,
provide-method bodies, `test` blocks, component guards, and both the
acquisition and the `undo` of an `effect`. `emit hole …` is refused: `emit`
marks a call to a declared `emission` operation, and a hole is not a call.

**Known limit.** revl does not check that a nominal type name is declared
anywhere (`fn f(x: Nope)` is accepted today), and `hole[Nope]` inherits that.
Builtin generic arity *is* checked, so `hole[List[Int, Str]]` is rejected.

## 3. Obligations

Every hole is reported as `{file, line, type, message}`:

* `revl compile` prints the list to **stderr** (stdout stays exactly the IR
  document, so `revl compile x.rvl > x.json` is unaffected) and exits **0** —
  a draft is not a failure;
* `--json-diagnostics` prints the same list as
  `{"ok": true, "holes": [{severity: "obligation", code: "T3", file, line,
  expected, message, guarantee}, …]}`;
* the IR document carries `ir["holes"]`, present only when non-empty, so an
  IR document for finished code is byte-identical to what it was before this
  feature existed;
* the MCP `revl_check` result carries the same list under `holes` — an
  agent's remaining work, in the same call that told it the draft checks.

`ok: true` with a non-empty `holes` means *checked, and not admissible*.

## 4. Admission refuses holes

> A hole may never enter a running composition.

Compiling a draft is one question ("is what I wrote so far consistent?");
admitting it is another ("may this run?"). The second answer is no while any
obligation is open: a hole has a type and no implementation, so the
composition it joined would be one method call away from a runtime that
cannot answer.

The gate is `compile_files(files, manifest=running_ir)` — the same entry
point the CLI, `revl_admit` and `revl_swap` use. It refuses with code `T3`,
category `admission`:

```
cand.rvl:3: admission refused: this candidate still has 1 typed hole — cand.rvl:3 (`Str`)
  a hole is a recorded obligation, not code: it type-checks so the rest of the
  draft can be checked, but it has no implementation, so it may never enter a
  running composition. `revl compile` lists every hole; fill them, then admit
```

Booting is admission too, so `revl run` (including a `--watch` recompile) and
the MCP session's `revl_load` refuse a draft on the same grounds. A rejected
edit under `--watch` leaves the running composition untouched, as always.

## 5. Backends refuse to emit holes

Emitting a hole would mean writing a placeholder into Python/TypeScript/Rust/
Java/WAT and letting *that* toolchain be the thing that complains — in its own
vocabulary, about a line revl generated. revl already knows the draft is
unfinished, so each backend's `emit` refuses before a single character is
produced:

```
EmitError: refusing to emit Rust: this document still has 2 typed hole(s) —
draft.rvl:4 (expects `Db`), draft.rvl:6 (expects `Str`). A hole type-checks so
the surrounding draft can be checked, but it has no implementation and there is
nothing to lower. Fill every hole, then emit (docs/holes.md).
```

The check lives inside each backend rather than in one shared helper because
the backends are standalone modules by design — `emit.py` is loaded by path
and imports nothing from `revl` — and a refusal that can be bypassed by
calling the emitter directly is not a refusal. `revl test` therefore also
fails on a draft: there is nothing to run.

The conformance matrix (`tools/conformance.py`) is unchanged; holes are
covered by `tests/test_holes.py` instead, so the matrix keeps measuring what
each tier does with *emittable* constructs.

## 6. IR

One new expression node, in `functions`, `components` and `tests`:

```json
{"kind": "hole", "type": "Str", "file": "draft.rvl", "line": 6,
 "message": "look up in the store"}
```

`message` is omitted when there is none. No `ir_version` bump: a hole node
can only reach a backend that would refuse it anyway, and every backend does.
`file` is the declaration's own provenance (relative to the invocation cwd,
matching `component.source`), so an obligation in a multi-file composition
names the file you must actually open.

## 7. Grammar delta

```
expr := … | hole
hole := 'hole' [ '[' type ']' ] [ STRING ]
```

## 8. Fill specs — hole-directed generation

An obligation says *that* an agent owes an expression of some type at some
position. That is most of what a hole is for, but not all of it: the
obligation names what the fill must eventually *be*, and says nothing about
what the agent has to *work with*. Everything the checker knew standing at the
hole's position — the expected type, whether a fill may cross the emission
boundary, the bindings in scope, the services within reach — it already
computed and then dropped. A **fill spec** is that context, serialized.

The `revl_check` MCP result enriches every open hole with one:

```json
{
  "severity": "obligation", "code": "T3", "category": "hole",
  "file": "draft.rvl", "line": 8, "expected": "Str",
  "message": "look it up", "guarantee": "…",
  "fillSpec": {
    "expected": "Str",
    "capability": {"mayEmit": false, "bound": [],
                   "reason": "a non-emission provide-method — pure"},
    "bindings": [
      {"name": "ttl", "type": "Int"},
      {"name": "key", "type": "Str"},
      {"name": "raw", "type": "Str"}
    ],
    "reachableServices": [
      {"service": "Db", "method": "q", "signature": "q(sql: Str) -> Str",
       "instance": "db", "emission": false}
    ]
  }
}
```

The base obligation fields are byte-identical to §3, so an agent that only
reads `expected` and `message` keeps working; `fillSpec` is purely additive.

* **`expected`** — the hole's type (§2). A fill that does not have it is a
  type error before it is a wrong answer.
* **`capability`** — the emission upper bound at this position, the G4 question
  (docs/capabilities.md). A hole is a pure expression and can never be `emit
  hole` (§2); the question is what the *fill that replaces it* may do. An
  expression here may cross the emission boundary only inside a provide-method
  whose service method is declared `emission`, and then only within that
  method's bound: `bound` is the named capabilities of a scoped
  `emission[db, log]`, `null` for a bare `emission` ("any boundary"), and an
  empty list with `mayEmit: false` for every other position — a plain `fn`, a
  non-emission method, a `test`, or component setup. A fill that reaches for an
  emitting call where `mayEmit` is false is refused by the same G4 check that
  guards a hand-written body.
* **`bindings`** — every name in scope at the hole, with its type: the
  component's `config` fields, the enclosing method's parameters (typed from
  the service's declaration), and the `let` bindings that *precede* the hole
  (a binding declared after it is not in its scope). A type shown is one a
  declaration already fixed; a binding whose type no declaration pins down is
  listed with a `null` type rather than a guess.
* **`reachableServices`** — the component's injected dependencies (`requires`),
  each expanded to its full method table with rendered signatures, so a fill
  knows exactly what it may call and with what. `emission` flags the methods
  that themselves cross the boundary.

None of this is new inference. Each field is read off the compiled IR — the
services table, the component's `requires`/`config`, the enclosing method's
declared emission, the preceding bindings — the same facts a check established
and the same ones admission and the backends already trust.

### The scaffold-then-fill loop

The spec changes the shape of the work. Without it the loop is
**generate-whole → refuse → regenerate**: an agent writes a plausible whole
component, admission refuses the draft (§4), and the agent regenerates against
a diagnostic that describes one line at a time. With fill specs the loop is
**scaffold → fill → fill**:

1. write the component's skeleton with a `hole` at every not-yet-known
   expression — it compiles (§3), so the checker's verdict on the parts that
   *are* written arrives immediately;
2. call `revl_check`; each open hole comes back with its fill spec;
3. fill one hole, constrained by its spec — the expected type, the bindings to
   draw from, the services to call, and whether an emitting call is even
   permissible. Most wrong answers are unrepresentable before they are written;
4. re-check and repeat until `holes` is empty, then admit (§4).

Each fill is a bounded, local decision against a spec, rather than a whole-file
gamble against a refusal. The token economics of the two loops are the subject
of the note in `bench/README` under item 20's demand harness.

This loop, named end to end with the CLI verb for each step (scaffold, fmt,
explain, admit, with fill specs as this section), is
[authoring-for-agents.md](authoring-for-agents.md).
