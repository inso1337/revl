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
| an `extern local fn append_line` (item 2146)                    | *none — it names no boundary, because it crosses none* |
| a boundary with no reachable name (defensive; unreachable today) | `*` |

The `local` row is the one entry that contributes nothing, and that is the
point: a `local` extern is a durable write to **local** state (a file, a row in
this process's own store) that crosses no boundary, so there is no counterparty
to hand a token to and no approval to ask for. It is still enumerated — `revl
audit` names the class and the erase report lists it — but it is not a crossing
and it seeds no capability (docs/syntax-2.0.md §6.1.1).

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

### Parameters, and a capability's own resource dimensions

A capability may carry resource parameters — `fs.write(path="/data")`,
`gateway.send(host="api.stripe.com")` — and a composition may declare the
parameters of its OWN capability (issue #1938):

```rvl
capability mail.send(account: discrete, folder: path)
```

The declaration is top-level and names the capability's dotted token. Each
parameter is `name: kind`, and the **kind vocabulary is closed** to `path`
(containment), `discrete` (equality) and `ceiling` (an integer bound), which
are the three orders the core vocabulary already uses. A declaration therefore
adds a DIMENSION, never a new order: the algebra, the audit and the approval
order are unchanged. The kind is not part of the token's bytes, so the spelling
an operator reads is `mail.send(account="ops")` whatever kind `account` was
given.

The registry is closed against *undeclared* names: a parameter that is neither
a core name (`path`, `host`, `table`, `calls`/`requests`, `size`/`bytes`,
`time`) nor a parameter of that capability's declaration is refused at parse
with `unknown capability parameter`. That is what keeps a typo from silently
narrowing nothing. See design 294, "Declared resource dimensions".

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
caps(extern emission fn e)       = { e }
caps(extern emission[C] fn e)    = C
caps(extern witnessed[C] fn e)   = C
caps(extern local fn e)          = {}      // item 2146: crosses no boundary
caps(fn f)                       = ⋃ { caps(g) | f calls g }
```

The seed is `capabilities or (name,)`, §2's rule written out: a scope replaces
the name, so the token G4 refuses on is the token `policy.component_reach`
reports and a `capability <glob>` rule names. Seeding a scoped extern by its
name instead made G4 refuse a provider that was exactly in bounds
(`emission[db]` implemented through `extern emission[db] fn pg_write`) and hand
the author a repair that widened a correct declaration toward a token no rule
can select.

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

### The reversible class: `witnessed[C]` (item 562)

A service operation may also be declared `witnessed[C]`:

```
service Box { witnessed[store] fn set(v: Str) -> Str }
```

The bound is the same shape and the same direction as `emission[C]`, over a
narrower kind of effect. A `witnessed` extern is the item-243 class: its writes
**persist on commit and revert on abort**, so the runtime both records what to
undo and can undo it. `emission[C]` says *this provider may cross the boundary
irreversibly, through these capabilities*; `witnessed[C]` says *this provider
may leave reversible state behind, through these capabilities, and nothing
more.*

It exists because the two classes are not interchangeable at the call site. A
record store offering `fn create_x(row) -> Str` beside one genuine emission
(`delete_with_history`) has no lawful `emission` spelling for `create_x`: the
only bound the checker used to offer widened the declaration to an irreversible
one, which then (a) reads as `set: emission` in `revl audit`, (b) lets the
provider reach a true emission behind a consumer that assumed a revertible
step, and (c) pends every write under a policy that pends by declaration. A
provider that reaches a true `emission` is still refused under `witnessed`
exactly as it is under a plain `fn` — and the repair it is offered is the wider
*class*, never a scope the class cannot have:

```
`Box.set` is declared `witnessed[store]`, but this implementation reaches
`a host emission`, `publish()`
  a `witnessed[...]` declaration bounds a provider to the effects a commit
  settles and an abort reverts; a step that cannot be undone belongs to the
  wider class — declare `emission fn set(...)` in service `Box` (G4)
```

A capability the declaration does not name is refused the way `emission[C]`
refuses it, with the repair kept in-class:

```
`Box.set` is declared `witnessed[store]`, but this implementation reaches
`tmp` (reaching `write_val()`, `scratch()`)
  a capability-scoped `witnessed` declaration bounds *where* a provider may
  cross the boundary — widen the declaration to `witnessed[store, tmp] fn
  set(...)` in service `Box`, or route this write through a declared
  capability (G4)
```

Three consequences worth stating outright:

- **Purer is always fine.** `witnessed[C] fn m` may be implemented by a body
  that touches nothing at all; the reverse (a plain `fn m` implemented over a
  `witnessed` write) is refused by the existing rule, and its diagnostic is
  unchanged.
- **The subset algebra is over the emission class.** The fixed point G4 runs is
  still `caps()` above (`witnessed[C]` seeds `C`, so a caller sees the
  capability), but the *provider* bound is checked against the reversible-only
  reach: a body that reaches one capability witnessedly and another
  irreversibly is refused, and the repair offered stays inside the class
  (`witnessed[store, bus]`), never silently upgraded to `emission`.
- **The surfaces that read the declaration read the class, not a widening.**
  `revl audit` lists the extern under `host code: ... (witnessed, py+ts)`, not
  among the component's `emissions`; `distributability` no longer reports the
  operation as an emission; and `revl mcp schema` drops the read-only proof
  (§6) while `destructiveHint` stays false. Before this spelling existed the
  only way to admit such a body was to widen the declaration, and every one of
  those surfaces then read an irreversible crossing.
- **A consumer call to a `witnessed` operation is a step, not a crossing.** The
  consumer still writes it plainly — `box.set(v)`, no `emit` — because a
  witnessed effect is one the caller's abort will undo. Only a true emission
  needs the marker.

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

A `witnessed[C]` operation is the same shape with its own key, and — this is
the point of the class — `emission` is **false**:

```json
"set": {"params": [...], "returns": "Str",
        "emission": false, "witnessed": ["store"]}
```

The key is absent unless the operation declares it, and `emission` is not set,
because a consumer reads `emission` to decide whether a call crosses the
boundary irreversibly. A witnessed operation does not: the call is a recorded
step its caller's abort will undo. That is also why no existing IR or backend
golden moved for this — the key is additive and present only when spelled.

## 5. `revl audit`

The G8 report annotates each emission call site with the scope the *called*
operation declares, and prints the union — where this component can reach:

```
component PgCache
  requires: bus, db
  boundary: emissions: bus.publish, db.execute (0 of them compensated); capabilities: *

component Front
  requires: cache
  boundary: emissions: cache.put [bus, db] (0 of them compensated); capabilities: bus, db
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

A `witnessed[C]` operation is the one class where the hints move, and for the
reason they exist: `readOnlyHint: true` is the checker's *proof* that a body
mutates nothing (docs/mcp-bridge.md, aka.ms/mcp annotations), and a reversible
write is still a write. Such a tool therefore carries `readOnlyHint: false`
with `destructiveHint: false` — the checker refused anything it could not
revert — plus `x-revl.classification: "witnessed"`, the vocabulary
`revl mcp import` already uses for this class, the scope under
`x-revl.witnessed` (`["*"]` for bare `witnessed`, `[]` for a plain operation),
and an `x-revl.guarantee` that names the witnessed bound.

## 7. Where this lives

| concern | file |
|---|---|
| syntax | `src/revl/parser.py` — `_capability_list`, `MethodDecl.capabilities` |
| analysis | `src/revl/lower.py` — `_emitting_capabilities`, `_method_emissions`, the G4 check in `_lower_provide` |
| audit | `src/revl/__main__.py` — `_boundary` |
| policy reach | `src/revl/policy.py` — `component_reach` (the `capabilities or (name,)` rule) |
| MCP | `src/revl/mcp/schema.py` — `_tool`, `_method_effects` |
| tests | `tests/test_capabilities.py`, `tests/test_247_capability_reach_spellings.py`, `examples/rejections/g4_capability_not_declared.rvl`, `tests/test_witnessed_service_op_1912.py` (the reversible class) |
| the reversible class | `src/revl/parser.py` — `MethodDecl.witnessed`; `src/revl/emission_analysis.py` — `_witnessed_hint`, `_method_emissions(irreversible_only=True)`; `src/revl/lower.py` — the `decl.witnessed` arm of the G4 check |
