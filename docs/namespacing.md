# Namespaced provision keys (roadmap §5, the deferred half)

Provision keys wire a composition together: a component `provides` a service
under a key, and a consumer `requires` that key to have the service injected.
Until now the key space was **flat** — a bare identifier such as `db`. That is
fine inside one authored program, but a **component registry** (item 49) draws
components from many authors, and two of them independently providing `db`
collide on key identity under **G2** (provision disjointness) with no way to
tell them apart. `docs/registry-probe.md` named this as the one remaining
prerequisite for a *multi-author* registry; the structural compatibility half
of §5 (`docs/service-compat.md`) was already done.

This document defines the other half: **namespaced keys**.

## Syntax

A provision key may be qualified with a namespace using `::`:

```
ns::key
```

Both segments are ordinary identifiers. The form is accepted everywhere a
provision key is written:

```
component AcmeDb provides acme::db: Database {
  provide acme::db { … }
}
component App requires acme::db: Database {
  let c = effect db.open() undo db.close()   // bound as `db` — see below
}
```

`isolate`, `intercept` and a lifecycle test's `call` name a key the same way
(`isolate acme::db in realm`, `call acme::db.query(sql)`).

An **unqualified** key (`db`) is exactly as before: it has the empty namespace,
and everything below degenerates to the pre-namespacing behaviour. v1 programs
parse, lower and emit **byte-for-byte identically** — the syntax and semantics
are purely additive.

### Why `::` and not `ns/key` or `ns.key`

`::` is the smallest addition that fits revl's grammar with **no lexer change**:
the separator is simply two adjacent `:` tokens, so a single `:` remains the
`key: Service` separator and nothing else in the language shifts meaning. A `/`
form would clash with the division operator, and `.` is already method/field
access (`db.query`), so `acme.db.query` would be ambiguous. `::` reads as a
path separator and never appears elsewhere, so it is unambiguous in every key
position.

## Semantics

The **qualified string is the key's wiring identity.** `acme::db` and
`bcorp::db` are two distinct keys:

* **G2 / linking** compares the full string, so the two coexist in one
  composition without conflict, while two providers of the *same* qualified key
  still collide exactly as two `db` providers do today. Uniqueness is checked
  where it always was — the linker's per-`(key, realm)` provider table
  (`lower._link`); namespacing simply makes the strings it compares distinct.
* **Injection resolution** matches a requirement's qualified key against a
  provider's qualified key, so `requires acme::db` resolves to the component
  that `provides acme::db` and never to `bcorp::db`.
* **The admission gate** (`src/revl/admission.py`) and `plan._interface_drift`
  read these same qualified keys off the IR, so search-as-admission works per
  namespace: a registry query for a provider of `acme::db` is filtered against
  the consumers of `acme::db` alone.

### The binding name

A `requires` clause also introduces a **local name** into the component body —
you call methods on it (`db.query(...)`). That name is the key's **trailing
segment**: `requires acme::db` binds `db`. The namespace qualifies *resolution*
(which provider), the local segment is the *code-facing name*. So the body is
written the same whether the requirement is `db` or `acme::db`.

One consequence: a single component cannot require two keys that share a local
segment (`acme::db` and `bcorp::db` both want to bind `db`) — that is a
duplicate-binding error, the same one two `db` requirements raise today. A
component that genuinely needs both authors' databases is the rare case an
explicit alias would serve; that is left for a future extension and is not
needed by the registry.

## Lowering

* The AST carries the qualified string as the key of each `requires`/`provides`
  bind (`ns::key` or a bare `key`).
* A component's `Env` keys `requires` by the **binding** (trailing segment) for
  body and type resolution, and keeps a `binding → qualified-key` map alongside.
* The IR component record emits the **qualified** key in `provides` and
  `requires` (and hence `inject` in the manifest). For an unqualified key the
  qualified string equals the binding, so the IR — and every backend's emitted
  output — is unchanged. Backends therefore need **no** change for the
  unqualified path; the *qualified* path reaches a target runtime as of issue
  #1914 — see **Host identifiers** below.

## Host identifiers (issue #1914)

The qualified string is the *wiring* identity — the IR, G2's one-provider rule,
`revl audit` and the dependency queries all compare it — but `::` is not legal
in any target language's identifier grammar, so each emitter mangles the key
into a host identifier, at **one helper per backend** (`_host_key`) through
which every site passes: the `provide` site, every `call` site, every injected
reference, the runtime registry lookup and anything the seam or the sourcemap
prints.

```
_host_key(k) = k                                              if "::" not in k
             = "__".join(p.replace("_", "_u") for p in k.split("::"))
```

| key | host identifier |
| --- | --- |
| `db` | `db` — unchanged, byte for byte |
| `acme::greeter` | `acme__greeter` |
| `a::b__c` | `a__b_u_uc` |
| `a__b::c` | `a_u_ub__c` |

An unqualified key is returned verbatim, so a v1 program's emitted output does
not move — the whole emitted corpus is byte-identical across this change.

### Why the rule is injective

`p → p.replace("_", "_u")` is injective, because every `_` it writes is
followed by a `u`, so the escape can be read off left to right. Its image
therefore contains **no** `_` that is not followed by `u`; in particular no
segment image ends in `_` and no segment image contains `__`. So in a mangled
qualified key `__` occurs *exactly* at the segment joins: splitting on `__`
recovers the segment images and unescaping each recovers the segments, which
reconstructs the key. Two distinct **qualified** keys therefore cannot share a
mangled spelling.

One residual pair is left, and it is deliberate: a *flat* key that already
spells a mangled qualified one — `a__b` beside `a::b`, both `a__b`. A flat key
must keep its spelling (that is what byte-identity above buys), so this is the
one collision the rule admits; it is **refused by name** before anything is
emitted, never mangled into a silent alias:

```
provision keys 'acme__greeter' and 'acme::greeter' both mangle to the Python identifier 'acme__greeter': rename one of them
```

`_check_host_keys` sweeps `provides`, `requires`, `routes`, `isolate` and
`intercept` in one place per backend, so no key reaches an identifier site
without passing `_host_key` and no ambiguous pair reaches a runtime.

### Per tier

| tier | a qualified key becomes | note |
| --- | --- | --- |
| `py`, `ts`, `java`, `rust` | mangled, each `::`-part validated as an identifier | `::` is illegal in all four identifier grammars |
| `go` | mangled | as above, and `_camel` collapses `_`, so go's guard compares `_camel(_host_key(k))`: `a::b` and `a__b` both name `AcmeGreeter` |
| `wasm` | carried **verbatim** | a WAT identifier may contain `:` and the emitter's addresses are `.`-joined, so `$req.acme::greeter.hello` already names exactly one key; no mangling and no refusal, but each part is still held to the identifier grammar so a key cannot smuggle a `.` or `/` into an address |

A tier that cannot name a key must say so rather than emit something that will
not build; there is no tier left that silently writes an unusable identifier.

### Self-host ports

`selfhost/emit_*.rvl` mirror the reference emitters byte for byte, and the ports
carry a provision key unqualified. No corpus document uses a namespaced key, so
the ports stay byte-identical to their references on the corpus — the contract
`tests/test_selfhost_compile.py` asserts. Porting the mangling (and the
collision refusal) into the six ports is owed as its own item.

## What this unblocks (item 49)

`docs/registry-probe.md` proved the registry's search primitive is the §5
admission gate run as a filter, with one named dependency: key namespacing,
"so two independent authors both providing `db` do not collide on key
identity." That dependency is now satisfied. Two authors publish `acme::db`
and `bcorp::db`; both live in one composition; a consumer resolves the one it
asked for; and the admission gate filters candidate providers per namespaced
key. The multi-author registry's remaining pieces (an index and a non-raising
batch driver over the gate) are the mechanical layer the probe already
sketched.
