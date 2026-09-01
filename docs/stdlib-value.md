# The stdlib Value module (roadmap item 180)

**The need:** a self-hosted revl emitter walks the backend-IR document
(`docs/backend-ir.md`) — a `kind`-discriminated tree of records, lists and
scalars whose static shape only the runtime knows. revl has no dynamic
`dict[str, Any]`, and the checker refuses stdlib methods on an unpinned `Any`
receiver (`node.args.length()` on an `Any` node → *"stdlib method `length` on a
value of unknown type … (G8)"*). So the first self-hosted py emitter
(`selfhost/emit_py.rvl`) had to bridge every access through a private set of
`@py` primitive accessors — `g`, `gs`, `alist`, `at`, `as_str`, `is_none`,
`child_nodes`, `ir_version` — and **every** IR-consuming Path B stage would
re-derive that same bridge. This module is that bridge, once, typed, and
reusable: the walk becomes PURE revl.

It is the named generalisation of the `Any` boundary `docs/stdlib-json.md`
already documents — `json_parse` hands back an `Any`; `stdlib/value.rvl` is how
you then navigate it.

## The type

`Value` is the **erased-dynamic type**: the runtime union of every
host-representable shape — record, list, scalar (`Str`/`Int`/`Float`/`Bool`),
or null. The checker (`src/revl/typecheck.py`) reserves `Value` as a builtin
type and makes it compatible with every type **in both directions** at
value-flow positions:

- a concrete `Str`/`Int`/`List`/record flows **into** a `Value` argument (it is
  boxed to the host's dynamic representation);
- a `Value` result flows **out** into any typed position (the total accessors
  are the checked exits).

This is what lets `value_str(value_field(node, "op"))` type-check without
pinning the whole IR as an ADT and without the G8 refusal. It is deliberately
narrower than adding `Value` to the wildcard set: only value-flow compatibility
is affected, so type-parameter unification and inference joins still treat
`Value` as the ordinary nominal it is (`Value` never silently quantifies a
generic, and a stray `[Value]` type parameter is refused as a builtin-shadow).

`Value` differs from `Any` in exactly the way the item asked for: `Any` is the
anonymous wildcard with no surface, while `Value` is a **named, reusable** type
whose accessor module *is* the documented boundary — so no emitter re-invents
`g`/`gs`/`alist` again.

## The surface

`use "stdlib/value.rvl" { … }` — the file lives in the repo as
`stdlib/value.rvl`, one `pub extern pure fn` per accessor. Every accessor is
**TOTAL**: a shape mismatch returns a typed default (`""`, `0`, `false`, `[]`, a
null `Value`) or an `Opt` `None` — it never crashes, so a walk over an
untrusted or partially-shaped document cannot fault.

| fn | signature | semantics (total) |
|---|---|---|
| `value_kind(v)` | `Value -> Str` | shape tag: `record`/`list`/`str`/`int`/`float`/`bool`/`null` |
| `value_is_null(v)` | `Value -> Bool` | is the value null |
| `value_field(v, name)` | `(Value, Str) -> Value` | record field; missing/non-record → null `Value` |
| `value_opt(v, name)` | `(Value, Str) -> Opt[Value]` | field as `Opt`; missing/null/non-record → `None` |
| `value_field_or(v, name, default)` | `(Value, Str, Value) -> Value` | field or the caller's `default` — the `dict.get(k, default)` total read (item 368); missing/null/non-record → `default`, never a raise |
| `value_has(v, name)` | `(Value, Str) -> Bool` | record carries a non-null key |
| `value_keys(v)` | `Value -> List[Str]` | record field names in insertion order (item 188); non-record → `[]` |
| `value_list(v)` | `Value -> List[Value]` | list elements; non-list → `[]` |
| `value_at(v, i)` | `(Value, Int) -> Value` | list element by index; OOB/non-list → null `Value` |
| `value_children(v)` | `Value -> List[Value]` | record values or list elements; scalar → `[]` (the walk driver) |
| `value_len(v)` | `Value -> Int` | length of list/record/str; scalar/null → `0` |
| `value_str(v)` | `Value -> Str` | as string; non-str → `""` |
| `value_int(v)` | `Value -> Int` | as int (bool excluded); non-int → `0` |
| `value_bool(v)` | `Value -> Bool` | as bool; non-bool → `false` |

```revl
// the two accessors this snippet needs come from stdlib/value.rvl via `use`;
// inlined here so the doc block is a complete, compilable program
pub extern pure fn value_field(v: Value, name: Str) -> Value = @py {
    return v.get(name) if isinstance(v, dict) else None
}
pub extern pure fn value_str(v: Value) -> Str = @py {
    return v if isinstance(v, str) else ""
}

// a kind-discriminated expression IR rendered back to source, in PURE revl
fn render(node: Value) -> Str {
  let k = value_str(value_field(node, "kind"))
  if (k == "bin") {
    let l = render(value_field(node, "left"))
    let r = render(value_field(node, "right"))
    return `(${l} ${value_str(value_field(node, "op"))} ${r})`
  }
  if (k == "lit") { return value_str(value_field(node, "text")) }
  return "?"
}
```

## Key enumeration — `value_keys` (item 188)

The value-side accessors above reach a record's **values** (`value_field`,
`value_children`, `value_at`) but never its **keys**. Yet the backend-IR records
`services` / `requires` / `provides` are keyed *by name*, and an emitter has to
walk those names — in order — to build the SERVICES table and the `inject` list.
The `emit_py` component slice (item 185) had to bridge that gap with a local
`@py` `record_keys(v) -> List[Str]` of its own; every future Path B emitter would
re-derive the same one. `value_keys` is that bridge, promoted into the module:

```revl fragment
// walk a keyed record in PURE revl — no @py of the caller's own
for (k of value_keys(comp)) {
  let svc = value_field(comp, k)   // value_keys + value_field = the entry
  // …
}
```

**Ordering contract.** `value_keys` returns exactly `list(d.keys())` on the
erased host dict — **insertion order**, the order the document was built or
parsed in. It does **not** sort; a caller that needs a canonical order sorts the
result itself (`selfhost/emit_py.rvl` already sorts where it wants determinism,
e.g. the checker's `sort_strs(tbl.keys())`). Python dicts preserve insertion
order, so this is stable and matches item 185's `record_keys` byte-for-byte.
**Total:** a non-record receiver (list, scalar, null) yields `[]`.

**Why no `value_entries`.** A `List[(Str, Value)]` entries accessor was
considered and **not** shipped: revl has no tuple/pair type in its surface
(`_BUILTIN_TYPE_NAMES` has no `Tuple`/`Pair`; the only pairs in the compiler are
Python-internal), so a `(Str, Value)` return has no clean revl type to name. The
proven pattern — and the exact shape item 185's bridge is used in — is to pair
`value_keys` with `value_field` for each key, which needs no new type. If a pair
type later lands, `value_entries` is a purely additive follow-up.

## Which `emit_py.rvl` `@py` accessors this obsoletes

The module replaces the IR-**navigation** half of `selfhost/emit_py.rvl`'s
private `@py` bridge:

| `emit_py.rvl` `@py` accessor | `stdlib/value.rvl` replacement |
|---|---|
| `g(node, key)` | `value_field(v, key)` |
| `gs(node, key)` | `value_str(value_field(v, key))` |
| `alist(v)` | `value_list(v)` |
| `at(v, i)` | `value_at(v, i)` |
| `as_str(v)` | `value_str(v)` |
| `is_none(v)` | `value_is_null(v)` |
| `child_nodes(v)` | `value_children(v)` |
| `record_keys(v)` (item 185's local bridge) | `value_keys(v)` |
| `ir_version(ir)` | `value_int(value_opt(ir, "ir_version") ?? 1)` (caller supplies the `1` default) |
| `is_py_float(v)` | `value_kind(v) == "float"` |

What it does **not** replace, correctly: `py_repr` (host `repr()` of a literal),
`newline` (a genuine `U+000A`), and `mangle` (Python keyword renaming) are host
*formatting* facilities, not IR navigation — they stay `@py` externs. (Those
would move behind their own stdlib surfaces — a `repr`/format module — not this
one.) `selfhost/*` is owned by item 171 and was not edited here; the mapping
above is the specification for that rewrite.

## Tier status

`Value` erases to each host's dynamic representation, exactly as
`stdlib/json.rvl`'s `Any` does. The **py** and **ts** tiers ship; rs/go/java are
the additive follow-up.

| tier | erases `Value` to | status |
|---|---|---|
| py | native object graph (`dict`/`list`/`bool`/`int`/`float`/`str`/`None`) | **executed by tests** (`tests/test_value_stdlib.py`) |
| ts | JS value (record → object, list → array, `Int` → `bigint`, `Float` → `number`, null/absent → `null`/`undefined`), `typeof` / `Array.isArray` discrimination | **executed by tests** (`tests/test_value_total_field.py`, run under node) — the total field read is identical to py (item 368) |
| rs | `cordis::Value` / `serde_json::Value` (as `json.rvl` already boxes) | **deferred** — add `@rs` bodies |
| go | `any` (type-switch discrimination) | **deferred** — add `@go` bodies |
| java | a provider's JSON tree (Jackson/Gson) | **deferred** — needs a provider on the classpath |
| wasm | — | **no representation**: the substrate value model (Int/Bool/Str/List) holds no Float/Map/record, same limit as `json.rvl` |

Adding a tier is purely additive: append its `@<backend>` body to each accessor
in `stdlib/value.rvl` (the emitter refuses an extern with no body for its tier,
the documented honesty gate). The accessor **contracts above are the spec** each
tier must meet — most importantly TOTALITY, so a mismatched shape returns the
same typed default on every tier.
