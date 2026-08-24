# Derived semantic versioning

The version number is a **measurement**, never a promise. A hand-picked semver
is a claim nobody can check; revl computes the required bump from the diff of
two compiled compositions, the same way Elm derives a package's version from
its public API diff — and one step further, because a revl interface carries
*effects*.

`revl version --against <previous.json>` prints the required bump and why.

## The rules

The bump is read straight off the drift classification the admission gate
(`_service_compatible`, DESIGN §5) already produces for every interface change.
It is not a second, hand-rolled comparison: for every operation shared between
the previous and current composition, `revl version` hands a single-method
projection of the old and new service to that real predicate and reads its
verdict.

| change | classification | bump |
| --- | --- | --- |
| a method or service **added** | additive surface | **minor** |
| a parameter **widened**, a return **narrowed**, a capability scope **narrowed** | compatible (relaxing) | **minor** |
| a method or service **removed** | breaks a consumer's call site | **major** |
| a parameter **narrowed**, a return **widened**, an arity/`async`/`commutative` change | breaks a consumer's call site | **major** |
| an operation **gains an emission** | consumers' G8 audits change meaning | **major** |
| an operation **loses an emission** | strictly purer (the direction §5 admits) | **minor** |
| nothing changed | identical interface | **patch** (no bump) |

The composition's bump is the **join** over every change: any major makes it
major, else any minor makes it minor, else patch.

### Why an emission is a semver event

An interface's shape can be byte-for-byte compatible and the version still has
to go major. If an operation that was pure becomes an `emission`, every
consumer's call site to it is now an *unmarked* boundary crossing: the same
G4/G8 audit that used to read "this call reaches nothing outside the system"
silently changes meaning. That is a breaking change to what the consumer can
prove, so it forces **major** even though no parameter or return moved.

The reverse — an operation that drops its `emission` — is strictly purer. A
consumer's audit only ever *shrinks*, so nothing a consumer proved is
invalidated. The drift predicate does not flag it as a break (correctly), but
it is not a no-op either: the narrowed authority is recorded as a **minor**
bump.

## Usage

`--against` takes a *compiled composition document* — the JSON `revl compile`
emits, which carries the `services` table (parameter/return types and the
`emission` classification) the diff reads. Produce one from the previous
sources, then diff the current sources against it:

```
# snapshot the released interface
revl version prev/*.rvl --emit-manifest > released.json

# later, derive the bump the current tree requires
revl version src/*.rvl --against released.json --current-version 1.4.2
```

```
version: MAJOR bump required against released.json
  1.4.2 -> 2.0.0

why: 2 interface change(s) (1 major, 1 minor)

breaking (major) — a running consumer's use is invalidated:
  [major] Store.get  (emission)
      `get` becomes an `emission` — a running consumer's call site is
      unmarked and would silently cross the boundary (G4/G8)

compatible (minor) — additive or strictly-purer surface:
  [minor] Store.size  (added)
      `size` is added to service `Store` — additive surface, breaks no
      running consumer
```

- `--current-version X.Y.Z` echoes the previous version and prints the computed
  next version (`1.4.2 -> 2.0.0`). Without it, only the bump kind is printed.
- `--json` emits the machine form: `{ bump, changes: [{service, method, kind,
  bump, reason}...], previousVersion, nextVersion }`. Each change carries its
  own classification and bump contribution, so a tool can render or gate on the
  derivation, not just the verdict.
- `--emit-manifest` prints the compiled composition document (the diff input a
  later `--against` reads) instead of deriving a bump. An `revl audit --json`
  interchange document is **not** a valid `--against` input: it carries the G8
  boundary surface, not the interface table the diff needs, and `revl version`
  says so rather than diffing nothing.

## The consumer regime

Semver protects any downstream consumer of the published interface, so the diff
runs the drift predicate in its **consumer regime** (`providers_retained =
False`): a consumer's call sites were type-checked against the old interface and
are never recompiled, so the new interface must keep them valid. This is
exactly §5's non-strict relation — a widened parameter or added method is safe,
a narrowed parameter or removed method is not. The bump does *not* depend on
which components happen to be wired in the current composition: a removal is
major even if nothing currently injects the service, because a future consumer
could.

## Relationship to the registry (item 49, phase 2)

This is the versioning half of item 9 made mechanical. `revl version` only
*measures* the bump; it does not enforce it. The registry's phase-2 publish gate
(roadmap item 49) is where the measurement becomes a rule: a publish whose
**declared** version contradicts the version this diff **computes** is refused,
the same way admission refuses an interface drift. The number stops being a
promise and becomes a measurement the registry can check.
