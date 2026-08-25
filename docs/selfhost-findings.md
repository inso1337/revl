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
