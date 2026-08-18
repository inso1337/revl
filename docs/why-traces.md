# Why-traces — the derivation, not just the verdict

Most of revl's rejections are local: a type does not match, a name is not
declared, an `effect` has no `undo`. The message names the thing, the line
points at it, and the author is done.

Three are not local. They are the verdict of a *search* the compiler runs
over the whole composition:

| code | search | verdict |
| ---- | ------ | ------- |
| G4 | least fixed point over the call graph (`_emitting_fns`) | this method reaches an emission |
| G3 | cycle detection over the provider→consumer graph | these components cannot be ordered |
| G2 | the provider table | this key has two providers |

Before why-traces, the compiler ran the search, printed the conclusion, and
threw the derivation away. The author (or the agent) then had to re-run the
search by hand — grep for callers, sketch the graph, find the second
provider — to answer the only question that matters: *why?*

A **why-trace** keeps the evidence and attaches it to the diagnostic.

```
examples/rejections/g4_chain.rvl:18: `Cache.put` is declared plain, but this implementation reaches `write_through()`
  a service declaration bounds what its providers may do — mark it `emission fn put(...)` in service `Cache`, or move the irreversible call out of this method (G4)
  why `Cache.put` is emission:
    put -> write_through -> audit_log -> audit_write   (emission)
      put            g4_chain.rvl:18  provision `cache`
      write_through  g4_chain.rvl:7
      audit_log      g4_chain.rvl:3
      audit_write    g4_chain.rvl:1   emission
```

## What a trace is

Evidence is **data, never a pre-formatted string** — that is the whole
point. `src/revl/why.py` defines two frozen value objects:

```python
TraceStep(name, kind, file, line, detail)
WhyTrace(kind, subject, steps, shape)
```

* `name` — the thing this step is about: a function, a provide-method, a
  component, a `local.method` emission.
* `kind` — its role: `"provide-method"`, `"call"`, `"emission"`,
  `"component"`, `"provider"`.
* `file` / `line` — **best-effort**, both may be `None`. An ambient
  component read back from a running manifest has a file but no line;
  `compile_source` on a bare string has neither. Consumers must tolerate
  it; the renderer drops the location column when nothing is located.
* `detail` — a short *classification*, not prose: `"emission"`,
  ``"provides `db`"``, ``"provides `kv` in realm `tenant_a`"``. It says
  why an arrow was followed without re-deriving it.
* `capabilities` — what the hop reaches, when the analysis knows more than
  "an emission". Empty today; see *Composing with capability sets* below.
* `shape` — `"chain"` when consecutive steps compose (`a -> b -> c`),
  `"set"` when they are co-equal exhibits (two providers of one key).
* `kind` on the trace is an **open string**, not an enum, so a later
  analysis (capability sets on emissions, realm-aware conflicts) can add a
  trace kind without touching a single consumer.

Three consumers read it, and only they format anything:

| consumer | how |
| -------- | --- |
| human `error:` output | `why.render(trace)`, appended after the fix hint |
| `revl compile --json-diagnostics` | `WhyTrace.to_json()` under the diagnostic's `why` key |
| MCP (`revl_check`, `revl_admit`, `revl_swap`, …) | the same JSON, via `diagnostics.report` |

## The three traces

### G4 — emission propagation

A plain-declared provide-method is rejected because it *transitively*
reaches an irreversible host effect. The chain starts at the method and
ends at the emission:

```json
{
  "kind": "emission-propagation",
  "subject": "Cache.put",
  "shape": "chain",
  "path": ["put", "write_through", "audit_log", "audit_write"],
  "steps": [
    {"name": "put",           "kind": "provide-method", "file": "…", "line": 18,
     "detail": "provision `cache`"},
    {"name": "write_through", "kind": "call",           "file": "…", "line": 7},
    {"name": "audit_log",     "kind": "call",           "file": "…", "line": 3},
    {"name": "audit_write",   "kind": "emission",       "file": "…", "line": 1,
     "detail": "emission"}
  ]
}
```

When the emission is a service operation rather than an `emission` extern,
the terminal step is located at the **service declaration** — the line the
author must edit to make the declaration honest — and its `detail` names
it: ``emission `Database.execute` ``.

The chain is the *shortest* derivation, chosen deterministically: when a
function reaches an emission through more than one callee, the witness is
the callee with the fewest onward hops, ties broken alphabetically.

The message already names every culprit; the trace explains the **first**
one. One worked example is what an author needs, and it keeps the block a
fixed size no matter how many emissions a method reaches.

### G3 — dependency cycles

One step per component in the cycle, closing on the component it started
from. Each step names the key that carries its outgoing edge, so the
closing repeat shows exactly which provision shuts the loop:

```
  why `Alpha` is in a dependency cycle:
    Alpha -> Beta -> Gamma -> Alpha
      Alpha  cycle.rvl:5   provides `a`
      Beta   cycle.rvl:9   provides `b`
      Gamma  cycle.rvl:13  provides `c`
      Alpha  cycle.rvl:5
```

The degenerate case — a component that requires a key it provides itself —
gets a one-step trace with ``detail: "provides and requires `s`"``.

### G2 — provision conflicts

A `"set"`-shaped trace: two providers, two locations, no arrow.

```
  why `db` has more than one provider:
    PgDatabase      conflict.rvl:3  provides `db`
    SqliteDatabase  conflict.rvl:7  provides `db`
```

Realms are part of the classification, so a v2 realm conflict reads
``provides `kv` in realm `tenant_a` `` on both sides.

## Composing with capability sets

G4's emission analysis is growing: `emission` is becoming a *set of
capabilities* rather than a boolean. A chain is far more useful when it
says which capability each hop pulled in, so `TraceStep` already carries
one:

```python
TraceStep("audit_write", "emission", "a.rvl", 1, "emission",
          ["net:write", "fs:append"])
```

```
put -> write_through -> audit_write   (emission [net:write, fs:append])
  audit_write  a.rvl:1  emission [net:write, fs:append]
```

Rendering and the JSON projection (`"capabilities": [...]`, omitted when
empty) are already wired. There are exactly two producer seams, both of
which read a `capabilities` attribute off the declaration and yield `()`
today:

* `_EmissionEvidence.capabilities_of(name)` in `lower.py` — fns and
  `extern emission` declarations;
* `service_emission_step` in `_method_emissions` — `emission fn`
  operations on a service.

Populate the declarations and the traces start showing capabilities with
no edit to `why.py`, the renderer, `diagnostics.py`, or the MCP layer.

## What a trace is not

* **It never changes a verdict.** The set that decides G4 is the same set
  it always was; `_emitting_fns` takes an optional `witness` out-parameter
  and records the edge that put each derived name in, nothing more. Same
  for `_method_emissions` and its `steps_out`. Collecting evidence is
  observationally invisible to the checker.
* **It never changes a first line.** The trace is appended after the
  message and the fix hint. `examples/rejections/` and the substring
  assertions in `tests/test_frontend.py` are untouched.
* **It is not attached to every rejection.** A direct verdict —
  `` `missing` is not declared in this function `` — is its own
  explanation, and `error.why` is `None` there. The `why` key is simply
  absent from the JSON diagnostic.

## `revl explain <code>`

The trace explains *this* rejection. `revl explain` explains the *code*:

```console
$ revl explain g4
G4  every mutation carries an inverse, or admits irreversibility with `emit`
  fix: give the mutation an `undo`, or admit it is irreversible — `emit` at the call site and `emission fn` on the service operation

$ revl explain G2 --json
{
  "ok": true,
  "code": "G2",
  "guarantee": "provision disjointness: one provider per key (per realm)",
  "fix": "one provider per key per realm — withdraw one component, or `isolate` them into different realms"
}
```

Codes are case-insensitive; an unknown code answers with the roster rather
than nothing. The same table is served over MCP by `revl_grammar` as
`fixes`, alongside `guarantees` — so an agent that gets a code back can act
on it without a second round trip.

## Adding a trace to another rejection

1. Build `TraceStep`s where the search already has the answer — inside the
   loop that found the cycle, the table that found the conflict.
2. Pass `why=WhyTrace(...)` to `RevlError`.

That is all. Rendering, the JSON projection and the MCP hand-off are
already wired. Give the trace a new `kind` if it is a new shape of
argument; `why.py`'s `_HEADLINES` falls back gracefully if you do not.
