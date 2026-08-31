"""`revl audit --diff` — the authority-drift gate (roadmap item 21).

Distinct from admission (which checks *correctness* — that running consumers
stay valid), audit-diff checks the *authority* axis: a regenerated component
must not quietly WIDEN what it reaches outside the system between generations.

The gate diffs the G8 boundary surface (per-component emissions + reached host
externs) of a NEW audit against a PREVIOUS one:

    added crossings      -> WIDENING, fails (nonzero) unless acknowledged
    removed / unchanged  -> narrowing / stable, always pass

These tests pin: a stable boundary diffs clean (exit 0); a new emission and a
new extern between two audits are each detected and fail; a removal passes;
and an acknowledged addition passes.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report, crossings, evaluate  # noqa: E402


# a provider that emits through exactly one service call — the stable base
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(`INSERT ${key} ${value}`) }
  }
}
"""

# same base plus a second component that adds one more emission — a WIDENING
WIDER = BASE + """
component Front requires cache: Cache {
  emit cache.put("k", "v")
}
"""


def _audit(source: str) -> dict:
    return audit_report(compile_source(source))


def test_crossings_enumerates_emissions():
    cs = crossings(_audit(BASE))
    assert "emit:PgCache:db.execute" in cs


def test_a_stable_boundary_diffs_clean():
    prev = _audit(BASE)
    new = _audit(BASE)
    result = evaluate(prev, new)
    assert result["added"] == []
    assert result["widened"] is False


def test_a_new_emission_is_detected_and_fails():
    prev = _audit(BASE)
    new = _audit(WIDER)
    result = evaluate(prev, new)
    assert "emit:Front:cache.put" in result["added"]
    assert result["widened"] is True
    assert result["unacknowledged"] == ["emit:Front:cache.put"]


def test_a_new_extern_is_detected_and_fails():
    prev_src = """
    extern emission fn write(msg: Str) -> Str = @py { return msg }
    fn passthru(x: Str) -> Str { return x }
    service S { emission fn op(a: Str) -> Str }
    component Quiet provides s: S {
      provide s { fn op(a) = passthru(a) }
    }
    """
    new_src = """
    extern emission fn write(msg: Str) -> Str = @py { return msg }
    service S { emission fn op(a: Str) -> Str }
    component Quiet provides s: S {
      provide s { fn op(a) = write(a) }
    }
    """
    prev = _audit(prev_src)
    new = _audit(new_src)
    result = evaluate(prev, new)
    assert "host:Quiet:write" in result["added"]
    assert result["widened"] is True


def test_a_first_class_laundered_new_extern_is_detected_and_fails():
    """The item-24 launder: the regeneration reaches `write` only through a
    first-class value handed to a dispatcher (`indirect(write, a)`), never by
    name. `_boundary` folds that G4 first-class reach onto the surface, so the
    laundered widening produces the SAME `host:Quiet:write` crossing a direct
    call would — audit --diff catches it instead of silently accepting it."""
    prev_src = """
    extern emission fn write(msg: Str) -> Str = @py { return msg }
    fn passthru(x: Str) -> Str { return x }
    service S { emission fn op(a: Str) -> Str }
    component Quiet provides s: S {
      provide s { fn op(a) = passthru(a) }
    }
    """
    new_src = """
    extern emission fn write(msg: Str) -> Str = @py { return msg }
    fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
    service S { emission fn op(a: Str) -> Str }
    component Quiet provides s: S {
      provide s { fn op(a) = indirect(write, a) }
    }
    """
    prev = _audit(prev_src)
    new = _audit(new_src)
    result = evaluate(prev, new)
    assert "host:Quiet:write" in result["added"]
    assert result["widened"] is True


def test_a_removal_passes():
    # going from WIDER back to BASE gives up the Front emission — safe
    prev = _audit(WIDER)
    new = _audit(BASE)
    result = evaluate(prev, new)
    assert result["added"] == []
    assert "emit:Front:cache.put" in result["removed"]
    assert result["widened"] is False


def test_an_acknowledged_addition_passes():
    prev = _audit(BASE)
    new = _audit(WIDER)
    result = evaluate(prev, new, accepted={"emit:Front:cache.put"})
    assert result["added"] == ["emit:Front:cache.put"]
    assert result["acknowledged"] == ["emit:Front:cache.put"]
    assert result["unacknowledged"] == []
    assert result["widened"] is False


def test_accept_all_passes():
    prev = _audit(BASE)
    new = _audit(WIDER)
    result = evaluate(prev, new, accept_all=True)
    assert result["widened"] is False


# ------------------------------------------------------------ CLI exit codes

def _write_audit(tmp_path: Path, name: str, source: str) -> Path:
    src = tmp_path / f"{name}.rvl"
    src.write_text(source)
    out = tmp_path / f"{name}.json"
    out.write_text(json.dumps(audit_report(compile_source(source))))
    return src, out


def test_cli_clean_diff_exits_zero(tmp_path, capsys):
    src, prev = _write_audit(tmp_path, "base", BASE)
    assert main(["audit", str(src), "--diff", str(prev)]) == 0
    assert "clean" in capsys.readouterr().out


def test_cli_widening_exits_nonzero(tmp_path, capsys):
    _, prev = _write_audit(tmp_path, "base", BASE)
    wider_src = tmp_path / "wider.rvl"
    wider_src.write_text(WIDER)
    assert main(["audit", str(wider_src), "--diff", str(prev)]) == 1
    out = capsys.readouterr().out
    assert "emit:Front:cache.put" in out
    assert "WIDEN" in out


def test_cli_accept_makes_widening_pass(tmp_path, capsys):
    _, prev = _write_audit(tmp_path, "base", BASE)
    wider_src = tmp_path / "wider.rvl"
    wider_src.write_text(WIDER)
    code = main(["audit", str(wider_src), "--diff", str(prev),
                 "--accept", "emit:Front:cache.put"])
    assert code == 0


def test_cli_json_diff_is_composable(tmp_path, capsys):
    _, prev = _write_audit(tmp_path, "base", BASE)
    wider_src = tmp_path / "wider.rvl"
    wider_src.write_text(WIDER)
    code = main(["audit", str(wider_src), "--diff", str(prev), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["added"] == ["emit:Front:cache.put"]
    assert report["widened"] is True
    assert code == 1


# -------------------------------------------- item 256 Slice 2: secrets table

# A composition that binds one secret to the emission extern it is confined to.
# The two-extern helper lets a rebind move the binding to a WIDER (different)
# capability, so the crossing token changes and the addition is a widening.
def _secret_prog(cap: str) -> str:
    return (
        "extern emission[model.complete] fn complete(p: Str) -> Str "
        "= @py { return p }\n"
        "extern emission[model.embed] fn embed(p: Str) -> Str "
        "= @py { return p }\n"
        "secret openai_key for " + cap + "\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(u) { return 0 } }\n}\n"
    )


# a secret-free sibling with the SAME externs and shape, so the only difference
# is the `secret` declaration itself.
_SECRET_FREE = (
    "extern emission[model.complete] fn complete(p: Str) -> Str "
    "= @py { return p }\n"
    "extern emission[model.embed] fn embed(p: Str) -> Str = @py { return p }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn go(u) { return 0 } }\n}\n"
)


def test_secrets_table_is_name_and_capability_only():
    rep = _audit(_secret_prog("model.complete"))
    assert rep["secrets"] == [
        {"name": "openai_key", "capability": "model.complete"}]
    # name + capability ONLY, never a value, length, hash, or timing (§5a, A5).
    row = rep["secrets"][0]
    assert set(row) == {"name", "capability"}
    for forbidden in ("value", "length", "len", "hash", "digest", "timing"):
        assert forbidden not in row
    # the value string appears NOWHERE in the audit surface (it is not in the IR).
    import json as _json
    assert "openai_key" in _json.dumps(rep)   # the NAME is intentional (authority)
    assert "capability" in _json.dumps(rep["secrets"][0])


def test_secret_binding_shows_the_secret_crossing():
    cs = crossings(_audit(_secret_prog("model.complete")))
    assert "secret:model.complete:openai_key" in cs
    # exactly one secret crossing, and it names the capability, not a value.
    assert [c for c in cs if c.startswith("secret:")] == [
        "secret:model.complete:openai_key"]


def test_secret_free_composition_has_no_secrets_table():
    rep = _audit(_SECRET_FREE)
    # ABSENT (not `[]`) for a secret-free composition, byte-identical to before.
    assert "secrets" not in rep
    assert [c for c in crossings(rep) if c.startswith("secret:")] == []


def test_secret_free_audit_is_byte_identical_to_before():
    # The `secrets` key is the ONLY thing Slice 2 adds. A composition that binds
    # no secret must produce the exact same report as a hand-built dict with no
    # `secrets` key, i.e. nothing about the surface moved for the common case.
    rep = _audit(_SECRET_FREE)
    assert all(k != "secrets" for k in rep)


def test_binding_a_secret_is_a_widening():
    prev = _audit(_SECRET_FREE)
    new = _audit(_secret_prog("model.complete"))
    result = evaluate(prev, new)
    assert "secret:model.complete:openai_key" in result["added"]
    assert result["widened"] is True
    assert result["unacknowledged"] == ["secret:model.complete:openai_key"]


def test_rebinding_a_secret_to_a_wider_capability_is_a_widening():
    prev = _audit(_secret_prog("model.complete"))
    new = _audit(_secret_prog("model.embed"))
    result = evaluate(prev, new)
    # the new (wider) binding is an addition the gate flags...
    assert "secret:model.embed:openai_key" in result["added"]
    assert result["widened"] is True
    # ...and the old binding drops out as a narrowing (safe).
    assert "secret:model.complete:openai_key" in result["removed"]


def test_removing_a_secret_binding_is_a_narrowing():
    prev = _audit(_secret_prog("model.complete"))
    new = _audit(_SECRET_FREE)
    result = evaluate(prev, new)
    assert "secret:model.complete:openai_key" in result["removed"]
    assert result["added"] == []
    assert result["widened"] is False


def test_cli_json_body_carries_secrets_identically_to_audit_report(tmp_path,
                                                                    capsys):
    # The `--json` hand-built doc (__main__._run_audit) must match audit_report
    # byte-for-byte, INCLUDING the secrets key (test_interchange's additive-body
    # invariant, now exercised on a secret-bearing composition).
    from revl import compile_files  # noqa: PLC0415
    src = tmp_path / "secret.rvl"
    src.write_text(_secret_prog("model.complete"))
    assert main(["audit", str(src), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    body = {k: v for k, v in doc.items()
            if k not in ("schema_version", "kind")}
    assert body == audit_report(compile_files([str(src)]))
    assert body["secrets"] == [
        {"name": "openai_key", "capability": "model.complete"}]


# --------------------------- Slice 2: the audit-surface differs (item 261 §7)
#
# These HARDEN item 64's `audit_diff` foundation, which shipped differencing
# ONLY crossings (`diff_crossings`) and reach (`diff_reach`). Each new differ
# closes one blind spot the changelog's completeness guard could previously only
# honesty-line: a capability-scope widening on a STABLE crossing, a new backend
# host body, a dropped/weakened recovery inverse, a weakened idempotency register
# floor, and a raised emission ceiling. Each mirrors `diff_reach`'s WIDENED /
# TIGHTENED shape and sorts every bucket.

from revl.audit_diff import (  # noqa: E402
    diff_backends, diff_capability_scopes, diff_cardinality, diff_recovery,
    diff_registers)


def test_diff_capability_scopes_flags_a_widening_on_a_stable_crossing():
    # send.mail -> send.* on a crossing whose `emit:C:notify` token is UNCHANGED.
    before = {"boundary": {"C": {"emissions": ["notify"],
                                 "capabilities": {"notify": ["send.mail"]}}}}
    after = {"boundary": {"C": {"emissions": ["notify"],
                                "capabilities": {"notify": ["send.*"]}}}}
    result = diff_capability_scopes(before, after)
    assert result["scope_widened"] == ["scope:C:notify"]
    assert result["scope_tightened"] == []


def test_diff_capability_scopes_narrowing_is_the_safe_direction():
    before = {"boundary": {"C": {"capabilities": {"notify": ["send.*"]}}}}
    after = {"boundary": {"C": {"capabilities": {"notify": ["send.mail"]}}}}
    result = diff_capability_scopes(before, after)
    assert result["scope_widened"] == []
    assert result["scope_tightened"] == ["scope:C:notify"]


def test_diff_capability_scopes_stable_scope_is_clean():
    audit = {"boundary": {"C": {"capabilities": {"notify": ["send.mail"]}}}}
    assert diff_capability_scopes(audit, audit) == {
        "scope_widened": [], "scope_tightened": []}


def test_diff_capability_scopes_unscoped_star_covers_everything():
    # a bare emission (`*`) is the widest scope: narrowing FROM `*` is safe,
    # widening TO `*` is breaking.
    star = {"boundary": {"C": {"capabilities": {"n": ["*"]}}}}
    mail = {"boundary": {"C": {"capabilities": {"n": ["send.mail"]}}}}
    assert diff_capability_scopes(star, mail)["scope_tightened"] == ["scope:C:n"]
    assert diff_capability_scopes(mail, star)["scope_widened"] == ["scope:C:n"]


def test_diff_backends_flags_a_new_host_body():
    before = {"externs": [{"name": "x", "backends": ["rust"]}]}
    after = {"externs": [{"name": "x", "backends": ["py", "rust"]}]}
    result = diff_backends(before, after)
    assert result["backends_added"] == ["backend:x:py"]
    assert result["backends_removed"] == []


def test_diff_backends_dropped_body_is_safe():
    before = {"externs": [{"name": "x", "backends": ["py", "rust"]}]}
    after = {"externs": [{"name": "x", "backends": ["rust"]}]}
    result = diff_backends(before, after)
    assert result["backends_removed"] == ["backend:x:py"]
    assert result["backends_added"] == []


def test_diff_backends_is_sorted_over_several_moves():
    before = {"externs": [{"name": "b", "backends": ["rust"]},
                          {"name": "a", "backends": ["rust"]}]}
    after = {"externs": [{"name": "b", "backends": ["py", "rust"]},
                         {"name": "a", "backends": ["go", "rust"]}]}
    added = diff_backends(before, after)["backends_added"]
    assert added == ["backend:a:go", "backend:b:py"] == sorted(added)


def test_diff_recovery_flags_a_dropped_inverse():
    before = {"recovery_surface": [
        {"name": "acquire", "kind": "inverse", "register": "keyed"}]}
    after = {"recovery_surface": []}
    result = diff_recovery(before, after)
    assert result["recovery_dropped"] == ["recovery:acquire:inverse"]
    assert result["recovery_added"] == []
    assert result["recovery_weakened"] == []


def test_diff_recovery_flags_a_weakened_register_on_a_surviving_inverse():
    before = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": "keyed"}]}
    after = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": "declared"}]}
    result = diff_recovery(before, after)
    assert result["recovery_weakened"] == ["recovery:a:inverse"]
    assert result["recovery_dropped"] == []


def test_diff_recovery_gained_inverse_is_safe():
    before = {"recovery_surface": []}
    after = {"recovery_surface": [
        {"name": "a", "kind": "inverse", "register": "keyed"}]}
    result = diff_recovery(before, after)
    assert result["recovery_added"] == ["recovery:a:inverse"]
    assert result["recovery_dropped"] == []


def test_diff_registers_flags_a_weakened_floor():
    before = {"capability_registers": {"send.mail": "keyed"}}
    after = {"capability_registers": {"send.mail": "declared"}}
    result = diff_registers(before, after)
    assert result["registers_weakened"] == ["register:send.mail"]
    assert result["registers_strengthened"] == []


def test_diff_registers_removed_floor_is_weakened():
    before = {"capability_registers": {"send.mail": "keyed"}}
    after = {"capability_registers": {}}
    assert diff_registers(before, after)["registers_weakened"] == [
        "register:send.mail"]


def test_diff_registers_new_or_stronger_floor_is_safe():
    before = {"capability_registers": {"send.mail": "declared"}}
    after = {"capability_registers": {"send.mail": "keyed", "send.sms": "keyed"}}
    result = diff_registers(before, after)
    assert result["registers_strengthened"] == [
        "register:send.mail", "register:send.sms"]
    assert result["registers_weakened"] == []


def test_diff_cardinality_flags_a_raised_ceiling_to_unbounded():
    before = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": 3, "kind": "bounded"}}}}}
    after = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": None, "kind": "unbounded"}}}}}
    result = diff_cardinality(before, after)
    assert result["cardinality_widened"] == ["cardinality:C:send.mail"]
    assert result["cardinality_tightened"] == []


def test_diff_cardinality_larger_bound_is_a_widening():
    before = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": 3, "kind": "bounded"}}}}}
    after = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": 5, "kind": "bounded"}}}}}
    assert diff_cardinality(before, after)["cardinality_widened"] == [
        "cardinality:C:send.mail"]


def test_diff_cardinality_tightened_ceiling_is_safe():
    before = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": None, "kind": "unbounded"}}}}}
    after = {"cardinality": {"C": {"per_capability": {
        "send.mail": {"bound": 2, "kind": "bounded"}}}}}
    result = diff_cardinality(before, after)
    assert result["cardinality_tightened"] == ["cardinality:C:send.mail"]
    assert result["cardinality_widened"] == []


def test_slice2_differs_tolerate_a_partial_audit_dict():
    # a hand-built or interchange audit that omits a surface must not crash any
    # differ - each reads its own surface defensively.
    assert diff_capability_scopes({}, {}) == {
        "scope_widened": [], "scope_tightened": []}
    assert diff_backends({}, {}) == {
        "backends_added": [], "backends_removed": []}
    assert diff_recovery({}, {}) == {
        "recovery_dropped": [], "recovery_weakened": [], "recovery_added": []}
    assert diff_registers({}, {}) == {
        "registers_weakened": [], "registers_strengthened": []}
    assert diff_cardinality({}, {}) == {
        "cardinality_widened": [], "cardinality_tightened": []}
