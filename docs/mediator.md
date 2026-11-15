# The trusted mediator

A **mediator** is one component that alone holds a raw capability and gives
everyone else a narrow wrapper. The raw crossing exists in exactly one place, so
a review has one component to audit and every other component is wired to the
wrapper instead of the boundary. `examples/mediator.rvl` is the worked example:
`FsMediator` is the only component wired to `fs`, and `NotesConsumer` is wired to
the mediator's `append` wrapper.

Capability attenuation already narrows what a *spawned child* may reach
([capability-attenuation.md](capability-attenuation.md)). A mediator is the
composition-level version of the same discipline: the boundary is not handed
around, it is confined to one provider.

## What the gate proves

`revl audit examples/mediator.rvl` prints the asymmetry (the cardinality clause
is elided here):

```
component FsMediator  (examples/mediator.rvl)
  requires: fs
  provides: writer
  boundary: emissions: fs.write [fs] (0 of them compensated); capabilities: fs

component NotesConsumer  (examples/mediator.rvl)
  requires: writer
  provides: notes
  boundary: emissions: writer.append (0 of them compensated); capabilities: *
```

The raw token `fs` is on the mediator and nowhere else. The consumer reaches
`writer.append`, and because the wrapper is declared as a bare `emission`
(destination unnamed, the spelling item 561 names by the service it sits on),
the consumer's surface says `*` ("a boundary the wrapper owns"), not a token
the consumer may cross itself.

The consumer's direct attempt is refused by name. If it writes
`emit fs.write(...)` while its only requirement is `writer`, the gate refuses
with

```
G1 `fs` is not a declared requirement of DirectConsumer
```

This is the important half of the property: confinement is not a convention the
consumer follows, it is a declaration the compiler checks. A wrapper whose own
service *named* `fs` would hand the token to every consumer, which is why the
example declares `emission fn append` and not `emission[fs] fn append`.

## Marking it in the registry

A mediator is worth marking so that replacing it is a stronger decision than
replacing an ordinary component. The marking is a `mediator` facet in the
entry's `meta.json`:

```json
{
  "description": "the only holder of the raw fs boundary",
  "tags": ["fs"],
  "mediator": {"component": "FsMediator", "capability": "fs"}
}
```

`build_index` verifies it against the compiled audit: the named component must
hold the capability through a declared `requires` service whose method declares
it, must not re-declare it on any provided service, and must be the **only**
component in the entry's composition whose G8 reach carries that capability.
`*`, the unnameable boundary a bare emission has, cannot be marked. A marking
another component also reaches, a wrapper that hands the token to its consumers,
a capability nobody reaches or a capability not declared as a requirement is a
refusal, not a shrug. `verify` and the read-time index check treat the marking
like every other authority field, so an index that quietly drops it is not
believed. [registry.md](registry.md#15-marking-a-trusted-mediator) has the full
rules.

Once marked, `resolve` keeps the mediator's place unless a replacement clears a
stronger evidence bar: a cryptographically valid attestation that binds a
passing fault sweep. A candidate that cannot show both is ranked below the
mediator and its result carries `replacementBar` with the reason. When several
marked mediators match one need, the answer does not pick one by authority (a
decoy marking could win that choice); it reports the ambiguity in
`mediatorAmbiguity`, no candidate is exempt, and every candidate that lacks the
bar is ranked below those that have it. This is a hard lexical rule, not a trust
score; see [registry.md](registry.md#2-revl_resolve--the-one-search-verb).

## Honest limits

- The example's wrapper forwards `path_under_dir` unchanged. The name is the
  wrapper's contract; enforcing a path policy inside `append` is the mediator's
  own job, and this example is about the G8 confinement, not about writing that
  policy.
- Capability tokens are opaque. The gate proves the raw token is confined; it
  does not prove that `append` is *semantically* narrower than `fs.write`. A
  mediator is trusted code, which is why replacing it needs evidence.
- The marking is a single component and a single capability per entry. A
  composition with two mediators for two capabilities would need two entries,
  or a later extension; nothing here half-supports it.
