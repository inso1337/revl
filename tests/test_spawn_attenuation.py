"""Capability attenuation on spawn (roadmap item 66).

A spawned child's capability set must be a checked SUBSET of its spawner's:
a spawn may narrow (pass down less), never widen (grant a boundary the parent
does not hold). Monotone shrinkage — the same direction §5 admits for purity.
This closes the activation-body hole: without it a supervisor is a capability
amplifier (docs/capability-attenuation.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


# A router that holds two tenant boundaries and spawns one worker per tenant,
# each scoped to its own store.
TWO_TENANTS = """
service StoreA { emission[kv_a] fn write_a(row: Str) -> Int }
service StoreB { emission[kv_b] fn write_b(row: Str) -> Int }
service Worker { emission fn tenant() -> Str }
component TenantAWorker requires kv_a: StoreA provides worker: Worker {
  provide worker { fn tenant() { emit kv_a.write_a("a") return "a" } }
}
component TenantBWorker requires kv_b: StoreB provides worker: Worker {
  provide worker { fn tenant() { emit kv_b.write_b("b") return "b" } }
}
component Router requires kv_a: StoreA requires kv_b: StoreB {
  let a = effect spawn TenantAWorker with { } undo a.dispose()
  let b = effect spawn TenantBWorker with { } undo b.dispose()
}
"""


def test_narrowing_spawn_admits():
    """A parent holding {kv_a, kv_b} may spawn a child that reaches only kv_a."""
    ir = compile_source(TWO_TENANTS, "t.rvl")
    instances = ir["manifest"]["instances"]
    by_child = {e["child"]: e for e in instances}
    assert by_child["TenantAWorker"]["granted"] == ["kv_a"]
    assert by_child["TenantBWorker"]["granted"] == ["kv_b"]


def test_least_authority_per_instance_is_enforced():
    """The tenant_a instance is granted kv_a and provably cannot reach kv_b,
    even though the spawner holds both — least authority, per instance."""
    ir = compile_source(TWO_TENANTS, "t.rvl")
    by_child = {e["child"]: e for e in ir["manifest"]["instances"]}
    a = by_child["TenantAWorker"]
    assert a["holds"] == ["kv_a", "kv_b"]
    assert "kv_b" not in a["granted"]          # cannot reach the sibling store
    assert a["attenuated"] == ["kv_b"]         # the boundary dropped on the way down
    b = by_child["TenantBWorker"]
    assert "kv_a" not in b["granted"]
    assert b["attenuated"] == ["kv_a"]


def test_audit_shows_the_attenuation_chain(capsys):
    """`revl audit` renders the spawner -> child narrowing per instance."""
    from revl.__main__ import main  # noqa: PLC0415

    src = ROOT / "examples" / "tenant_attenuation.rvl"
    rc = main(["audit", str(src)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "capability attenuation" in out
    assert "Router" in out and "TenantAWorker" in out
    assert "grants [kv_a]" in out
    assert "dropped: kv_b" in out


def test_widening_spawn_is_refused_with_the_chain_named():
    """A parent holding only kv_a cannot spawn a child that reaches kv_b."""
    src = """
service StoreA { emission[kv_a] fn write_a(row: Str) -> Int }
service StoreB { emission[kv_b] fn write_b(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Leaker requires kv_b: StoreB provides task: Task {
  provide task { fn go() { emit kv_b.write_b("x") return 0 } }
}
component Supervisor requires kv_a: StoreA {
  let l = effect spawn Leaker with { } undo l.dispose()
}
"""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "w.rvl")
    msg = str(excinfo.value)
    assert "Supervisor" in msg and "Leaker" in msg
    assert "granting it `kv_b`" in msg
    assert "holds only `kv_a`" in msg
    assert "never widen" in msg


def test_widening_is_transitive_over_the_spawn_graph():
    """A child that itself spawns a grandchild reaching kv_c makes its own
    reach include kv_c; spawning that child is widening if the parent lacks
    kv_c."""
    src = """
service StoreC { emission[kv_c] fn write_c(row: Str) -> Int }
service StoreA { emission[kv_a] fn write_a(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Grand requires kv_c: StoreC provides task: Task {
  provide task { fn go() { emit kv_c.write_c("x") return 0 } }
}
component Mid requires kv_c: StoreC {
  let g = effect spawn Grand with { } undo g.dispose()
}
component Top requires kv_a: StoreA {
  let m = effect spawn Mid with { } undo m.dispose()
}
"""
    with pytest.raises(RevlError, match="granting it `kv_c`"):
        compile_source(src, "t.rvl")


def test_pure_supervisor_cannot_amplify_to_host():
    """A supervisor that holds nothing cannot spawn a child that reaches an
    unnameable host boundary — the amplifier the rule forbids."""
    src = """
extern emission fn blast(msg: Str) -> Int = @py { return 0 }
service Task { emission fn go() -> Int }
component Blaster provides task: Task {
  provide task { fn go() { emit blast("boom") return 0 } }
}
component Sup {
  let b = effect spawn Blaster with { } undo b.dispose()
}
"""
    with pytest.raises(RevlError, match="unnameable host boundary"):
        compile_source(src, "h.rvl")


def test_no_spawn_means_no_instances_section():
    """A non-spawning composition carries no `instances` key — the manifest is
    byte-identical to before (additive, spawn-only)."""
    src = """
service Cache { fn get(k: Str) -> Str }
component C provides cache: Cache { provide cache { fn get(k) = "v" } }
"""
    ir = compile_source(src, "n.rvl")
    assert "instances" not in ir["manifest"]


def test_worker_holding_nothing_admits_under_any_parent():
    """A child that reaches no boundary is trivially a subset of every parent."""
    src = """
service Counter { fn value() -> Int }
component Worker provides counter: Counter {
  config { tag: Str }
  provide counter { fn value() = 0 }
}
component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }
"""
    ir = compile_source(src, "s.rvl")
    edge = ir["manifest"]["instances"][0]
    assert edge["granted"] == []
    assert edge["attenuated"] == []
