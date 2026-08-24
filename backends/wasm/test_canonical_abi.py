"""Canonical-ABI WASI Preview 2 component emission — item 41 slice-3.

Run with:
    .venv/bin/pytest backends/wasm/test_canonical_abi.py -q

The emit/golden/refusal tests run everywhere. The build+execute test needs the
standard toolchain (`wasm-tools` to wrap the core module into a component,
`wasmtime` to run it under the component model); it skips when they are absent
unless REVL_REQUIRE_WASMTIME is set (CI), matching the wasm tier's existing
policy in test_v3_emit.py — a missing runtime is a hard failure in CI, a quiet
skip in local dev.
"""

import importlib.util
import os
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
GOLDEN = BACKEND / "golden"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402


def _canonical():
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_canonical", BACKEND / "canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")

# The fixed sources and their goldens are the single source of truth; regenerate
# with `python3 backends/wasm/golden/regen_canonical.py`.
_SRC = (GOLDEN / "canonical_echoer.revl").read_text(encoding="utf-8")
_SERVICE = "Echoer"
_AGG_SRC = (GOLDEN / "canonical_aggregates.revl").read_text(encoding="utf-8")
_AGG_SERVICE = "Registry"
# The service-level fixture: the SAME value surface, but presented from a
# component's `provide` methods (slice-3's final piece) rather than top-level fns.
_SVC_SRC = (GOLDEN / "canonical_service.revl").read_text(encoding="utf-8")
_SVC_SERVICE = "Registry"


def _emit():
    return _canonical().emit_component(compile_source(_SRC), service=_SERVICE)


def _emit_agg():
    return _canonical().emit_component(
        compile_source(_AGG_SRC), service=_AGG_SERVICE)


def _emit_svc():
    return _canonical().emit_component(
        compile_source(_SVC_SRC), service=_SVC_SERVICE)


# --------------------------------------------------------------------------- #
# Emit + golden — runs everywhere, no toolchain needed.
# --------------------------------------------------------------------------- #

def test_canonical_core_wat_matches_golden():
    res = _emit()
    golden = (GOLDEN / "canonical_echoer.core.wat").read_text(encoding="utf-8")
    assert res["core_wat"] == golden


def test_canonical_wit_matches_golden():
    res = _emit()
    golden = (GOLDEN / "canonical_echoer.wit").read_text(encoding="utf-8")
    assert res["wit"] == golden


def test_canonical_core_has_the_boundary_machinery():
    """The three things a standard component-model host needs that the custom
    tier never emitted: cabi_realloc, an interface-qualified export name, and
    the bare->internal string lift."""
    core = _emit()["core_wat"]
    assert '(func (export "cabi_realloc")' in core
    assert '(func (export "revl:exported/echoer#echo")' in core
    assert "$__canon_lift_str" in core


def test_canonical_wit_interface_is_export_wit_verbatim():
    """The component's exported interface must be exactly what `revl export wit`
    (slice-1) prints — the binary and the interface documentation agree."""
    from revl.export_wit import export_wit  # noqa: PLC0415
    res = _emit()
    ir = compile_source(_SRC)
    synthetic = {"services": {_SERVICE: {"methods": {
        fn["name"]: {"params": fn.get("params") or [], "returns": fn.get("returns")}
        for fn in ir.get("functions") or [] if fn.get("returns") == "Str"
        and all(p.get("type") == "Str" for p in fn.get("params") or [])}}},
        "types": {}, "externs": []}
    interface = export_wit(synthetic, service=_SERVICE, package="revl:exported")
    # every non-empty line of the exported interface appears verbatim in the WIT
    assert interface.rstrip() in res["wit"]


def test_aggregate_core_wat_matches_golden():
    res = _emit_agg()
    golden = (GOLDEN / "canonical_aggregates.core.wat").read_text(encoding="utf-8")
    assert res["core_wat"] == golden


def test_aggregate_wit_matches_golden():
    res = _emit_agg()
    golden = (GOLDEN / "canonical_aggregates.wit").read_text(encoding="utf-8")
    assert res["wit"] == golden


def test_aggregate_wit_is_valid_types_inside_interface():
    """`revl export wit` (slice-1, item 97 `ec97d07`) emits a referenced
    `record`/`variant`/`enum` INSIDE the interface body — valid WIT, since a
    named type must live inside an interface. The component embeds that output
    directly (no relocation), so the exported WIT has no type declaration at the
    package top level and the record sits in the interface body. Assert this
    holds for `export_wit`'s output directly, which is what the component
    embeds."""
    from revl.export_wit import export_wit  # noqa: PLC0415
    canonical = _canonical()
    wit = _emit_agg()["wit"]

    def _no_top_level_type(text: str, label: str) -> None:
        depth = 0
        for line in text.split("\n"):
            s = line.strip()
            if depth == 0 and s.startswith(("record ", "variant ", "enum ")):
                raise AssertionError(f"top-level type declaration in {label}: {s!r}")
            depth += line.count("{") - line.count("}")

    # Build the same synthetic service IR the component embeds (the boundary
    # functions grouped under the service name) and call `export_wit` directly:
    # its output is already valid WIT — the named `record` sits inside the
    # interface, nothing at the package top level — so the component embeds it
    # verbatim with no relocation.
    ir = compile_source(_AGG_SRC)
    canon = canonical._Canon(ir.get("types") or {})
    boundary = canonical._boundary_functions(ir.get("functions") or [], canon)
    methods = {fn["name"]: {"params": fn.get("params") or [],
                            "returns": fn.get("returns")} for fn in boundary}
    synthetic = {"services": {_AGG_SERVICE: {"methods": methods}},
                 "types": ir.get("types") or {}, "externs": []}
    interface = export_wit(synthetic, service=_AGG_SERVICE, package="revl:exported")
    _no_top_level_type(interface, "export_wit output")
    assert "  record person { name: string, age: s64 }" in interface

    # and the embedded WIT is that output verbatim, so it inherits the property
    _no_top_level_type(wit, "embedded WIT")
    assert "  record person { name: string, age: s64 }" in wit
    assert interface.rstrip() in wit


def test_aggregate_interface_now_carries_records_lists_variants():
    """The slice widens the boundary from Str-only: records, lists, Opt/Result
    and non-Str scalars now cross, so they appear on the exported interface."""
    res = _emit_agg()
    assert set(res["functions"]) == {
        "make", "age_of", "rename", "dbl", "flip", "pair", "head", "roster",
        "maybe", "or_zero", "checked"}
    wit = res["wit"]
    assert "make: func(nm: string, a: s64) -> person;" in wit
    assert "pair: func(a: s64, b: s64) -> list<s64>;" in wit
    assert "maybe: func(n: s64) -> option<s64>;" in wit
    assert "checked: func(n: s64) -> result<s64, string>;" in wit


def test_refuses_when_no_lowerable_boundary_function():
    canonical = _canonical()
    ir = compile_source("fn f(x: Float) -> Float { return x }")
    with pytest.raises(canonical.EmitError, match="no canonical-ABI-emittable"):
        canonical.emit_component(ir, service="Floaty")


def test_unlowerable_functions_stay_off_the_interface_but_in_the_core():
    """A variant PARAM whose payload is an aggregate is off the interface — the
    direct-flattened join of an aggregate payload is the remaining gap — yet the
    function stays in the core module so a boundary function can still call it.
    The SAME variant as a RESULT crosses the boundary (it goes through memory)."""
    canonical = _canonical()
    ir = compile_source(
        "type Person = { name: Str, age: Int }\n"
        "fn tag(s: Str) -> Str { return `[${s}]` }\n"
        "fn unbox(o: Opt[Person]) -> Int { return 0 }\n"
        "fn box(nm: Str, a: Int) -> Opt[Person] { return Some({name: nm, age: a}) }\n")
    res = canonical.emit_component(ir, service="Mixed")
    assert res["functions"] == ["tag", "box"]        # box: Opt[Person] result ok
    assert "$unbox" in res["core_wat"]               # variant-param helper present
    assert 'export "revl:exported/mixed#unbox"' not in res["core_wat"]


# --------------------------------------------------------------------------- #
# Service-level canonical lowering — the final piece of slice-3. A component's
# `provide` methods (not just top-level pure fns) cross the canonical boundary.
# --------------------------------------------------------------------------- #

def test_service_core_wat_matches_golden():
    res = _emit_svc()
    golden = (GOLDEN / "canonical_service.core.wat").read_text(encoding="utf-8")
    assert res["core_wat"] == golden


def test_service_wit_matches_golden():
    res = _emit_svc()
    golden = (GOLDEN / "canonical_service.wit").read_text(encoding="utf-8")
    assert res["wit"] == golden


def test_service_provide_method_is_named_and_wrapped():
    """The provide-method export the component tier emits (an anonymous
    `provide:<key>.<method>` core func) is given a callable `$__prov_*` symbol,
    and a canonical wrapper with the interface-qualified export name delegates to
    it over cabi_realloc + the lift/lower library."""
    core = _emit_svc()["core_wat"]
    assert '(func (export "cabi_realloc")' in core
    assert '(func $__prov_reg_greet (export "provide:reg.greet")' in core
    assert '(func (export "revl:exported/registry#greet")' in core
    assert "(call $__prov_reg_greet" in core
    assert "$__canon_lift_str" in core


def test_service_wit_interface_is_export_wit_verbatim():
    """The embedded interface is exactly `revl export wit --service Registry`
    over the same component IR — the binary and the interface documentation
    agree. (`export_wit` emits the record inside the interface body since item
    97, so the component embeds its output directly — no relocation.)"""
    from revl.export_wit import export_wit  # noqa: PLC0415
    res = _emit_svc()
    ir = compile_source(_SVC_SRC)
    interface = export_wit(ir, service=_SVC_SERVICE, package="revl:exported")
    for line in interface.splitlines():
        s = line.strip()
        if s.endswith(";") and "func(" in s:      # a method declaration line
            assert s in res["wit"], s


def test_service_presents_the_whole_lowerable_surface():
    """Str, records (Int+Str fields), lists of records, non-Str scalars, and
    Opt/Result all cross as *service methods*, not just top-level fns."""
    res = _emit_svc()
    assert res["functions"] == [
        "greet", "make", "age_of", "rename", "dbl", "roster", "maybe", "checked"]
    wit = res["wit"]
    assert "greet: func(nm: string) -> string;" in wit
    assert "make: func(nm: string, a: s64) -> person;" in wit
    assert "roster: func(nm: string, a: s64) -> list<person>;" in wit
    assert "checked: func(n: s64) -> result<s64, string>;" in wit


def test_service_unlowerable_method_stays_off_interface_but_in_core():
    """A method whose signature the canonical layer cannot present (a variant
    PARAM with an aggregate payload — the same remaining gap as the fn path) is
    left off the exported interface, yet its `provide:` export stays in the core
    module. The SAME variant as a RESULT crosses (it goes through memory)."""
    canonical = _canonical()
    ir = compile_source(
        "type Person = { name: Str, age: Int }\n"
        "service Mixed {\n"
        "  fn tag(s: Str) -> Str\n"
        "  fn unbox(o: Opt[Person]) -> Int\n"
        "  fn box(nm: Str, a: Int) -> Opt[Person]\n"
        "}\n"
        "component C provides mx: Mixed {\n"
        "  provide mx {\n"
        "    fn tag(s) = `[${s}]`\n"
        "    fn unbox(o) = 0\n"
        "    fn box(nm, a) = Some({ name: nm, age: a })\n"
        "  }\n"
        "}\n")
    res = canonical.emit_component(ir, service="Mixed")
    assert res["functions"] == ["tag", "box"]
    assert '(export "provide:mx.unbox")' in res["core_wat"]     # in the core
    assert 'export "revl:exported/mixed#unbox"' not in res["core_wat"]  # off iface


def test_service_refuses_when_no_lowerable_method():
    canonical = _canonical()
    ir = compile_source(
        "service Floaty { fn f(x: Float) -> Float }\n"
        "component C provides fl: Floaty { provide fl { fn f(x) = x } }\n")
    with pytest.raises(canonical.EmitError, match="no canonical-ABI-emittable method"):
        canonical.emit_component(ir, service="Floaty")


# --------------------------------------------------------------------------- #
# Build + execute under wasmtime's COMPONENT MODEL — the real proof.
# --------------------------------------------------------------------------- #

def _toolchain_or_skip(canonical):
    if canonical.wasm_tools_binary() is None or canonical.wasmtime_binary() is None:
        if _REQUIRE:
            pytest.fail(
                "wasm-tools and/or wasmtime absent, so the canonical-ABI "
                "component cannot be built and run. REVL_REQUIRE_WASMTIME is "
                "set (CI), so this fails instead of skipping.", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


def test_component_builds_validates_and_round_trips(tmp_path):
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    res = _emit()
    component = canonical.build_component(
        res["core_wat"], res["wit"], tmp_path, res["world"], name="echoer")
    # loads as a valid component under the component model
    canonical.validate_component(component)
    # the canonical string ABI round-trips both directions, run by wasmtime's
    # component model (wasmtime --invoke only accepts a component here)
    assert canonical.run_component_str(component, "echo", "world") == "world"
    assert canonical.run_component_str(component, "shout", "hi") == "hi!"
    assert canonical.run_component_str(component, "greet", "revl") == "Hello, revl!"
    # empty string is a real canonical case (ptr valid, len 0)
    assert canonical.run_component_str(component, "echo", "") == ""


def test_aggregate_component_builds_validates_and_round_trips(tmp_path):
    """The real proof for the follow-on: records (Int + Str fields), lists,
    Opt/Result and non-Str scalars cross the canonical boundary and run under
    wasmtime's component model, both as parameters and as results."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    res = _emit_agg()
    component = canonical.build_component(
        res["core_wat"], res["wit"], tmp_path, res["world"], name="registry")
    canonical.validate_component(component)

    def call(invoke):
        return canonical.run_component(component, invoke)

    # non-Str scalars
    assert call("dbl(21)") == "42"
    assert call("flip(true)") == "false"
    # record — take Str+Int, return a record; and a record-in/record-out
    assert call('make("revl", 7)') == '{name: "revl", age: 7}'
    assert call('age-of({name: "x", age: 9})') == "9"
    assert call('rename({name: "old", age: 3}, "new")') == '{name: "new", age: 3}'
    # lists — of a scalar, and of a record (nested aggregate through one buffer)
    assert call("pair(4, 5)") == "[4, 5]"
    assert call("head([10, 20, 30])") == "10"
    assert call('roster("z", 2)') == '[{name: "z", age: 2}]'
    # variants — Opt result (both discriminants), Opt param, Result result
    assert call("maybe(9)") == "some(9)"
    assert call("maybe(-1)") == "none"
    assert call("or-zero(some(42))") == "42"
    assert call("or-zero(none)") == "0"
    assert call("checked(7)") == "ok(7)"
    assert call('checked(-2)') == 'err("nonpositive")'


def test_service_component_builds_validates_and_round_trips(tmp_path):
    """The final slice-3 proof: a component's `provide` methods — lowered by the
    heavier `_ComponentEmitter`, not the pure-fn path — cross the canonical
    boundary and run under wasmtime's component model. A real service crosses as
    a standard component, Str and the full aggregate surface alike."""
    canonical = _canonical()
    _toolchain_or_skip(canonical)
    res = _emit_svc()
    component = canonical.build_component(
        res["core_wat"], res["wit"], tmp_path, res["world"], name="store")
    canonical.validate_component(component)

    def call(invoke):
        return canonical.run_component(component, invoke)

    # a Str-surface method (the shippable target)
    assert canonical.run_component_str(component, "greet", "revl") == "Hello, revl!"
    assert canonical.run_component_str(component, "greet", "") == "Hello, !"
    # a non-Str scalar method
    assert call("dbl(21)") == "42"
    # record-surface methods — Str+Int -> record, record -> Int, record -> record
    assert call('make("revl", 7)') == '{name: "revl", age: 7}'
    assert call('age-of({name: "x", age: 9})') == "9"
    assert call('rename({name: "old", age: 3}, "new")') == '{name: "new", age: 3}'
    # list of records, and Opt/Result results — all through provide methods
    assert call('roster("z", 2)') == '[{name: "z", age: 2}]'
    assert call("maybe(9)") == "some(9)"
    assert call("maybe(-1)") == "none"
    assert call("checked(7)") == "ok(7)"
    assert call('checked(-2)') == 'err("nonpositive")'
