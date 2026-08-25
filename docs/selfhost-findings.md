# Self-hosting, stage two: what building a parser in revl actually cost

The self-hosted lexer (syntax-2.0 §11) proved the pure stratum runs. This
is the second stage — `selfhost/parser.rvl`, the pure-expression grammar
(§3.2) — and its purpose is **not** self-hosting. A self-hosted compiler
is a bootstrap liability that taxes the iteration speed which is currently
this project's best asset. The purpose is a **differential oracle**: two
independent implementations of one grammar, forced to agree on every
input. Neither is the spec, so a disagreement is always a real defect in
one of them.

Scope is deliberately the expression layer, because that is where
precedence, associativity and lookahead bugs live. Declarations are
linear and comparatively boring.

**Stop here.** The checker is where a `Map` becomes load-bearing and where
the work stops being grammar and starts being semantics — a different and
much more expensive kind of problem.

## The headline: the parser needed *less* than a Map

The question this exercise was set to answer was "if it needs more than a
`Map` to get there, that's your answer." It did not need one at all.
Token access is indexing into `List[Token]`; an AST is a tree, not a
symbol table. `Map` becomes unavoidable at the *checker* (scopes,
environments), and that is precisely the boundary this stage stops at.

## What carried the weight

Each of these was probed against the compiler before a line was written,
and each works — compiled *and* executed on the python backend:

- **Recursive ADTs.** `type Expr = … | Bin(BinN)` where `BinN` holds
  `Expr` fields, including forward references between the two
  declarations. This is the load-bearing capability; an AST is exactly
  this shape and nothing else would have substituted.
- **`List[T]` of ADT values inside record payloads** — call arguments,
  list literals, match arms.
- **Mutually recursive functions** across the whole precedence ladder.
- **Modules**: `pub type` plus `use "./lexer.rvl" { Token, lex_src }`.
- `if`/`else`, `while`, `var`, early `return` from inside a loop.
- The existing stdlib surface, unextended.

## Friction, ranked by cost per unit

1. **Bindings are function-scoped, not block-scoped.** Two sibling `if`
   branches cannot each write `let r = …`. In the TypeScript subset revl
   advertises, `let` *is* block-scoped, so this is legal TS that revl
   rejects. It rejects loudly rather than silently diverging, so §0 holds
   — but a parser is a stack of branches doing identically-shaped work,
   which concentrates the tax exactly where it hurts. Four renames were
   needed in `p_primary` and `p_unary` alone.
2. **A `var` may not appear in a record literal** (the non-escaping rule,
   §3.5). Correct, and well-diagnosed — but a parser threads a cursor
   through record constructors constantly, so it forced about ten `let`
   copies that exist only to hand a value to a constructor.
3. **An ADT payload is exactly one *named* type.** Neither
   `Add(Expr, Expr)` nor `Add({ left: Expr, right: Expr })` parses; the
   record must be declared first. Sixteen of the auxiliary type
   declarations in `parser.rvl` exist for no other reason.
4. **A bare `match` is not a function body.** `fn f(e: E) -> Int { match e
   { … } }` is rejected ("body never returns a value"); `return match …`
   is required, and the `fn f() = expr` short form is not available at
   top level (only in provide-methods). The diagnostic is good and the
   fix is obvious, but the ML-family idiom is the one models will reach
   for first.
5. **No `Map` value type.** Known, documented as Planned, and not needed
   here — see above.
6. **`push` is persistent.** Right for a parser, which only appends. It
   will be the performance question for a symbol table, not a correctness
   one.

None of these blocked the work. All of 1–4 are ergonomics, and 1 and 3
are the two a model would trip on.

## What the oracle found

Three real defects, all in `selfhost/lexer.rvl`, all invisible to the
existing corpus test — which is the point worth internalizing: **a
differential oracle only covers what its corpus exercises.**

1. **`hole` was missing from the self-hosted keyword set.** The reference
   had it; the revl lexer did not, so `hole[Int] "todo"` lexed as an
   identifier. No corpus file uses `hole`, so the two lexers agreed on
   every input they were ever asked about. Fixed, and the *class* closed:
   the keyword sets are now compared as sets.
2. **`${…}` accepted only a bare identifier.** The reference captures the
   body as raw brace-balanced source for the parser to re-parse, so
   `${a + b}`, `${r.count}` and `${ {a: 1} }` are ordinary revl that the
   self-hosted lexer rejected outright with an `error` token. Fixed, with
   the missing forms added as a case list.
3. **The template part encoding was ambiguous.** Parts are flattened into
   one `Str` joined by `"|"` — so `${a || b}`, or a template nested
   inside an interpolation, put the separator into a payload and made the
   parts unrecoverable. Found by the fuzz at roughly one occurrence in
   400 random expressions; invisible to every hand-written case. Payloads
   are now escaped (`%%` for `%`, `%p` for `|`) with a left-to-right
   decoder, mirrored in the lexer, the parser and the test's reference
   canonicalizer.

The oracle also caught two bugs in the new parser itself within minutes
of first running — a match-arm loop that tested for its separator *after*
consuming it, and an optional-call branch that fell through into the
optional-field branch. Both were the same shape (a missing `else`), both
would have been invisible to a single implementation, and both were found
by comparison rather than by inspection.

## How it is checked

`tests/test_selfhost_parser.py` renders the reference AST to the same
canonical S-expression the revl parser produces and compares:

- ~100 curated accepted forms and ~27 rejected ones;
- a seeded generator over the whole grammar, 12 seeds × 60 expressions in
  CI, run at 40,000 during development with zero disagreements after the
  encoding fix.

Errors are values on the revl side (the pure stratum has no exceptions),
so the reference *raising* and the parser returning `Bad` is the
agreement checked on rejection. Messages are not compared — accept/reject
and shape are.

## Known limit

`p_type` implements the common type forms (named, generic application,
`?` sugar, the parenthesised group and function form). Anything outside
that is a parse failure rather than a wrong tree, so a divergence would
surface as a rejection disagreement, not a silent one.

---

# Path B, the wasm emitter (item 200, slice 1): what a byte oracle for WAT costs

`selfhost/emit_wasm.rvl` mirrors `backends/wasm/emit.py` byte-for-byte over the
same interchange IR — the wasm instance of the Path B emit oracle. wasm is the
hardest tier and the slice is deliberately the *smallest byte-reproducible
corner*: a function-only v3 document over the SCALAR value ABI (Int = i64,
Int32/Bool = i32) with arithmetic, comparisons, `&&`/`||`, unary `!`/`-`,
let/var/assign, if/else, while, bare-expr `(drop)` and assert. That corner is
the one where `heap_start` stays 0, no `data` segment is emitted, and none of
the demand-driven helpers (`$f64_to_str`/`$str_index_of`/`$str_split`/
`$str_join`) are pulled in — so the output is a pure function of the IR.

## The blocker that shapes the slice: everything past a scalar is linear memory

Unlike py/ts/rust, a wasm function does not get *incrementally* harder as you add
a type — it falls off a cliff. The moment a value is a `Str`, `List`, record or
tagged cell, the reference pools string literals into `data` (moving
`heap_start`), threads `$alloc`/`_str_ptr`/`_slot_load`/`_slot_store`, and hands
out a *nesting-depth-indexed scratch pointer* (`_acquire_tmp`). That is a whole
allocation-shaped surface, not a new leaf case, so it is one clean cut: the
scalar corner is in, anything touching memory is a follow-on slice. `.to_int()`
widening looks scalar but is a `builtin` node (not a bare `widen` marker), so it
lands on the builtin surface and is out too. A byte oracle is still the right
check here — the covered corner is fully deterministic — but only for that
corner; the memory surface will want the same treatment slice by slice.

## The ~430-line constant preamble is a second implementation, embedded verbatim

Every function-bearing module emits the full `_helper_funcs()` preamble (checked
`$int_add`/`$int32_*`, the str/list runtime), whether or not a body uses it. To
stay byte-identical the port embeds that block as one `"""…"""` verbatim literal
— a fixed second implementation of the exact bytes, kin to `emit_rust.rvl`'s
`_module_header`. It carries no `"` or `\`, so the triple-quoted form reproduces
it exactly.

## New friction (wasm-specific): `$ident` in a plain string is a hard error

WAT is written with `$`-sigil identifiers *everywhere* — `$p_a`, `$l_x`,
`call $int_add`, `(global $__hp …)`, `(func $name (export "name") …)`. Every
such plain-string literal trips the lexer's dead-1.x interpolation guard
(`_lex_string`, `src/revl/lexer.py`): `"call $int_add"` hard-errors with
```
`$int_add` in a plain string — this was interpolation in 1.x and would
silently change meaning
```
This is DISTINCT from item 183 (the `\"`/`\\` escape-table gap) — it is the
`$[A-Za-z_]…` / `$$` staleness check, not an escape. Repro: any `.rvl` line
`return "call $int_add"`. Workaround used throughout the emitter: author every
WAT fragment as a backtick template (a bare `$` not followed by `{` is literal
there) or a `"""triple"""`. It bites wasm far harder than any prior tier because
py/ts/rust/go/java identifiers carry no sigil, so this is the first emitter where
the *natural* string form is unusable for nearly every output line. Not a bug —
a documented papercut worth one line in the self-host emitter guide ("WAT
literals must be backtick or triple-quoted"). LOW. (Not fixed here.)

## stdlib-kit validation (positive)

The kit held with zero new bridges. `list_sort(list_dedup(names))`
(items 189/193/194) reproduced Python `sorted(set(local_names))` byte-for-byte
for the header local ordering, and `stdlib/value.rvl`'s accessors navigated the
interchange IR with only the two genuine host-formatting primitives kept `@py`
(`num_str` = `str(v)` for the numeral, `newline` = `chr(10)`) — the item-180
"NOT obsoleted" category, nothing more.

## How it is checked

`tests/test_selfhost_emit_wasm.py`: compile `selfhost/emit_wasm.rvl` through the
py backend, exec it, run `emit_src` on the interchange-IR corpus
(`tests/fixtures/emit_wasm_corpus/`), and assert the WAT equals
`backends/wasm/emit.py`'s `emit(ir)["functions"]` to the last byte, over
`arith.rvl` (checked int/int32 `+ - *`, `%`, i64/i32 comparisons, `&&`/`||`,
`!`, unary `-`) and `control.rvl` (if/else, while, let/var/assign, bare-expr
drop, assert, the trailing-`unreachable` divergence rule).

# Path B, the py emitter (item 206, slice 4): externs, config, method-body effects

Slice 4 closes the three forms slice 3 flagged: `_emit_externs`, component
`config`/`ConfigSchema`, and method-body `effect`/`emit … compensate` (the
`_revl_frame.adopt` accumulator + the `_label`/`_effect_N`/`_emit_N` counter).
All three land byte-identical to `backends/python/emit.py`. Three notes below are
friction worth acting on (none fixed here); two are positive validations.

## stdlib::dedent validation (positive) — item 193 held in real use

`_emit_externs` is `textwrap.dedent(bodies["py"].strip("\n")).splitlines()`.
`stdlib/str.rvl::dedent` (item 193) reproduced `textwrap.dedent` byte-for-byte on
a real multi-line `@py` body — the nested-indent margin, and the
whitespace-only-line normalization that turns a trailing `"…\n    "` into a
trailing `"\n"`. That trailing `\n` is exactly where the port had to be careful
(next note). The dedent step needed NO `@py` of its own: the whole externs form
is pure revl over str.rvl + two local `Str`-surface helpers. Item 193 delivered
precisely what it promised.

## `str.splitlines()` is NOT `split("\n")` — the one subtle host-semantics gap

Symptom: `textwrap.dedent(body).splitlines()` and `body.split("\n")` agree on
every body EXCEPT one that ends in a newline — which dedent PRODUCES whenever the
`@py` body's last line was whitespace-only (normalized to `""`, so the join
re-emits a trailing `\n`). `"a\n".splitlines()` is `["a"]`; `"a\n".split("\n")`
is `["a", ""]`. A naive `split("\n")` therefore emits a spurious blank line
inside the `def`. Repro: any extern whose surface `@py { … }` block closes with
the brace on its own indented line (the common case). Fix used: a local
`splitlines` that splits on `\n` then drops the final empty segment iff the text
ends in `\n`. Line boundaries other than `\n` (`\r`/`\v`/`\f`/`\x1c…`) are out of
scope — real py extern bodies are `\n`-separated — and are noted as such. Worth
one line in the self-host emitter guide: "splitlines ≠ split on a trailing
newline." LOW, informational (the port handles it).

## Friction (item 189 ergonomics): a `use` links an imported module's PRIVATES,
## so the importer may not define ANY name that module defines

Symptom: adding `use "../stdlib/str.rvl" { dedent }` to pull ONE function made
the compile fail with `duplicate function is_word_ch` — a name emit_py.rvl
declared locally and `dedent` never references. Cause (compiler.py, the
pure-declaration closure): an imported module's ENTIRE top-level fn set — its
module-PRIVATE helpers included — is emitted into the linked program so its own
fns can call them, and `_lower_fns` then rejects any duplicate NAME across the
link. So importing `dedent` silently drags in str.rvl's private `is_word_ch` /
`is_alpha_us` / `is_sp_tab` / `nl` / `line_is_ws_only` / …, and the importer must
not itself declare `trim`/`lstrip`/`last_index_of`/`is_alpha_us`/`is_word_ch`/
`ident_tokens` — all of which slices 1-3 hand-rolled. Repro: any module that both
`use`s str.rvl and declares a fn whose name str.rvl declares privately.

This is not a bug — the private helpers genuinely must ride into the link — but
the ERROR is misdirected: it blames the importer's own line for a "duplicate"
whose other definition is an invisible private of the imported module, with no
hint that the collision came through a `use`. Two things would help: (a) name the
importing `use` and the source module in the duplicate-function error when one
side is an imported private; (b) document in the module-authoring guide that a
public module's PRIVATE fn names are effectively reserved against every importer.
The silver lining: it FORCED the item-193 migration the str.rvl header calls for
— emit_py.rvl now `use`s `trim`/`lstrip`/`last_index_of`/`ident_tokens` from
str.rvl and deleted its six local copies, and slices 1-3 stayed byte-identical
(the refactor's own proof). So the kit did its job; the diagnostic is the gap.
MEDIUM (diagnostic clarity).

## in-file `test` blocks could not call the file's own externs — RESOLVED by item 182

Symptom (hit while writing the slice-4 unit tests): a `test` block calling
`newline()` or `upper()` (both `extern pure fn` declared in the same file, freely
called from the file's `fn` bodies) failed with `` `newline` is not declared in
this function `` — `_lower_tests` (lower.py) built its callable set as
`_HOST_CALLABLES | _BUILTIN_CONSTRUCTORS | {fn names}`, omitting `program.externs`
so a file's own externs were invisible to its `test` blocks. This independently
re-surfaced the exact gap **item 182** closes ("externs are in scope inside test
blocks, not only fn bodies", landed on `origin/main` in parallel and merged into
this branch) — the dogfood signal and the fix agree. The slice-4 tests keep the
backtick-newline form (tier-portable, as str.rvl's `nl` does) rather than depend
on the just-landed fix; either spelling now works. Closed; kept as a note that
two independent efforts converged on the same diagnostic.

# Path B, the rust emitter (item 205, slice 2): the typed-core, and why components stayed out

Slice 1 (item 191) ported the FUNCTION-ONLY corner of `backends/rust/emit.py`
byte-for-byte. Slice 2 extends it to the **v3 typed-core** — user `type` decls
(record → `pub struct`, variant → serde-tagged `pub enum`), record literals with
the by-value field clone, field access, ADT construction (`Enum::Case` /
`Enum::Case(arg)`), and `match` over user variants — all still byte-identical
against the reference (`tests/fixtures/emit_rust_corpus/{records,variants}.rvl`).

## The headline: (a) components/services could NOT be a byte-exact target, and it is structural

The slice was scoped to *both* the typed-core AND components/services. The
typed-core landed clean; components/services did not, and the reason is not
effort — it is that **the component surface is entangled with the explicitly
deferred Value/serde erasure surface**, so no service/component fixture can be
byte-exact without also porting the bridge this slice was told to defer:

  * `_emit_components` unconditionally runs `_emit_service_traits`,
    `_emit_host_stubs`, AND `_emit_bridge` — even for a document with zero
    components.
  * `_emit_bridge` emits its full interop-RPC proxy the moment `ir["services"]`
    is non-empty (`if not services: return []` is the only escape). So a lone
    `service` declaration — the smallest possible components-dialect fixture —
    already drags in the ~200-line bridge (`_revl_rpc`, per-service consumer
    proxies, serde erasure) that the slice brief lists under "Value/serde
    ERASURE surface … DEFER".
  * A `component` additionally fires the host stubs and the whole
    `_emit_component_*` machinery (effect/undo accumulators, config structs,
    timer preamble, router structs, provide impls).

There is therefore no byte-exact *sub*-slice of components to take here: the
first honest cut is "service traits + bridge + host stubs" as one unit, which is
squarely the deferred erasure work. Recommend the next rust slice pair the two
(traits are trivial; the bridge is the real content). Reported, not worked
around.

## `record_update` is refused by the reference — a structural exclusion, not an un-port

`{r | f = e}` (functional record update) raises `EmitError` in
`backends/rust/emit.py` itself ("not emitted by the rust backend yet … lift it
into a helper fn"). So it is not a byte target on this tier at all — excluded
structurally, like `let_pattern`'s output-buffer-indexed temp name.

## The Ctx-threading tax recurs (kin to item 195's `Sout` tax), but the mitigation is in-hand

Extending `_V3Ctx` from 2 fields (`vt`, `fr`) to 5 (`+ ca`, `cp`, `rbf`) meant
every `Ctx` record LITERAL had to be rewritten by hand — `set_vt`, the per-fn ctx
in `emit_v3_functions`, and the in-file tests — because revl has no shared
mutable state and the reference just mutates one dict in place. This is the same
threading tax item 195 logged for `Sout`. The clean fix is functional
record-update (`{ ctx | vt: ctx.vt.set(k, v) }`), which would leave the other
four fields implicit — and it IS available here, because the self-host emitter is
compiled by the PYTHON backend, which emits `record_update` fine. This slice kept
explicit literals for parity with slice-1 style, but the ergonomic lesson stands:
a growing threaded-context record is exactly the shape record-update sugar exists
for. LOW.

## stdlib-kit validation (positive)

The typed-core needed ZERO new `@py` bridges beyond the four host-formatting
primitives slice 1 already keeps (`string_lit`, `num_str`, `newline`, `mangle`).
`value_keys` (item 188) navigated both the `types` dict and each record's
`fields` dict in pure revl. The one place slice 2 reaches for `sorted(...)` — the
`record_by_fields` key, `tuple(sorted(field_names))` in the reference — is served
byte-for-byte by `list_sort` from the list kit (item 194), which is itself
fuzz-pinned against Python `sorted()`; an initial hand-rolled selection sort +
private `str_lt` was deleted in favour of `use "../stdlib/list.rvl" { list_sort,
str_lt }`. The kit did its job: no re-hand-roll survived.

## How it is checked

`tests/test_selfhost_emit_rust.py`: compile `selfhost/emit_rust.rvl` through the
py backend, exec it, run `emit_src` on the interchange-IR corpus, and assert the
Rust equals `backends/rust/emit.py`'s `emit(ir)` to the last byte — now over the
6 slice-1 fixtures PLUS `records.rvl` (struct/field/List[Point]/clone) and
`variants.rvl` (enum/nullary+payload ctor/match with bind, nullary, `_` wildcard
vs `unreachable!()`, built-in Some/Ok coexisting). Excluded, by construction:
every service/component fixture (would fire the deferred bridge), `record_update`
(reference raises), float interpolation, stdlib builtins, and `let_pattern`.

## emit_ts.rvl slice 2 (item 204): components/services

### stdlib-kit validation (positive) — `value_keys` closes the item-185 gap
The component tail keys three IR sections BY NAME (`services` = name->service,
a component's `requires` = local->service, `provides` = key->service) and the
service-interface loop, the `inject` array, and `_context_augmentation` all
iterate those keys. `stdlib/value.rvl`'s `value_keys` (item 180) navigated every
one in PURE revl with ZERO new bridges — the exact private `record_keys` `@py`
that emit_py.rvl (item 185) had to hand-roll before `value_keys` existed. This
is the positive datapoint item 189 wants: the kit is now adequate for the
name-keyed IR sections; a Path B component emitter needs no private key bridge.

### Near-miss: hand-rolled a `contains_str` the kit already had (`list_contains`)
I wrote a 5-line `contains_str(xs, target)` for the first-occurrence dedup in
`_context_augmentation` before noticing `stdlib/list.rvl::list_contains` (items
189/193/194) does exactly it. Symptom: the kit's membership predicate is easy to
miss because the emitter already imports only `value.rvl`, so nothing prompts a
second `use`. Fix applied: deleted the local, imported `list_contains`. Informs
item 189 — the kit is complete enough that the friction is DISCOVERY, not a gap;
a one-line "reach for stdlib/list.rvl before hand-rolling List predicates" in the
self-host emitter guide would have saved the near-miss. LOW.

### Friction: no mutable emitter state -> manual counter threading tax (LOW)
The reference (`backends/typescript/emit.py`) carries the document-wide match-temp
counter as `ctx._counter` (a mutable one-cell list) and mutates it in place, so a
body renderer returns only `list[str]`. revl has no method/closure state, so the
counter must be THREADED: every body renderer here (`method_body`,
`provide_impl`, `component_step`, `component_lines`) returns `{lines, counter}`
and every call site rebinds `c = r.counter`. Repro: any recursive line-producing
helper that may render a `match` — the counter has to ride the return value.
Not a bug (it is the pure-functional cost the self-host pays for the reference's
`ctx._counter`), but it is the single biggest source of mechanical plumbing in
the component tail and the easiest place to drop a `c = r.counter` and desync
silently. Worth a note in the emitter guide: "a body renderer returns StmtOut
(lines + counter); thread the counter through every child call." (Not fixed —
it is inherent; flagged for the guide only.)

### ts-component-specific (positive): one `_expr`, no dialect fork needed
The reference's single-renderer discipline (`_expr` covers both the 2.0 and the
component dialects, dispatching the `call` kind on SHAPE not kind) ported cleanly:
adding `req`/`config`/`name` as three scalar branches in `expr_inner` plus a
`value_has(node, "target")` check in `render_call` was the WHOLE component-expr
surface. The method-body plain-`bin` case (`a + b` -> `(a + b)`, NO `revlI64`,
vs a top-level fn's `revlI64(n + n)`) needed no special-casing — it falls out of
the IR omitting `operands` on component-body arithmetic, and the existing
`render_bin` already keys on `operands`. `value_has` (value.rvl) was the one new
accessor pulled in, and it read naturally.

### How it is checked (slice 2)
`tests/fixtures/emit_ts_corpus/`: `services_methods` (provide methods, params,
ternary, builtin, context aug), `services_body` (bound let-effect, if/fail,
emit/compensate, multi-require inject), `services_config` (config interface +
`applyConfigDefaults`), `services_method_block` (block provide method: let/return,
req-as-ctx), `components_mixed` (a pure fn beside a provider — independent match
counters, i64-helper gating). Each `emit_src(ir) == backends/typescript/emit.py
emit(ir)` byte-for-byte. Deferred features (async, timer/await, spawn, realms,
v1/v2, composite signature types) are EXCLUDED from the corpus, not approximated.

## emit_ts.rvl slice 3 (item 208): composite signatures + host/format/fn/adt
Extended the TS self-host to the COMPOSITE service/provide-method signature types
(List/Opt/Map/Result/fn-type + the declared-record `known_types` path in
`ts_type_v1`), the RECORD half of `_emit_ts_types`, and the four remaining
component-dialect expr kinds `host`/`format`/`fn`/`adt`. Async coloring, the
variant half of `_emit_ts_types`, spawn/instances, realms, the v1/v2 path, and
the component-body `await`/`timer` steps stay deferred and EXCLUDED from the
corpus (a divergence if fed one, not a bug).

### Reference asymmetry: the v1 fn-type return uses `.lstrip()` where v3 assumes `)->`
The single non-trivial defect surfaced porting the fn-type branch of the v1
`_ts_type`. The reference v3 renderer (`_ts_v3_type`) and the reference v1
renderer (`_ts_type`) both recover a function type's return, but by DIFFERENT
means: v3 was mirrored in `ts_type` here as `slice(arrow + 3, n)` (assumes the
IR spells the arrow `)->` with no space), whereas the reference v1 path goes
through `_split_fn_type`, which does `name[i+1:].lstrip()` then `rest[2:].strip()`
— tolerant of the space the IR ACTUALLY carries (`(Int, Str) -> Bool`). Symptom:
the first `services_composite` run rendered `((a0: bigint, a1: string) => unknown)`
(the return parsed as `> Bool` -> `unknown`) against the reference's `=> boolean`.
Repro: any service op with a function-type parameter (`fn e(f: (Int, Str) -> Bool)`).
Fix applied HERE (in `ts_type_v1`): recover the return with the `_split_fn_type`
spelling (`py_strip(slice(arrow+1))` then drop the `->`), not the v3 `arrow+3`.
NOTE for a future maintainer: the v3 `ts_type` fn-type branch (line ~245) still
uses `slice(arrow + 3, n)` and is UNEXERCISED by any v3 fixture — if a v3 fn body
ever annotates a spaced function type, it will mis-render the same way. Not fixed
(out of this slice's file-touch scope for the v3 path's own corpus; flagged).

### Counter-threading tax recurs — now compounded by a `known_types`-threading tax (LOW)
Item 195's `Sout`/counter tax (also noted in slice 2 above) recurred verbatim, and
slice 3 added a second parameter that must be plumbed through the SAME call chain
for the SAME structural reason: `known_types` (the document's declared type names)
is read once at the top of `emit_component_tail` and threaded down through
`service_interfaces`, `component_lines`, `component_step` (incl. its own `if`
recursion), and `provide_impl` purely to reach `ts_type_v1` at the leaves. The
reference carries it as `ctx.types` on the single mutable `_Ctx`, so no reference
call site names it; the pure-functional port has to widen five signatures. Repro:
adding any new document-scoped read (types, async_ops, function_names) to a leaf
renderer forces a full-chain signature edit. Not a bug — the inherent cost of
`_Ctx` being a bag of document context that a pure port must destructure — but it
is the same friction item 195 flagged, now with a concrete second instance. A
`_Ctx`-shaped record threaded ONCE (one `type DocCtx = {known_types, counter, …}`
argument instead of N loose ones) would collapse both taxes; worth considering for
the emitter-guide pattern. Flagged only; not fixed.

### Kit ergonomics (item 189): the format scanner wanted a char-class the kit hides
`render_format` re-implements the reference's `re.finditer(r"\$\$|\$(\d+)")` scan
by hand (revl has no host regex in the emitter). That needed `is_digit` and a
digit-run integer parse — both trivially writable, but `stdlib/str.rvl` ALREADY
defines `is_word_ch`/`is_alpha_us` (the `\w`/`[A-Za-z_]` classes) and they are
NOT `pub`, so a self-host emitter cannot import them and re-derives its own
`is_word_ch`/`is_digit`. Symptom: two tiny char-class helpers duplicated between
`stdlib/str.rvl` (private) and this file. Not a blocker (the copies are 4 lines),
but it is the same DISCOVERY-vs-GAP friction the slice-2 `list_contains` near-miss
named: the classes exist, they are just not reachable. Informs item 189 — either
`pub`-export the `str.rvl` character classes or document that emitters roll their
own. Not fixed (would edit `stdlib/str.rvl`, out of this slice's scope).

### Positive: the single-`_expr` dispatch absorbed all four new kinds unchanged
As in slice 2, the reference's one-renderer discipline ported with no dialect
fork: `fn`/`adt` (shared kinds) and `host`/`format` (component-only) were four
independent `if (kind == …)` branches in `expr_inner` reusing the existing
`commajoin`/`expr` threading, plus one `render_format` helper. No existing branch
changed. `adt` reuses `json_dumps` for the case tag exactly as the reference
reuses `_string`; `host` renders its dotted `fn` VERBATIM (not through `ident`),
matching the reference's IDENT_RE-validated-upstream contract. Record-type
emission (`emit_ts_types`) was 12 lines reusing `value_keys`/`ts_type` — the same
zero-new-bridge story the kit keeps delivering for name-keyed IR sections.

### How it is checked (slice 3)
`tests/fixtures/emit_ts_corpus/`: `services_composite` (List/Opt/Map/Result/
fn-type interface signatures + the declared-record `List[Msg]` -> `Msg[]` path,
with `interface Msg` emitted), `services_composite_provide` (composite
provide-method params `Row[]`/`Map<…>` via the same `ts_type_v1`), and
`component_exprs` (`host.Job.run`, a `` `…${config.count}` `` format literal, a
`tag(...)` top-level `fn` call, and an `Ok(x)` -> `{ kind: "Ok", value: x }` adt).
Each `emit_src(ir) == backends/typescript/emit.py emit(ir)` byte-for-byte; the
deferred features are excluded from the corpus, not approximated.

## Go emitter — Path B slice 2 (v3 typed-core, item 209)

`selfhost/emit_go.rvl` now mirrors the go PURE typed-core byte-for-byte: user
`type` decls (`_emit_v3_go_types` — record -> Go `struct` with unexported
source-spelled fields, no json tags on the pure tier; variant -> sealed interface
+ per-case struct + seal method), record literals + field access, ADT
construction (nullary `<Variant><Case>{}` / payload `{Value: arg}`), `match` over
user variants as a Go type-switch IIFE, and user type names in `go_type`. New
fixtures `records.rvl` / `variants.rvl` cross-check `emit_src(ir) ==
backends/go/emit.py emit(ir)` to the last byte; slice-1 fixtures stay green.
Deferred (excluded, not approximated): functional record-update (the go reference
RAISES on it — python/ts only), the built-in Opt/Result/Map surface and its
preambles, stdlib builtins, and the live-component world.

### Friction: 8-field threaded Ctx, no record-update to spread it (LOW)
The lowering ctx grew from 4 fields to 8 (`vt fr er ca cp rbf rf rt`) to carry the
user-type tables. Because the self-host emitters avoid functional record-update
by convention (the same `{r | f = e}` the go tier itself defers), `set_vt` has to
respell ALL EIGHT fields to update ONE (`vt`), and every ctx-construction site
(`emit_functions`, the in-file `typed_ctx` test helper, the per-arm `set_vt` in
`render_match`) does the same. Repro: add a field to `type Ctx` and every literal
must be hand-edited in lockstep; a dropped field is a compile error (good) but the
mechanical tax is real and grows with the table count. Symptom is identical to the
ts slice's "counter threading" note but for a WIDER record. A single-field
`with`-style update in the language (even restricted to the tiers that already
emit `record_update`) would collapse `set_vt` to one line. Not a bug — the pure
port pays this for the reference's in-place `ctx.var_types[k] = v`; flagged for the
emitter guide / a future record-update-in-selfhost decision. (Not fixed.)

### Observation: `use { … }` brace list appears non-enforcing (LOW)
`value_children` (from `stdlib/value.rvl`) is used in `emit_go.rvl` (`flag_walk`,
`arrow_param_hint`) but was NOT in that file's `use "../stdlib/value.rvl" { … }`
brace list, yet the file compiled and ran green before this slice — i.e. a pub
symbol resolves whether or not it is named in the selective-import list. If that
is intended (brace list is advisory / all pub symbols import), the guide should
say so; if selective import is meant to be enforced, an unlisted-symbol use is a
silently-missed check (and a typo'd name would resolve to the wrong module's
export under shadowing). I ADDED `value_keys` to the list for this slice to be
explicit, but did not rely on enforcement. Repro: remove any imported name from a
`use { … }` list while still using it; observe it still compiles. Kit-ergonomics
(item 189) note only — not fixed, flagged for the import-semantics owner.

### Positive: `go_type` needed ZERO change for user type names
The reference `_go_v3_type` maps a user record/variant name and an UNKNOWN named
type to the SAME `_v3_ident(t)` passthrough (the `t in types` branch and the
fallthrough are byte-identical), so `emit_go.rvl`'s existing `go_type` fallback
`return v3_ident(t)` already emitted `Point` / `[]Point` / `RevlOpt[Point]`
correctly — no `types`-table parameter had to be threaded into `go_type` at all,
unlike the rust port's `rust_type_t(t, tnames)`. The go tier's decision to not
special-case the known-vs-unknown named type paid off directly in the self-host.

## emit_java.rvl slice 2 (item 210): the v3 typed-core

### Component/service tail deferred AS ONE UNIT — bridge-entangled, as feared
Per the slice's own gate (defer components if a lone service drags in a large
interop block, like rust's `_emit_bridge`, item 205/207), I checked the Java
component path before committing to it and deferred it whole. `backends/java/
emit.py`'s component tail is the single largest surface in the file
(`_emit_component`/`_emit_component_modern` ~250 lines, plus `_emit_service_
interfaces_v3`, `_emit_plugin_ctors`, `provide`/`req`/`config`, the modern-vs-
legacy `_component_needs_modern` split) AND it is entangled with the host-stub
`HashMap<String,V>` machinery: `_emit_host_stubs` -> `_map_value_expr_type` /
`_map_expr_inserts` / `_map_insert_candidates` / `_map_value_surface_type` do
per-SITE value-type inference by walking every `insert` call in the document to
learn the map's `V`. That is a second whole analysis pass, not a formatter, and
it is reachable ONLY through a component (v3 top-level fns never lower a `host`
node). Landing "just a service interface" would have pulled the erasure block in
behind it. Coherent green sub-slice = the typed-core; components are a clean
follow-on (call it slice 3). NOT a defect — a scoping call the gate anticipated.

### stdlib-kit validation (positive) — `value_keys` again closes the gap, ZERO bridges
The typed-core keys three IR shapes BY NAME: the document `types`
(name->spec), each record's `fields` (name->type), and — for the record-literal
nominal-type inference — a record's DECLARED field order. All three navigated in
pure revl with `stdlib/value.rvl::value_keys` (item 180) and `list_sort`/
`list_dedup`/`list_contains` (stdlib/list.rvl), with NO new `@py`. The slice
added exactly zero bridges beyond the two slice-1 host-formatting externs
(`json_dumps`, `num_str`). Corroborates the item-189 finding from emit_ts slice 2:
the kit is adequate for the name-keyed IR sections a typed-core emitter touches.

### Friction (corroborates emit_ts): the counter-threading tax, now with an ORDER trap (MED)
Same root cause as the emit_ts slice-2 note — the reference carries the
document-wide match-temp counter as mutable `_V3Ctx._match_counter` and mutates
in place, so revl must THREAD it: this slice converted EVERY expression renderer
from `(node, ctx) -> Str` to `(node, ctx, counter) -> {text, counter}` and every
statement renderer to `-> {lines, counter}`. That is the whole expr/stmt layer
re-plumbed for one integer. The Java-specific sharp edge on top of the ts note:
the counter's numbering is ORDER-SENSITIVE in a way that is invisible in the
reference's imperative code. In `_v3_match_expr`, each arm's `body` is rendered
(`_expr(...)`, advancing the counter for any NESTED match) BEFORE that arm's own
`__revl_case_N`/`__revl_ignored_N` is allocated (`ctx._match_counter += 1`). So a
nested match inside an arm body gets a LOWER number than the arm that contains
it (the corpus `describe` fn: outer Circle arm is `__revl_case_8`, its nested
match's arm is `__revl_case_7`). Repro: render the arm name before the arm body
and the numbering desyncs from the reference while every OTHER fixture still
passes — a silent, single-fixture byte diff. Symptom: the pure-functional port
has to reproduce not just WHICH counter values are used but the exact evaluation
ORDER the reference's expression statements imply. Fix applied here: render the
body first (`let br = render_expr(body, ctx, c); c = br.counter`), then allocate.
Not a bug in revl — inherent to porting stateful numbering — but worth a sharper
line in the emitter guide than the ts note carried: "thread the counter AND
match the reference's sub-expression evaluation order; a body that may nest a
match must be rendered before the enclosing site consumes a counter value." MED
(a real trap that only one fixture would have caught).

### Positive: `record_by_fields` set-key inference ported straight
The record-literal nominal-type inference (`_V3Ctx.record_type_for_fields`: a
literal's field SET -> its unique declared record class) reduced to a
canonical sorted-join key (`list_sort(names).join(",")`) used on BOTH the decl
side (`value_keys(fields)`) and the literal side, with `<<AMBIG>>` standing in
for the reference's `None`-on-collision — the same shape emit_rust slice 2 used.
The literal renders its VALUES in literal order (threading the counter) but emits
the ctor args in DECLARED order (`rfields`), and that split — value-order for the
counter, decl-order for the output — fell out cleanly once the two orders were
kept as separate lists. No kit friction.

### How it is checked (slice 2)
`tests/fixtures/emit_java_corpus/`: `records` (record decls, OUT-OF-ORDER literals
that must reorder to declared field order, field access, records nested in a
record and in `List[Point]`), `adts` (sealed-interface variants, `adt` ctors incl
a nullary `new Shape.Dot()`, `match` that is exhaustive-with-no-`default`,
wildcard-`default`, partial-synthesised-`default`, and NESTED — the counter
threads across the whole document — plus the `final Shape c = …` adt-binding
`let`), and `optmatch` (the built-in Opt Some/None `.map(..).orElseGet(..)`
path, kept clear of the deferred Result surface). Each `emit_src(ir) ==
backends/java/emit.py emit(ir)` byte-for-byte. Deferred features (components/
services, the host `HashMap<String,V>`, stdlib builtins, built-in Result/Ok/Err,
`record_update`, float interpolation, async/spawn/externs/tests, `let_pattern`)
are EXCLUDED from the corpus, not approximated.

## selfhost/emit_rust.rvl — slice 3 (item 207): components/services + bridge

Ported the Rust component surface as ONE unit (the item-205 finding: a lone
`service` unconditionally fires `_emit_bridge`, and a `component` fires the host
stubs + full impl machinery, so traits + provider + bridge cannot be a byte-exact
sub-slice piecemeal). NOW byte-identical to `backends/rust/emit.py`:
`_emit_service_traits`, `_emit_component` (the SIMPLE provider path — no
isolate/intercept/effect), and the whole `_emit_bridge` erasure block (the
`_revl_rpc` preamble, per-service consumer proxy + provider dispatch with the
SCALAR marshalling, and the key/service/plugin/isolate/load routing tables).
Cross-checked over two new fixtures — `service.rvl` (one service + one provider)
and `services_multi.rvl` (two services, one component providing both:
i64/bool/void marshalling, multi-provision routing) — each `emit_src(ir) ==
emit(ir)` to the last byte, and the eight slice-1/2 fixtures stay green.

### Finding: `ir_version` gate — a components-only doc lowers to v1, not v3 (MEDIUM)
Repro: a `.rvl` with ONLY a `service` + `component` (no functions/types) compiles
to `ir_version: 1`, whose `_module_header` banner and `#![allow(..)]` line DIFFER
from v3's, so `emit_src` (a v3-only assembler) cannot be byte-exact for it. The
self-host file itself is v3 (it carries the emitter functions), so this slice
scopes to v3 documents-with-components: the fixtures add a trivial `fn` to pin
`ir_version 3`, matching the real dogfood shape (emit_rust.rvl's own wrapper is
emitted through the v3 path). NOT a defect — the version dispatch is correct — but
a self-hosted `emit` that must accept ANY document needs an `ir_version`
front-door (v1/v2 headers + `_emit_components` with no types/functions section),
which this slice DEFERS. Symptom surfaced only because the first fixture happened
to be components-only; worth a one-line caveat in the self-host emitter guide.

### Friction: reserved-keyword collisions on IR-shaped local names (MEDIUM, recurs)
The IR's own vocabulary — `service`, `component`, `provides`, `isolate`, `struct`
— is exactly revl's reserved-keyword set, so the natural local names for walking
that IR (`let service = …`, `for (component of …)`, `let provides = …`) are all
rejected at parse (`expected ident, found 'service'`). Every component/bridge
emitter has to pick oblique names (`srv`, `comp`, `provs`, `iso`). Repro: `let
service = value_field(services, sname)` → parse error. This is the mirror of the
TARGET-keyword `_mangle` the emitter already carries for Rust output, but here it
bites the emitter's OWN source. Not a bug; a note in the guide ("name IR-walking
locals `srv`/`comp`/`provs` — the obvious names are keywords") plus, ideally, a
parser hint that suggests the exact rename would remove the trip-ups. LOW-to-fix.

### Ergonomics (positive): total `value_*` accessors erased the null-guarding
`value_keys(null)`/`value_list(null)` returning `[]` (value.rvl's totality
contract, item 188) meant `_emit_components`' unconditional run needed NO guard:
`emit_service_traits(<absent services>)` and the `components` loop and
`emit_bridge` all no-op on a types+functions-only document, so the eight existing
fixtures stayed byte-exact with zero special-casing. `value_bool(null) == false`
similarly made the `emission`/`idempotent`/`mutable`/`public` flag reads
one-liners with no presence check — a clean match for the reference's `if
node.get("x")` falsy-on-absent idiom. The item-195 `var_types`/`Sout` threading
tax did NOT recur here: the covered provider methods are pure single-`return`
bodies, so `pure_method_statements` reuses the existing `render_expr(node, ctx)`
with a per-method `Ctx` seeded from the service signature — no counter to thread.

### Host-formatting kept `@py` (item-180 "NOT obsoleted" category)
Two new bridged host helpers, kin to the existing `string_lit`/`mangle`:
`snake` (`_snake` verbatim — needs `str.lower()`/`str.isupper()`, which the str
kit does not expose as a code-point-free primitive) and `camel` (`_camel`
verbatim — `str.capitalize()` per `_`-split part). Everything else — the trait
assembly, the provider/plugin scaffold, the bridge preamble + routing tables, and
the scalar marshalling dispatch — is PURE revl over `value_*`. The one escape-gap
brush: the `_revl_rpc` preamble's `line.push('\n');` needs a literal backslash-n
in the OUTPUT, written `"line.push('\\n');"` (item-183 `\\`/`\"` in a plain
single-line string); the whole preamble stays a plain double-quoted block (braces
are literal, no `${}` interpolation in `"…"`), so no `$`-fragment needed backticks.

---

## Item 220 — `emit_wasm.rvl` slice 2: the string-literal memory ABI (Str `data` pooling + `_str_ptr`)

Slice 1 (item 200) mirrored the scalar value ABI and flagged that the reference
"falls off a cliff" at the first `Str`/`List`/record because it pools literals
into a `data` segment (moving `heap_start`) and threads a nesting-depth scratch
pointer (`_acquire_tmp`). This slice took the **string-literal** corner of that
surface — the smallest byte-reproducible subset — byte-for-byte: a Str literal in
return/let/assign/bare-expr position. New corpus fixture `strlit.rvl`; cross-check
green over all three fixtures (arith/control/strlit), 5/5 in the target test, full
`pytest tests/` = 3382 passed / 254 skipped, `backends/wasm/test_v3_emit.py` 37/37.

### The feared blocker did NOT bite: the pool traversal is deterministically reproducible
The task flagged `_collect_string_literals` and `_acquire_tmp` as candidate
item-179-class hazards (an `id()`/traversal-order dependency a second impl can't
reproduce). Neither did:
- `_acquire_tmp` keys the scratch name off **nesting depth** (`len(self._tmp_stack)`),
  which is structural and deterministic — and it is not even reached by a bare Str
  literal (a literal lowers straight to `(i32.const <offset>)`, no scratch). It
  only matters once an allocation nests inside another (record/list/variant), i.e.
  slice 3+.
- `_collect_string_literals`' offset assignment IS traversal-order-dependent (first
  encounter in a pre-order DFS over `node.values()`), but that order is **fully
  reproducible** from stdlib: `value_children` is documented as exactly
  `list(v.values())` for a dict / `list(v)` for a list (value.rvl §"generic
  recursion driver"), and `list_dedup` keeps the first occurrence — so the revl
  `collect_lits` walk + dedup reproduce `seen.setdefault` byte-for-byte. This is the
  same shape emit_py's whole-document walk already relies on; no new primitive, no
  `@py` escape.

So the byte oracle IS the right check for the string-literal memory surface — the
data-segment bytes, offsets, and `heap_start` are a pure function of the IR.

### The one genuine boundary: multi-byte (non-ASCII) string literals are OUT, cleanly
`_wat_bytes` operates on **utf-8 bytes** (`value.encode("utf-8")`, byte length in
the u32 prefix, per-byte escape). revl's string kit is code-point-based:
`s.length()` counts code points and `s.charCodeAt(i)` yields a scalar value, with
no code-point-free primitive to get utf-8 byte length or the byte sequence of a
non-ASCII scalar. For **ASCII** (scalar < 0x80) code point == byte and length ==
byte count, so the encoding is exact; for anything above 0x7f it would need a
utf-8 re-encoder in revl. Excluded and documented in `strlit.rvl`'s header. Repro:
a literal `"é"` would pool one byte too short. Fix belongs in the str kit (a
`str_utf8_bytes`/`str_byte_length` bridged primitive), NOT here — noted for a
future kit item. LOW-to-fix, and only unblocks non-ASCII data, which no covered
fixture needs.

### Ergonomics
- **`/` is Float, silently, until the return type catches it (papercut).** The
  align/hex/byte-split math (`(x+3)/4`, `b/16`, `n/256`) reads as integer division
  but `/` yields `Float` on this tier, so the first compile failed only at the
  `emit_src` boundary with `expects Int, got Float` — the error points at the
  *return*, not the `/`. The fix is `.div_trunc(k)` (or `.div_floor`), which for
  the non-negative offsets here equals the reference's `& ~3` bit-mask. Worth a
  guide line: "integer floor/trunc is `.div_trunc`/`.div_floor`; bare `/` is Float
  and will surface as a type error somewhere downstream." `%` is already integer
  (`i64.rem_s`), so the mixed `n.div_trunc(256) % 256` byte-split reads oddly but
  is correct.
- **`value_children` as the generic walk driver is a clean win.** Pooling needed
  exactly one small recursive `collect_lits` over `value_children` + `list_dedup`
  — no `@py`, no counter threading, and it dedups in first-encounter order for
  free. This is the ergonomic payoff of value.rvl's totality contract (item 188)
  showing up again.
- **The `$ident`-in-string lexer papercut (item 203) did NOT bite here, but was
  one character away.** The printable-ASCII lookup table
  `" !\"#$%&'()*+…~"` (needed for `chr(b)` on printable bytes) embeds a literal
  `$` immediately followed by `%`. It compiled fine — `$%` is not `$ident`, so the
  interpolation lexer left it alone — and `\"`/`\\` inside the plain
  double-quoted literal worked (item 183 closed). Had the table instead been built
  with a `$`-then-letter neighbour it would have tripped item 203. Still open; the
  table dodges it by luck of ASCII ordering, not by design.
- **Threading the pool through `Scope` (not a new parameter) kept the diff small.**
  The string pool is document-global and constant, so adding a third `strs`
  field to the already-threaded `Scope` record (mirroring the reference's
  `self.literal_offsets`) reached every expression site through the existing
  scope plumbing — only `scope_bind`, the `emit_function` seed, and the `render_inner`
  Str branch changed. No signature churn across the statement emitters.

### New findings
None beyond the two above (the `/`-is-Float surfacing-point papercut and the
str-kit utf-8-byte gap). No divergence from the reference in the covered subset.

## selfhost/emit_java.rvl — slice 3: the SIMPLE component/service unit (item 216)

Ported the smallest byte-exact corner of the java component surface item 210
deferred whole: `_emit_service_interfaces_v3`, the LEGACY `_emit_component`
simple-provider path, and the no-config `_emit_plugin_ctors` + A8-self-revert
`apply`. Two new fixtures (`service.rvl`, `services_multi.rvl`) are byte-identical
to `backends/java/emit.py`; the nine slice-1/2 fixtures stayed green with zero
change (the services/components blocks are non-empty-guarded, so a
functions-only document is untouched).

### Reference-behavior smell: an empty void op emits a THROWING stub (MEDIUM, REPORT)
`_method_body` (backends/java/emit.py:2536) admits exactly one shape — a single
`return <expr>`. EVERY other body, INCLUDING a legitimately EMPTY void op, falls
through to `throw new UnsupportedOperationException("effectful method body");`.
Repro: a provider `fn reset() { }` (a deliberately no-op void operation) lowers to
`public void reset() { throw new UnsupportedOperationException(...); }` — calling
the op at runtime throws, though the source said "do nothing". `services_multi.rvl`
exercises exactly this (its `reset()`), so the port reproduces it byte-for-byte,
but the behavior is a smell: an empty void body and an unported effectful body are
indistinguishable to the reference, and the former is silently turned into a trap.
A one-line special-case (`len(steps) == 0 and _method_return(...) is None` ->
emit an empty `{ }` body) would make an intentionally-empty op a no-op. NOT fixed
here (byte-frozen surface; the port must MATCH it, and the fix belongs in the
reference with a golden update). Rust/TS likely share the shape — worth a sweep.

### Ergonomics: absent-vs-empty asymmetry on component sub-maps (item 189, LOW)
The `component_simple_ok` predicate must gate on `requires`/`isolate`/`intercept`,
but the IR is inconsistent about presence: `config`/`requires` are always emitted
(`[]`/`{}`), while `isolate`/`intercept` are OMITTED when unused (the key is
absent, so `value_field(comp, "isolate")` is null, not an empty map). `value_*`
totality (item 188) softens this — `value_keys(null)` would still need a guard, so
a two-line `map_nonempty`/`list_nonempty` (null-reads-as-empty) covers both the
present-empty and absent cases uniformly. Not a bug; a note that the erased-IR
walker cannot assume a component carries every optional sub-map, so "absent" and
"present-but-empty" must be unified at the predicate, not the accessor.

### Positive: the item-195 counter-threading tax did NOT recur in the component block
The document-wide `__revl_case_N` match counter (item 210's threaded Int) is a
FUNCTION-body concern only: legacy provider method bodies render through the v1
`_expr` (ctx=None) which never touches the match counter, and `_emit_v3` emits
components AFTER functions, so the component block neither reads nor advances it.
`emit_component`/`method_body` are plain `List[Str]` builders with no counter in
their signatures — a clean contrast to the `Rendered { text, counter }` threading
the typed-core carries. One nicety worth reusing: the v1 legacy `_lit` and the v3
`java_lit` render a `lit` node byte-identically, so `method_body` reused the
existing `java_lit(node)` for the two kinds the simple provider forces (`lit`/
`name`) with no second literal path.

### Host-formatting kept `@py` (item-180 "NOT obsoleted"): `camel` only
One new bridged helper, `camel` (`_camel` verbatim — `"".join(part.capitalize()
for part in name.split("_"))`), kin to `json_dumps`/`num_str`; item 207 bridged
the byte-identical body for the rust slice. Everything else — the interface
assembly, the provider/plugin scaffold, and the `apply` LIFO undo list — is PURE
revl over `value_*`. The reserved-keyword-vs-IR-vocabulary collision (item 207's
note above) recurred verbatim: `service`/`component`/`provides` are all revl
keywords, so the IR-walking locals had to be `srv`/`comp`/`provs` again — a second
data point for the same guide note (`struct`/`case`/`comp` are FINE as locals;
`service`/`component`/`provides` are NOT).

---

## emit_ts.rvl slice 4 (item 219) — async coloring across the component tail

Landed byte-identical to `backends/typescript/emit.py`: async service operations
(`Promise<T>` signatures), `async` provide methods, the item-141 await-seed on a
req-keyed async-op `call`, and the async-generator activation body
(`ctx.effect(async function* …)`) with the `await` step's iteration boundary.
Cross-checked over two new fixtures — `services_async.rvl` (async op sigs + async
methods + await-seed direct AND nested in a ternary arm) and `components_await.rvl`
(activation-body `await` → `async function*`) — each `emit_src(ir) == emit(ir)` to
the last byte; the 14 slice-1/2/3 fixtures stay green. DEFERRED: the MODULE-FN
async path (async fns/externs via `async_names`/`async_locals`, async arrows,
async match), `timer`, spawn/instances, realms, the v1/v2 dispatch, canonical.

### Finding: the item-195 state-threading tax hit HARDEST here (HIGH, headline)
Symptom: mirroring the reference's async coloring meant threading its doc-level
`in_async`/`in_arrow`/`async_ops` state — which the reference carries for free on
the mutable `_Ctx` it already passes to `_expr` — down the self-host expr tree.
revl has no shared render context (the emitter already threads `$revl_match_N`
EXPLICITLY as a returned counter), so a NEW piece of downward-only state forced a
new `ACx` param onto **~20 functions and ~55 call sites** (`expr`/`expr_inner` and
every render helper that recurses into `expr`: `render_bin`/`render_un`/`match_expr`
/`render_record`/`render_list`/`render_arrow`/`render_interp`/`render_format`/
`render_call`/`paren_target`/`commajoin`/`int_as_number`/`float_operand`/…), even
though only ONE branch (the component-`call` await-seed) actually reads it. Repro:
the seed lives in `render_call`, reachable only via `expr → expr_inner →
render_call`, so every ancestor that calls `expr` must forward `ACx` or the
context is lost at that node — a template arm, a ternary arm, a `let` value each
need it independently. The reference edits ~6 lines for the same feature. Fix (not
mine to make): this is the second emitter (after item-195's `var_types`/`Sout`) to
pay the tax; a first-class threaded "render context" record — one `Ctx { counter,
acx, … }` returned-and-passed like the counter already is, or better, a lightweight
reader-monad/`with`-style sugar — would collapse both taxes. Until then EACH new
bit of emitter state is an O(call-sites) edit. Worth escalating on item 195.

### Friction: no `null` literal + no empty-map literal shaped the ACx design (MEDIUM)
Symptom: the reference resolves the seed as `(scope.requires[name], method) in
async_ops` — needing the requires map AND services map at the call site. Modeling
that in `ACx` would want nullable/absent map fields for the sync (`sync_acx()`)
and function-pass contexts, but revl **refuses a `null` literal** (checker:
"`null` has no type in revl — absence is Opt[T]") and offers no obvious empty-map
literal (`{}` reads as an empty record; `Map.new()` is a host builtin, not a
compile-time literal). Repro: `let a = { …, services: null }` → refused; `services:
{}` → wrong shape. Fix (design workaround, not a defect): PRE-RESOLVE the check
into `ops: List[Str]` (`"<reqKey>#<method>"` keys, built once per component via
`async_ops_of`), so `ACx` holds only `Bool`s + a `List[Str]`, and the sync
sentinel is the always-legal empty list `[]`. Cleaner than the reference's runtime
tuple-set membership, but the driver was a language gap, not taste. A blessed
empty-map/`Opt`-field idiom for record fields would remove the nudge.

### Friction (recurs, 3rd time): IR-vocabulary keyword collision — `requires` (LOW)
`fn async_ops_of(requires: Any, …)` → parse error `expected ident, found
'requires'`. Same family as the prior `service`/`component`/`provides` note; add
`requires` to the running list of IR keys whose natural local name is reserved
(rename → `req_map`). A parser hint that names the collision already fires
("`requires` is a reserved keyword …") — the remaining gap is only that the
obvious name is the RIGHT one and the emitter author must invent an oblique alias.

### Ergonomics (positive, item 189): `value_bool(null) == false` made async gating guard-free
Every async decision is a single falsy-on-absent read: `value_bool(value_field(spec,
"async"))` for the op signature/method prefix, `value_bool(value_field(node,
"async"))` for the arrow — no presence check, matching the reference's `if
method.get("async")` idiom exactly. `list_contains` (already in the file for the
context-augmentation dedup) covered the seed's membership test with no new kit. And
`body_has_await` over the top-level steps is a three-line total fold — the
async-generator gate needed no helper beyond it.

---

## Rust self-host slice 4 (item 218) — the effectful/config/req component surface

### Finding: item-195 `var_types`/`Sout` threading tax RECURS for effectful bodies (MEDIUM)
Slice 3 dodged it (pure single-`return` provider bodies). The effectful path
brings it straight back: `_method_body_lines` MUTATES `env.v3_ctx().var_types` as
it walks — a `let answer = model.complete(seed)` seeds `answer: Str` (from the
required service's declared return) so a LATER `emit`/`return` clones it by value
(item 114). revl has no shared mutable ctx, so the port threads a `Ctx` through a
`{lines, ctx}` return (`Sout`, reused from the fn-body renderer) and rebuilds it
per step. Repro: `effect_emit.rvl`'s `let v = store.read(k)` then `emit
store.write(k, v)` — the second `v` must render `v.clone()`, which only happens
if the let's inferred `Str` survived into the emit step's `Ctx`. Not a defect; the
threading is mechanical but it is the SAME tax the guide flagged at item 195, now
paid a second time in a second emitter. A shared "statement-fold returns
(lines, ctx)" helper across the fn-body AND method-body renderers would amortise
it; this slice open-codes both. LOW-to-fix (a refactor, not a bug).

### Ergonomics (positive): the capture-rename map rides ON the `Ctx`, not a param
The reference threads `rename: dict` as a SEPARATE parameter through every
`_render_expr`/`_v3_match_expr`/`_v3_interp`. Porting that literally would have
added a param to ~13 `render_*` functions and every call site (churn the pure
slices 1-3 would have to absorb). Instead the port carries `rn: Map[Str,Str]` as
a FIELD on the already-threaded `Ctx`: only the four kinds that consult it
(`name`/`req`/`config`/component-form `call`) read `ctx.rn`, and match/interp/
list/record subexpressions pick up the active rename FOR FREE because they already
forward `ctx`. Pure bodies pass `rn = Map.empty()`, so slices 1-3 stay byte-exact
with zero call-site edits. The varying per-render scopes (method `self.*`, acquire
`param.clone()`, undo `*_undo`) are just `set_rn(ctx, …)` before each render.
A clean win where the erased-IR `Ctx` bundle was the right seam.

### Friction: reserved-keyword collisions bite AGAIN, now on step/effect vocab (LOW, recurs)
The item-207 note (`service`/`component`/`provides` are keywords) extends to the
STEP vocabulary: `acquire` and `undo` are reserved too, so the natural
`let acquire = …` / `var undo_node = …` / `let undo = render(…)` in the
effect-body renderer all parse-fail (`expected ident, found 'acquire'`). Same for
a service op literally named `drop` in a FIXTURE (`fn drop(id: Str)` in a service
block → `expected ident, found 'acquire'`-class error is avoided only because the
service parser path differs, but a body local `drop` would trip). Repro: `let
acquire = render_expr(acqnode, …)` → parse error; renamed to `acq`. Mirror of the
Rust-side `_mname` (`drop`→`drop_`) the emitter already carries for OUTPUT — the
same collision class, on the emitter's own SOURCE. Not a bug; the guide's
"name IR-walking locals obliquely" caveat should list the step words
(`acq`/`acqnode`/`undonode`/`undox`) alongside `srv`/`comp`/`provs`.

### No defect found — the reference is ground truth and the port matches byte-for-byte
The effectful/config/req subset (5 new fixtures) is byte-identical to
`backends/rust/emit.py` across `_emit_component_new`, `_emit_component_auto`, the
config struct/Default/application/`_revl_load`, and the `Inject`/`ctx.require`
gate. Two reference behaviours look surprising but were replicated verbatim, NOT
"corrected": (1) a by-value service-call argument that is already a renamed
`param.clone()` gets a SECOND `.clone()` from `_by_value_arg` (`store.write(k
.clone().clone(), …)`), because `_arg_ref_name` reads the NODE's identity, not the
rendered string; (2) `_revl_load`'s `{pascal}Config` uses the RAW component name
while the struct decl uses `_ident(name)` — identical for non-keyword names, a
latent divergence for a keyword-named component that neither backend exercises.
Both are the reference's, so the differential oracle stays the arbiter.

### Note: the G4 emission gate shapes what an effectful FIXTURE can say (LOW)
`effect X` must wrap a REVERSIBLE op (a service `fn`, host acquire) with an
explicit `undo`; an emission (`emission fn`) must be `emit`-marked, not
`effect`-ed (`call to emission 'logger.log' must be marked emit (G4)`). And a
RETURN-position `fn f() = emit g()` is NOT an `emit` STEP — it lowers to a plain
observation and stays on the SIMPLE path; only a BLOCK-body statement `emit g()`
becomes an emit step that routes to `_emit_component_new`. Repro: `requires.rvl`
(`= emit compiler.propose(…)`) routes SIMPLE; `effect_emit.rvl` (`emit
store.write(k, v)` as a statement) routes NEW. A self-host author reaching for an
effectful fixture has to know this split; a one-line note in the guide
("`= emit` is an expression, block `emit` is a step") would save the round-trip.

---

## `selfhost/emit_java.rvl` — modern component path, config/req/effectful (item 225)

Slice 3 addendum for the Java tier: the config / required-service-routing /
effectful-method-body corner of `_emit_component_modern`, byte-exact against
`backends/java/emit.py`. Two fixtures (`comp_config_req.rvl`, `comp_multi_effect.rvl`).

### Reused the rust seam: `rn` as a Ctx FIELD, not a threaded `rename` param (kit ergonomics, item 189)
Item 218's rust port solved this and the same seam paid off verbatim for Java.
The reference threads `rename: dict` as a separate parameter through every
`_expr` recursion; the port carries `rn: Map[Str,Str]` on the already-threaded
`Ctx`. Only three kinds consult it (`name`/`req`, plus the component-form v1
`call` whose receiver renders through it) — `config` renders a BARE `_ident`
with no rename in the Java reference, unlike rust's `self.config.field.clone()`.
Cost of adding a 7th field to the erased-IR `Ctx` record: two literal sites
(`build_ctx`, the new `set_rn`) — immutability churn, but bounded, and slices 1-3
stay byte-exact passing `rn = Map.empty()` with zero call-site edits.

### State-threading tax (item 195): the match counter RESETS per component
`_method_body_lines` renders method bodies through `_expr`, which threads the
document-wide `_V3Ctx._match_counter` (a mutable field). The port threads it as
an `Int` (`Sout.counter`) through `method_body_lines` and ACROSS the methods of
one component. The subtle part: the reference constructs a FRESH `_V3Ctx` inside
`_emit_component_modern`, so the counter RESETS to 0 per component and is
independent of the free-function bodies. The port starts `counter = 0` per
component to match. Nothing in the covered corpus puts a `match` in a provide
method, so the value never leaves 0 — but had the port threaded the free-function
counter in instead, a component-body `match` would have silently diverged. The
immutable-record threading forced the question the reference's in-place mutation
hides; getting it right was a read of WHERE the reference news-up the ctx.

### Friction: reserved-keyword collisions, now on the COMPONENT vocabulary (LOW, recurs)
The item-207/218 note (`service`/`component`/`provides`/`acquire`/`undo` are
keywords) claims two more: `requires` and `provides` cannot name a local, so the
natural `let requires = value_field(comp, "requires")` / `let provides = …`
parse-fail (`expected ident, found 'requires'`). Renamed to `reqs_m`/`provs_m`.
Same collision class as every prior slice — the guide's "name IR-walking locals
obliquely" caveat should list the component header words
(`reqs`/`provs`/`caps`) next to the step words.

### Two reference behaviours replicated verbatim, NOT "corrected" (latent keyword divergences)
The differential oracle stays the arbiter, so both were mirrored exactly:
(1) a provider method emits its PARAMETER with the RAW param name (`{p}`) while
the method BODY references the same param MANGLED (`_ident(p)`) — identical for
every non-keyword param, a latent divergence for a param literally named `long`
that neither backend exercises; (2) `_param_type`/`_method_return` look up the
service method by the MANGLED method name against a service table keyed by the
ORIGINAL name — again identical unless a service op is a Java keyword. Both are
the reference's shapes; the port carries them rather than "fixing" one side.

### Note: the G4 emission gate shapes an effectful FIXTURE (LOW, mirrors the rust note)
`effect X undo Y` must wrap a REVERSIBLE observation (a service `fn`); an
`emission fn` must be `emit`-marked, not `effect`-ed. So an effect body cannot
acquire through an emission — `effect bus.send(..)` where `send` is an
`emission fn` fails `call to emission 'bus.send' must be marked emit (G4)`. The
fixture had to add an observation `fn touch(..)` for the `effect`/`undo` pair and
keep `send`/`retract` for the `emit`/`compensate` pair. A self-host author
writing an effectful Java fixture hits the identical split the rust note flags.

### No NEW compiler defect found — the port matches byte-for-byte
Both modern fixtures are byte-identical across the provider-class shape
(`Context`/`Context.EffectScope fx`/`<Svc>` fields + ctor), `_method_body_lines`
(`return`/`effect`+`undo`/`emit`+`compensate`), `_emit_plugin_ctors` (param + no-arg
ctors, `_config_default_lit`/`_zero_java_value`), and the `apply` effect-scope /
`ctx.get` / A8 self-revert try-catch. A host-`Map` `let-effect` component
(`demo/components/user_cache.rvl`) routes to the loud
`<<DEFER-component-nonsimple:UserCache>>` marker instead of mis-emitting — the
`_emit_host_stubs` `HashMap<String,V>` per-site inference stays slice 4+.

---

## emit_ts.rvl slice 5 (item 226) — the MODULE-FN async path

Landed byte-identical to `backends/typescript/emit.py`: async externs
(`export async function …: Promise<T>` carrying the verbatim, dedented `@ts`
body) via a new `emit_externs`; phase-2 async-colored module `fn`s (same
signature form, body rendered `in_async`); a `var`-callee `call` naming an async
callable (`async_names` — async externs + colored fns) or an async-value
parameter (`async_locals`, the item-92 `(…) -> Async[T]` slot) awaited; the async
`match` shape (Opt IIFE + tagged switch, arm-arrows and inner calls awaited); and
the async ARROW (`async (…) => …`). Cross-checked over five new fixtures
(`async_module_{extern,local,match,switch,arrow}.rvl`), each `emit_src(ir) ==
emit(ir)` to the last byte; the 18 slice-1..4 fixtures stay green (23 total).
DEFERRED (byte-safe, reported): the COMPONENT-dialect async call surface — an
async callable reached through the `fn` expr kind or from an async PROVIDE method
(`provide_impl`/`method_body` thread `async_names` EMPTY; no covered component
body names one, so byte-exact, but a follow-on slice must thread the doc set to
make the component tail faithful to the reference's provide-method awaiting);
variant type declarations (so async `match` is exercised over Opt/built-in
`Result` only); `timer`, spawn/instances, realms, the v1/v2 dispatch, canonical.

### NEW DEFECT (in the slice's own file): v3 `ts_type` mis-slices a SPACED fn-type return (MEDIUM, fixed)
Symptom: the async-value-local param `step: (Str) -> Async[Str]` rendered
`((a0: string) => > Async<unknown>)` instead of `((a0: string) => Promise<string>)`
— and, downstream, the call `step(x)` was NOT awaited (its `async_locals`
membership silently missed). Root cause: `ts_type` (the v3 renderer) extracted a
function type's RETURN slot as `py_strip(name.slice(arrow + 3, n))`, a fixed `+3`
that assumes the compact `)->T` spelling. The IR's surface form carries spaces
(`) -> Async[Str]`), so `arrow + 3` lands mid-token and yields `> Async[Str]`;
`ts_type("> Async[Str]")` then falls through to the `unknown`-args generic branch.
The v1 twin `ts_type_v1` and the reference `_split_fn_type` BOTH strip the arrow
correctly (`rest = s[i+1:].lstrip(); returns = rest[2:].strip()`) — only the v3
`ts_type` carried the naive offset. Latent since slice 3 (composite sigs): no
prior fixture rendered a v3 fn-type param whose surface form had spaces, so it
never fired; slice 5's async-local is the first. Repro: `fn f(g: (Str) -> Str)`
through `ts_type` → `((a0: string) => > Str)`. Fix (in-file, mine): replace the
`+3` with the same strip-`->`-strip the v1 path uses; `is_async_fn_type` reuses
that corrected extraction so the await-gate and the type render agree. Both the
type and the await are now byte-exact. Worth a REGRESSION fixture in the
non-async composite corpus too (a plain `(Str) -> Str` param) — the bug is
independent of async.

### Finding: the item-195 threading tax is now CHEAP to extend — but the record has no partial-update (MEDIUM, corroborates 219 + Go slice-2)
Item 219 paid the headline tax standing up `ACx` across ~20 fns/~55 call sites.
Slice 5's async-state addition (`async_names` + `async_locals`) rode that existing
plumbing for FREE through the whole expr tree — the marginal cost of a 2nd/3rd bit
of downward state, once the `ACx` seam exists, is near zero at the recursion
sites. The residual friction is elsewhere: `ACx` is a STRUCTURAL record, and revl
has no field-default nor a spread/`with` for a FRESH literal, so widening the type
from 3 fields to 5 forced editing EVERY construction site by hand — `sync_acx()`,
`render_arrow`'s `body_acx`, `provide_impl`'s async `body_acx`, and three in-file
`test` literals — each re-typing `async_names: [], async_locals: []` even where
they are inert. Same class as the Go slice-2 "8-field Ctx, no record-update to
spread it" note. A record-update literal (`{ acx | in_async: true }`) or field
defaults would have made this a one-line type edit. One genuinely new thread was
needed: `v3_stmt` (fn-body STATEMENTS) had hard-coded `let a = sync_acx()`, so an
async fn's `return`/`let`/branch would not await — it now takes `acx` and forwards
it through the if/while/for recursion. That is the only expr-vs-stmt seam the
prior slices had not already threaded.

### Ergonomics (item 189): async gating stayed guard-free; extern verbatim body needed 3 host-format `@py` helpers
`value_bool(value_field(fnode, "async"))` (falsy-on-absent) gated async fn/extern
emission and the doc-level `async_names` build with no presence checks, matching
the reference's `if fn.get("async")` exactly; `list_contains` (already in-file)
covered both `async_names` and `async_locals` membership with no new kit. The one
addition was on the HOST-FORMAT side (item-180 "NOT obsoleted"): an extern's
verbatim `@ts` body is rendered `textwrap.dedent(body.strip("\n"))` then
line-split, so three thin `@py` externs joined the existing `json_dumps`/
`template_text`/`py_rstrip` set — `py_dedent`, `py_strip_nl` (`.strip("\n")`), and
`py_splitlines`. These are pure host string-formatting (no emitter LOGIC), so they
sit squarely in the kept-`@py` category; still, a stdlib `str`-level
`dedent`/`splitlines` would let the emitter stay entirely off `@py` for this.

### Reference-faithful quirk replicated, NOT corrected (LOW)
The reference seeds `async_names` with RAW names (`fn.get("name")`) but `_fn_call`
compares the `_ident`-MANGLED name against it, while the `var`-callee `_expr`
branch compares the RAW callee name — an inconsistency that is invisible for any
non-JS-reserved name (`_ident` is the identity there) and that no fixture can
exercise otherwise. The port mirrors it verbatim (raw seed + raw var-callee
compare; the `fn`-kind await path is deferred with the component dialect), so the
differential oracle stays the arbiter rather than "fixing" a divergence the
reference does not have.

## `selfhost/lower.rvl::lower_to_ir` — the `functions` + `types` sections (item 232)

Item 227 produced the STRUCTURAL IR surface (services, component headers, the
simple component body). Item 232 adds the module-function spine: the `functions`
section (every `fn` with its full lowered body) and the `types` section (record/
variant declarations). Both are byte-identical to `src/revl/lower.py` across the
whole emit_py/emit_rust corpus, and the function corpus is emitter-ready end to
end (the reference python emitter renders the native IR to the same bytes as the
reference IR for every function document).

### The type annotations are the whole difficulty; the AST re-read is free
`lower_to_ir` re-reads each body straight from the shared parser's `Expr` AST
(`expr_at`), so the node SHAPES (`bin`/`un`/`builtin`/`call`/`field`/`index`/
`record`/`list`/`arrow`/`match`/`interp`/`optcall`, and the `let`/`assign`/`if`/
`while`/`for`/`return` steps) fall out of a direct structural walk. What the IR
also carries — and 227 deferred for exactly this reason — is the checker's TYPE
information: the `operands` tag on `+ - * / %` (and unary `-`), the `recv` tag on
`to_int`, match-arm `payload_type`, and an arrow's resolved `param_types`/
`returns`. Reproducing it needed a projection of `infer_ast` (`binop_ty` +
`builtin_ret` + a structural-record field lookup) threaded through a per-body
type environment. The projection is deliberately partial: it only has to
ANNOTATE, never diagnose (`admit_src` already rejected the real mismatches), so a
call result or an opaque host receiver types as unknown and simply omits the
annotation — which is exactly what the reference does when its own inference is
undetermined (`infer_ast(..., None)`). The sharpest witness is `divmod`: `mod` is
in the lowering's builtin table but NOT the checker's signature table, so `a.mod(b)`
lowers to a `builtin` node yet types as unknown — and the reference IR drops the
`operands` tag on every later `+` that reaches it. The port matches that byte for
byte.

### Record-update is read at the token level (shared-parser gap)
`selfhost/parser.rvl`'s expression grammar has no record-update production
(`p_inits` reads only `field: expr`), so `{ r | x = b }` returns `Bad`. Rather
than change another agent's parser, `lower_to_ir` recognises the form at the
token level (a depth-0 `|` inside a brace block is unambiguous — a template's `|`
lives inside the flattened `template` token and `||` is its own token) and hand-
builds the `record_update` node. This is the one fn-body form the AST cannot
carry; everything else routes through `expr_at`.

### Deferred, and why (reported, not worked around)
- **The `externs` section.** An extern's `bodies.{py,ts,...}` is the VERBATIM
  `@py { ... }` block, dedented. Reconstructing it byte-exact needs the raw
  source SLICE of the block (the reference reads it and runs stdlib `dedent`),
  but the token stream carries only `line`, not source offsets, so the exact
  whitespace/indentation cannot be recovered from tokens. A source-offset on the
  lexer's tokens (lexer.rvl, another slice's file) would unblock it.
- **The typed COMPONENT/method expression body.** `ir_body` is still 227's
  simple slice (effect/undo/provide over required-service calls + literals). The
  full typed spine there (config reads, match/ADT, saga `emit … compensate`,
  timers, spawn) is the remaining heavy piece; the module-fn `infer`/`lir_expr`
  built here is the reusable core for it, but the component dialect adds the
  `req`/`config`/`host`/`spawn` node kinds and the emission-gated step lowering
  on top.
## `selfhost/emit_java.rvl` — slice 4: realm placements (isolate/intercept) (item 235)

Extended the modern-component path to the `isolate`/`intercept` REALM-placement
corner, byte-exact against `backends/java/emit.py`. Two new corpus fixtures
(`comp_realm_isolate.rvl`, `comp_realm_intercept.rvl`) cross-check identical to the
reference; all 15 prior corpus fixtures + the scaffold + in-file-test assertions
stay green (17 java tests total, full `pytest tests/` = 3534 passed, 259 skipped).

### Scope: the placement, not the async/host-Map that shares the deferral bucket
The slice-3 comment lumped "isolate/intercept realms, async/await/spawn, host-Map"
into one `slice 4+` deferral. They are NOT one unit: the realm PLACEMENT is a
pure apply()-header addition (`ctx = ctx.isolate(<Svc>.class, "..")` before the
effect scope; `ctx.intercept(ServiceKey.of(<Svc>.class), <meta>)`), independent of
the body/method shapes. Splitting it out kept this a small byte-exact slice —
async (AsyncPlugin / CordisException import widening) and the host `HashMap<String,V>`
`_map_value_expr_type` surface are reported STILL deferred (slice 5+).

### State-threading (item 195): zero new threading — realms are apply-local
The realm lines read `comp`'s `isolate`/`intercept` maps and emit into the
apply() `out` list with no new accumulator: no counter, no rename map, no Ctx
field. Service resolution (`env.provides.get(key) or env.reqs[key]` for isolate,
`env.reqs[key]` for intercept) is a two-line `isolate_service` helper over the
already-bound `provs_m`/`reqs_m`. This is the cleanest modern-path extension so
far — unlike the effectful-body slice (225), nothing threads through the
provider-class loop. The gate change was two lines: drop the isolate/intercept
early-`false` in `modern_supported`, add the isolate/intercept `needs_modern`
trigger in `needs_modern_subset` (mirroring the reference `_component_needs_modern`
returning True on either).

### Ergonomics (item 189): `_metadata_lit` ported with no new kit
`metadata_lit` (the `intercept ... with <meta>` object literal) is a direct
`value_kind` dispatch reusing the in-file `json_dumps` (str + map keys), `num_str`
(int/float), `value_list`/`value_keys`/`value_field` — plus the nested
`java.util.List.of(..)`/`java.util.Map.of(..)` backtick joins. The one subtlety
that "just worked": `value_kind` (stdlib/value.rvl) checks `bool` BEFORE `int`,
exactly as the reference's `isinstance(value, bool)`-before-`int` order, so a
`true`/`false` metadatum renders `true`/`false` and never `1L`/`0L`. No falsy-gate
hazard — `map_nonempty`/`value_is_null` (already in-file) covered the absent-map
case that the reference spells `component.get("isolate") or {}`.

### NEW finding: none
No emitter/kit/stdlib defect surfaced. The realm surface fell out of the existing
value-model + host-format kit with only additive helpers; the differential oracle
stayed the arbiter throughout.

## Slice 6 (item 234) — spawn/instances + realm placements (emit_ts)

### The realm-metadata JSON stayed PURE revl — the `_json` dict is reconstructible byte-for-byte (LOW, good ergonomics)
The reference renders `isolate`/`intercept` metadata with `_json(x) == json.dumps(x)`.
For `isolate` the port passes the raw metadata dict straight to the existing
`json_dumps` @py helper (byte-identical, one line). For `intercept`'s dict-form
`inject` — the reference builds `{key: intercept.get(key) for key in inject_keys}`
then `_json`s it — the port did NOT need to build a host dict (revl has no dict
constructor to hand to `json_dumps`): `json.dumps` of `{k: v, …}` equals a
piecewise build with `": "` after each key and `", "` between entries, so
`` `${json_dumps(k)}: ${json_dumps(value_field(icept, k))}` `` joined with `, ` and
wrapped in `{…}` reproduces it exactly, staying off `@py` for the LOGIC. This is
the payoff of the item-180 erased-IR boundary: metadata that is already plain JSON
in the IR needs only the host-format leaf, never a navigation `@py` block.

### `value_field(...)` returning `null` for BOTH absent and present-null keys matched the reference for free (LOW)
The reference's `intercept.get(key)` yields `None` for an absent key OR a
present-but-null one, both rendering `null`. The port's `value_field(icept, k)`
collapses the same two cases to a null `Value` → `json_dumps` → `"null"`, so no
present-vs-absent distinction was needed (had it been, `value_opt` was the escape
hatch). One fewer branch than the reference's dict comprehension implies.

### `spawn`/`instance-get` are guard-free single-expression kinds — the state-threading tax (item 195) was ZERO here (LOW)
Both new expr kinds are pure `expr_inner` branches that thread the existing
`counter`/`acx` through their sub-exprs (spawn's config values, instance-get's
target) with no new downward state — the `ACx` seam stood up in 219 carried them
untouched. The `_uses_spawn` import gate is a plain `value_children` walk in the
shape of every other `uses_*` gate; adding `spawn` to the import list reused the
same `join(", ")` the header already builds. No kit gap, no new plumbing.

### Ergonomics (item 189): reserved contextual nouns cannot be spelled as record-literal KEYS in an in-file test (MEDIUM — recurring)
`component`, `config`, `intercept`, `isolate`, `realm`, `requires` are reserved
keywords, so a `spawn` IR node — whose fields are literally `component:`/`config:`
— CANNOT be written as a revl record literal in an in-file `test` block (the lexer
rejects the key). The corpus differential oracle covers `spawn` byte-for-byte, but
the focused in-file unit test had to be dropped for it (only `instance-get`, whose
keys are `target:`/`key:`, is spellable). Same class bit the emitter code: three
component-tail locals had to be renamed off `requires`/`intercept`/`isolate` to
`req_keys`/`icept`/`iso`. A record literal keys a field by NAME, and a name that
collides with a contextual-keyword should be allowed as a key (it is unambiguous
after `{`/`,` and before `:`), the way many languages permit keywords as member
names. Symptom: `expected ident, found 'component'` at the literal. Repro:
`let n = { component: "W" }`. Fix: allow reserved contextual nouns as record-field
keys (parser `_name()` in key position) — NOT fixed here (out of this slice's file
scope; the parser lives in src/revl/).

### Deferred, byte-honestly: routed requires + the v1/v2 path
`routes` (item 167: `isolate <k> in realms("w1"…) strategy(...)`) is deferred whole
— it pulls the `_TS_ROUTER_SRC` runtime literal, the `realmLabel` import, the
`inject_keys = requires − routes` subtraction, the per-key `revlRouter` proxy
before the body, AND the routed-`req` read path (`_revl_route_<k>`), none of which
composes byte-exact without all parts landing together. Separately, a component
that uses ONLY `isolate`/`intercept` (no spawn, no 2.0 types) compiles to
ir_version 2 and the reference routes it through `_emit_v1`, which the port does
NOT mirror (the v1/v2 path is deferred) — so both realm-placement fixtures carry a
trivial top-level 2.0 `fn` to stay on the `_emit_v3` path the port is faithful to.
A future slice covering `_emit_v1` (or routed requires) removes that scaffolding.

## wasm Path B slice 3 — the List/record allocation ABI (`emit_wasm.rvl`, item 236)

Slice 2 pooled Str literals (the `data` segment). Slice 3 mirrors the rest of the
*allocation* surface — `List` and record VALUES in linear memory — byte-for-byte:
`$alloc`, the `[u32 count][slot…]` / declared-order-field-slot layouts,
`_slot_store` (an `Int` stored native i64, a Bool/pointer `i64.extend_i32_u`-widened
in), the nesting-depth scratch pointer (`_acquire_tmp` -> `__revl_tmp` /
`__revl_tmp_n1` …), and the `_type_comments` layout block. Two corpus docs
(`listmem.rvl`, `recmem.rvl`) each emit `== backends/wasm/emit.py`, and stress
fixtures beyond the corpus — triple-nested lists, list-of-records, record-with-list
field, records built from params, allocation inside an `if` branch, let-bound
records recovered by field-set match — all landed byte-identical.

### Finding: `_acquire_tmp`'s nesting scratch is byte-REPRODUCIBLE (NOT the item-179 class) — reconstructed from a pure max-depth pass
The STOP-report hazard this slice was scoped against was whether the reference's
`self._tmp_stack` / `self._tmp_extra` — mutable instance state pushed/popped during
the emit walk — hides an `id()`/traversal-order dependency a second implementation
cannot reproduce (item 179). It does NOT. The scratch NAME is a pure function of
lexical allocation-nesting depth (`__revl_tmp` at depth 0, `__revl_tmp_n<d>`
deeper), and `_tmp_extra` is just `{n1..nD}` for D = the max nesting depth, which is
CONTIGUOUS (you cannot reach depth d without an allocation at each of 0..d-1 in the
same chain). So the port threads `depth` as a plain downward argument (no mutable
stack) and reconstructs the header's extra-local set from a separate pure
`body_max_depth` pass — sorted-set-identical to `sorted(self._tmp_extra)` for D<10
(nesting never approaches that). A genuinely stateful allocator (an `id()`-keyed
offset cache, say) would have been the blocker; the depth-indexed name is not one.

### Finding (item 203, THIRD hit): `$ident`-in-a-plain-string blocks the natural way to build WAT
Every WAT fragment names a `$local`/`$func`, so the item-203 papercut — a plain
`"…$alloc…"` string is REJECTED as would-be 1.x interpolation — hit twice while
writing this slice: first building the `(call $alloc …)` / `(local.get $__revl_tmp)`
lines in `render_list`/`render_record`, then again in an in-file `test` asserting
`slot_store`'s output literal (`(local.get $p)`). The fix each time is to switch the
plain string to a backtick template (where a bare `$name` is literal and only
`${…}` interpolates), so the emitter's most natural output — string-concatenated
WAT — is exactly the form the lexer flags, and a WAT-heavy `.rvl` must write nearly
every line as a backtick even when it interpolates nothing. Symptom: `RevlError:
`$alloc` in a plain string — this was interpolation in 1.x`. Repro: any
`"…$x…".concat(…)` in a `.rvl`. A `\$` plain-string escape, or exempting a string
with no `${` from the 1.x-ambiguity check, would remove the tax. (Do NOT fix here.)

### Ergonomics (item 189): the kits carried records for free; `value_keys` gave the byte-critical field ORDER
The record ABI's one correctness-critical fact is field ORDER — the reference stores
fields in DECLARED order (the type table), not the literal's source order. The
stdlib-value kit already exposes this exactly: `value_keys(value_field(spec,
"fields"))` returns declared order (`list(dict)` insertion order, item-180 contract),
and `value_field(fmap, name)` the field type — so the whole `_record_fields` /
declared-order walk is pure `use`d kit with no private `@py`. `record_type_of`'s
field-SET match (for an unannotated `let`-bound record) was a 6-line `names_eq` over
`value_keys`. The only threading tax was widening `Scope` with a `rectypes: Any`
field (the `ir["types"]` table) so `_record_expr` can reach the type table the same
way the string pool already rides the scope — the same "structural record, no
record-update literal" friction the async slice-5 note logged: adding one field to
`Scope` meant editing every `{ slots:…, types:…, strs:… }` construction site
(here only `scope_bind` and the `emit_function` seed) by hand.

---

## `emit_wasm.rvl` slice 4 — tagged cells + reads + the preamble builtin surface (item 239)

Extended the wasm self-host emitter past the write-only value ABI of slice 3 to
the READ and tagged-CONSTRUCTION surface, byte-identical to
`backends/wasm/emit.py` over four new corpus fixtures (`reads.rvl`,
`variants.rvl`, `forloop.rvl`, `builtins.rvl`) plus the four prior ones, all
still green:

- **field/index reads + `len`** — `_slot_load` (the read twin of `_slot_store`:
  an `Int` is the i64, everything narrower is `i32.wrap_i64`'d back out), a
  record field at `8*declared_position`, a list index with the constant-fold /
  variable-address split, and `x.length` (the `len` node) counting code points
  for a Str and the u32 prefix for a List.
- **tagged unions** — `_make_tagged` / `_tagged_layout` / `_tag_of` for the
  `[u32 tag][pad][slot payload]` cell: the built-in `Opt`/`Result` (parsed from
  the type spelling) and user `variant`s (from the type table), nullary vs
  payload cases, nested cells + lists of cells, riding the same depth-indexed
  scratch as records/lists.
- **the `for (x of xs)` list walk** — the `for_ptr`/`for_cnt`/`for_idx` cursor
  triple, declared in the header in pre-order loop-id order.
- **the `builtin`/`len` method surface** — the subset whose runtime helpers all
  live in the always-emitted preamble.

### Finding (item 179 class, AVOIDED): the reference's mutable `_loop_counter` / `_lid` is emit-time state
`backends/wasm/emit.py` numbers `for` loops by MUTATING the IR: `_collect_locals`
walks the body, does `self._loop_counter += 1` at each `for`, and writes the id
back onto the node (`stmt["_lid"] = …`) plus appends `for_ptr_N`/`for_cnt_N`/
`for_idx_N` to `self._for_temps`; `_emit_for` later reads `stmt["_lid"]`. That is
exactly the "non-reproducible emit-time state" shape item 179 warned about — a
pure re-run can't mutate the shared IR. It was reproducible here *only because*
the numbering is a deterministic function of position: the Nth `for` in
document pre-order gets id N, and the header's `_for_temps` is that same order.
So the self-host mirrors it with (a) a `count_fors` pass for the header's
`1..N` cursor run and (b) a `loop: Int` counter threaded in and out of
`emit_stmts`/`emit_stmt` (added to `Sout`), incremented at each `for` before
recursing its body — a pure fold that lands the same ids without touching the
IR. Had the reference instead numbered loops by, say, first-visit into a hash of
node identity, this slice would have STOP-reported it.

### Finding (item 203, still biting): every helper mnemonic is a `$ident` plain string
The papercut logged twice in slice 3 hit ~14 more times here: the builtin
lowerings pick a runtime helper by name (`var helper = "$str_concat"`,
`"$list_slice"`, `"$int_div_floor"`, …), and each such plain string is REJECTED
as would-be 1.x interpolation (`RevlError: `$list_concat` in a plain string`).
The parallel agent's 203 fix (lexer + fmt) was not yet in this worktree, so the
workaround was to wrap each helper-name literal in `"""…"""` (a triple-quoted
verbatim string, where a bare `$name` is literal) rather than a backtick — a
plain assignment `helper = """$str_concat"""` reads better than a template for a
constant. Symptom/repro unchanged from the slice-3 note; still do NOT fix here.

### Ergonomics: `Str` `[i]` indexing is a frontend refusal, so `_index_expr`'s Str arm is dead
`_index_expr` in the reference handles a `Str` target (`$str_char_at`), but the
frontend typechecker rejects `s[i]` on a `Str` outright (`` `Str` has no index
operator ``, steering to `s.charAt(i)`), so that arm is never reached from real
source. The self-host mirrors it for faithfulness but the corpus can't exercise
it — `charAt`/`charCodeAt`/`slice` cover the Str-read surface instead. Not a
defect, just a reference branch that no `.rvl` input can reach.

### Scope note: reading a tagged cell (`match`/`??`) is the natural next slice
Slice 4 constructs tagged cells but does not read them. `_match_expr` mints a
per-match scrutinee scratch plus one `l_<bind>` per arm, and `_nullish_expr`
(`??`) an Opt-cell scratch and a `(if (result …))` payload branch — both add
header-ordered locals (`match_binds` / `match_scruts`, sorted, slotted between
the `l_` locals and the `for_temps`) that this slice's `emit_function` ordering
does not yet reproduce. They are the clean unit for slice 5, alongside the
demand-pulled reader helpers (`indexOf`/`split`/`join`) that would need the
preamble to become demand-driven rather than a fixed verbatim block.

---

## ts Path B slice 7 — the v1/v2 `_emit_v1` DISPATCH path (`emit_ts.rvl`, item 240)

Item 234 flagged that "a component using only isolate/intercept compiles to irv2,
which the port doesn't mirror" and kept its realm fixtures on the v3 path by adding
a trivial top-level 2.0 `fn`. Slice 7 chased that flag down and found the mirror
ALREADY HOLDS byte-for-byte, with NO emitter change: `emit_src` is
version-agnostic, and for a component-only document `_emit_v1` and `_emit_v3` emit
the identical byte stream (same header — a v1/v2 doc can carry no test, so no
`vitest`/lifecycle branch fires; same `_revl_helpers`; same service interfaces —
`_ts_type`'s `known_types` default is `frozenset()`, exactly what `_emit_v3` passes
when `types` is empty; same `_context_augmentation`; same per-`_component` object).
Three v1/v2-dispatch fixtures now pin it: `v1_component_body.rvl` (irv1: config +
effect/undo + emit/compensate + provide-method ternary), `v2_isolate_only.rvl` and
`v2_intercept_only.rvl` (irv2, the item-234 case with the trivial `fn` removed).
All three == `backends/typescript/emit.py` to the last byte.

### Finding (NOT a bug — a verification win): the version dispatch was a phantom gap
The scoped hazard was that `_emit_v1` might diverge from the version-agnostic
`emit_src` — a header conditional, a `known_types`-flavored signature, an
ordering. None materialized: `_emit_v1` is a proper byte-subset of the
`_emit_v3` assembly for a component-only input. The only real v1/v2 surface that
DOES diverge is a ROUTED require (item 167), which also lowers to irv2 but needs
machinery `emit_src` does not emit — so the "v1/v2 path" deferral was really a
"routed-requires" deferral wearing the version label. Recording it so a later
slice does not re-audit the whole dispatch when only the router is missing.

### Finding (item 203-adjacent, blocks routed-requires): `_TS_ROUTER_SRC` is a `${…}`/backtick literal that cannot be embedded byte-exact
Routed-requires is the clean remaining v2 sub-slice, but its runtime realization
`_TS_ROUTER_SRC` is ~60 lines of verbatim TypeScript that itself contains JS
template literals — `` `revl: router for ${JSON.stringify(key)} …` `` — i.e. both
backticks AND `${…}`. In revl a backtick template interpolates `${…}`, and a plain
string rejects a bare `$name` as would-be 1.x interpolation (item 203, logged
thrice on the wasm slice). So there is no literal form that reproduces this blob
byte-for-byte: a backtick template would try to evaluate its inner `${…}`, and a
plain string trips the 1.x guard on the `$` in `${…}`/`$JSON`. A verbatim/raw
string form (an `r"…"` with no interpolation and no 1.x check, or sourcing the blob
through an `@py`-returned constant the way `newline()`/`template_text` already
bridge host text) would unblock it. Symptom/repro: pasting the router source into
an `.rvl` string, either flavor, fails to round-trip. Deferred, not fixed here.

### Ergonomics (items 189/195): a zero-code slice — no kit gap, no threading tax
This slice added no emitter logic (only fixtures + comments), so it surfaced no new
item-189 kit gap and no item-195 state-threading friction. It is a small data
point FOR the port's design: because `emit_src` threads its context as plain
arguments and keys emission off feature presence (never off an `ir_version`
branch), a whole reference DISPATCH arm was covered for free. The reference's
`emit()` needs an explicit `if version in (1, 2)` fork; the port did not, and was
byte-identical anyway. (Do NOT change the reference — the fork is its right shape.)
