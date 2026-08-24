# truc — the revl component manager

**Name (decided 2026-08-24): `truc`, always lowercase.** · roadmap item 136 ·
identity and vocabulary (this doc) · technical design in
[the architecture](design/truc-architecture.md) · builds on
[registry.md](registry.md) and [capabilities.md](capabilities.md)

truc is how you bring other people's revl into your own — the fetch, resolve,
and publish front-end onto the component [registry](registry.md). It is a
package manager the way revl is a language: the familiar shape is there, but the
one promise underneath it is different. Every other manager fetches first and
finds out later. truc **admits every component through revl's own gate before it
joins the assembly**, so a fetched piece that would break your composition is
refused at assemble time — not discovered at runtime, in production, by you.

---

## 1. Identity

The identity is the act of assembly: taking little bits of stuff and putting
them together into something bigger. That *is* the composition model. Components
are the *petits bouts* — the little pieces — and a composition is what you get
when you assemble them. So the flagship verb is **assemble**, never "install".
You do not install a piece of a thing you are building; you fit it in.

The name is French, playful, deliberately unpretentious — the anti-"install",
the anti-ceremony. It is a manager for people (and agents) who build by
collecting bits and snapping them together, and who want the result to still be
*true* when they are done.

**Tone rule:** the inspiration (the Stupeflip DIY-collage ethos) is referenced
in prose only — never quote the lyric in docs, taglines, release notes, or
marketing copy. People who know, know. Keep it that way.

---

## 2. Vocabulary

Establish this vocabulary deliberately and use it consistently everywhere truc
appears — commands, docs, errors, release notes.

- a component is a ***petit bout*** — a little piece; the plural, **petits
  bouts**, is the whole point
- the things you depend on, vendored into your project, are your **trucs**
  (the plural is the gift)
- your project is a **composition** — the assembly of your petits bouts
- truc **assembles**; it does not "install", "download", or "pull in". Reserve
  *assemble* for the flagship act and mean it
- **admit** is the verb for passing revl's gate — a component is *admitted*
  into the assembly, or it is *refused*. Fetched is not admitted

Say *assemble*, *petits bouts*, *admit*, *composition*. The words carry the
model; drift in the words is drift in the idea.

---

## 3. The command story

The first loop is four verbs. This section is the conceptual reference — the
schema, host-extern surface, and gate-invocation mechanics live in
[the architecture](design/truc-architecture.md).

### `truc add` / `truc rm`

Pull a petit bout — a component and its manifest — into the composition.
`add` fetches the component from the registry, records it in `truc.toml`, pins
the resolved version in `truc.lock`, and vendors the source under `trucs/`.
`rm` is the exact inverse: it removes the entry, the pin, and the vendored copy.

```console
$ truc add audited_database
$ truc rm audited_database
```

`add` fetches and records. It does not, by itself, promise the composition
still holds — that is `assemble`'s job, and it is a separate, honest step.

### `truc assemble`

The flagship verb. Assemble does three things in order:

1. **resolve** — pick a compatible version of every truc, against the
   registry's `provides`/`requires` surfaces
2. **admit** — run each resolved component through revl's admission gate: **G2**
   (no two providers race for one key) and **G4** (no operation reaches farther
   than the capabilities it declares). A component that would violate either is
   *refused here*, with a why-trace, before it is ever part of the assembly
3. **compose** — with every piece admitted, fit them together into the
   composition

```console
$ truc assemble
```

If a fetched truc would break G2 or exceed its declared capabilities (G4),
assemble stops and tells you which piece, and why. You do not get a broken
composition and a runtime surprise. You get a refusal and a reason.

### `truc ship`

Publish. `ship` is the verb the ecosystem already uses, and it means the same
here: take your composition (or a component of it) and publish it back to the
registry so others can `truc add` it. What you ship carries the same admitted
surface everyone else will read — `provides`/`requires`, capability counts,
manifest and source hashes.

```console
$ truc ship
```

### Files and layout

- **`truc.toml`** — the manifest. What your composition is, which petits bouts
  it depends on, what it provides and requires. Hand-edited or written by `add`.
- **`truc.lock`** — the resolved lock. The exact versions `assemble` picked,
  pinned so the assembly is reproducible.
- **`trucs/`** — where vendored petits bouts live, in the tree, readable.

### Two front doors

truc has a dual entry point. There is the standalone `truc` command — the brand,
the registry surface, the full vocabulary:

```console
$ truc add audited_database
$ truc assemble
$ truc ship
```

…and there is a `revl` alias, so the language keeps one front door for
newcomers who never left `revl`:

```console
$ revl add audited_database
$ revl assemble
```

Both drive the same engine. `truc` for people who think in trucs; `revl add`
for people who just want to add a thing.

---

## 4. What makes it not npm

This is the differentiator, and it is worth saying without hedging.

Every other package manager fetches a package, wires it into your build, and
*then* — maybe, at runtime, in production — you find out it does something it
should not. The check, if any, is downstream of the decision to depend on it.

truc inverts that. A fetched component is not yet part of your assembly; it is a
candidate. Before it joins, it goes through **revl's own admission gate** — the
same gate the language already runs on every composition:

- **G2** refuses a truc whose provider would collide with one your composition
  already has. Two petits bouts cannot both claim the same key.
- **G4** refuses a truc whose operations reach farther than the capabilities it
  declares. A piece that quietly emits, or crosses a boundary it never advertised,
  does not get in.

The refusal happens **at assemble time, with a why-trace**, not at runtime with
a stack trace. truc's promise is not "packages, but faster." It is *assembly you
can admit*: the bits declare what they cross before they are in, and the ones
that lie about it never join.

That is the verification-first story, and it is what makes truc safe to hand to
an agent. You can let an agent go `truc add` a dozen petits bouts, and the
composition it hands back is either admitted — every piece through G2 and G4 —
or it is refused with a reason the agent can read and act on. There is no third
state where it fetched something dangerous and shipped it anyway.

---

## 5. Agent-first, and built in revl

Two things make truc more than a wrapper around the registry.

**It is built in revl.** truc is itself a revl composition — it dogfoods the
model it manages. The fetch/resolve/publish tool is assembled out of petits
bouts, admitted through the same gate it makes you pass. If the vocabulary and
the guarantees did not hold up under their own weight, truc could not be written
this way. It is.

**It is the front-end onto the registry that already exists.** truc does not
invent a package format. It is the fetch/resolve/publish face of
[`registry/`](registry.md), whose `index.json` already carries, for every
component:

- its `provides` / `requires` service surfaces (what resolve matches on),
- its `manifestHash` / `sourceHash` (what a pin verifies against), and
- its capability and emission counts (what G4 admits against).

The registry made those surfaces first-class so that resolve *is* admission —
search returns only what the gate would admit ([registry.md](registry.md) §5).
truc is the ergonomic loop on top: `add` to fetch a surface, `assemble` to admit
and compose it, `ship` to publish one back. The hard part — a machine-checkable
notion of "does this piece fit, and is it honest about what it does" — the
language already had. truc gives it a front door, a vocabulary, and a name.

---

*For the manifest schema, the host-extern fetch design, and how `assemble`
invokes the gate, see [the architecture](design/truc-architecture.md). For the
gate itself, [threat-model.md](threat-model.md) (G2, G4) and
[capabilities.md](capabilities.md). For the registry surfaces truc reads,
[registry.md](registry.md).*
