"""The wasm least-authority chain (roadmap item 289).

    host imports  subset-of  declared caps  subset-of  policy-allowed

For a wasm component all three sets are statically decidable -- the import
section is in the emitted module, the declared/reached caps are the G8 reach,
and the policy is the item-33 allow-list -- so the whole chain is enforceable,
unlike a G8-opaque @py/@ts host body. These tests pin both legs:

  * `host imports subset-of declared` holds BY CONSTRUCTION (the import set is
    generated from the reached requires); the emitter re-asserts it, and a
    synthetic violation is refused;
  * `declared subset-of policy` refuses a wasm cell whose declared caps exceed
    the boundary policy, naming the capability; a cell within policy admits.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.least_authority import (  # noqa: E402
    LeastAuthorityBreach,
    component_breaches,
    enforce_wasm_least_authority,
    least_authority_breaches,
    wasm_import_capabilities,
)
from revl.errors import RevlError  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

BACKEND = ROOT / "backends" / "wasm"


def _emit(ir: dict) -> dict:
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir), module


STORE = """
service KV { emission[kv] fn put(key: Str, value: Str) }
component Store requires kv: KV {
  emit kv.put("a", "b")
}
"""

TWO_KEYS = """
service KV  { emission[kv]  fn put(key: Str, value: Str) }
service Log { emission[log] fn write(line: Str) }
component Writer requires kv: KV, log: Log {
  emit kv.put("a", "b")
  emit log.write("hello")
}
"""

# A bare `emission` -- "any capability", the widest set. `component_reach`
# spells that top `*` (`policy.UNBOUNDED`), which is not a capability NAME.
BARE_EMISSION = """
service KV { emission fn put(key: Str, value: Str) }
component Store requires kv: KV {
  emit kv.put("a", "b")
}
"""


# ---------------------------------------------- host imports subset-of declared

def test_import_capabilities_read_from_the_emitted_module():
    ir = compile_source(STORE)
    modules, _ = _emit(ir)
    # the capability is the required KEY, read off the `coeffect:<key>` import.
    assert wasm_import_capabilities(modules["Store"]) == {"kv"}


def test_structural_imports_are_not_capabilities():
    # the async pump, config/spawn/dispose/instance seams and the durable-WAL
    # framing channel are ABI, not capabilities.
    wat = (
        '(module\n'
        '  (import "coeffect:tenant_a/kv" "put" (func $x (param i32)))\n'
        '  (import "route:bus" "publish" (func $y (param i32)))\n'
        '  (import "coeffect:revl:wal" "record" (func $w (param i32 i32 i32 i32)))\n'
        '  (import "host" "job_run" (func $j (param i32)))\n'
        '  (import "config" "size" (func $c (result i32)))\n'
        '  (import "spawn:Worker" "new" (func $s (result i32)))\n'
        ')'
    )
    # realm-scoped `coeffect:<realm>/<key>` still contributes `<key>`.
    assert wasm_import_capabilities(wat) == {"kv", "bus"}


def test_import_subset_of_declared_holds_by_construction():
    # every capability the real module imports is in the component's reach, so
    # there is no `import>declared` breach for a normally-emitted component.
    ir = compile_source(TWO_KEYS)
    modules, _ = _emit(ir)
    breaches = least_authority_breaches(ir, None, modules)
    assert breaches == []


def test_import_exceeding_declared_is_refused():
    # the by-construction invariant, asserted: a module importing a capability
    # outside the declared/reached set is the `import>declared` leg failing.
    breaches = component_breaches(
        "Widget", import_caps={"net"}, declared_caps={"kv"}, allow=None)
    assert [b.leg for b in breaches] == ["import>declared"]
    assert breaches[0].capabilities == ("net",)
    assert "host imports subset-of declared" in breaches[0].message()


def test_emitter_reasserts_the_import_subset_invariant():
    _, emit_module = _emit(compile_source(STORE))
    # a subset passes silently...
    emit_module._assert_imports_within_requires(
        "W", {"kv"}, {"kv", "db"}, {"coeffect:kv"})
    # ...an import for an unrequired key is a named emitter refusal.
    with pytest.raises(emit_module.EmitError) as excinfo:
        emit_module._assert_imports_within_requires(
            "W", {"kv", "net"}, {"kv"}, {"coeffect:kv", "coeffect:net"})
    assert "least-authority (289)" in str(excinfo.value)
    assert "'net'" in str(excinfo.value)


# --------------------------------------------- declared subset-of policy-allowed

def test_within_policy_admits():
    ir = compile_source(STORE)
    modules, _ = _emit(ir)
    policy = parse_policy("component Store may reach kv")
    assert least_authority_breaches(ir, policy, modules) == []
    enforce_wasm_least_authority(ir, policy, modules)  # does not raise


def test_declared_exceeding_policy_is_refused_naming_the_capability():
    ir = compile_source(STORE)
    modules, _ = _emit(ir)
    policy = parse_policy("component Store may reach net")  # not kv
    breaches = least_authority_breaches(ir, policy, modules)
    assert [b.leg for b in breaches] == ["declared>policy"]
    assert breaches[0].component == "Store"
    assert breaches[0].capabilities == ("kv",)

    with pytest.raises(RevlError) as excinfo:
        enforce_wasm_least_authority(ir, policy, modules)
    msg = str(excinfo.value)
    assert "kv" in msg
    assert "Store" in msg
    assert "declared caps subset-of policy-allowed FAILED" in msg


def test_no_allow_rule_leaves_the_component_unconstrained():
    # a policy that names no allow rule for this component does not bound it --
    # the same as `policy.evaluate`. Only the import leg (by construction clean)
    # applies, so a within-reach component admits.
    ir = compile_source(STORE)
    modules, _ = _emit(ir)
    policy = parse_policy("component Other may reach kv")
    assert least_authority_breaches(ir, policy, modules) == []


def test_partial_over_reach_names_only_the_offending_capability():
    ir = compile_source(TWO_KEYS)
    modules, _ = _emit(ir)
    policy = parse_policy("component Writer may reach kv")  # allows kv, not log
    breaches = least_authority_breaches(ir, policy, modules)
    assert [b.leg for b in breaches] == ["declared>policy"]
    assert breaches[0].capabilities == ("log",)


def test_no_policy_checks_only_the_import_leg():
    ir = compile_source(TWO_KEYS)
    modules, _ = _emit(ir)
    # with no policy, the chain reduces to the by-construction import invariant,
    # so a normal composition is clean and enforcement is a no-op.
    enforce_wasm_least_authority(ir, None, modules)


def test_breach_message_wording_for_the_import_leg():
    b = LeastAuthorityBreach("import>declared", "C", ("fs", "net"))
    m = b.message()
    assert "capabilities `fs`, `net`" in m
    assert "host imports subset-of declared caps FAILED" in m


# ------------------------------------- `*` is the top of the lattice, not a name

def test_unbounded_declared_reach_admits_every_import():
    # `*` is `policy.UNBOUNDED` -- a bare `emission`, "any capability". A
    # component whose reach carries it cannot have an import section that
    # exceeds what it declared, so the leg is vacuous rather than unsatisfiable.
    assert component_breaches(
        "C", import_caps={"kv"}, declared_caps={"*"}, allow=None) == []
    # the same reach beside a named capability, the shape a component with one
    # scoped and one bare emission produces.
    assert component_breaches(
        "C", import_caps={"kv", "net"}, declared_caps={"kv", "*"},
        allow=None) == []


def test_unbounded_declared_reach_does_not_mask_a_real_import_breach():
    # a NAMED-only declaration still catches an import outside it: the fix
    # relaxes the top of the lattice, not the leg.
    breaches = component_breaches(
        "C", import_caps={"net"}, declared_caps={"kv"}, allow=None)
    assert [b.leg for b in breaches] == ["import>declared"]
    assert breaches[0].capabilities == ("net",)


def test_bare_emission_component_is_not_refused_by_the_import_leg():
    # end-to-end on a real emitted module: the component declares a bare
    # `emission` and the module imports only `kv`, so the by-construction leg
    # holds and the no-policy path `revl run --wasm` takes admits.
    ir = compile_source(BARE_EMISSION)
    modules, _ = _emit(ir)
    assert wasm_import_capabilities(modules["Store"]) == {"kv"}
    assert least_authority_breaches(ir, None, modules) == []
    enforce_wasm_least_authority(ir, None, modules)  # does not raise


def test_bare_emission_component_admits_under_an_unbounded_grant():
    # the policy grants `*` itself, which is the only thing `_allowed` accepts
    # for an unnameable reach -- and the composition is admitted.
    ir = compile_source(BARE_EMISSION)
    modules, _ = _emit(ir)
    policy = parse_policy("component Store may reach *")
    assert least_authority_breaches(ir, policy, modules) == []
    enforce_wasm_least_authority(ir, policy, modules)  # does not raise


def test_bare_emission_component_reports_the_policy_leg_not_the_import_leg():
    # A named allow-list cannot prove an unbounded reach in-bounds, so the
    # breach is `declared>policy` naming `*`. The import leg must not pre-empt
    # it with a capability the unbounded declaration does cover: enforcement
    # raises breaches[0], so the wrong leg means the operator is told a
    # capability "it does not declare" and never sees the rule that refused.
    ir = compile_source(BARE_EMISSION)
    modules, _ = _emit(ir)
    policy = parse_policy("component Store may reach kv")
    breaches = least_authority_breaches(ir, policy, modules)
    assert [b.leg for b in breaches] == ["declared>policy"]
    assert breaches[0].capabilities == ("*",)

    with pytest.raises(RevlError) as excinfo:
        enforce_wasm_least_authority(ir, policy, modules)
    msg = str(excinfo.value)
    assert "declared caps subset-of policy-allowed FAILED" in msg
    assert "import section exceeds" not in msg
