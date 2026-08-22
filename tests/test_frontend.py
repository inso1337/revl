"""Frontend tests: the reference roundtrip and the rejection suite.

The rejection files in examples/rejections/ are the checker's executable
spec (DESIGN.md §9): each must fail to compile with the message its header
comment promises.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

EXAMPLES = ROOT / "examples"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- roundtrip

def test_user_cache_compiles_to_reference_ir():
    """The frontend must produce exactly the hand-lowered IR that both
    backends were built and tested against."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    reference = json.loads((EXAMPLES / "user_cache.ir.json").read_text())
    assert ir == reference


def test_end_to_end_python_golden():
    """source -> frontend IR -> cordis-py emitter == checked-in golden."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_py_emit", ROOT / "backends" / "python" / "emit.py")
    golden = (ROOT / "backends" / "python" / "golden" / "user_cache.py").read_text()
    assert emitter.emit(ir) == golden


def test_end_to_end_typescript_golden():
    """source -> frontend IR -> cordis-ts emitter == checked-in golden."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_ts_emit", ROOT / "backends" / "typescript" / "emit.py")
    golden = (ROOT / "backends" / "typescript" / "golden" / "user_cache.ts").read_text()
    assert emitter.emit(ir) == golden


def test_end_to_end_rust_golden():
    """source -> frontend IR -> cordis-rs emitter == checked-in golden.

    The emitter is pure Python, so this needs no rust toolchain — and without
    it the golden drifts silently (it did, through a merge: the emitter's
    doc-comment wording changed on one branch while the golden was
    regenerated on another)."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_rust_emit", ROOT / "backends" / "rust" / "emit.py")
    golden = (ROOT / "backends" / "rust" / "golden" / "user_cache.rs").read_text()
    assert emitter.emit(ir) == golden


def test_end_to_end_java_golden():
    """source -> frontend IR -> cordis4j emitter == checked-in golden."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_java_emit", ROOT / "backends" / "java" / "emit.py")
    golden = (ROOT / "backends" / "java" / "golden" / "user_cache.java").read_text()
    assert emitter.emit(ir) == golden


# ---------------------------------------------------------------- rejections

REJECTIONS = {
    "a1_await_in_method.rvl": "`await` is only allowed in a component body",
    "v2_async_signature_mismatch.rvl": "method `stats` of provision `db` is not async but service Database declares it async",
    "v2_same_realm_conflict.rvl": "provision conflict: key `kv` in realm `tenant_a` is provided by both StoreOne and StoreTwo (G2)",
    "v2_dynamic_realm.rvl": "dynamic realm labels are not supported",
    "v2_intercept_on_provision.rvl": "`intercept` applies to required keys only — `kv` is a provision",
    "v2_intercept_undeclared.rvl": "`db` is not a declared requirement of Watcher",
    "v2_compound_assign_on_let.rvl": "cannot reassign `n` — it is `let` (single-assignment)",
    "v2_isolate_after_effect.rvl": "`isolate` must precede every effect, emit, await, and provide statement",
    "v2_let_reassignment.rvl": "cannot reassign `n` — it is `let` (single-assignment)",
    "v2_match_nonexhaustive.rvl": "non-exhaustive match: missing case `Invalid`",
    "v2_var_in_record.rvl": "`var` `n` cannot be used in a record literal",
    "v2_undeclared_fn_var.rvl": "`missing` is not declared in this function",
    "v2_use_cycle.rvl": "import cycle:",
    "v2_use_private.rvl": "`helper` is module-private",
    "v2_extern_unclassified.rvl": "unclassified extern",
    "v2_extern_acquire_no_undo.rvl": "acquire extern `listen` must declare `undo` (G4)",
    "v2_fail_in_pure_fn.rvl": "`fail` is only allowed in a component activation body (A8)",
    "arith_zero_divisor.rvl": "`mod` by a literal zero is undefined",
    "t20_int_literal_range.rvl": "Int literal `9223372036854775808` is outside the 64-bit range",
    "host_method_not_on_surface.rvl": "`Map` has no method `putt`",
    "v2_verified_direct_recursion.rvl": "verified fn `recurse` is not total",
    "g1_undeclared_access.rvl": "`db` is not a declared requirement of Logger",
    "t1_service_arg_type.rvl": "`db.query` argument `sql` expects `Str`, got `Int`",
    "t2_null_in_expression.rvl": "`null` has no type in revl",
    "g2_provision_conflict.rvl": "provision conflict: key `db` is provided by both PgDatabase and SqliteDatabase (G2)",
    "g3_dependency_cycle.rvl": "dependency cycle: Alpha -> Beta -> Alpha (G3)",
    "g4_missing_undo.rvl": "effect has no `undo` and `Pool.open` is not pure",
    "g4_unmarked_emission.rvl": "call to emission `db.execute` must be marked `emit` (G4)",
    "g4_emission_not_declared.rvl": "`Cache.put` is declared plain, but this implementation reaches `db.execute`",
    "g4_capability_not_declared.rvl": "`Cache.put` is declared `emission[db]`, but this implementation emits through `bus`",
    "g6_impure_statement.rvl": "plain expressions have no effect to record (G6)",
    "a2_acquire_after_provide.rvl": "acquisition after `provide`",
    "a6_method_not_in_service.rvl": "`db.execute` is not a method of service Database",
    "g1_template_undeclared.rvl": "`nobody` is not declared in this function",
    "t3_config_default_type.rvl": "config field `n` default expects `Int`, got `Str`",
    "t4_field_arg_type.rvl": "`s.take` argument `s` expects `Str`, got `Int`",
    "t5_destructure_nonrecord.rvl": "record destructuring requires a record, but `List[Int]` is not a record",
    "t6_bare_generic.rvl": "`Opt` takes 1 type argument(s), got 0",
    "v2_use_missing.rvl": "cannot find imported module `./does_not_exist.rvl`",
    "v2_optional_chain_nonoptional.rvl": "an optional access `?.` can only be followed by another `?.`",
    "t7_provide_param_annotation_mismatch.rvl": "parameter `sql` of `query` (from service `Db`) expects `Str`, got `Int`",
    "v2_nullish_mixed_with_or.rvl": "`||` cannot be mixed with `??` without parentheses",
    # typing follow-ups: programs the checker used to accept and the strict
    # tiers refuse (docs/v2.0-roadmap.md, "Typing follow-ups")
    "t8_missing_return.rvl": "`budget` is declared to return `Int` but its body never returns a value",
    "t9_return_path_incomplete.rvl": "`rank` is declared to return `Int` but control can reach the end of its body without a `return`",
    "t10_call_arity.rvl": "`scale` takes 1 argument(s), 2 given",
    "t11_field_through_opt.rvl": "field access `.name` on `Opt[Row]`: the optional wrapper has no such member",
    "t12_str_index.rvl": "`Str` has no index operator — `[...]` indexes a `List` only",
    "t13_unknown_match_case.rvl": "`Pending` is not a case of `Status` (cases: `Active`, `Retired`)",
    "t14_optional_chain_on_nonoptional.rvl": "`?.` needs an optional on the left, got `Row`",
    "t15_generic_call_site.rvl": "this function's return expects `Int`, got `Str`",
    "t16_provide_method_missing_return.rvl": "`get` implements `Store.get`, which returns `Str`, but this body never returns a value",
    "t17_arrow_body_unchecked.rvl": "field access `.name` on `Opt[Row]`",
    "t18_type_alias_cycle.rvl": "type alias cycle: Handle -> Ref -> Handle",
    "t19_union_type.rvl": "revl has no union types",
    # lifecycle tests (docs/syntax-2.0.md §7.1)
    "lifecycle_unknown_component.rvl": "unknown component `Ghost`",
    "lifecycle_double_load.rvl": "`Kv` is already loaded",
    "lifecycle_unknown_assertion.rvl": "unknown lifecycle assertion `no_leaks`",
    "lifecycle_unknown_operation.rvl": "`kv.put` is not an operation of service Store",
    "lifecycle_stmt_in_pure_test.rvl": "`load` is only allowed in a `lifecycle test` body",
    "lifecycle_config_unknown_field.rvl": "`sixe` is not a config field of Kv",
    "lifecycle_no_swap.rvl": "there is no `swap` statement",
    # admission compatibility (roadmap §5): a service redeclared in one
    # document is a duplicate (identity), never a compatible swap — the
    # compatibility relation only relaxes exact-match across the runtime
    # boundary. See tests/test_service_compat.py for the manifest-relative
    # drift rejections, which need a running composition and so cannot be a
    # single-file fixture.
    "service_compat_duplicate.rvl": "duplicate service `Cache`",
}

# ------------------------------------------------------------------ coverage
# The rejection suite is the checker's *definition* of sound: for every
# guarantee/amendment the checker can refuse there is a program above that it
# must refuse. Six rules have no entry here *because a source program cannot
# violate them at compile time* — they are guaranteed by construction, by a
# lowering transform, or by another guarantee. Verified empirically (each was
# probed with a candidate rejection that did not — and provably cannot —
# refuse for the stated reason); recorded so the coverage claim is complete,
# not sampled. See DESIGN.md §4 and docs/contract-errata.md "Contract
# rejection coverage".
#
#   G5  teardown cannot register effects — BY CONSTRUCTION. `undo`/`compensate`
#       bodies are pure expressions typed in `teardown` mode; `effect`/`emit`
#       are statements only well-typed in `setup` mode (DESIGN.md §5). There is
#       no syntactic slot for an acquisition during teardown — `effect` in an
#       `undo` is a parse error, not a G5 diagnostic. A boundary-crossing
#       emission reached from an `undo` stays fully enumerable (G8): the audit
#       walk in `revl.__main__._boundary` recurses teardown-position call
#       sites, so nothing about teardown escapes the boundary surface. The
#       residue G5 guards against is a runtime/library concern (the cordis-TS
#       `assertActive` bug in docs/contract-errata.md), not a source program.
#   G7  derived teardown is LIFO-complete — BY LOWERING (DESIGN.md guarantee
#       table: "by lowering, Thm. 16", the same non-compile category as G5).
#       Teardown is compiler-derived; it cannot be written incorrectly, so
#       there is no program to refuse. The property is exercised by the runtime
#       scenarios in backends/rust/scenarios/ and backends/java/scenarios/. The
#       one checker-side lever — totality of a `verified fn` that could feed a
#       derived teardown — is already covered by v2_verified_direct_recursion.rvl
#       (diagnostics.classify buckets "verified fn ... is not total" under G7).
#   A3  host-safe identifiers — LOWERING TRANSFORM. Names colliding with host
#       keywords are *renamed* (`class`->`class_`, `frame`->`frame_`), never
#       rejected. Positive test: test_a3_host_colliding_names_are_renamed.
#       (`config`/`await` reject as revl's *own* reserved words — unrelated to
#       A3 host safety.)
#   A4  `format` escaping — LOWERING TRANSFORM. Literal `$N` lowers to `$$N`;
#       there is nothing to refuse. Positive tests:
#       test_a4_literal_dollars_are_escaped, test_plain_string_dollar_is_literal.
#   A5  compensation accompanies an emission — BY CONSTRUCTION. `compensate` is
#       an *optional* slot (DESIGN.md §3.5: an emission "may declare" one) that
#       the grammar binds only to an `emit`, and `emit` requires an `emission`
#       (G4). There is no "compensation required but missing" program. Positive
#       test: test_a5_compensate_lowering.
#   A7  emission flags — ADVISORY to backends; enforcement is G4 itself
#       (docs/contract-errata.md A7). Covered by g4_unmarked_emission.rvl and
#       g4_emission_not_declared.rvl.


def test_every_rejection_file_is_covered():
    on_disk = {p.name for p in (EXAMPLES / "rejections").glob("*.rvl")}
    assert on_disk == set(REJECTIONS)


@pytest.mark.parametrize("filename,expected", sorted(REJECTIONS.items()))
def test_rejection(filename, expected):
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(EXAMPLES / "rejections" / filename)])
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------- v1 features

def test_migrator_compiles_to_reference_ir():
    """await + compensate lowering matches the frozen v1 reference."""
    ir = compile_files([str(EXAMPLES / "migrator.rvl")])
    reference = json.loads((EXAMPLES / "migrator.ir.json").read_text())
    assert ir == reference


def test_a3_host_colliding_names_are_renamed():
    from revl import compile_source

    ir = compile_source(
        """
        component Renamer {
          let frame = effect Map.new() undo frame.drop()
          let class = effect Map.new() undo class.drop()
        }
        """
    )
    body = ir["components"][0]["body"]
    binds = [step["bind"] for step in body]
    assert binds == ["frame_", "class_"], "host-reserved names must be renamed (A3)"
    assert body[0]["undo"]["target"]["id"] == "frame_"


def test_a4_literal_dollars_are_escaped():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Pricer requires bus: Bus {
          let item = effect Map.new() undo item.drop()
          emit bus.send(`cost: $9.99 for ${item}`)
        }
        """
    )
    fmt = ir["components"][0]["body"][1]["expr"]["args"][0]
    assert fmt["template"] == "cost: $$9.99 for $0"
    assert fmt["args"] == [{"kind": "name", "id": "item"}]


def test_template_literal_parses_to_interp():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Pricer requires bus: Bus {
          let item = effect Map.new() undo item.drop()
          emit bus.send(`hello ${item}`)
        }
        """
    )
    fmt = ir["components"][0]["body"][1]["expr"]["args"][0]
    assert fmt["template"] == "hello $0"
    assert fmt["args"] == [{"kind": "name", "id": "item"}]


def test_plain_string_dollar_is_literal():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Pricer requires bus: Bus {
          emit bus.send("cost: $9.99")
        }
        """
    )
    arg = ir["components"][0]["body"][0]["expr"]["args"][0]
    assert arg == {"kind": "lit", "value": "cost: $9.99"}


def test_a5_compensate_lowering():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Notifier requires bus: Bus {
          emit bus.send("hello") compensate bus.send("goodbye")
        }
        """
    )
    step = ir["components"][0]["body"][0]
    assert step["step"] == "emit"
    assert step["compensate"]["method"] == "send"


# ---------------------------------------------------------------- diagnostics

def test_rejections_carry_file_and_line():
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(EXAMPLES / "rejections" / "g1_undeclared_access.rvl")])
    rendered = str(excinfo.value)
    assert "g1_undeclared_access.rvl:" in rendered
    assert excinfo.value.line == 12  # the `let rows = effect db.query(...)` line


# --------------------------------------------------------- Int literal range
# `Int` is 64-bit two's complement (docs/arithmetic.md): in-range literals
# compile, the boundary value compiles, and one step past either edge is a
# compile-time diagnostic instead of a behaviour that differs per tier.

def _compile_return(expr_src: str) -> None:
    from revl import compile_source

    compile_source(f"fn probe() -> Int {{ return {expr_src} }}")


@pytest.mark.parametrize("literal", [
    "0",
    "1",
    "0 - 1",
    "9223372036854775806",
    "9223372036854775807",  # INT64_MAX
])
def test_int_literal_in_range_compiles(literal):
    _compile_return(literal)


def test_int_literal_min_by_computation_compiles():
    # `Int.MIN` has no spelling (see next test); computing it from in-range
    # literals is a runtime concern and compiles fine.
    _compile_return("(0 - 9223372036854775807) - 1")


@pytest.mark.parametrize("literal,shown", [
    ("9223372036854775808", "9223372036854775808"),    # MAX + 1
    ("-9223372036854775809", "9223372036854775809"),   # MIN - 1
])
def test_int_literal_out_of_range_rejected(literal, shown):
    with pytest.raises(RevlError) as excinfo:
        _compile_return(literal)
    message = str(excinfo.value)
    assert f"Int literal `{shown}` is outside the 64-bit range" in message


def test_int_min_has_no_spelling():
    # docs/arithmetic.md: the same refusal is why `Int.MIN` cannot be written.
    # Unary minus applies to the *positive* literal, so `-9223372036854775808`
    # negates an out-of-range literal and is rejected before any tier sees it.
    with pytest.raises(RevlError) as excinfo:
        _compile_return("-9223372036854775808")
    assert "Int literal `9223372036854775808` is outside the 64-bit range" \
        in str(excinfo.value)
