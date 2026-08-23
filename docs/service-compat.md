# Structural interface-compatibility at the admission gate (roadmap §5 / paper §6.6)

`compile_files(files, manifest=running)` is revl's runtime **admission gate**:
it decides whether a freshly-compiled component may be swapped into a running
composition. When a candidate redeclares a service the running composition
already provides, the gate used to require the two interfaces to be
**structurally identical** (`lower._service_equal`). The roadmap called this
out (§5, "no versioning or structural compatibility, paper §6.6").

This document defines the **compatibility relation** that replaces exact-match,
`lower._service_compatible`, and justifies why it is sound.

## What admission is protecting

An interface is used from two sides, and a change can break either:

* a **consumer** injected the key and *called* methods on it; its call sites
  were type-checked against the old interface and are **never recompiled**;
* a **provider** *implemented* the interface. A6 requires a provider to
  implement **every** method of the service it provides, with matching arity,
  `async`, parameter and return types, and a checked emission/capability
  classification — so a provider conforms to *exactly* the interface it was
  compiled against.

The relation is therefore not a generic subtype rule. It is **consumer /
provider-relative**: it asks which running components touch this service and
protects each according to how it uses it.

## The relation

Let `old` be the running interface and `new` the redeclaration. `new` is an
admissible replacement for `old` when, for the resulting composition, every
running component that touches the service still type-checks.

### When a provider is retained

A running provider is *retained* when this admission does not replace it. (A
swap that re-provides the key withdraws the old provider — G2 forbids two
providers of one key — so the common hot-swap has **no** retained provider.)

A retained provider conforms to exactly `old`. Any change strands it: an
**added** method is an unfilled obligation (A6 completeness); a **retyped**
method fails A6; a **dropped** emission or a **re-scoped** capability set
contradicts the classification the provider was validated under. So when a
provider is retained the only admissible replacement is **identity** — this is
the pre-§5 behaviour, and it is the sound one whenever the provider stays.

### When the provider is replaced (the hot-swap)

The new provider is checked against `new` in this very compilation, so only
running **consumers** constrain the change. A consumer's call site
`r.m(a…)` type-checks against `old` iff each `aᵢ` is `compatible` with `old`'s
parameter type and the result is used at `old`'s return type. For every such
call site to remain valid against `new`:

| change to a method `m` a consumer may call | admissible? | why |
|---|---|---|
| **add** a new method | yes | no existing call site references it |
| **remove** `m` | no | a consumer may call it |
| parameter **widens** (`compatible(new_pᵢ, old_pᵢ)`, contravariant) | yes | a value the consumer passed still fits the wider position |
| parameter **narrows** | no | a value the consumer passes may no longer fit |
| return **narrows** (`compatible(old_ret, new_ret)`, covariant) | yes | the consumer still uses the result at the old type |
| return **widens** | no | the consumer uses the result as the old (narrower) type |
| parameter **count** changes | no | call sites pass the old arity |
| `emission` **dropped** (emission → plain) | yes | purity tightened; the consumer's `emit` marker now sits over a plain call, and the boundary only shrank (G8) |
| `emission` **introduced** (plain → emission) | no | the consumer's call site is unmarked and would silently cross the boundary — breaks **G4/G8** |
| emission **capabilities widen** | no | the emission reaches capabilities the consumer never accounted for |
| `async` flips | no | the call site is written to await (or not) differently |
| `commutative` flips (service- or method-level) | no | it changes the reordering the consumer relied on |

Parameter contravariance and return covariance are read directly off revl's own
`typecheck.compatible` (which already models `Int→Float` widening, `Opt`
injection and function-type variance), so the relation stays in step with the
type system rather than reimplementing it.

### When nothing touches the service

If the running composition has **no** consumer and **no** retained provider of
the service — and every touched key resolved to a service (see below) — then no
running component can break, and the change is admitted **however it drifts**.
This is the honest strengthening the manifest makes possible: a change that
breaks the interface is refused only when there is actually something to break.

## Consumer/provider identification, and the conservative fallback

The composition manifest is `cordisc`-shaped: each component entry carries
`inject` and `provides` as bare **key** lists, not the service each key names.
`compile_files` therefore threads a `provision_services` map (key → service)
alongside the ambient manifest, built from the full IR document's `components`
(whose `provides` is a `{key: service}` map). `lower._service_touchers` uses it
to list the running consumers and providers of a redeclared service.

When a key cannot be resolved to a service — e.g. only a bare manifest
projection was supplied — the gate cannot rule out that the key touches this
service, so it stays **conservative**: it assumes the service is both provided
(strict/identity rule) and consumed (no vacuous admission). **Soundness beats
the relaxation.**

## The diagnostic

An incompatible swap is refused with a structured rejection that:

* keeps the load-bearing phrase *"differs from the running manifest"*, so
  `diagnostics.classify` still buckets it as an admission rejection (`G2`,
  category `admission`) — downstream consumers (`plan`, the MCP bridge) see no
  change of shape;
* names the **method** that broke compatibility and **why** (removed / added /
  signature / emission appeared / capabilities widened / …);
* names the affected running components (both consumers and a retained
  provider), and attaches a `why`-trace (`kind: "interface-drift"`) listing the
  service and each toucher.

Example:

```
service `Store` differs from the running manifest in a way that breaks `App`:
`get` becomes an `emission` — a running consumer's call site is unmarked and
would silently cross the boundary (G4/G8)
```

## Where it is wired in

* `lower._service_compatible(new, old, providers_retained)` — the relation.
* `lower._service_touchers` / `lower._admit_service_replacement` — identify the
  running touchers and route the verdict; called from `check_and_lower`'s
  service loop, replacing the old `_service_equal` gate.
* `lower._service_equal` is retained: it is the identity notion the relation
  uses as its fast path, the strict rule collapses to it, and it still guards
  genuine identity elsewhere (`plan`, capability round-trip tests). Two
  declarations of one service in a single compilation remain a **duplicate**,
  never a compatible swap.
* `compiler._provision_services` / the `provision_services` ambient field —
  the key → service map the touchers read.

## The rest of §5's "Do" (now landed)

**Key namespacing** — the other half of §5's "Do" — was deferred with this
pass and has since landed; see `docs/namespacing.md`. A provision key may be
written `ns::key`, the linker's per-`(key, realm)` table compares the qualified
string, and the admission gate reads the same qualified keys off the IR — so
two authors' `acme::db` and `bcorp::db` coexist while the compatibility
relation above still runs per service. Unqualified keys are unchanged, so this
document's examples and every v1 golden are byte-identical. Together the two
halves satisfy the multi-author-registry precondition `docs/registry-probe.md`
named for item 49.

**`plan._interface_drift`** no longer previews drift with a plain structural
`!=` over the two `services` tables: it runs this document's relation
(`lower._service_compatible` via the same touchers the gate uses), so a
*compatible* swap the gate admits is reported by `revl plan` with no drift, and
a genuine break is named the same way the G2 rejection names it.
