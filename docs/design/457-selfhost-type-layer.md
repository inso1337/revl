# Design: the self-host type layer

Design-doc id 457 (next free number under `docs/design/`; the roadmap item of
the same number is unrelated). Roadmap items served: 417 (issue #108, the type
layer as a distinct sub-goal), 391 (issue #106, self-host parity), 332 (issue
#98, the crate's Stage 4 `compile_to`), 146 (issue #84, full self-host), and
the rust-gate structural frontier (issue #346). Item 429 owns the oracle
classifier this design extends.

Sources studied, all at `0e3eaf37`: `src/revl/typecheck.py` (`infer_ast`,
`check_ast`, `compatible`, `_binop_type`, `builtin_check`, `unify`),
`src/revl/lower.py` (`_check_and_lower`, `_lower_fns`, `_lower_pure_stmt`,
`_lower_pure_expr`, `_check_returns_on_every_path`,
`_check_match_exhaustiveness`, `_refuse_callable_shadowing`, `_link`),
`src/revl/gate.py`, `src/revl/compiler.py` (the `manifest=` path),
`selfhost/checker.rvl`, `selfhost/lower.rvl`, `selfhost/compile.rvl`,
`tools/build_gate_crate.py`, `crates/revl-gate/src/lib.rs`,
`tools/gate_reference_census.py`, and the oracles under `tests/test_selfhost_*`,
`tests/test_gate_*`, `tests/test_inprocess_gate_rust.py`.

## 0. The decision in one paragraph

The reference types a function body and lowers it in ONE walk
(`_lower_pure_stmt` calls `infer_ast`/`check_ast` and emits the IR step in the
same function). The self-host already has that walk: `selfhost/lower.rvl`'s
`lir_*` family threads a typed environment through every statement to
annotate the IR (`operands`, `widen`, `payload_type`), but it never refuses.
The type layer is that walk made refusing, driven by an expression algebra
that lives in `selfhost/checker.rvl` (its oracle already compares inferred
types against `infer_ast`) over a type-spelling algebra that moves into a new
leaf module `selfhost/types.rvl`. `admit_src` and `lower_to_ir` become two
projections of one `lower_checked(src)`. On top of that walk, `admit_into`
takes the running manifest as the same JSON document `revl.gate.admit_into`
takes, and the crate gains an `Admitted` arm that is sound because a
generated FAMILY frontier (not just the lexical one) declines any program
that can reach a reference check the self-host does not run. Every slice is
a differential oracle in the shape of the existing ones, and the gap is named
executably from slice zero so no red oracle ever discovers it.

## 1. The gap, measured

Over the 137 fixtures in `examples/rejections/` at `0e3eaf37`, the reference
refuses every one and `selfhost/lower.rvl::admit_src` raises no objection to
69 of them (the roadmap's "87 of 122" and "67" figures predate the corpus
growing). By the reference's own code field: 24 carry `T1`, 1 `T2`, 2
`HOST-METHOD`, 1 `A1`, 1 `G4`, 2 `G5`, and 38 carry no code at all.

Grouped by which check refuses them, which is what decides the slice each
belongs to (the fixture names are the corpus family each slice must move from
"pinned gap" to "agrees"):

| family | fixtures | reference site |
|---|---|---|
| fn-body binding rules (G1/G6) | `g1_template_undeclared`, `v2_undeclared_fn_var`, `v2_let_reassignment`, `v2_compound_assign_on_let`, `v2_duplicate_let_block_scope`, `shadowed_module_fn_call`, `g6_closure_mutates_capture` | `_lower_pure_stmt`, `_lower_pure_expr` `ExprVar`, `_refuse_callable_shadowing`, `_mutable_free_vars` |
| expression typing (T1/T2) | `t2_null_in_expression`, `t11_field_through_opt`, `t12_str_index`, `t14_optional_chain_on_nonoptional`, `t21_int32_narrow_implicit`, `t22_int32_width_mix`, `t23_int32_remainder`, `t28_bitwise_non_int32`, `t26_anon_record_update_wrong_type`, `t27_anon_record_update_undeclared_field`, `t36_float_literal_range` | `infer_ast`, `_binop_type`, `mismatch`, `opt_escape_error` |
| calls and signatures | `t10_call_arity`, `t15_generic_call_site`, `v2_map_set_value_mismatch`, `v2_map_value_unknown_method`, `arith_zero_divisor`, `t24_opaque_receiver_builtin`, `host_method_not_on_surface`, `g4_extern_undo_wrong_arg_type` | `infer_ast` `ExprCall`, `builtin_check`, `host_family_check`, `_lower_pure_expr` method branch |
| arrows and function values | `t17_arrow_body_unchecked`, `t32_arrow_value_result_flows`, `t33_arrow_value_arity`, `t35_arrow_annotation_not_quantified`, `t34_arrow_self_declared_async` | `_check_arrow`, `call_function_value`, `_resolve_arrow`, `refuse_self_declared_async` |
| return paths and match | `t8_missing_return`, `t9_return_path_incomplete`, `t13_unknown_match_case`, `v2_match_nonexhaustive` | `_check_returns_on_every_path`, `_check_match_exhaustiveness` |
| declarations | `t18_type_alias_cycle`, `t6_bare_generic`, `t5_destructure_nonrecord` | `_resolve_type_aliases`, `check_type_wellformed`, `_lower_let_pattern_stmt` |
| provide-method and component bodies | `t1_service_arg_type`, `t4_field_arg_type`, `t7_provide_param_annotation_mismatch`, `t16_provide_method_missing_return`, `t31_index_non_int_provide_method`, `t3_config_default_type`, `a6_method_not_in_service`, `g6_method_local_shadows_component` | `_lower_provide`, `_check_component_call`, `infer_ir`/`check_ir`, `_config_default_type` |

The other 23 false-admits are NOT the type layer and stay out of this design:
extern `undo`/`compensate` slot validation (`g4_extern_*`, 4 of the 5),
`g4_missing_undo`, the G5 inverse-emission pair, `a2`, `a9`, `service_compat_
duplicate`, the parameterized spawn cone (`g4_spawn_widens_parameter`, already
named in `KNOWN_BYPASSES`), the three `v2_use_*` fixtures (the reference
refuses them for needing `modules=`, which the crate cannot supply either),
and the parser- and statement-form fixtures (`t19_union_type`,
`v2_keyword_as_field_name`, `v2_nullish_mixed_with_or`,
`v2_optional_chain_nonoptional`, `v2_extern_unclassified`,
`v2_dynamic_realm`, `v2_fail_in_pure_fn`, `g6_impure_statement`,
`v2_extern_acquire_no_undo`). Item 391's per-feature port list owns those.

Why the census does not show this today: `tools/gate_reference_census.py`
buckets a false admission as `false-admit/<tag>` only when
`tests/test_selfhost_lower.py::_classify` names the reference refusal with a
tag the gate claims. Every type-layer refusal classifies as `OUT:`, so the 46
fixtures above sit in `no-objection-out-of-slice`, a bucket the baseline
tolerates by design. Slice T0 changes that vocabulary first, so the whole
plan runs against a named, shrinking list rather than an adjective.

One structural finding worth stating because it shapes section 3: the fn-body
model the composition gate walks (`p_fn` -> `p_stmt_run` -> `Stmt`) reads only
the statement forms admission cares about. A fn body's `if`/`while`/`for`
falls through to `expr_at`, parses as `Bad`, and `p_stmt_run` skips to the end
of the body. That is why `g1_template_undeclared` is a false-admit: the
undeclared name sits after an `if`. The complete fn-body reader is
`lir_one_stmt`, which is the reason the type layer builds on `lir_*` and not
on `Stmt`.

## 2. Obligations: what the reference decides that the self-host must mirror

The comparison unit is fixed by the existing oracles and must not change:
the self-host returns `"<TAG>|<message>"` where `message` equals
`RevlError.message` byte for byte (never the rendered `file:line:` prefix,
never the hint), and `TAG` equals what `_classify` derives from the
reference error. A hint is free text on the self-host side and is not
compared. `render_type` semantics (strip the `?` tparam marker, `None` renders
as the literal string the reference chooses per site) are part of the message
and therefore part of the obligation.

### 2.1 Declaration level, in the reference's order

1. `_resolve_type_aliases`: transparent `type X = Y` erasure through type
   applications and fn-type spellings; refusal `type alias cycle: A -> B -> A`.
   (checker.rvl slice three already ports the erasure; the cycle message is
   what is missing.)
2. `_validate_declared_types` / `check_type_wellformed`: every declared type
   at fn params/returns, extern params/returns, service method params/returns,
   record fields, case payloads, config fields; bare generic (`` `Opt` takes 1
   type argument(s), got 0 (`Opt`) ``), `Async[...]` only as a fn-type return,
   and the config-is-data rule (`check_config_field_is_data`).
3. `_lower_type_decls`: `duplicate type`, `duplicate field ... in record`,
   `duplicate case ... in type`.
4. `_signature_table`: per fn/extern `{params, returns, tparams, defaults,
   required}` with type parameters MARKED (`?T`): explicit `fn id[T]` list
   (validated by `validate_explicit_tparams` against declared types) or, only
   when no list is written, the implicit single-uppercase rule.
5. `_case_table`: `Some`/`None`/`Ok`/`Err` seeded; a user case name declared
   by two ADTs is dropped (silent).
6. `_refuse_callable_shadowing`: a body that both binds and calls a name that
   is a visible module fn/extern.
7. `_lower_fns`: `_check_verified_totality` first; then per fn in declaration
   order: `duplicate function`, `duplicate parameter ... in fn`, the body
   (2.3), then `_check_returns_on_every_path`.

Where this sits in `admit_src`: after `BAD` (parse) and before
`check_cache_fns`, which is where `_lower_fns` sits relative to
`_check_cache_declarations` in `_check_and_lower`. `_lower_fns` raises
directly (it is not a `_collect` site), so on a single-refusal program its
verdict wins over every later phase.

### 2.2 The expression algebra (`infer_ast` in its raising mode, `check_ast`)

Per `Expr` kind, the result type and the refusals. Only the refusal SHAPES
are listed; the exact strings are read off the reference at port time and
pinned by the corpus.

| kind | infers | refuses |
|---|---|---|
| literal | `Int`/`Float`/`Str`/`Bool` | `null` (T2); Int outside i64 (the literal text compared against `9223372036854775807`); Float that folds to infinity |
| var | tenv, else nullary user case -> its ADT, else a monomorphic non-unit fn -> `(P..) -> R`, else unknown | (resolution is 2.3's) |
| bin | `_binop_type` verbatim: `== !=` need a common type (`incomparable`); `< <= > >=` numeric or `Str` only; `&& \|\|` Bool; `??` Opt on the left; bitwise Int32-only; `+` string rule; `+ - * %` no Int32/Int mix, `%` Int-only under Int32; `/` is Float | `mismatch(where=operand of ...)` and the five dedicated T1 messages |
| un | `!` Bool, `~` Int32, `-` numeric | `mismatch` / the `~` message |
| field | `.length` on `Str`/`Bytes`/`List`; structural record; nominal record | Opt escape (`opt_escape_error`); `Any`/`Value` erased read; `has no field` with the sorted field list |
| optfield/optcall | `Opt[...]` of the member/builtin result, never double-wrapped | non-Opt left side |
| index | `List` element, `Str` refused | `Str has no index operator`; non-Int index |
| ternary | `join` | `ternary branches disagree` |
| list | `List[join]` or `List[Never]` | element checks in check position |
| record | structural `{a: T, b: U}` (sorted, unknown -> `Any`) | in check position against a nominal record: missing/unknown fields, per-field check |
| record update | base type | not a record; unknown field; per-field check |
| block arm | tail under the arm's `let`s | (statements validated at lowering) |
| call, `Var` callee | local fn-typed binding first; ADT case (payload check, `Some`/`Ok`/`Err` spelling); signature: arity with `required..len` window and the `is declared (..)` hint, monomorphic arg compatibility, hole pinning, arrow args; generic: `unify` per argument then `substitute` the return | `takes N argument(s), M given`; `argument i of ...` |
| call, `Field` callee | `Map.empty()`; host constructor families (`_HOST_FAMILIES`, `_HOST_RESULT_SIG`); list transforms desugared to their free fn; `builtin_check` (receiver family, `to_int` rows, bottom learning on `[]`/`Map.empty()`, `@elem`/`@member`/`@self`) | `builtin X needs a ... receiver`; `has no form for a ... receiver`; `builtin X argument expects` |
| call, other callee | `call_function_value` when the callee infers to a fn type | arity and argument messages of a function value |
| match | join of arms with the payload binding typed from the variant table or `Opt`/`Result` args | (exhaustiveness is 2.3's) |
| arrow | item 75(a) §3.1/3.2: annotations or bottom per parameter; return from annotation or, when the body cannot mention a bottom parameter, from the body; always a fn type with `Any` for bottoms | self-declared async colour (A1); annotation checked against the body only when independent |

`check_ast` is the bidirectional half: record literal against a nominal
record, arrow against a fn type (`_check_arrow`: contravariant params, the
resolved signature recorded on the node), list elements, ternary and match
arms per arm, record update, block arm tail, then `compatible(expected,
actual)` with `mismatch(where)`. The `where` strings are part of the message
(`this function's return`, `` `let x: T` ``, `` assignment to `x` (a `T`
variable) ``, `` argument i of `f(...)` ``, `` field `f` of `R` ``,
`` element of `List[T]` ``, `` `if` condition ``).

`compatible` is the value-flow relation and must be ported exactly, in its
order: `Never` one-way; wildcards (`Any`, `?T`, the poison sentinel);
`Value` two-way; equality; structural-vs-nominal record resolution (one-sided,
through the declared table); `Int -> Float`, `Int32 -> Int/Float`; `Async[T]`
on the expected side reduces to `T`; fn types contravariant/covariant;
`Opt` injection; same-head elementwise. `join`, `widen_bottom` (the
accumulator idiom), `unify`/`substitute`, `format_type`/`parse_type`,
`structural_fields`/`format_structural`, `render_type` are the rest of the
spelling algebra.

### 2.3 The statement layer (`_lower_pure_stmt`)

Block scoping is by COPY: each `if` arm, loop body and match arm starts from
a snapshot of `scope` and `type_env`; a `let` inside does not leak out and
disjoint siblings may reuse a name. A persistent `Map` gives this for free.

| statement | typing | refuses |
|---|---|---|
| `let`/`var` | annotation is the check position (`check_ast`), then `type_env[name]` is the annotation, else the inferred type when known; host provenance recorded; `_pin_empty_literal`; widen marker | `` `x` is already declared in this function `` |
| assign / compound | the declared type is the check position for an arrow; `widen_bottom` for `var m = Map.empty()` then `m = m.set(..)`; else `compatible`, else `mismatch(assignment to ...)` | `` `x` is not declared in this function ``; `` cannot reassign `x` `` (it is `let`) |
| `return e` | `check_ast(e, expected_return, "this function's return")`; `_inject_opt`; widen | `` bare `return` in a function declared to return `T` `` |
| `if`/`while`/`assert` | `_bool_cond` (`` `if` condition `` expects Bool) | |
| `for (x of e)` | `e` must infer to `List[...]` when known; `x` typed as the element | `` `for ... of` iterates a `List[...]`, got ... `` |
| expr stmt | `infer_ast` in raising mode | |
| `break`/`continue` | none (parser owns placement) | |
| let-pattern | record destructuring against a record type | `record destructuring requires a record` |
| after the body | `_check_returns_on_every_path` (the Java/Rust rule: `if` needs both arms, `for`/conditional `while` never count, `while (true)` counts unless a targeting `break`) | the two `is declared to return ... but` messages |
| every `match` | `_check_match_exhaustiveness`: unknown arm name first, then missing cases unless `_` | `` `P` is not a case of `T` (cases: ...) ``; `non-exhaustive match: missing case(s) ...` |

Name resolution in a fn body (`_lower_pure_expr` `ExprVar`): a name must be in
`scope` or in `callables` (`_HOST_CALLABLES` ∪ `_BUILTIN_CONSTRUCTORS` ∪
`endorse` ∪ module fns ∪ externs, per-module for a `use`d fn), else the
item-384 foreign redirect fires, else `` `x` is not declared in this
function ``. The method branch refuses `no builtin method X on values` with
the sorted stdlib surface, `builtin X takes N argument(s)`, the literal
zero divisor, and the unpinned-receiver `HOST-METHOD` refusal.

### 2.4 Provide-method and component bodies

The component dialect is typed by `infer_ir`/`check_ir` over the lowered
node rather than the AST, but the obligations are the same algebra with a
different environment: a method's parameters take the SERVICE declaration's
types (`t7`: an annotation that disagrees with the service is refused), the
method body is checked against the service's return (`t16`: `` `get`
implements `Store.get`, which returns `Str`, but this body never returns a
value ``), a required-service call checks its arguments against the method
signature (`t1`, `t4`: `` `db.query` argument `sql` expects `Str`, got `Int`
``), `a6`: `` `db.execute` is not a method of service Database ``, a config
default is checked against the field type (`t3`), and a method-local binding
may not shadow a component name (`g6_method_local_shadows_component`).
`unknown service `S` in `requires`/`provides` of C` belongs here too (it is
the standalone refusal the harness's `cache_layer` candidate gets, and the
crate's `TYPE_LAYER_GAP` list carries the `provides` twin).

`selfhost/checker.rvl` slice two (`check_service_src`) already ports a part of
this message-for-message (G4 declaration bound, required-service argument
typing, bare fn call arity/type). Section 3.5 says what happens to it.

### 2.5 Not the type layer

These reference checks are reachable from a program the type layer admits
and are NOT ported by this design. They matter because section 3.6's
`Admitted` arm must decline any program that can reach one of them:
typed holes (`T3`, `refuse_admission`), taint (`check_taint`, G9), ownership
O1/B1/R0 (item 308, deferred by `tests/test_selfhost_ownership_gap_308.py`),
extern `undo`/`compensate` slot validation and the G5 inverse-emission walk,
witnessed/deferred/approval externs (243/246/399/400), cache admission
(`_check_cache_declarations`, beyond the `check_cache_fns` twin), the
parameterized spawn cone (294), events and streams (130), `lifecycle`/`prop`/
`fault` tests, secrets (256), poly externs (388), `use` modules.

### 2.6 Ordering

`admit_src` returns the first refusal by PHASE; the reference (item 386)
collects and orders by LINE. `test_which_refusal_wins_diverges_when_a_program_
has_several` pins that as a known divergence (419c) and the corpus stays
single-refusal. The type layer inherits that discipline: its phases are
inserted at the reference's positions (2.1 for declarations and fn bodies,
inside `check_component` for method bodies, before that component's G4/A1
verdict), and no corpus program carries two refusals. Within one fn body the
statement order IS the line order, so the two implementations agree there.

## 3. Where it lands

The stage map: `parser.rvl` and `checker.rvl` are expression-only, fn-body
parse and lowering live in `lower.rvl`, and a feature ports to the file whose
oracle covers it. Applied here:

### 3.1 `selfhost/types.rvl` (new leaf module)

The type-SPELLING algebra, with no AST dependency: `parse_type`/`format_type`
(head and top-level args, paren-aware for fn types), `structural_fields`/
`format_structural` (sorted), `render_type`, `is_wildcard`, `is_poison`,
`mark_tparams`/`collect_tparams`/`validate_explicit_tparams`, `compatible`,
`join`, `widen_bottom`, `unify`/`substitute`, `nominal_record_fields`,
`check_type_wellformed` (message shapes), and the expanded-alias reader
checker.rvl slice three carries today. Both `checker.rvl` and `lower.rvl`
currently carry private copies of parts of this (`split_type`/`expand_ty` in
one, `parse_head`/`type_args`/`split_top_type_commas`/`struct_field`/
`fn_param_types` in the other); they switch to the shared module and the
copies are deleted. Its oracle is new and independent of any AST: generated
type strings against `typecheck.py`'s functions.

`use`d by checker.rvl and lower.rvl. Joins the crate closure and the crate's
`DIGEST_INPUTS`.

### 3.2 `selfhost/checker.rvl`: the expression algebra, exported

`infer_t`/`check_t` over the full `Expr` ADT (2.2), against a typed
environment and a program table (4.1, 4.2), returning `Infer` (4.3). Method
resolution (`resolve_method`, 4.4) and the call-site signature check live
here because they are expression typing. The existing `infer_t(e, env, tt,
ct)` grows into this; `base_env`/`infer_expr_str`/`infer_prog_expr` stay as
the oracle entry points. `pub` everything lower.rvl needs; the file remains
expression-only (no statement reader).

### 3.3 `selfhost/lower.rvl`: the walk that refuses

`lir_one_stmt`/`lir_stmts`/`lir_function` gain a refusal channel:
`StmtOne` gains `refuse: Str` (`"<TAG>|<message>"`, `""` when clean) and
`lir_stmts` stops at the first non-empty one. `lir_function` runs the
declaration-level obligations for its fn (duplicate params, body, returns on
every path) and `fns_walk` the program-level ones (2.1). A new

    pub fn lower_checked(src: Str) -> LowerR   // { verdict: Str, ir: Str }

runs lex -> foreign scan -> nesting bound -> parse -> declarations -> fn bodies
-> the existing composition phases -> link, producing the IR in the same pass.
`admit_src` becomes `lower_checked(src).verdict` and `lower_to_ir` its `.ir`,
so `compile.rvl` calls the front end once and there is exactly one typed walk
(today `compile_to` lexes and walks twice, and `lower_to_ir` has no refusal
path at all, which is the wave-through the crate's `compile_to` must never
inherit).

The provide-method twin lands in `cir_method_stmts`/`cir_prov_methods`, the
component-body IR walk, with the service signature as the environment (2.4),
placed inside `check_component` ahead of the G4/A1 verdicts.

`admit_into(src, manifest_json)` lands beside `admit_src` (section 3.6, 4.5).

### 3.4 The composition

`lower.rvl` gains `use "./checker.rvl" { infer_t, check_t, ... }` and both gain
`use "./types.rvl"`. Item 228 keeps each file's private `Bind`/`Stmt`/`FnD`/
`Prog`/`Ctx` from colliding; checker.rvl's `pub type Prog`/`TypeD`/`CaseD`
are only read by its oracle and are renamed (`CkProg`, ...) if the merge
reports a duplicate. `tools/build_gate_crate.py` `SELFHOST_CLOSURE` and
`DIGEST_INPUTS` add `types.rvl` and `checker.rvl` (and `stdlib/json.rvl` at
T5). `test_three_way_composition_co_compiles` and the crate drift gate are
the guards; the composition change is made in the first slice that needs it
(T1) with zero behaviour change, so a composition failure is isolated from a
typing failure.

### 3.5 What happens to checker.rvl slice two

`check_service_src` is a second, partial implementation of provide-body
typing with its own copies of `p_service`/`p_component`/`p_stmt_run`. After
T4 the single owner of body typing is lower.rvl's `cir_*`, and
`check_service_src` becomes a wrapper over `lower_checked` restricted to the
component phase (keeping `tests/test_selfhost_checker.py` slice two and
`tests/test_selfhost_ownership_gap_308.py` green without a second walk to
maintain). Its private parser copies are deleted in the same slice. This is
a decision to confirm (section 8); the alternative is to leave it as a
frozen oracle and accept the duplication.

### 3.6 The crate and the frontier

`admit` today: lexical frontier scan (keywords the self-host cannot lex,
builtins it does not lower) BEFORE the gate, then `Refused` / `NoObjection`
/ `OutsideFrontier`. After the type layer, `""` from the self-host still does
not mean the reference admits, because of 2.5. So:

* the generator gains a FAMILY registry: `{family, reference site, trigger
  tokens, ported: bool}` for every check family in 2.5 (and, until each is
  ported, the type-layer families too). A family's trigger is a token shape
  whose absence proves the family unreachable: `hole`; `Untrusted[`/
  `Trusted[`/`Secret[`/`endorse`; `witnessed`/`deferred`/`requires approval`;
  `undo`/`compensate` on an `extern` declaration; `cache`; `spawn ... with {`;
  `event`/`subscribe`; `lifecycle test`/`prop test`/`fault test`; `secret`;
  `fn|async`; `use`. Ownership (O1/B1) has no cheap syntactic proxy short of
  "any `effect` in a component", and that is its trigger until item 308's
  port lands: the `Admitted` arm covers pure fn/type programs and
  effect-free components first, which is exactly what Stage 4's fn corpus
  needs.
* `frontier.rs` runs the lexical tables before the gate (unchanged) and the
  family table AFTER it, only on an empty wire: a refusal is sound regardless
  of what else the program reaches and keeps flowing; an empty wire with a
  fired family becomes `OutsideFrontier { reason: "<family> is not decided
  natively" }`.
* `Verdict` becomes `Refused | Admitted | OutsideFrontier`; `NoObjection` is
  removed (its meaning, "no type layer", no longer exists); `to_json` emits
  `"admitted": true` on `Admitted` only; `COVERED_LAYER` and the README's
  "This gate issues no admissions" section are rewritten; `GATE_API_VERSION`
  moves to `2.0.0` on both tiers together (the generator enforces lockstep).
* the census's python mirror imports the family table like it imports the
  lexical ones, and gains a zero-tolerance bucket `false-admission` (gate
  `Admitted`, reference refuses for ANY reason). That bucket is the security
  clause after the flip, and it is held over the >300-program census corpus
  plus the fuzz draw on every PR.

## 4. Data

### 4.1 The program table `Tbl` (built once per program)

    types:     Map[Str, TySpec]      // record {params, fields: Map} | variant {params, cases}
    cases:     Map[Str, CaseInfo]    // case -> {adt, payload}, builtins seeded, ambiguous dropped
    fns:       Map[Str, FnSig]       // fn AND extern: {params (marked), returns (marked), tparams, required, defaults: List[Expr]}
    callables: List[Str]             // host roots, Some/None/Ok/Err, endorse, module fns, externs, user ctors
    services:  Map[Str, SvcSig]      // name -> {methods: Map[Str, {params: List[ParamN], returns, isEm, isAsync, caps}]}
    hostFams:  (constant)            // _HOST_FAMILIES / _HOST_RESULT_SIG twins
    ambient:   AmbientR              // 4.5, empty for admit_src

`fns` replaces the `List[Bind]` pseudo-entries lower.rvl uses today
(`tenv_get(env, "field T.f")`, `"payload N"`); those hacks are deleted in
T3c. `services` carries TYPED method signatures, which `lower.rvl`'s `MSig`
does not today (it keeps only emission/async/caps); the composition-gate
`SvcD` stays as is and `SvcSig` is built beside it.

### 4.2 The body environment `TEnv` (threaded through a walk)

    binds:    Map[Str, Str]     // name -> type, block-scoped by value copy
    scope:    Map[Str, Str]     // name -> "let" | "var" | "host" (the reference's scope dict)
    expected: Str               // the fn's declared return, "" for none
    arrows:   Map[Str, ArrowRes] // token index -> {paramTypes, returns, async} (4.3)
    where:    Str               // "fn f" | "Comp.method" for the where-strings that name the owner

The reference mutates AST nodes to remember an arrow's resolved signature
(`expr.param_types`, `expr.resolved_type`, `pin_hole` -> `known_type`). A
pure port cannot; `ArrowN` gains `tok: Int` (its opening token index, set by
the parser like `own_marks` keys ownership births by token) plus the written
annotations (`ptys: List[Str]`, `ret: Str`), and the resolution is recorded
in `arrows` by the checking position that decides it. `lir_arrow` reads it
back for the IR's `param_types`/`returns`/`async`. Holes work the same way
(`HoleN.tok`, a `holes` map).

### 4.3 The result `Infer`

    type Infer = { ty: Str, ok: Bool, tag: Str, msg: Str, env: TEnv }

`ty == ""` is the reference's `None` (unknown); `ok == false` carries the
refusal with its tag; `env` returns the environment because arrow/hole
resolutions and bottom-learning (`[].push("s")` retypes the receiver) are
environment updates. The non-raising mode (`filename=None` in the reference,
used for annotations) is `infer_t` with `raise_: false`, which never sets
`ok == false`.

Tags (the `_classify` extension, item 429's territory, landed in T0):
`e.code` when it is `T1` or `T2`; `G1` for `is not declared in this function`
and `unknown service `; `G6` for `is already declared`, `cannot reassign`,
`is bound here and called in this body`, `already bound in`; the codes
`revl.diagnostics.classify` already assigns by pattern (`A6` for `is not a
method of service`, `G7` for verified totality, `T1` for `non-exhaustive
match`, `has no field`, `takes N argument`, `no builtin method`); and one
new append-only tag `TYPE` for the code-less remainder (`is not a case of`,
`record update names`, `record update requires`, `ternary branches disagree`,
`cannot order`, `type alias cycle`, `Map.empty() takes no arguments`,
`duplicate function|parameter|type|field|case`, `record destructuring
requires`, `iterates a List`, the two literal-range messages, `mod by a
literal zero`, `bare return`). `HOST-METHOD` is already a reference code and
passes through. Open question: promote the `TYPE` family to reference codes
later so the tag is the code on both tiers (section 8).

### 4.4 Method resolution order (exactly the reference's)

For `recv.m(args)`: (1) `Map.empty()` on the bare `Map` root; (2) a host
constructor family root not shadowed by a local (`Map.new`, `Pool.open`, ...)
-> `host_check`, result is the family name; (3) a receiver whose static type
is a host family -> `host_family_check`, result from `_HOST_RESULT_SIG` or
unknown; (4) `m` in `LIST_TRANSFORMS` and the receiver is not a host family ->
desugar to the free function and type that call; (5) `builtin_check` by
`_BUILTIN_SIG` row (single family or per-receiver-head rows); (6) at
LOWERING (not inference), `no builtin method`, arity, zero divisor, the
unpinned-receiver refusal, and the `Value` accessor redirect. Steps 1 to 5 are
checker.rvl; step 6 is lower.rvl's `lir_builtin`, because that is where the
reference does it and the messages differ.

### 4.5 The manifest value for `admit_into`

`admit_into(src: Str, manifest_json: Str) -> Str` takes the same JSON the py
gate takes: a compiled IR document, or its `{manifest, services}` projection.
Parsed with `stdlib/json.rvl` (`json_parse` has `@py` and `@rs` bodies; the
crate takes a `serde_json` dependency, the one cost). Fields read, and only
these:

* `services`: `name -> {methods: {name -> {params: [{name, type}], returns,
  emission, capabilities?, async?}}}` -> `Tbl.services` and the composition
  gate's `SvcD`, so a candidate's `requires store: Store` resolves and its
  call sites are typed against the RUNNING signature. A service the candidate
  redeclares is admitted only through `_admit_service_replacement`'s rules
  (the `differs from the running manifest` G2 refusal against running
  consumers/providers; needs `provision_services`, which is `components[].
  provides` as `{key: service}` when the full document was supplied, else
  every key is treated as unresolved, as the reference does);
* `manifest.components` (or `components` when only a projection was given):
  `{name, inject, provides, isolate?, intercept?, routes?, boot?}` -> ambient
  `Prov3` entries and G3 edges in `link_g2_g3`, and the `boot` count. A
  candidate component with a running component's name REPLACES it (the entry
  is dropped from the ambient set before linking), the hot-swap rule;
* `components[].handoff`: not ported; a manifest carrying one yields
  `OutsideFrontier` (the handoff-replacement family), never a guess.

Anything else in the document is ignored, as the reference ignores it.
`admit_src(src) == admit_into(src, "{}")` by construction and is pinned.

### 4.6 The wire after the flip

`"<TAG>|<message>"` and `""` are unchanged, so `gate.py`'s
`Verdict.from_native` stays valid. The crate maps `""` to `Admitted` only
when the family frontier is silent; otherwise `OutsideFrontier` with the
family named. `bench/inprocess_gate_rust` reports an `into` verdict per
manifest-batch candidate beside the standalone one.

## 5. Slice plan

Each slice is independently landable, additive on the IR bytes (the
`test_selfhost_lower_ir.py` and `test_selfhost_compile.py` byte-exactness is
the standing guard on every one), and carries its oracle in the same PR. A
slice that introduces a tag pins it with in-file `test` blocks AND the
`_classify` marker (item 429). Sizes are in lines of `.rvl` to write, as a
guide to dispatch.

**T0. Name the gap.** `_classify` learns the 4.3 vocabulary; the census is
re-recorded and `KNOWN_BYPASSES` in `tests/test_gate_reference_census.py`
lists every fixture from section 1's table by name under its family; a
`TYPE_LAYER_GAP` test in `tests/test_selfhost_lower.py` in the shape of
`test_selfhost_ownership_gap_308.py` asserts the divergence per family
(reference refuses with tag X, self-host returns `""`) so each later slice
flips its family by deleting lines. No `.rvl` change. Oracle: census
`--check` green with the named list; the pin test green. ~150 lines of
Python.

**T1. `selfhost/types.rvl`.** The spelling algebra (3.1) and the composition
change (3.4): both checker.rvl and lower.rvl `use` it and drop their copies;
crate closure and digest inputs updated; crate regenerated. Oracle: new
`tests/test_selfhost_types.py`, a differential over generated type strings
(scalars, `Opt`/`Result`/`List`/`Map`, fn types incl. `Async`, structural
records, `Never`/`Any`/`Value`, marked `?T`) for `compatible`, `join`,
`widen_bottom`, `unify`+`substitute`, `format_type`/`parse_type`,
`structural_fields`, `render_type`, `check_type_wellformed` messages, plus a
fuzz draw; IR byte-exactness unchanged; `test_three_way_composition_co_
compiles` green; drift gate green. ~600 lines.

**T2a. Expression typing with messages: operators, fields, index, ternary,
lists, records.** checker.rvl `infer_t`/`check_t` over those kinds with the
`Infer.tag`/`msg` channel; Int/Float literal range. Oracle:
`tests/test_selfhost_checker.py` compares the MESSAGE on refusal (today only
the verdict), corpus extended per row of 2.2, fuzz over typed binops
extended with fields/index/records. Fixtures flipped: `t2`, `t11`, `t12`,
`t21`, `t22`, `t23`, `t28`, `t26`, `t27`, `t36` (flipped in the pin test only;
they reach `admit_src` at T3a). ~500 lines.

**T2b. Calls and signatures.** `FnSig` table with marked tparams, arity
window and defaults, monomorphic and generic call typing, ADT case calls,
`builtin_check` with bottom learning and per-receiver rows, host families,
`Map.empty`, list-transform desugar. Oracle: checker corpus with a program
prefix (`infer_prog_expr` already takes one) per shape; fuzz over
signatures. Fixtures: `t10`, `t15`, `v2_map_set_value_mismatch`, `t24`,
`host_method_not_on_surface`, `g4_extern_undo_wrong_arg_type`. ~600 lines.

**T2c. Arrows and function values.** Parser: `ArrowN.tok`/`ptys`/`ret`,
`HoleN.tok`. Checker: §3.1/3.2 inference, `_check_arrow`,
`call_function_value`, `refuse_self_declared_async`, the `arrows` map.
Oracle: checker corpus (the reference's arrow fixtures in
`tests/test_function_types*.py` are the seed) and `test_selfhost_parser.py`
for the parser additions. Fixtures: `t17`, `t32`, `t33`, `t35`, `t34`. ~400
lines. Hardest expression slice; ordered after T2b because it needs the
signature table.

**T2d. Match, record update, optional chaining.** Arm payload typing from
the variant table and `Opt`/`Result` args; record-update rules; `?.` rules.
Oracle: checker corpus. Small (~250 lines); can run in parallel with T2c.

**T3a. The fn-body statement layer.** `lir_*` gains the refusal channel and
`TEnv`; `let`/`var`/assign/compound/return/`if`/`while`/`for`/`assert`/expr
rules of 2.3 including block scoping; name resolution (G1) and the
lowering-time method refusals (4.4 step 6); `lower_checked` and the
`admit_src`/`lower_to_ir` projections; `compile.rvl` switched to one call.
Oracle: `tests/test_selfhost_lower.py` `REJECTED_PROGRAMS` gains the
fixtures of families 1 to 3 of section 1 (via `_fixture`), plus a generator
`_typed_fn_body` for the single-line fuzz variants; census re-recorded with
those names removed from `KNOWN_BYPASSES`; pin test lines deleted. ~500
lines. Depends on T2a/T2b.

**T3b. Totality, exhaustiveness, declarations.** `_check_returns_on_every_
path`, `_check_match_exhaustiveness` and unknown-case, `duplicate function`/
`duplicate parameter`, `_lower_type_decls` duplicates, alias cycle, wellformed
declared types at every site, `_refuse_callable_shadowing`, verified
totality, the parser strictness that refuses `fn f() -> { }` with the
reference's message. Oracle: lower corpus; fixtures `t8`, `t9`, `t13`,
`v2_match_nonexhaustive`, `t18`, `t6`, `t5`, `shadowed_module_fn_call`,
`g6_closure_mutates_capture`. ~400 lines. Independent of T3a except for the
shared channel; can be dispatched in parallel with it if T3a's channel lands
first as a tiny preparatory PR.

**T3c. One engine.** Delete lower.rvl's private `infer`/`binop_ty`/
`builtin_ret`/`join_ty`/`infer_field`/`infer_callee`/`operands_of` and the
pseudo-binding hacks; the IR annotations come from `infer_t(raise_: false)`.
Oracle: IR and emitted-bytes byte-exactness over every corpus (`test_selfhost_
lower_ir.py`, `test_selfhost_compile.py`, every `test_selfhost_emit_*.py`),
`tools/selfhost_differential_survey.py` unchanged. Net negative lines; its
whole value is that two inference engines can no longer drift.

**T4. Provide-method and component bodies.** `cir_*` typed against the
service signature (2.4); `unknown service` in `requires`/`provides`; config
defaults; method-local shadowing; `check_service_src` becomes the wrapper of
3.5 and its parser copies go. Oracle: lower corpus with fixtures `t1`, `t4`,
`t7`, `t16`, `t31`, `t3`, `a6`, `g6_method_local_shadows_component`; the
checker slice-two tests and `test_selfhost_ownership_gap_308.py` unchanged
and green. ~500 lines.

**T5. `admit_into`.** lower.rvl `admit_into` per 4.5; `stdlib/json.rvl` in
the crate closure; the crate exports `admit_into(source, manifest_json)`;
`revl.gate` untouched. Oracle: new `tests/test_selfhost_admit_into.py`
against `revl.gate.admit_into` on (running, candidate) pairs: the harness's
`RUNNING`/`CANDIDATE`, `_REDECLARE_RUNNING`, `_CALLS_MISSING_METHOD`, a
same-name replacement, a second `boot`, a per-realm conflict, plus a
generator; `admit_src == admit_into(_, "{}")` pinned. ~400 lines plus the
generator template.

**T6. The `Admitted` arm.** The family registry and post-gate family scan
(3.6); `Verdict` rewrite; api `2.0.0` lockstep; census `false-admission`
bucket; `bench/inprocess_gate_rust` manifest batch; the crate README. Oracle:
section 6. Rust template and Python only, no `.rvl`. ~400 lines.

Dependency order: T0 -> T1 -> {T2a, T2d} -> T2b -> T2c -> T3a -> {T3b, T3c,
T4} -> T5 -> T6. Parallelisable pairs: T2a with T2d; T3b with T3a once the
channel exists; T4 with T3c. Stage 4 `compile_to` (item 332) starts after T3c
and needs T6 to export an admission; it is not in this plan.

## 6. The exit test

`tests/test_inprocess_gate_rust.py::test_the_manifest_gap_is_priced_not_
hidden` is replaced by `test_the_manifest_gap_is_closed`, which holds, on the
identical bytes the py harness screens:

1. `admit(cache_layer)` on rust is `Refused` with `code == _classify(ref)`
   and `message == ref.message` for the py STANDALONE refusal (`unknown
   service `Store` in `requires` of CacheLayer`), no longer a no-objection.
2. `admit_into(cache_layer, json.dumps(base_manifest()))` on rust is
   `Admitted`, `"admitted": true` on the wire, and `py_gate.admit_into(...)
   .admitted is True` for the same bytes. Two questions, and rust is now
   asked both.
3. For every candidate in the manifest batch (`cache_layer`,
   `_REDECLARE_RUNNING`, `_CALLS_MISSING_METHOD`, `standalone_twin`), the
   rust `into` verdict equals the py `admit_into` verdict on
   `(admitted, tag, message)`.

Beside it, `test_the_measured_layer_gap_is_real_and_never_reads_as_an_
admission` becomes `test_every_rust_admission_is_a_py_admission`: for every
candidate the rust gate returns `Admitted`, `py_gate.admit` admits the
identical bytes (zero tolerance, the release-blocking direction stated
positively), and the hole draft is `outside_frontier` with a reason naming
the holes family. `test_the_gate_surfaces_are_kept_in_lockstep` asserts
`layer` no longer contains `NOT the reference type layer` and both `api`
strings read `2.0.0`.

In `tests/test_gate_crate_admit.py`, `TYPE_LAYER_GAP` empties into
`test_crate_and_reference_agree`'s cases (each probe refused with the
reference's tag and message), and `test_the_crate_issues_no_admission_for_
anything_in_the_corpus` becomes `test_every_admission_is_a_reference_
admission` over `ACCEPTED_PROGRAMS` (each admitted, or declined with a named
family, never refused) and `REJECTED_PROGRAMS` (none admitted). The census
`--check` holds `false-admission == []` and `false-admit/* == []` over the
whole corpus and the fuzz draw. When all of that is green on the landed sha,
item 417's remaining exit is met and 332 Stage 4 is unblocked.

## 7. The hardest sub-problems, and how each is de-risked

1. **Arrow resolution without AST mutation.** The reference records the
   checking position's decision on the node and reads it back at lowering.
   De-risk: token-indexed side tables (4.2), the same device `own_marks`
   already uses in lower.rvl; the parser addition is a separate oracle
   (`test_selfhost_parser.py`) so a mismatch is attributed to the right file;
   T2c is ordered last among the expression slices and its fixtures include
   the item-75(a) rule C1/C2 cases (`t34`) so the async-colour certificate is
   pinned, not inferred.
2. **Generic instantiation.** `unify`/`substitute` over string spellings with
   the `?` marker, the implicit single-uppercase rule vs an explicit `[T]`
   list, and `render_type` stripping. De-risk: it is string algebra with no
   AST, so it ports first (T1) under a fuzzable oracle; T2b's call typing
   consumes it as a black box.
3. **Byte agreement across ~60 message shapes.** Sorted field lists, the
   `_` placeholder for an unknown param type, `render_type` of `None`, the
   `hint`-carrying `where` strings. De-risk: every slice's corpus is the
   reference's own fixtures plus a generator whose ill-typed draws are
   compared on the message too; the census compares over the whole tree on
   every PR (`msg-mismatch/<tag>` is a bucket), so a shape that drifts in a
   later reference change reds CI rather than the crate.
4. **Refusal ordering (419c).** Phase order vs line order. De-risk: phases
   inserted at the reference's positions; the corpus stays single-refusal;
   the type layer adds no new multi-refusal fixture; the divergence test
   stays as the one place it is written down. A collecting sink with line
   numbers on every gate refusal is item 186's deferred work and is not
   smuggled in here.
5. **Two inference engines during the transition.** Between T2 and T3c the
   IR-annotation engine and the refusing engine coexist. De-risk: T3c is a
   named slice with a negative diff; until it lands, the byte-exactness
   guards prove the annotation engine unchanged, and T3a's refusals are
   pinned only through the lower oracle.
6. **Literal ranges without big integers.** i64 bounds as a digit-string
   comparison (length, then lexical, sign-aware); Float infinity as a
   normalized decimal exponent above 308 (the reference asks Python's
   `float`). De-risk: a boundary fuzz in T2a's oracle (`9223372036854775807`,
   `...808`, `-...808` written as negation, `1e308`, `1e309`, `0.1e310`,
   mantissa-shifted forms); today the self-host CRASHES on the Int case
   (item 391), so any port is an improvement and the crate's `catch_unwind`
   stays the backstop.
7. **The `Admitted` arm's soundness.** The family table is a claim about the
   reference's reachability, and a missing trigger is a false admission.
   De-risk: the table is a registry the census reads too, so "family X is
   unreachable without token Y" is measured over every corpus program that
   the reference refuses under family X (each must carry Y); a family with a
   refusal the census finds trigger-free cannot be marked `ported: false`
   with that trigger, the generator refuses. Ownership's coarse trigger
   (`effect`) is deliberately conservative; narrowing it is item 308's port.
8. **Cost.** The rust screen already grows roughly with the square of the
   token count (333's finding 3). The type layer must not add a second lex
   or parse: `lower_checked` walks the token stream once and the `Expr` tree
   once per statement. De-risk: `bench/inprocess_gate_rust` and
   `tools/bench_selfhost.py` run per slice; a slice that more than doubles
   the representative screen is not landed without a measured reason.

## 8. Decisions to confirm

* `checker.rvl` slice two retired into a wrapper at T4 (3.5), versus kept as
  a frozen second oracle.
* `NoObjection` removed at T6 with an api bump to `2.0.0` on both tiers,
  versus kept as a never-returned arm under `1.1.0`.
* The `TYPE` tag as an oracle-only vocabulary, versus giving the reference a
  `code` on every code-less type-layer refusal so `Verdict.code` agrees on
  py and rust without a classifier (preferred long-term; changes
  `revl compile --json` output, so it is its own reference-side item).
* `admit_into` takes JSON through `stdlib/json.rvl` (adds `serde_json` to the
  crate), versus a purpose-built line format the py side would render.
* Ownership's `Admitted` trigger is `effect` in any component (coarse) until
  item 308 ports O1/B1.

## 9. Non-goals

Stage 4 `compile_to` on the crate (the `@rs` emitter helper externs, item
332); item 186's collecting sink and per-refusal line numbers; the
parser-family and extern-slot fixtures listed in section 1; ownership,
taint, holes, approvals, events, secrets, poly externs (each stays a named
family in the frontier until its own port); any change to `revl.gate`'s py
surface beyond the api string; the LSP navigation surface.
