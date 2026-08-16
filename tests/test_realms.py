"""v2: isolation realms & interception — frontend semantics
(docs/design-v2-realms.md). Runtime verification lives in
backends/python/tests/test_v2_realms.py."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402

EXAMPLES = ROOT / "examples"


def test_tenants_example_compiles_at_v2():
    ir = compile_files([str(EXAMPLES / "tenants.rvl")])
    assert ir["ir_version"] == 2
    stores = {c["name"]: c for c in ir["components"]}
    assert stores["TenantAStore"]["isolate"] == {"kv": "tenant_a"}
    assert stores["TenantAApp"]["intercept"] == {"kv": {"quota": 5, "tags": ["tenant_a"]}}
    # both stores provide `kv`; per-realm G2 makes that legal
    providers = [c["name"] for c in ir["components"] if "kv" in c["provides"]]
    assert providers == ["TenantAStore", "TenantBStore"]


def test_v1_documents_stay_at_version_1():
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    assert ir["ir_version"] == 1
    assert all("isolate" not in c and "intercept" not in c for c in ir["components"])
    for entry in ir["manifest"]["components"]:
        assert set(entry) == {"name", "file", "inject", "provides"}


def test_realms_break_cycles():
    """A would-be cycle across realms is not a cycle: the consumer's realm
    doesn't match the provider's, so no edge exists (G3 realm-aware)."""
    src = """
    service A { fn ping(t: Str) -> Str }
    service B { fn pong(t: Str) -> Str }
    component Alpha requires b: B provides a: A {
      isolate a in realm("left")
      provide a { fn ping(t) = b.pong(t) }
    }
    component Beta requires a: A provides b: B {
      provide b { fn pong(t) = a.ping(t) }
    }
    """
    # Beta requires `a` in the shared realm; Alpha provides `a` in realm
    # "left" — unresolved, so no Alpha -> Beta edge, no cycle
    ir = compile_source(src, "cycle.rvl")
    assert ir["ir_version"] == 2
    # and the same source WITHOUT the isolate is the g3 cycle
    with pytest.raises(RevlError, match="dependency cycle"):
        compile_source(src.replace('isolate a in realm("left")', ""), "cycle2.rvl")


def test_admission_gate_is_realm_aware():
    running = compile_files([str(EXAMPLES / "tenants.rvl")])
    # a third store in a fresh realm admits cleanly
    third = """
    service Kv { fn get(k: Str) -> Opt[Str] fn set(k: Str, v: Str) }
    component TenantCStore provides kv: Kv {
      isolate kv in realm("tenant_c")
      let store = effect Map.new() undo store.drop()
      provide kv {
        fn get(k) = store.get(k)
        fn set(k, v) { effect store.insert(k, v) undo store.remove(k) }
      }
    }
    """
    admitted = _admit(third, running)
    names = {e["name"] for e in admitted["manifest"]["components"]}
    assert "TenantCStore" in names and "TenantAStore" in names
    # ...but the same store in an OCCUPIED realm conflicts through ambient
    with pytest.raises(RevlError, match="in realm `tenant_a` is provided by both"):
        _admit(third.replace("tenant_c", "tenant_a").replace("TenantCStore", "Intruder"), running)


def _admit(source: str, running: dict):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".rvl", delete=False) as handle:
        handle.write(source)
    return compile_files([handle.name], manifest=running)


def test_audit_renders_realms(capsys):
    from revl.__main__ import main

    assert main(["audit", str(EXAMPLES / "tenants.rvl")]) == 0
    out = capsys.readouterr().out
    assert "kv@tenant_a" in out
    assert "intercept: kv {'quota': 5" in out
