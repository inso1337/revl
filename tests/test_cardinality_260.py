"""Emission cardinality bounds, Slice 1 (item 260).

docs/design/260-emission-cardinality-bounds.md. Slice 1 proves an exact
per-capability crossing count for non-looping, non-recursive bodies and reports
`unbounded` (loudly, with a reason) for every loop, every recursion, every
first-class emitting arrow, and every crossing behind a host extern.

The load-bearing soundness property (§5.1, and the MEDIUM count-soundness fix):
the count fold reuses the EXACT reach `_boundary` computes, mirroring its
`walk_expr` arms, so a spawn-handle emission reached through an `instance-get`
(item 246) is COUNTED, never reported 0.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.cardinality import _merge_max, cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

EXAMPLES = ROOT / "examples"


def _card(src: str) -> dict:
    return cardinality(compile_source(src, "t.rvl"))


# --------------------------------------------------------------------------
# §2.1 - the exact count for non-looping bodies
# --------------------------------------------------------------------------

def test_straight_line_req_emissions_count_exactly():
    """Two `emit`s through the same required key sum to a proved `<= 2`."""
    card = _card("""
service DB { emission[db] fn exec(s: Str) -> Int }
service Svc { emission[db] fn run() -> Int }
component C requires db: DB provides svc: Svc {
  provide svc { fn run() -> Int { emit db.exec("a"); emit db.exec("b"); return 0 } }
}
""")
    assert card["C"]["verdict"] == "bounded"
    assert card["C"]["per_capability"]["db"] == {"bound": 2, "kind": "bounded"}


def test_spawn_handle_emission_is_counted_not_zero():
    """The MEDIUM fix: a component whose ONLY crossing is a provision-method
    call on a spawn handle (`w.wl.bump(...)`, reached through an `instance-get`,
    item 246) must COUNT the crossing, never report 0. A fold keyed only on
    `emit` steps would miss it entirely."""
    card = _card("""
service KV { emission[worklog] fn put(n: Int) -> Int }
service Worklog { emission[worklog] fn bump(n: Int) -> Int }
service Probe { emission fn go() -> Int }
component Worker requires worklog: KV provides wl: Worklog {
  config { tag: Str }
  provide wl { fn bump(n) { emit worklog.put(n); return n } }
}
component App requires worklog: KV provides probe: Probe {
  let w = effect spawn Worker with { tag: "a" } undo w.dispose()
  provide probe { fn go() -> Int { emit w.wl.bump(1); return 0 } }
}
""")
    # counted through the spawn handle, attributed to the spawned method's
    # declared capability, exactly as _boundary enumerates it.
    assert card["App"]["verdict"] == "bounded"
    assert card["App"]["per_capability"]["worklog"] == {"bound": 1,
                                                         "kind": "bounded"}
    # and it is NOT 0/absent
    assert card["App"]["per_capability"]["worklog"]["bound"] == 1


# --------------------------------------------------------------------------
# §2.3 - unbounded for every loop and every recursion (Slice 1)
# --------------------------------------------------------------------------

def test_recursion_is_unbounded_with_a_reason():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn rec(n: Int) -> Int { if (n <= 0) { return 0 } let x = boom(); return rec(n - 1) }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return rec(5) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None
    assert entry["kind"] == "unbounded"
    assert "recursion" in entry["reason"]
    assert "rec" in entry["reason"]


def test_loop_is_unbounded_with_a_reason():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn loopy(n: Int) -> Int {
  var t = 0
  var i = 0
  while (i < n) { let x = boom(); t = t + x; i = i + 1 }
  return t
}
component C provides svc: Svc {
  provide svc { fn run() -> Int { return loopy(5) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None
    assert "loop" in entry["reason"]


# --------------------------------------------------------------------------
# §5.1 - a crossing whose reach cannot be exactly counted is never 0
# --------------------------------------------------------------------------

def test_host_extern_reached_crossing_is_unbounded_never_zero():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn helper() -> Int { let x = boom(); return x }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return helper() } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = card["C"]["per_capability"]["boom"]
    assert entry["bound"] is None            # never an optimistic 0
    assert entry["kind"] == "unbounded"
    assert "host code" in entry["reason"]


def test_first_class_emitting_arrow_is_unbounded_never_zero():
    card = _card("""
service Svc { emission fn run() -> Int }
extern emission fn boom() -> Int = @py { return 1 }
fn dispatch(f: () -> Int) -> Int { return f() }
fn emitter() -> Int { let x = boom(); return x }
component C provides svc: Svc {
  provide svc { fn run() -> Int { return dispatch(emitter) } }
}
""")
    assert card["C"]["verdict"] == "unbounded"
    # the unnameable boundary `*` is present and unbounded, never counted 0
    star = card["C"]["per_capability"]["*"]
    assert star["bound"] is None
    assert star["kind"] == "unbounded"
    assert "first-class" in star["reason"]


# --------------------------------------------------------------------------
# §1 / Exit-5 - byte-identity of the per-component entry, top-level key present
# --------------------------------------------------------------------------

def test_crossing_free_component_entry_byte_identical_top_level_key_present():
    ir = compile_source("""
service Svc { fn run() -> Int }
component C provides svc: Svc { provide svc { fn run() = 0 } }
""", "t.rvl")
    report = audit_report(ir)
    # the top-level document gains the key even when nothing crosses (LOW fix)
    assert "cardinality" in report
    assert report["cardinality"] == {}
    # but the per-component boundary ENTRY is byte-identical to today: no
    # cardinality member is spliced into it.
    assert "cardinality" not in report["boundary"]["C"]
    assert report["boundary"]["C"] == {
        "emissions": [], "capabilities": {}, "compensated": 0,
        "awaits": 0, "externs": [],
    }


# --------------------------------------------------------------------------
# the audit surfaces carry it
# --------------------------------------------------------------------------

def test_audit_json_body_matches_audit_report(capsys):
    """`revl audit --json` carries `cardinality` and it is byte-for-byte the
    `audit_report` value (the additive-body invariant test relies on this)."""
    code = main(["audit", str(EXAMPLES / "user_cache.rvl"), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    body = {k: v for k, v in doc.items() if k not in ("schema_version", "kind")}
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    assert "cardinality" in body
    assert body["cardinality"] == audit_report(ir)["cardinality"]


def test_text_audit_renders_cardinality_line(capsys):
    code = main(["audit", str(EXAMPLES / "user_cache.rvl")])
    assert code == 0
    out = capsys.readouterr().out
    # UserCache crosses its `db` boundary exactly once per activation
    assert "cardinality:" in out
    assert "per activation" in out


# --------------------------------------------------------------------------
# the branch-count fold takes the MAX over arms, never the SUM
# --------------------------------------------------------------------------

def test_merge_max_is_max_not_sum():
    total = {"db": 1}                       # the scrutinee/cond side sums in
    _merge_max(total, [{"db": 2}, {"db": 1}, {"model": 3}])
    assert total == {"db": 3, "model": 3}   # 1 (base) + max(2,1)=2 -> 3; model 3
