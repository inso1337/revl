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
