# `verified effect` — inverse round-trip testing

*Upgrading "trust the author's undo" to "this undo survived N round trips."*

Implementation: `src/revl/parser.py` (the `verified` modifier),
`src/revl/lower.py` (the IR marker + the activation-body placement rule),
`src/revl/fault.py` (the round-trip runner and the fingerprint),
`src/revl/test.py` (surfaced through `revl test`), `tests/test_verified_effect.py`,
`examples/verified_effect.rvl`. Roadmap item 26.

---

## 1. The gap this closes

Every effect in revl declares its inverse (G4: **inverse-or-emit**), and the
checker proves that inverse is *present and well-shaped*. It never proves it is
*correct*. `docs/replay.md` §4.1 states the concession outright — the things
the runtime will **not** catch:

> an `undo` that is wrong, partial, or a no-op.

That is the paradigm's biggest honest concession. Backwards replay runs the
registered inverses and says so; whether they *restore state* is "the
application's own equivalence, not something the runtime observes or asserts."

`verified effect` builds the buildable middle between "trust the author" and "a
proof." It is not a proof. It is a **test the author did not write**: for a
marked effect, `revl test` auto-generates a property check —

> snapshot the observable in-process state → activate the component (the effect
> runs, its inverse accumulates) → tear the component down (the inverse runs,
> LIFO) → assert the state fingerprint returned to baseline — **N randomized
> rounds, on the real py runtime.**

A pass upgrades trust-the-author's-undo to *this-undo-survived-N-round-trips*.
The same honesty rule OpenAPI import applies to "safe-by-spec" applies here:
the marker is the author's claim; the round trip is that claim machine-checked,
only so far.

## 2. Writing one

`verified` is a modifier on an **activation-body** effect, exactly as
`verified fn` is a modifier on a function (syntax-2.0 §7). One token in one
position; nothing is added to the lexer (`verified` is already a keyword).

```revl fragment
component Seeder requires s: Store provides r: Ready {
  config { tag: Str }

  let hold = verified effect s.seed(config.tag) undo s.unseed(config.tag)

  provide r { fn ok() = "ok" }
}
```

Both effect forms take the modifier:

* `let h = verified effect <acquire> undo <inverse>`
* `verified effect <acquire> undo <inverse>` (anonymous)

`config` fields are the **randomized input surface** — every round activates
the component with fresh, type-directed values (positive `Int`s, printable
`Str`s, random `Bool`s), so the effect's inverse is exercised across inputs
rather than at one hand-picked point. A component with no `config` is still
round-tripped N times; there is simply no surface to vary.

`verified effect` is refused in a **provide-method** body. The round trip is
defined by a closed *activate → tear down* window, and the fiber runs an
activation effect's inverse on teardown; a method effect runs per request and
has no such window. The refusal is a compile error, not a silent no-op.

## 3. What the round trip actually measures

The fingerprint is the runtime's **own observable-mutation ledger** — the same
`set_trace` stream the lifecycle harness pairs for its R1 residue check
(`backends/python/emit.py::_revl_unreleased`). `src/revl/fault.py::_outstanding`
folds that ledger, purely, into the net outstanding in-process state:

| ledger pair | what it tracks |
| --- | --- |
| `<tag>.new` / `<tag>.drop` | a `Map` acquired but not released |
| `<tag>.open` / `<tag>.close` | a `Pool` opened but not closed |
| `<tag>.insert <key>` / `<tag>.remove <key>` | a key set but not cleared |
| `<tag>.acquire conn=<k>` / `<tag>.release conn=<k>` | a connection checked out but not returned |

The round trip **holds** when the fold after `activate; teardown` equals the
fold from before activation — i.e. the marked effect and its inverse net to
nothing observable. When it does not, the report names the exact residual *and*
the randomized config that produced it (the first failing round; no shrinking).

This is strictly stronger than the R1 residue check for the round-trip
property: R1 pairs only acquire/release verbs, so a `undo` that leaves an
*inserted key* standing passes R1 and the fault sweep but **fails** the round
trip. That is the wrong/partial-inverse class §4.1 calls out.

## 4. What it does not — and cannot — measure

Scope is honest and narrow. The fingerprint is derived from the trace ledger,
so anything the ledger does not carry is, by construction, invisible. The
generated report's header names these every run:

* **aliased references** the component handed out — state reachable through a
  reference a caller still holds is not in the ledger;
* **external effects** an emission crossed — an `emit db.execute(…)` is a
  one-way boundary crossing (§4.2/§4.3); the round trip is *in-process* state
  only and never claims to have un-issued it;
* **clock- or random-derived values** — a value read from a clock does not come
  back, and the ledger has no entry for it.

These are exactly the out-of-reach categories in `docs/replay.md` §4.1. A
passing round trip does **not** cover them, and the header says so — the same
discipline `step_back` uses when it refuses to grow a `restored: true` field.

## 5. Running it

`revl test` runs the round trips on the **py reference tier** — like `fault
test`, they activate components for real, so they need the cordis-py runtime
(`sh backends/python/setup.sh`) and a missing runtime is a *skip with a reason*,
never a pass. The other tiers print a note that the round trips did not run
there.

```
$ revl test examples/verified_effect.rvl
verified-effect inverse round-trips — py reference tier (roadmap item 26)
  This is a TEST THE AUTHOR DID NOT WRITE, not a proof. …
  IN SCOPE: in-process observable state only … OUT OF REACH … aliased
  references … external effects … clock- or random-derived values.
PASS Seeder: 16 round(s), inverse held for [step 1 (verified effect `hold`)]
round-tripped 1 verified effect(s): 1 held, 0 broke (16 randomized rounds each)
```

## 6. Relationship to `prop test` (roadmap item 37)

Item 37 is a general property-testing form, with item 26 as "its first
instance." Item 37 is not built. This is the **specific** inverse-round-trip
generator, built directly: the snapshot/randomize/activate/teardown/compare
loop in `src/revl/fault.py`. When 37 lands, that loop is the machinery it
generalizes — arbitrary property, arbitrary generators — and `verified effect`
becomes one *derived* property among many rather than a bespoke runner.

The round-trip summary (`roundtrip_dossier`) is shaped like the fault sweep's
dossier (`kind`/`status`/`roadmapItem`/`counts`/`components`) so it can fill the
gauntlet's pending `inverseRoundTrip` slot
(`src/revl/mcp/gauntlet.py`, `roadmapItem: 26`) the way item 30 filled
`faultSweep` — wiring it in is a later step; the shape is compatible now.
