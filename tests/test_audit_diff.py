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
