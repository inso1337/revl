# Capability-scoped emissions

`emission` is a boolean, and a boolean is a weak thing to hand an auditor —
or an AI author. "This component emits something" tells you it crossed the
system boundary; it does not tell you *which* boundary. For a component you
did not write, the second question is the one that matters.

A capability-scoped emission answers it in the declaration, and the checker
holds providers to it:

```revl
service Cache {
  emission[db] fn put(key: Str, value: Str)
}
```

> `Cache.put` may emit, but only through `db`.

Bare `emission` keeps its old meaning — *any* capability, no promise — so no
existing source changes meaning.

## 1. Syntax

```
methoddecl := modifier* 'fn' IDENT '(' [tparam (',' tparam)*] ')' ['->' type]
modifier   := 'emission' ['[' IDENT (',' IDENT)* ']'] | 'async' | 'commutative'
```

`[...]` is already revl's parameterisation bracket: `List[Row]`,
`Map[Str, Int]`, `Result[Row, Err]`. In every one of those it means "the
thing to my left, parameterised by the comma-separated names inside me".
`emission[db, bus]` is read the same way — an emission parameterised by the
boundaries it may cross — so it costs the grammar nothing: no new keyword, no
new token, and no ambiguity, because no modifier keyword could be followed by
`[` before (syntax-2.0 §4b.1, §5).

The alternatives were considered and rejected:

- `emission(db)` — parentheses are the call/parameter bracket everywhere
  else in the language; a capability list is not an argument list.
- `emission via db` — a new keyword, for one construct, in a language whose
  stated aim is a grammar that fits in a prompt (`revl_grammar`, <4000 chars).
- `@capability(db)` — `@` is taken by host bodies (`@python { ... }`).

`emission[]` is a parse error: an operation that may cross no boundary is a
plain `fn`, and spelling it two ways would be a trap.

## 2. What counts as a capability

A capability is the **name of the boundary a crossing actually goes
through**, drawn from a single flat namespace of wiring names:

| the emission reaches                                          | capability |
|---------------------------------------------------------------|------------|
| a service operation declared `emission`, called through required key `db` | `db` |
| an `extern emission fn send`, directly or through a chain of `fn`s | `send` |
| an `extern emission[db] fn pg_write`, likewise                  | `db` |
| an `extern witnessed[fs] fn stash` (items 243/343)              | `fs` |
| a boundary with no reachable name (defensive; unreachable today) | `*` |

Three deliberate choices:

**Requirement keys, not service names.** A key is composition-wide — G2
refuses two components providing the same key, so `db` denotes exactly one
boundary in a composition. A *service* name does not: two keys may be bound
to the same `Database`, and telling them apart is the whole point ("it writes
to the audit log, not the customer table").

**Externs name themselves, functions do not.** `extern emission fn send` *is*
the boundary — the host code lives there. A `fn blast(...)` that calls `send`
is not a boundary, it is a path to one, so it contributes `send`, not
`blast`. A capability set therefore stays stable when a body is refactored
into helpers, which is what makes the transitive rule usable.

**A scope replaces the name; it does not join it.** An extern names itself only
when it declares no scope. `extern emission[db] fn pg_write` contributes `db`
and nothing else, because the author has said which boundary the host code goes
to and that is the boundary an operator reasons about (item 343). One extern
therefore yields exactly one spelling to every authority surface — the G4
subset check, the G8 audit reach, `secret K for db`, the item-246 approval gate
and the `capability <glob>` policy rules — rather than a name for some and a
token for others. Item 247 finished that: `__main__._boundary` was the last
surface still keying a directly-emitted extern by name, so an operator's
`capability db requires register keyed` selected nothing on a composition whose
only `db` crossing was a direct emission. See "Which spelling a rule selects" in
docs/boundary-policy.md for the full table.

**Names are not resolved at the declaration.** A `service` is routinely
written before any provider exists, so `emission[db]` does not require a `db`
to exist yet. The names are checked where they can be: against what a
provider's body actually reaches.

## 3. The rule (G4, refined)

syntax-2.0 §4b.1: *a service declaration is an upper bound on its providers'
effects.* Capabilities refine the bound from a flag to a set, keeping the
direction:

> For every provide-method implementing `emission[C] fn m(...)`, the
> capability set the body reaches must be a **subset** of `C`.

- Subset — including empty — is fine. A provider purer than declared is
  sound: the consumer already assumed the worst.
- A capability outside `C` is rejected, naming the offending capability and
  the declaration that forbids it:

```
`Cache.put` is declared `emission[db]`, but this implementation emits
through `bus` (reaching `bus.publish`)
  a capability-scoped emission bounds *where* a provider may cross the
  boundary — widen the declaration to `emission[db, bus] fn put(...)` in
  service `Cache`, or route this emission through a declared capability (G4)
```

- Plain `fn` (no emission at all) is unchanged: any emission is refused, by
  the existing rule.
- Bare `emission` (no bracket) is unchanged: any capability is allowed.

### Transitivity

The boolean version of this analysis was a least fixed point over the call
graph (`_emitting_fns` in `src/revl/lower.py`): a `fn` emits if it calls
something that emits. The capability version is the same fixed point over
*sets* (`_emitting_capabilities`):

```
caps(extern emission fn e) = { e }
caps(fn f)                 = ⋃ { caps(g) | f calls g }
```

iterated to the least fixed point, so a capability propagates through any
depth of `fn` calls and recursion terminates. A provide-method's capability
set is then the union of

- the required key of every emission call in its body (`emit db.execute(...)`
  and value-position `let r = emit db.query(...)`, including
  teardown-position ones — calling the method schedules them), and
- `caps(n)` for every emitting name `n` the body calls.

### What is *not* transitive

Calling `emission[db] fn put(...)` through key `cache` contributes capability
`cache` — **not** `db`. The declaration names the boundary this component
crosses; `db` is a boundary of some *other* component, reachable only by
reading that component's declaration in turn. Keeping the capability local
means the check needs no whole-program fixed point over the service graph,
and it matches what a reader of one component can verify. `revl audit`
composes the chain back together across the whole composition (§5).

## 4. IR

A scoped emission carries the set; a bare one does not carry the key at all:

```json
"put": {"params": [...], "returns": null,
        "emission": true, "capabilities": ["db"]}
```

Absence of `capabilities` means "any", which is exactly what every
pre-capability IR meant — so no existing reference IR or backend golden is
invalidated, and the emit matrix is untouched. Backends emit the call the
same way either way; the capability set is a checker/audit artefact, not a
codegen one.

`_service_from_ir` reads it back and `_service_equal` compares it, so a
service redeclared across modules must agree on its capability set too.

## 5. `revl audit`

The G8 report annotates each emission call site with the scope the *called*
operation declares, and prints the union — where this component can reach:

```
component PgCache
  requires: bus, db
  boundary: emissions: bus.publish, db.execute (0 compensated); capabilities: *

component Front
  requires: cache
  boundary: emissions: cache.put [bus, db] (0 compensated); capabilities: bus, db
```

`Front` calls one operation and the audit says where that lands: `bus` and
`db`. `PgCache` calls two unscoped emissions, so its reach is `*` — that is
the honest rendering. The audit reports what the declarations say, and an
unscoped `emission` says nothing; a `*` in the union is a live invitation to
scope the dependency.

Which *local* key each crossing goes through is already in `emissions` (the
label is `key.method`), so the capability map adds the downstream half rather
than repeating it.

In `--json`:

```json
"Front": {
  "emissions": ["cache.put"],
  "capabilities": {"cache.put": ["bus", "db"]},
  ...
}
```

### Host code carries its scope too

A crossing that goes straight into an extern has no emission label, so it is on
the `host code:` line instead. A scoped extern renders its declared token there
the same way a scoped emission does:

```
component Writer
  boundary: host code: pg_write [db] (emission, py), send_mail (emission, py)
```

`pg_write` crosses `db`; `send_mail` declares no scope, so it names itself. The
`externs` list stays keyed by extern NAME — it is the host-code table (class,
backends, ref provenance), and a reader needs the name to find the declaration —
with the scope beside it:

```json
"Writer": {
  "externs": [
    {"name": "pg_write", "class": "emission", "backends": ["py"],
     "capabilities": ["db"]},
    {"name": "send_mail", "class": "emission", "backends": ["py"]}
  ]
}
```

`capabilities` is absent for an unscoped extern, whose token is its own name, so
every pre-item-247 audit document is byte-identical. `policy.component_reach`
reads the token off this entry (`capabilities or (name,)`), which is what puts a
directly-emitted crossing under a `capability <glob>` rule.

## 6. MCP

`revl mcp schema` derives its annotations from the checker rather than from
an author's assertion (docs/mcp-bridge.md). The capability set joins them:

- the tool `description` states the scope, or states plainly that an unscoped
  emission promises nothing;
- `x-revl.capabilities` is the declared set (`["*"]` for bare `emission`,
  `[]` for a plain operation);
- `x-revl.effects.reachesCapabilities` is what the body *actually* reaches —
  a subset the compiler enforces, so the two together are a bound plus its
  witness;
- `x-revl.guarantee` names the bound.

`readOnlyHint`/`destructiveHint` are unchanged: a scoped emission is still an
emission.

## 7. Where this lives

| concern | file |
|---|---|
| syntax | `src/revl/parser.py` — `_capability_list`, `MethodDecl.capabilities` |
| analysis | `src/revl/lower.py` — `_emitting_capabilities`, `_method_emissions`, the G4 check in `_lower_provide` |
| audit | `src/revl/__main__.py` — `_boundary` |
| policy reach | `src/revl/policy.py` — `component_reach` (the `capabilities or (name,)` rule) |
| MCP | `src/revl/mcp/schema.py` — `_tool`, `_method_effects` |
| tests | `tests/test_capabilities.py`, `tests/test_247_capability_reach_spellings.py`, `examples/rejections/g4_capability_not_declared.rvl` |
