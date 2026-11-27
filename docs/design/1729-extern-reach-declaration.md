# 1729: An extern declaration states what its host body may reach

Roadmap: issue #2971, filed from revl-harness #706 (a gate reach issued from
inside a `@py`/`@ts` arm is invisible to every IR reach walk). Status: LANDED
with this note.

## The gap

An extern's verbatim host body is opaque to the compiler. `docs/threat-model.md`
§Non-goals 1 states the contract: the gate **surfaces** host code and does not
sandbox it. A `pure` arm can do anything a host block can do, so neither the
class nor the emitted text is a reach signal. A consumer that needs to know
which externs could reach a named capability therefore has no field to read,
must count every extern as a possible reach, and the unknown set saturates. A
fail-closed door over a saturated set refuses every legitimate holder, so the
fail-open answer is what ships.

## The surface

A `reaches [...]` clause, read after the return type on every extern class and
spelled exactly like item 519's `model role ... reaches [...]`:

```revl
extern emission[fs] fn write(path: Str, data: Str) -> Int
reaches [fs]
= @py { ... }

extern pure fn env(name: Str) -> Str
reaches [*]                       // declared unbounded
= @py { ... }

extern pure fn normalise(s: Str) -> Str   // omitted: UNDECLARED = unknown
= @py { ... }
```

Four states, and the last two are the point:

| written | IR | meaning |
| ------- | -- | ------- |
| `reaches [a, b]` | `reach.caps = ["a", "b"]` | declared subset |
| `reaches []` | `reach.caps = []` | declared to reach nothing |
| `reaches [*]` | `reach.caps = ["*"]` | declared unbounded |
| clause omitted | no `reach` key | UNDECLARED = unknown, never empty |

Tokens funnel through `_capability_params`, so a reach shares the one
capability namespace and canonical spelling with `emission[...]` and
`witnessed[...]` (`fs.write(path="/data/")` canonicalizes to
`fs.write(path="/data")`).

## The IR contract

`reach` is additive and is produced identically by the reference lowering
(`_lower_externs`) and the self-host producer (`selfhost/lower.rvl::ir_extern`);
the emit_py parity corpus pins the two producers. The item-373 confinement half
(`kind`/`target`) shares the field, because both answer "what may this crossing
reach". When both clauses are written the object carries both halves:

```json
"reach": {"caps": ["fs"], "basis": "declared"}
"reach": {"kind": "confined", "target": "cwd", "caps": ["fs"], "basis": "declared"}
```

`basis` is a tier, not decoration. **A declared reach may be narrowed on only
by a consumer that already trusts the declaring author's host body.** `basis:
"declared"` says the tokens are the author's claim; nothing in the compiler
verified that the body honours them. `basis` is reserved for a future
`"verified"` value from #2708 (the pinned and sandboxed kernel): once the body's
authority is enforced, the same field becomes a checked upper bound and a
consumer that does not trust the author may narrow on it. Absence is unknown in
BOTH tiers: no key means no claim was written, so no consumer may read it as an
empty reach.

## The one check

The compiler checks the fact it holds: a `reaches` that omits the extern's own
`emission[X]`/`witnessed[X]` scope is refused, because that claim would narrow
against the declaration's own crossing. A scope token may be covered by a wider
reach token (`fs.write` covers `fs.write(path="/tmp")`) or by `[*]`. An extern
with no scope (`pure`, `acquire`, `local`) expresses no compiler-known
boundary, so its claim is recorded and reviewed, never checked; that is exactly
what `basis: "declared"` states.

## What it does not do

It does not make the claim true. A host body that reaches further than its
`reaches` clause says is still a lie that review must catch, exactly as the
`pure` classification is a claim today (issue #2708). Detecting that lie is
#2708's enforcement. This note adds the field that enforcement will need and
that a trusting consumer can read now; no amount of walking the IR supplies it.
