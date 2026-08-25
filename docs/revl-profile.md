# `revl profile`: capability/emission profiling + over-declaration detection

*Diff what a component's code **may** emit against what a recorded run **did**
emit, and name the difference — the declared authority a run never used.*

Implementation: `src/revl/profile.py` (the pure computation, the human render,
the loaders), `src/revl/__main__.py` (`revl profile`), `tests/test_profile.py`.
Roadmap item 124.

---

## 1. What it is

`revl audit` reports the **static** boundary surface: which emissions a
component *may* cross (docs/capabilities.md). `revl metrics` (item 122) counts
which emissions a run *did* cross, as a whole-run total. `revl profile` sits
between them and pairs the two **per component**: it reads the declared surface
from the composition and the exercised emissions from a `revl run --trace`
JSONL trace, and reports, for each component, the set difference —

> **over-declaration**: a declared emission the run never exercised.

That is least-privilege hygiene. A component whose code can emit through both
`bus` and `db`, but whose run only ever crossed `db`, holds authority on `bus`
that this run did not need. Narrowing the declaration to what was used is a real
attack-surface reduction — and because the surface is a checked artefact, the
compiler can re-prove the narrowed component still composes.

```
$ revl profile writer.rvl run.jsonl
emission profile over 1 component(s) with a declared emission surface

component Writer
  declared : bus.publish, db.execute
  used     : db.execute
  OVER-DECLARED (never used): bus.publish [capabilities: bus]

summary: 1 over-declared emission(s) across 1 component(s); 1 over-declared capability(ies)
```

## 2. Input

```
revl profile <composition> <run.jsonl> [--json] [--strict]
```

* `<composition>` — the declarations. Auto-detected exactly like `revl diff` /
  `revl metrics`: a `.rvl` source is compiled on the spot, and a compiled IR
  (`revl compile -o`) or an `audit --json` document is taken as-is. An audit
  document already carries the boundary walk under its `boundary` key and is
  used directly; a full IR carries `components`/`services` and is walked here.
* `<run.jsonl>` — a causal trace written by `revl run --trace` (docs/why-runtime.md).

The composition on the left, whose `Writer` over-declares `bus.publish`, is a
complete program:

```revl
service Bus {
  emission[bus] fn publish(topic: Str)
}
service Db {
  emission[db] fn execute(sql: Str)
}

component BusImpl provides bus: Bus {
  let cells = effect Map.new() undo cells.drop()
  provide bus {
    fn publish(topic) {
      effect cells.insert("last", topic)
      undo   cells.remove("last")
    }
  }
}
component DbImpl provides db: Db {
  let cells = effect Map.new() undo cells.drop()
  provide db {
    fn execute(sql) {
      effect cells.insert("last", sql)
      undo   cells.remove("last")
    }
  }
}

component Writer requires bus: Bus, db: Db {
  emit db.execute("insert")
  emit bus.publish("topic")
}
```

## 3. Where the two sides come from

**Declared (static).** The same G8 boundary walk `revl audit` runs
(`__main__._boundary`, which reads `lower._emitting_capabilities` and each
service method's `capabilities`), reused read-only. Per component it yields the
emission **labels** the code can cross (`key.method`) and, per label, the
capability **scopes** the called operation declares.

**Used (runtime).** The v2 `emit` event (docs/why-runtime.md) records one
crossing as `{event:"emit", component, capability, key}`. The `key` is the same
`key.method` label the static walk uses, so the two sides speak one vocabulary
and the set difference is apples-to-apples. No field the trace lacks is needed —
v2's `emit` carries everything — so nothing is fabricated: a trace with **no**
`emit` events used nothing, and every declared emission then reads as
over-declared, which is the honest answer for a run that crossed no boundary.

## 4. The two grains

Over-declaration is reported at two grains, each a plain declared-minus-used set:

* **keys** — the emission **label** (`bus.publish`): the finest unit, exactly
  what both the static walk and the trace name.
* **capabilities** — the **required key** the label goes through
  (`bus.publish` → `bus`): the least-privilege unit, the boundary you would
  revoke. It is the label's first segment on *both* sides (docs/capabilities.md:
  "calling `emission[db] fn put` through key `cache` contributes `cache`"), so
  it never disagrees with itself.

The grains differ when one required key carries several methods. If `Store`
declares `db.read` and `db.write` and the run uses only `db.read`, then
`db.write` is over-declared **at the key grain**, but the capability `db` *was*
exercised, so it is **not** over-declared at the capability grain — you cannot
revoke `db` without breaking `db.read`.

## 5. `--json`

`--json` prints the machine-readable document `compute_profile` returns
(mirroring `revl metrics --json` and `revl diff --json`):

```json
{
  "components": {
    "Writer": {
      "declared":     {"keys": ["bus.publish", "db.execute"], "capabilities": ["bus", "db"]},
      "used":         {"keys": ["db.execute"],                 "capabilities": ["db"]},
      "overDeclared": {"keys": ["bus.publish"],                "capabilities": ["bus"]},
      "underDeclared":{"keys": [],                             "capabilities": []},
      "scopes":   {"bus.publish": ["bus"], "db.execute": ["db"]},
      "services": {"bus": "Bus", "db": "Db"}
    }
  },
  "unknownComponents": [],
  "summary": {
    "components": 1, "overDeclaredComponents": 1,
    "overDeclaredKeys": 1, "overDeclaredCapabilities": 1,
    "underDeclaredKeys": 0, "clean": false
  }
}
```

`scopes` is the declared downstream scope per label (the audit `capabilities`
map); `services` is the required-key → service wiring, present from a full IR
and empty from an audit document. Both are descriptive context — neither enters
the set difference.

## 6. Anomalies: under-declaration and unknown emitters

The inverse difference, **used − declared**, should be empty: the checker forbids
emitting through a boundary a component did not declare. A non-empty
`underDeclared` set — or an emitter that appears in the trace with **no** declared
surface at all (`unknownComponents`) — is therefore almost always a *mismatched
pair*: the composition and the trace are not the same system. Either is surfaced
as a loud warning, never folded into the over-declaration count and never
silently dropped. `profile` does not guess which of the two is wrong; it reports
the disagreement.

## 7. Exit status

`revl profile` is descriptive, not a gate: it **exits 0** by default, like `revl
metrics`. `--strict` opts into a least-privilege gate — it exits nonzero when any
component over-declares an emission the run never exercised, so CI can fail a
component that has drifted wider than its behaviour. Under-declaration and
unknown-emitter anomalies always print a warning regardless of `--strict`.

## 8. Caveats

* The declared surface is the **static** emission surface — the emission call
  sites the code actually contains. A required service the component never emits
  through at all does not appear as an emission (it is an unused *dependency*,
  which `revl audit` / `revl diff` cover), so `profile` flags declared *emissions*
  a run skipped, not declared *requirements* a component never used.
* A profile is scoped to **one run**. An emission a given run never took (a
  conditional branch, an error path) reads as over-declared for that run; union
  several representative traces before narrowing a declaration on the evidence.
