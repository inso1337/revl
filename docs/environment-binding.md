# The environment contract: what the host must inject

Some of what a composition needs is known before the composition exists. The
port it listens on, the auth token that gates it, the directory it keeps its
data in, which model provider it boots against: the host has to know all four
to stand the composition up, and it reads them from CLI flags, environment
variables and credential stores, outside revl, by construction. That floor is
real and it is not going away; `composition-bootstrap.md` explains why.

What was missing was a place to *write it down*.

Before item 350 those values arrived through `revl run --config`, an
undeclared, host-authored map keyed by component name. It could set any field
of any component. Nothing in the source said which values were environment,
nothing in the IR distinguished them from author-written configuration, and
nothing in `revl audit` showed them at all. The boundary existed; it was just
invisible.

A **`boot` component** is where you write it down.

```revl
service Env {
  fn data_root() -> Str
  fn port() -> Int
}

boot component HarnessBoot provides env: Env {
  config {
    data_dir: Str under "./.harness-data",
    port: Int = 8099,
    model: Str in ["mock", "real", "engine"] = "mock",
    auth_token: Secret[Str],
  }

  provide env {
    fn data_root() = config.data_dir
    fn port()      = config.port
  }
}
```

That `config {}` block is the **environment contract**: the exhaustive, typed
list of what the host must inject before anything can load. The host supplies
it with `revl run --env FILE`, a flat `name = value` table:

```toml
data_dir = "./.harness-data/live"
model    = "real"
port     = 9000
```

`boot` is a contextual keyword. It heads a declaration only immediately before
`component`, so a program that already uses `boot` as an ordinary name is
unaffected, and the lexer's keyword table (and with it the generated
`revl-gate` crate's frontier table) is untouched.

## Four rules make the contract closed

All four are enforced at admission, before any runtime is imported. That is the
same discipline the required-config preflight follows, so the diagnostic works on an
interpreter with no cordis installed at all.

1. **One door.** With a boot component declared, a `--config` table naming it is
   refused. Its values arrive only through `--env`. A reviewer reading the host
   invocation can see which values claimed to be environment.
2. **No silent arrival.** An `--env` key the contract does not declare is
   refused, not carried through and not quietly dropped. A typo in the host's
   env file fails the boot instead of leaving a field at its default.
3. **No missing value.** A required (non-defaulted) contract field with no
   injected value refuses the boot, naming the field and the `--env` door.
4. **Bounds.** A value outside a field's declared bound is refused, naming the
   field and the bound, never the value.

## Rule 4 is the authority rule

Rules 1 to 3 buy visibility. Rule 4 is why the declaration is more than
paperwork.

An environment value is data. Data adds no capability, so injecting one cannot
change the `emission[...]` set a component declares or the capabilities `revl
audit` reports. But where an injected value lands in a *resource-scoped*
position, it is the thing that **spells** the scope:

- a data dir becomes the filesystem path a component actually writes to;
- a provider identity becomes the network destination an emission actually
  posts to.

Both widen what the component really reaches while the declared capability set
stays exactly the same, so nothing in the audit moves. That is the silent
widening, and it is the reason a bare typed declaration would have been a
fail-open surface: writing `data_dir: Str` down and letting the host inject
anything is precisely as wide as the map it replaced.

A bound puts that widening back under admission:

- `under "<prefix>"`: a `Str` path confined to a prefix. A `..` segment is
  refused outright rather than normalized away; an absolute value under a
  relative prefix (or the reverse) is unrelatable and refused; and the
  normalized value must sit at or inside the normalized prefix, so a sibling
  that merely shares the prefix as a *string* (`./.harness-data-evil`) does not
  pass.
- `in [lit, ...]`: an enumerated admissible set, for the "which provider"
  class of value.

A bound is legal only inside a `boot` component's config block. On an ordinary
component it would be grammar nothing enforces, which is the shape this item
exists to remove, so it is refused there with that reason.

Diagnostics name fields, types and bounds freely, since those are author-written
source. They never echo an injected value: a contract field may be a
credential, and a preflight diagnostic is the one place a check like this could
leak one.

## What the audit shows

`revl audit` prints the contract ahead of the components, because it is what
the host must inject before any of them can load:

```
environment contract (boot component HarnessBoot):
  data_dir: Str  (required, under "./.harness-data")
  port: Int  (optional, unbounded)
  model: Str  (optional, in ['mock', 'real', 'engine'])
```

`revl audit --json` carries the same table under `env`, and the drift gate
gains an `env:<name>:<bound>` crossing per field. Three distinct authority
changes therefore all land as a token *addition*, which the gate flags as a
widening:

- a new contract field appears;
- a field's bound is removed (`env:data_dir:under=./.harness-data` becomes
  `env:data_dir:*`);
- a bound is loosened, so its fingerprint changes.

Removing a field, or tightening a bound, drops the old token: a narrowing,
always safe. `*` in a token means **unbounded**: the host may inject any value
of the declared type. The audit says so explicitly rather than leaving it
unsaid, so an unbounded field is a visible choice rather than an omission.

The table carries name, type, requiredness, the `Secret[T]` marking and the
author-written bound. It never carries a value: values arrive at `revl run
--env` time, after the audit, and never enter the IR.

## The MCP load seam

`revl_load` over the MCP surface has a single config channel, so there is no
`--env`/`--config` split to enforce there: a boot component's entry in the
supplied config map IS the environment injection. Everything that is a property
of the *value* rather than of the channel still holds, checked before any
runtime is touched: an undeclared key, a value of the wrong declared type, and a
value outside its declared bound are each refused. Without that, the MCP surface
would be a way to inject past the bounds the CLI enforces, which is the silent
widening this contract exists to remove. The refusal travels back over the wire,
so like the CLI's it names the field and the bound and never the value.

## Rules the shape implies

- **At most one boot component per composition.** Two contracts cannot both be
  the exhaustive list of what the host must inject, and an admission check
  against "the" contract would silently check only one of them. Refused at
  link, which sees ambient components too, so a swap that would introduce a
  second one is caught before the generation is admitted.
- **A boot component cannot be spawned.** A spawn's config comes from an
  author-written `with { … }` that no admission check bounds, so spawned
  instances would carry the contract's fields without its bounds. Move the
  spawnable work into an ordinary component.
- **Everything is conditional.** A composition that declares no boot component
  is byte-identical through parse, IR, manifest, audit and every emitted tier.
  `--env` against such a composition is refused rather than ignored.
- **A `Secret[T]` contract field is an inbound channel only.** The marking
  mints the `confidential` origin (item 256 §7a), so the field reads normally
  inside the component and is refused where it would leave: a log, an ordinary
  serialization, a model prompt, and the return of a `provide` method. That
  last one is why the contract above declares `auth_token` and never hands it
  back through `env`. A provide-method return is marshalled by value across the
  service / MCP bridge and the placement seam, to a caller that declared `->
  Str` and so never declared a `Secret[T]` receiver. Hand the credential to the
  component's own host binding instead, or downgrade it at a declared
  `endorse[confidential](…, reason = "…")` slot.

## What this does not close

Stated plainly, because a security surface with an unstated edge is worse than
one with a documented edge.

**The host can still bypass the contract.** Nothing stops a host from taking an
environment-read value and injecting it through a *non-boot* component's
`--config` table. revl cannot close that from the inside: the host compiles the
composition and plugs it, so it is trusted that far by construction. That is the
same floor `composition-bootstrap.md` documents for the manifest bootstrap. What
the boot component removes is the *silence*. The declared arrival point is in
source, typed, bounded, audited and diffed, and a host that routes around it is
doing something a reviewer can see it doing.

**`under` is lexical, not resolved.** The preflight runs before any runtime and
does not consult the filesystem, so it normalizes the spelling rather than
resolving it. A symlink *inside* the prefix still points wherever it points.
Confining that needs an enforcer holding the filesystem at run time, which is
roadmap item 411's sandbox lane (its container runtime is a trusted enforcer for
exactly this reason), not a check in the frontend.

**An unbounded field is unbounded.** Rule 4 is opt-in per field. A contract
that declares no bounds is exactly as wide as the host-written map it replaced;
it is just visible now, and the drift gate will flag the moment a bound goes
away.

## See also

- [composition-bootstrap.md](composition-bootstrap.md): why the first compile of
  any session is unavoidably host code, the floor this sits on
- [syntax-2.0.md](syntax-2.0.md) §6.2: the grammar
- [audit-diff.md](audit-diff.md): the `env:` crossing token
- [capabilities.md](capabilities.md): what a component declares it can reach,
  which an environment value never changes and always spells
