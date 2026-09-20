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

Of that table, the return-path pair (`t8_missing_return`,
`t9_return_path_incomplete`) has since LANDED — see T3b(returns) in section 5 —
so "return paths and match" now stands at its match half alone.

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
crate's `TYPE_LAYER_GAP` list carried the `provides` twin).

**That one has LANDED, ahead of T4** — see T4a in section 5, and T4b for
`a6`, the member half of the same resolve-a-name-against-a-declaration shape. It is the only
obligation in this section that needs no expression algebra: the component
header carries a name, and the decision is whether the program declares a
service by that name. The two exemptions it costs are written into the code:
a parameterized annotation (`Stream[T]`) is not a service, as the reference
also has it, and a text carrying a `use` declaration has no knowable service
set because a module can export a service and this gate does not read modules.
Both under-refuse, which is the direction the gate is allowed to err in.

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

**T1 LANDED** (the composition half, plus the declared-type phase it exists
for). `selfhost/types.rvl` landed on its own first, as a leaf module with its
own oracle and an explicit note in its header that the checker/lower `use` was
deferred. That deferral is closed: `selfhost/lower.rvl` now carries

    use "./types.rvl" { WfR, check_type_wellformed }

and `tools/build_gate_crate.py`'s `SELFHOST_CLOSURE` (and therefore
`DIGEST_INPUTS`) names `stdlib/str.rvl`, `stdlib/list.rvl` and
`selfhost/types.rvl` ahead of the three files it named before. Both crates are
regenerated and `tests/test_gate_crate_admit.py` builds the result with cargo.

Read this before the next slice, because two of the three findings are about
the COMPOSITION and not about types:

1. **A private record type can silently eat an ADT case constructor.** Item 228
   keeps each file's private `Bind`/`Stmt`/`Prog` from colliding, and section
   3.4 assumed that covers the merge. It does not cover a CASE NAME.
   `types.rvl` declared `type Field = { name: Str, ty: Str }`; `parser.rvl`
   declares the expression node `| Field(FieldN)`. The merged program compiles
   with no diagnostic, the record wins, and every `Field(...)` the gate builds
   becomes a call to a two-field record constructor: 415 of the 761 census
   programs faulted with `Field() takes no arguments`, and NOTHING but the
   census showed it (every unit oracle was green, because each file's own tests
   run the file alone). `types.rvl`'s type is now `TyField`. Before adding a
   `use` edge into `lower.rvl`, diff the new module's type names against
   `parser.rvl`'s `Expr` cases, and run the census rather than a unit suite.
2. **The `use` is cheap; the closure is the cost.** Adding `types.rvl` pulled
   `stdlib/str.rvl` and `stdlib/list.rvl` into the crate's emitted rust. That
   built and passed `test_gate_crate_admit.py` unchanged, so the remaining
   `use`-and-drop work (checker.rvl, and deleting lower.rvl's private
   `parse_head`/`type_args`/`split_top_type_commas`/`struct_field`/
   `fn_param_types` and checker.rvl's `split_type`/`expand_ty`/`compatible`) has
   no crate-shaped obstacle left in front of it. It is deliberately NOT in this
   change: it moves no census document and it rewrites call sites inside the IR
   walk, where the guard is byte-exactness rather than a verdict.
3. **`false-admit/T1` is the reference CODE T1, not this slice.** The 31
   documents in that census bucket are what the reference's type checker refuses
   with `code="T1"`; slice T1 is the spelling algebra. Exactly one of the 31 is
   a declared-type question (`t6_bare_generic`), and it is the one that moved.
   The other 30 are T2a/T2b/T3a/T3b work and no amount of T1 reaches them.

The phase this slice added is `_validate_declared_types` itself, ported far
enough to be worth having: `declared_types_refusal` runs
`check_type_wellformed` over every module `fn` and `extern` signature
(parameters then return, fns before externs — the reference's own loop order),
and `config_data_refusal` now asks the wellformed question per config field
ahead of the is-data question, which is where `_check_config` asks it. It is a
FAIL-FAST phase at the head of `collect_nonlink`, because the reference RAISES
inside `_validate_declared_types` before its collect-all sink exists — so a
declared-type refusal beats an earlier-LINE link refusal and rides alone. That
is checked and not assumed: three programs in `_MULTI_REFUSAL_PROGRAMS` pair it
with a duplicate provider (on an earlier line and on a later one) and with a
config-is-data refusal in the same component.

What the phase does NOT carry, and why:

* **service-method and type-declaration sites.** `_validate_declared_types`
  also walks service method params/returns and type-decl fields/case payloads.
  No census document turns on them, so they are fail-open surface rather than a
  measured divergence, and each needs its own line-bearing token scan. They are
  the obvious next few lines of this file, not of a later slice.
* **the `Async` ARITY refusal.** ```Async` takes 1 type argument, got N``
  is singular, so it misses `_classify`'s code-less ``type argument(s), got``
  marker and has no tag in the gate vocabulary. `wf_tag` returns `""` there and
  the site is left unrefused: withholding is the safe direction, and refusing
  under a tag the oracle cannot compare is not.
* **`Delegate[S]` and `Criterion`/`Guard`.** `types.rvl`'s
  `check_type_wellformed` predates both reference branches and does not model
  them. That makes it strictly more permissive than the reference, never less,
  which is why no false-reject appeared; porting them is a `types.rvl` change
  with its own oracle.

Census: `false-admit/T1` 31 -> 30, `agree-refuse/T1` 0 -> 1, every other bucket
byte-identical (no new `false-reject`, no `gate-fault`). The one document moved
is `examples/rejections/t6_bare_generic.rvl`, struck from `TYPE_LAYER_GAP`
(42 -> 41) and from `KNOWN_BYPASSES`, and folded into `REJECTED_PROGRAMS` with
eight neighbours covering the other declared-type sites.

**T2a. Expression typing with messages: operators, fields, index, ternary,
lists, records.** checker.rvl `infer_t`/`check_t` over those kinds with the
`Infer.tag`/`msg` channel; Int/Float literal range. Oracle:
`tests/test_selfhost_checker.py` compares the MESSAGE on refusal (today only
the verdict), corpus extended per row of 2.2, fuzz over typed binops
extended with fields/index/records. Fixtures flipped: `t2`, `t11`, `t12`,
`t21`, `t22`, `t23`, `t28`, `t26`, `t27`, `t36` (flipped in the pin test only;
they reach `admit_src` at T3a). ~500 lines.

**T2b. Calls and signatures. LANDED, in `lower.rvl` rather than
`checker.rvl`.** The obligation is expression typing, but the nine documents it
owns are measured through `admit_src`, and T3a had already built the refusing
expression walk there — so the slice extends that engine instead of starting a
second one. What it adds: a signature table for every module `fn` AND `extern`,
built in a second pass over the token stream (the single-uppercase heuristic is
switched off by a DECLARED type, and a `type` may be written after the `fn` that
mentions it), with each signature's type parameters marked once; the arity
window; `compatible` per argument for a monomorphic signature and
`unify`/`substitute` for a generic one; the host stub surface at both its
positions (`Root.verb(..)` as a constructor, whose result is the FAMILY, and a
method on a family-typed receiver); `builtin_check` with its receiver-family
rows, `@elem`/`@member`/`@self` specs, bottom learning and the `List[Str]`
constraint on `join`; and §4.4 step 6, the four refusals the reference makes
while LOWERING a method call rather than while typing it, as a second walk over
the same statement expression run only after the checking walk is clean.

Its premises are proven or it says nothing. A signature row is built only for a
parameter list this reader can spell in full, so a default value (item 187)
withholds the arity window rather than counting a call short. A name the body
rebinds is not resolved against the module declaration, because the reference
reads a local of function type first. The builtin surface, arity and
zero-divisor rules need the receiver PROVEN a stdlib value; the
unpinned-receiver refusal needs it PROVABLY untyped, which a binding whose
initialiser calls a declaration that returns nothing is and a type this walk
merely failed to infer is not.

What it buys, measured. All nine documents moved from `false-admit` to
`agree-refuse` on tag AND message: `t10_call_arity`, `t15_generic_call_site`,
`t25_explicit_tparam_heuristic_off`, `v2_map_set_value_mismatch`,
`v2_map_value_unknown_method`, `g4_extern_undo_wrong_arg_type` (the extern
inverse slot runs `check_ast` over its expression, so its call arguments are
typed like any other call site), `arith_zero_divisor`, `t24_opaque_receiver_
builtin` and `host_method_not_on_surface`. `false-admit/T1` 20 -> 14,
`false-admit/HOST-METHOD` 2 -> 0, `false-admit/TYPE` 4 -> 3; nothing entered a
false-admit bucket and `false-reject` is unchanged. Over 1500 drawn
call-carrying fn bodies, both gates run on the SAME inputs, agreement went 117
-> 1488 and message mismatches 811 -> 0.

A FALSE REJECTION it closed on the way. `case_binds` spells a module `fn`'s own
name as a function type and gives a returnless `fn` the return `Unit`, which is
the IR's spelling; the reference's signature table records `None` there, so
`assert nores(1)` reads as an unknown condition and is ADMITTED. Reading `Unit`
back off the function type refused it. The signature table's own return row is
empty and the call types unknown.

One divergence CLASS is left, pinned as a test: the reference desugars a
receiver-first list transform to its free function and then refuses that
undeclared NAME, which is the G1 name-read family no slice has built. The gate
refuses later, or not at all, which is the under-refusing direction. ~750 lines.

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

**T3b(returns). Returns on every path. LANDED, ahead of T3a and without its
channel.** The return rule needs no channel and no expression algebra: it is a
property of the STATEMENT TREE and the declared return spelling, and
`selfhost/lower.rvl` has read that tree since item 391's binding-discipline
slice (`fb_scan` builds `FbStep` with `if`/`while`/`for` arms and bodies for the
scope walk). So `fb_function` now runs `_check_returns_on_every_path` after
`fb_walk`, per `fn` in declaration order, which is exactly where `_lower_fns`
runs it — after the body, ahead of the next declaration. Both sentences, both
anchors: the never-returns one at the declaration line, the falls-through one at
`decl.body[-1].line`. `_definitely_returns` ports verbatim, `while (true)`
divergence included with item 379's targeting-`break` rule, and an `if` with no
`else` needs no flag because an absent arm is an empty step list and an empty
step list never returns.

What it withholds, and why each silence is the sound direction. This reader sees
fewer statements than the reference's AST walk and the asymmetry is the whole
argument: a `return` it fails to see turns an admitting body into a refusal,
which is the false-rejection direction this gate may not err in. So one "bail"
anywhere in the tree — an `else if` chain, a destructuring binder, any statement
`fb_one` cannot model — leaves the whole `fn` to the reference; `fb_scan` now
records its cursor-stall truncation as a bail for the same reason, which is
behaviour-preserving for `fb_walk` (both end a block clean). A declared return
that mentions a transparent type alias is withheld too: `_resolve_type_aliases`
substitutes `fn.returns` in place before the message is spelled, so quoting the
written spelling would disagree on the text even where the verdict agrees. The
alias detector deliberately over-includes, because naming one type too many
withholds a verdict while naming one too few spells an erased message wrong.

What it buys, measured. `t8_missing_return` and `t9_return_path_incomplete`
moved from `false-admit/T1` to `agree-refuse/T1` (message AND line), leaving that
census bucket at 29 over the baselined corpus. Over the `--all` sweep five
further programs moved the same way, every one of them a model-written
`bench/results` artifact, so the rule earns its keep outside the fixture
directory. In the 400-draw fuzz at seed 7 the same change moved five programs OUT
of a false-admit bucket and none into one, and no program anywhere moved toward
`false-reject`: `agree-admit` is unchanged at 431.

An ORDERING divergence it introduces, named rather than met. The reference lowers
a body statement by statement and asks the return question LAST, so a body that
both breaks an earlier rule and never returns draws the earlier refusal. This
gate does not run the expression algebra and decides only the assignment position
of the binding discipline, so on such a body it draws the return-path refusal
instead. Both refusals are true and the program is refused either way, so this is
a 419c-style naming divergence and never an admission the reference would not
give — and it is strictly better than what it replaced, which was a no-objection.
It shows only under the fuzz (three `tag-mismatch/G1->T1`, two `msg-mismatch/T1`,
each from a mutant that both corrupts a name and loses its `return`), never on
the baselined corpus, and it closes as the name-resolution and expression slices
land. `test_an_unresolved_name_read_outranks_the_return_path_on_the_reference`
pins it.

What it does NOT buy: nothing about match exhaustiveness, unknown cases, alias
cycles, duplicate declarations or declared-type wellformedness — the rest of T3b
is untouched, and the four `no builtin method` / `non-exhaustive match` families
still need the tables T2b and T2d build. It also does not close the T3a
statement channel: this rule is the one obligation of the fn loop that can be
decided from the tree alone, which is why it could land first.

A gap it MEASURED on the way past, not caused and not fixed. The first corpus
document to declare a transparent type alias (`type Count = Int`) showed that
`selfhost/lower.rvl`'s `lower_to_ir` does not erase aliases at declaration sites:
it emits `"returns": "Count"` where the reference, having run
`_resolve_type_aliases`, emits `"returns": "Int"`. No corpus document under
`tests/fixtures/emit_py_corpus/` had ever declared one, so both differential
oracles agreed trivially — the absence, not a divergence. That erasure is
§2.1 item 1 and belongs to T1's `types.rvl`; the alias case was dropped from
`return_paths.rvl` rather than half-fixed here, and this paragraph is the record
that it is known.

**T3c. One engine.** Delete lower.rvl's private `infer`/`binop_ty`/
`builtin_ret`/`join_ty`/`infer_field`/`infer_callee`/`operands_of` and the
pseudo-binding hacks; the IR annotations come from `infer_t(raise_: false)`.
Oracle: IR and emitted-bytes byte-exactness over every corpus (`test_selfhost_
lower_ir.py`, `test_selfhost_compile.py`, every `test_selfhost_emit_*.py`),
`tools/selfhost_differential_survey.py` unchanged. Net negative lines; its
whole value is that two inference engines can no longer drift.

**T4a. The component header's service-existence rule.** LANDED, out of order:
it sits in T4's list at 2.4 but depends on nothing T1..T3 build, because it
resolves a NAME rather than a term. `selfhost/lower.rvl` reads each component
header's `<key>: <Service>` annotations with their lines (`SvcRef`, requires
before provides, which is the reference's own order: `Env.__init__` walks
`comp.requires`, `_lower_component` the provisions), and refuses the first that
resolves against neither the program's own service declarations nor — on the
ambient path — the running composition's. The item-346 `!services` block, which
until now the manifest fold parsed and discarded, is what supplies the second
set; its `!services` HEADER is the exhaustiveness claim, and a wire that makes
no claim decides nothing. What it buys, measured: `admit_src` and `admit_into`
now give DIFFERENT answers about `bench/admission_latency.py::CANDIDATE`, so
clause 1 of section 6's exit test is met — the rust gate refuses `cache_layer`
standalone with the reference's own sentence, and lifts that refusal against a
manifest that declares `Store`. Four corpus documents (`demo/components/*`,
`examples/ecosystem-consumer/candidates/leaky_tool.rvl`) moved from
`no-objection-out-of-slice` to `agree-refuse/G1`, and the crate's
`TYPE_LAYER_GAP` lost its `provides` row to the agreement corpus. What it does
NOT buy: nothing about requirement RESOLUTION or the `Admitted` arm. The first of
those is T4b below; the second is still T6 in full.

**T4b. The required-service MEMBER rule (A6).** LANDED, out of order, for the
same reason T4a was: it resolves a NAME against a held declaration rather than a
term, so it needs nothing T1..T3 build. `selfhost/lower.rvl`'s `req_call` looks
the operation up in the service the requirement resolves to and refuses an
absent one with `` `db.execute` is not a method of service Database `` — at the
reference's own position, ahead of the arity count and ahead of the G4 emit-marker
arm, so an operation nothing declares draws A6 and not G4 even under `emit`. It
fires only where the declaration is HELD (`svc_decl_known`), which is the whole
soundness argument: `svc_of` answers an EMPTY method list for a service this gate
has no declaration for, and refusing against that would refuse every call on an
ambient service.

Two sources are exhaustive enough to be held, and both are exhaustive by
construction. The text's own `service S { … }`: `p_service` parses every
operation or fails the whole document, so a parsed `SvcD` is the complete
surface. And the RUNNING composition's, which is what makes this the
requirement-RESOLUTION slice the previous entry said was still open: the
item-346 `:S` row grew an operation list (`:S,get,bump,put`), rendered by
`revl.manifest.manifest_wire` off the IR's service table, and the fold keeps
those in `Ctx.ambOps` — deliberately NOT merged into `Ctx.svcs`, because an entry
there carries the emission/async/capability flags the G4 and A1 arms judge and
the wire carries operation NAMES only. The comma is the claim: `:S` is the wire
every producer without an operation table renders and decides no member, `:S,` is
the empty surface, `:S,a` is exactly `a`. A malformed list refuses the wire by
name (`MANIFEST`) rather than claiming a shorter surface than the composition
has, which would refuse calls the reference admits.

What it buys, measured: `a6_method_not_in_service` moved from
`false-admit/A6` to `agree-refuse/A6` (that census bucket is now EMPTY), and the
rust gate's manifest arm now gives different answers about two candidates that
differ only in the operation they call — `bench/inprocess_gate_harness.py::
_CALLS_MISSING_METHOD` is refused `A6` with the reference's own sentence where
`al.CANDIDATE` is not. That is clause 3 of section 6's exit test for the
`calls_missing_method` candidate, and the second of the three things T4a left
open. What it does NOT buy: argument typing and arity on the same call (both need
the expression algebra, still T4), the §5 compatibility relation on a
redeclaration (the block carries operation names, not signatures), and the
`Admitted` arm — which is now the ONLY thing standing between the rust gate and
clause 2, and is T6 in full.

**An ordering divergence T4b uncovered, and did not cause.** The A6 refusal is
INLINE: `body_line` anchors it at the offending statement. The handoff
compatibility verdict of item 186's wave part 2 is anchored at the COMPONENT
declaration line, and `pick_min` orders by `(line, seq)`, so the handoff verdict
outranks any inline body refusal on the same component. The reference does the
opposite and for a structural reason, not a line one: `_admit_handoff_replacement`
runs over `live_components`, which `src/revl/lower.py` builds by dropping every
component whose body lowering raised, so a component with a refusing body
contributes no handoff verdict at all.

It predates this slice — the same disagreement reproduces with the G1
undeclared-access refusal, inline-anchored since long before this design — and no
corpus program had caught it because the whole-component AGGREGATE verdicts (the
G4 emission reach) tie at the component line and are saved by `seq`. Both
refusals are true, so it is a 419c naming divergence and never a false admission.
`test_a_handoff_drift_outranks_an_inline_body_refusal_on_the_gate` pins it in
both directions; the fix belongs to the slice that owns `handoff_refusals`, needs
the poisoned-component set threaded into `collect_nonlink`, and carries its own
oracle rows.

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

**T5 PARTIAL — the running SIGNATURE, and the oracle.** Read the shape
decision first, because it is not 4.5's. `admit_into` did not wait for this
slice and did not take JSON: `selfhost/compile.rvl`'s
`admit_into(source, manifest)` and `crates/revl-gate`'s
`admit_into(&str, &str)` have been the manifest verb since #860, over the
item-186 ROW WIRE rather than a compiled IR document. Section 8 listed that as
a decision to confirm ("a purpose-built line format the py side would render")
and the tree confirmed it the other way round from 4.5's default, so
`stdlib/json.rvl` stays out of the crate closure and `serde_json` stays off
the dependency list.

What landed here is the half 4.5 names and the wire did not carry: the running
service's declared PARAMETER LISTS. `revl.manifest.manifest_wire` renders each
operation token as `op(name:Type|name:Type)`, the fold parses it into `AmbSvc.
sigs`, and `ct_req_msig` resolves a call through a requirement against the
RUNNING declaration when the candidate declares no service of that name. So
"a candidate's `requires store: Store` resolves and its call sites are typed
against the RUNNING signature" is now true of the arguments.

Measured, on `bench/admission_latency.py`'s running composition: a candidate
calling `store.bump(key)` on a running `bump(n: Int)` moved from a
no-objection to `T1|`store.bump` argument `n` expects `Int`, got `Str``, the
reference's sentence byte for byte, while its well-typed twin `store.get(key)`
still raises none. Over the oracle's 40 drawn pairs the native manifest arm
refused 0 before and 27 after, every one of them agreeing with the reference
on tag AND message. The census is byte-identical (it runs standalone
`admit_src`, which this slice does not touch): `false-admission` empty,
`false-reject` empty, `false-admit` 9.

Withheld, deliberately, and each is the under-refusing direction:

* the return type, and the `emission`/`async`/capability markings. The G4 and
  A1 arms read those, and an arm answering from a declaration nobody sent is
  the wave-through the block exists to avoid. Only `ct_req_msig` reads a
  wire-built `MSig`, and its rule reads `ps` alone;
* a parameter list whose spelling needs one of the wire's structural
  characters (`Map[Str, Int]` carries the operation separator). The renderer
  withholds the whole list rather than escaping it or emitting one that lost a
  parameter to the split; the row then carries the bare name, which the fold
  reads as silence about the arguments;
* ARITY. `store.get(key, key)` against a one-parameter running `get` is a
  reference refusal with no code, and the gate false-admits it standalone
  too — it is not a manifest question;
* the provide method's RETURN against the service it implements
  (`wrong_return_on_running`), for the same reason: a standalone false-admit,
  T4's;
* `_admit_service_replacement`'s section-5 relation on a redeclared running
  service. `tests/test_selfhost_admit_into.py::WITHHELD` is the list, and it
  is a test rather than a comment: a named entry that has silently started
  agreeing reds, and so does an unnamed pair that has started withholding.

Oracle: `tests/test_selfhost_admit_into.py`, the differential against the
public `revl.gate.admit_into` over (running, candidate) pairs — the harness's
`RUNNING`/`CANDIDATE`, `_REDECLARE_RUNNING`, `_CALLS_MISSING_METHOD`,
`_INCOMPLETE_PROVIDE`, the hole draft, a same-name replacement, a per-realm
conflict, the typed pair, plus the generator — asserting the asymmetric
property: every native refusal is a reference refusal with the same tag and
sentence, and every native silence is named. `admit_ambient(src, "") ==
admit_src(src)` is pinned over the whole corpus.

Still T5's, and not landed: nothing takes a compiled IR document. An embedder
holding one projects it with `revl.manifest.manifest_wire` on the py side,
which is the seam 337 Seam 2/3 would have to cross.

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
