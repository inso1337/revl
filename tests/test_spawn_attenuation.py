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


# ---------------------------------------------------------------------------
# F1: the fold compares BOUNDARIES, never wiring-key spellings.
#
# A `requires` key is a per-component spelling; the boundary is the DECLARED
# capability token on the emission method the key resolves to. Keying the fold
# element by the wiring key made `covers` clause 1 compare two identifiers that
# name nothing in common, so renaming a key silently laundered a boundary. Each
# test below ADMITS if the fold is keyed by the wiring key.
# ---------------------------------------------------------------------------

FIXTURE = (ROOT / "examples" / "rejections"
           / "g4_spawn_widens_capability.rvl").read_text()


def test_renaming_the_wiring_key_cannot_launder_a_boundary():
    """The project's OWN g4 fixture, with the CHILD's `requires` key renamed
    from `kv_b` to `kv_a`. `StoreB` is untouched, its declared emission is still
    `kv_b`, and the child still writes to it — only the local spelling moved.
    The verdict must not move with it."""
    attack = (FIXTURE
              .replace("requires kv_b: StoreB", "requires kv_a: StoreB")
              .replace("emit kv_b.write_b(", "emit kv_a.write_b("))
    assert "emission[kv_b]" in attack          # the declaration is unchanged
    assert "requires kv_b" not in attack       # only the wiring key moved
    with pytest.raises(RevlError) as exc:
        compile_source(attack, "g4.rvl")
    msg = str(exc.value)
    assert "granting it `kv_b`" in msg         # the boundary, not the key
    assert "never widen them" in msg


def test_a_pure_key_cannot_be_widened_into_a_host_crossing():
    """The strongest form: the parent holds a PURE, non-emission service under
    key `notes`; the child wires the SAME key to a service declaring
    `fs.write(path="/etc")` and actually crosses it. The parent holds no
    `fs.write` at all, so this is amplification, not attenuation."""
    src = """
service Notes { fn read(k: Str) -> Str }
service Etc { emission[fs.write(path="/etc")] fn write(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires notes: Etc provides task: Task {
  provide task { fn go() { emit notes.write("pwned") return 0 } }
}
component Parent requires notes: Notes {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    with pytest.raises(RevlError) as exc:
        compile_source(src, "w.rvl")
    msg = str(exc.value)
    assert 'granting it `fs.write(path="/etc")`' in msg
    assert "holds only `notes`" in msg


def test_a_parent_bounded_to_tmp_refuses_a_child_writing_etc():
    """The parent's ONLY `fs.write` authority is bounded to `/tmp`; it also
    wires an unrelated pure `notes` key. The child crosses `fs.write` at
    `/etc` under the `notes` spelling. The unrelated key must not launder the
    crossing, and `/etc` is outside the parent's `/tmp` cone."""
    src = """
service Tmp { emission[fs.write(path="/tmp")] fn write(row: Str) -> Int }
service Notes { fn read(k: Str) -> Str }
service Etc { emission[fs.write(path="/etc")] fn write(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires notes: Etc provides task: Task {
  provide task { fn go() { emit notes.write("pwned") return 0 } }
}
component Parent requires store: Tmp requires notes: Notes {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    with pytest.raises(RevlError) as exc:
        compile_source(src, "w.rvl")
    msg = str(exc.value)
    assert 'granting it `fs.write(path="/etc")`' in msg
    assert 'holds only `fs.write(path="/tmp")`, `notes`' in msg


def test_a_key_named_boundary_is_not_a_declared_token_of_the_same_name():
    """A method declaring `emission` with NO capability list names no token, so
    the G2 wiring key names that boundary. That element lives in its own token
    namespace: a key spelled `notes` must not cover — nor be covered by — a
    DECLARED token spelled `notes`, in either direction. Both halves use the
    SAME wiring key on both sides, so a fold that compares spellings admits
    them."""
    key_held = """
service Plain { emission fn get(k: Str) -> Str }
service Declared { emission[notes] fn put(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires notes: Declared provides task: Task {
  provide task { fn go() { emit notes.put("x") return 0 } }
}
component Parent requires notes: Plain {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    with pytest.raises(RevlError, match="never widen them"):
        compile_source(key_held, "k1.rvl")

    token_held = """
service Plain { emission fn get(k: Str) -> Str }
service Declared { emission[notes] fn put(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires notes: Plain provides task: Task {
  provide task { fn go() { emit notes.get("x") return 0 } }
}
component Parent requires notes: Declared {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    with pytest.raises(RevlError, match="never widen them"):
        compile_source(token_held, "k2.rvl")


# --------------------------------------------------------------- no false alarms


def test_the_same_boundary_under_different_keys_still_admits():
    """The dual of the bypass: two components may wire the SAME declared
    boundary under different local keys. Comparing spellings refused this; the
    boundary comparison admits it."""
    src = """
service Fs { emission[fs.write(path="/tmp")] fn write(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires sink: Fs provides task: Task {
  provide task { fn go() { emit sink.write("x") return 0 } }
}
component Parent requires store: Fs {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    edge = compile_source(src, "ok.rvl")["manifest"]["instances"][0]
    assert edge["holds"] == ['fs.write(path="/tmp")']
    assert edge["granted"] == ['fs.write(path="/tmp")']
    assert edge["attenuated"] == []


def test_a_narrowing_child_under_a_different_key_admits():
    """A genuine attenuation — `/tmp` down to `/tmp/job` — reached through a
    differently spelled key. Admitted, and the chain shows the narrowing."""
    src = """
service Wide { emission[fs.write(path="/tmp")] fn write(row: Str) -> Int }
service Narrow { emission[fs.write(path="/tmp/job")] fn write(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires sink: Narrow provides task: Task {
  provide task { fn go() { emit sink.write("x") return 0 } }
}
component Parent requires store: Wide {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    edge = compile_source(src, "ok.rvl")["manifest"]["instances"][0]
    assert edge["granted"] == ['fs.write(path="/tmp/job")']
    assert edge["attenuated"] == ['fs.write(path="/tmp")']
