"""Second-pass adversarial-review hardening for per-component tier placement
(roadmap item 363, F1-F6).

Each test pins one security/soundness hole the review found in the landed
conductor, and one additivity guarantee (a genuinely value-typed / innocent
composition still crosses / builds unchanged):

F1  a resource handle NESTED in a user record crosses a tier seam UNREFUSED
    (the taint walk only matched resource NAMES in the signature string). The
    taint is now transitive over the type table, and the boundary refusal keys
    on a STRUCTURED reason kind, not the substring "resource type".
F2  the capability gate dry-ran `emit` while the build runs `emit_placement`;
    for a v3 doc with a top-level decl `emit` drops components, so a go
    component tier-limit escaped the gate and surfaced as a raw build error.
F3  a node-placed component reaching a py-only extern was silently DROPPED by
    `ts_safe_ir` instead of refused (boot crash on the missing plugin).
F4  the whole [config] table was handed to every process, leaking a secret to
    a tier that hosts no reader of it; refs were over-broad too.
F5  a top-level probe tiered away from the default vanished -> silent green.
F6  a component whose method merely has a PARAMETER named like a py-only extern
    was dragged into its native slice and mis-refused.
"""

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.distribute import _resource_taint, distributability  # noqa: E402
from revl.placement import (  # noqa: E402
    cross_tier_boundary_check,
    expand_tiers,
    placement_slice,
    swap_admission,
    tier_capability_gate,
)


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _ir(tmp: Path, app: str) -> dict:
    return compile_files([_write(tmp, "app.rvl", app)])


# --------------------------------------------------------------------------
# F1 — a resource handle nested in a record crosses UNREFUSED
# --------------------------------------------------------------------------

# item 308: `Db.run() -> Session` returns a resource CARRIER; B1 clause 3 refuses
# a provide method returning a tainted carrier (a `Session` wrapping a `Sock`),
# so the resource service is DECLARED here and its crossing exercised through the
# seam functions' own wiring — the taint/distributability/seam analyses read the
# service and type tables, not a component body. `open_sock` seeds the `Sock`
# taint base; `Session` is tainted transitively over the type table.
_NESTED_RES = """
type Sock = { fd: Int }
type Session = { conn: Sock, label: Str }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(0) = @py { return {"fd": 1} }
service Db { async fn run() -> Session }
service Ctl { async fn go() -> Str }
component Front provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""

# a genuinely value-typed nested record — must still cross by value
_NESTED_VAL = """
type Inner = { a: Int }
type Outer = { inner: Inner, label: Str }
service Db { async fn run() -> Outer }
service Ctl { async fn go() -> Str }
component Store provides db: Db {
  provide db { async fn run() = { inner: { a: 1 }, label: "x" } }
}
component Front requires db: Db provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""


def test_f1_resource_taint_is_transitive_over_the_type_table(tmp_path):
    ir = _ir(tmp_path, _NESTED_RES)
    # Sock is the bare handle; Session carries it -> both tainted to a fixpoint
    assert _resource_taint(ir) == {"Sock", "Session"}
    verdict = distributability(ir)["Db"]
    assert verdict["verdict"] == "address-space-bound"
    # the STRUCTURED resource record the boundary check keys on
    assert verdict["resources"] == [{"method": "run", "type": "Session"}]


def test_f1_nested_handle_crossing_a_tier_seam_is_refused(tmp_path):
    ir = _ir(tmp_path, _NESTED_RES)
    requires = {"c": {"db": "Db"}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"db": "Db"}}
    owner = {"db": "w", "ctl": "c"}
    backends = {"c": "py", "w": "go"}   # cross-tier: py <- go
    problem, _ = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert problem is not None
    assert "Session" in problem and "Db" in problem


# item 308: the swap test needs a running `Store` that PROVIDES the
# resource-carrier service. Under B1 that provider must OWN the handle it lends
# (a built carrier or a per-call acquire is refused), so `Store` acquires the
# `Session` at activation scope and returns its own handle. `Session` is still a
# resource crossing (it carries a handle), so the swap seam refuses it.
_NESTED_RES_SWAP = """
type Sock = { fd: Int }
type Session = { conn: Sock, label: Str }
extern pure fn close_session(h: Int) = @py { return None }
extern acquire fn open_session() -> Session undo close_session(0)
  = @py { return {"conn": {"fd": 1}, "label": "x"} }
service Db { async fn run() -> Session }
service Ctl { async fn go() -> Str }
component Store provides db: Db {
  let sess = effect open_session() undo close_session(0)
  provide db { async fn run() = sess }
}
component Front requires db: Db provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""


def test_f1_swap_also_refuses_a_nested_handle_crossing(tmp_path):
    p = _write(tmp_path, "app.rvl", _NESTED_RES_SWAP)
    ir = compile_files([p])
    candidate, error = swap_admission([p], ir, "Store", "go")
    assert candidate is None
    assert error is not None and "Session" in error


def test_f1_value_typed_nested_record_still_crosses(tmp_path):
    ir = _ir(tmp_path, _NESTED_VAL)
    assert _resource_taint(ir) == set()           # nothing tainted
    assert distributability(ir)["Db"]["verdict"] == "transport-safe"
    problem, _ = cross_tier_boundary_check(
        ir, {"c": {"db": "Db"}}, {"c": {"ctl": "Ctl"}, "w": {"db": "Db"}},
        {"db": "w", "ctl": "c"}, {"c": "py", "w": "go"}, ir.get("services") or {})
    assert problem is None                         # value copy crosses cleanly


def test_f1_boundary_refusal_survives_a_reason_wording_change(tmp_path):
    """The refusal keys on the structured `resources` kind, not the human
    reason substring — so re-wording the reason cannot silently disarm it."""
    ir = _ir(tmp_path, _NESTED_RES)
    real = distributability

    def reworded(doc):
        report = real(doc)
        for v in report.values():
            v["reasons"] = ["(reworded, no magic substring)" for _ in v["reasons"]]
        return report

    with mock.patch.object(_placement, "distributability", reworded):
        problem, _ = cross_tier_boundary_check(
            ir, {"c": {"db": "Db"}}, {"c": {"ctl": "Ctl"}, "w": {"db": "Db"}},
            {"db": "w", "ctl": "c"}, {"c": "py", "w": "go"}, ir.get("services") or {})
    assert problem is not None                     # still refused


# --------------------------------------------------------------------------
# F2 — the gate must dry-run the SAME entry point the build uses (go)
# --------------------------------------------------------------------------

_GO_ARROW = """
fn bump(x: Int) -> Int { return x + 1 }
service Mw { async fn wrap(next: (Int) -> Int) -> Int }
service Ctl { async fn run() -> Int }
component Handler provides mw: Mw {
  provide mw { async fn wrap(next) = next(1) }
}
component Boss requires mw: Mw provides ctl: Ctl {
  provide ctl { async fn run() = bump(mw.wrap(v => v)) }
}
"""


def test_f2_go_component_limit_is_caught_at_the_gate_not_the_build(tmp_path):
    ir = _ir(tmp_path, _GO_ARROW)
    module = _placement._emit_gate_module("go")
    sliced = placement_slice(ir, {"Boss"})
    # the divergence the fix closes: `emit` DROPS the component (a top-level fn
    # is present) and succeeds, so the gate used to pass; `emit_placement` (the
    # real build entry) keeps the component and raises the tier limit.
    module.emit(sliced, "emitted")                 # OLD gate path: no error
    import pytest
    with pytest.raises(Exception):
        module.emit_placement(sliced, "emitted")   # real build path: raises
    # the gate now uses emit_placement, so it refuses AT PLAN naming comp+tier
    problem = tier_capability_gate(
        ir, {"Handler": "pp", "Boss": "pg"}, {"pp": "py", "pg": "go"})
    assert problem is not None
    assert "Boss" in problem and "`go`" in problem and "arrow" in problem


# --------------------------------------------------------------------------
# F3 — a node-placed component reaching a py-only extern is refused, not dropped
# --------------------------------------------------------------------------

_NODE_PY_ONLY = """
extern pure fn py_only(x: Str) -> Str = @py { return x }
service Log { async fn write(x: Str) -> Str }
service Ctl { async fn go() -> Str }
component Logger provides log: Log {
  provide log { async fn write(x) = py_only(x) }
}
component Front requires log: Log provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""


def test_f3_node_placed_dirty_component_is_refused_naming_both(tmp_path):
    ir = _ir(tmp_path, _NODE_PY_ONLY)
    problem = tier_capability_gate(
        ir, {"Logger": "pn", "Front": "pp"}, {"pn": "node", "pp": "py"})
    assert problem is not None
    # names the component, the tier, AND the py-only extern it reaches
    assert "Logger" in problem and "node" in problem and "py_only" in problem


def test_f3_clean_node_placement_is_unaffected(tmp_path):
    # Front reaches no py-only extern -> node placement of Front is fine
    ir = _ir(tmp_path, _NODE_PY_ONLY)
    assert tier_capability_gate(
        ir, {"Logger": "pp", "Front": "pn"}, {"pp": "py", "pn": "node"}) is None


# --------------------------------------------------------------------------
# F4 — a secret in [config.X] is not delivered to a tier that hosts no reader
# --------------------------------------------------------------------------

_F4_APP = """
service Work { async fn compute(x: Str) -> Str }
service Control { async fn go() -> Str }
component HotWorker provides work: Work {
  provide work { async fn compute(x) = x }
}
component ControlPlane requires work: Work provides control: Control {
  provide control { async fn go() = work.compute("crossed") }
}
"""


class _FakeProc:
    def __init__(self, name):
        self.name = name
        self.stdin = self
        self.stdout = iter([f"[{name}] UP", f"[{name}] DOWN"])

    def write(self, _):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _drive(tmp_path, toml_text):
    app = _write(tmp_path, "app.rvl", _F4_APP)
    toml = _write(tmp_path, "t.toml", toml_text)
    specs: dict = {}
    real = _placement.subprocess.Popen

    def fake_popen(cmd, **k):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real(cmd, **k)
        s = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        specs[s["name"]] = s
        return _FakeProc(s["name"])

    with mock.patch.object(_placement, "_cordis_py_installed", lambda: True), \
         mock.patch.object(_placement, "_preflight", lambda *a, **k: None), \
         mock.patch.object(_placement, "_build_go", lambda ir, tmp: "/fake/go"), \
         mock.patch.object(_placement.subprocess, "Popen", fake_popen):
        rc = _placement.run_placement([app], toml, once=True)
    return rc, specs


def test_f4_secret_config_is_not_leaked_across_the_seam(tmp_path):
    rc, specs = _drive(
        tmp_path,
        'default_tier = "py"\n'
        '[tiers]\nHotWorker = "go"\n'
        '[config.ControlPlane]\ndb_url = "postgres://S3CRET@h/db"\n'
        '[config.HotWorker]\nthreads = 4\n')
    assert rc == 0
    go, py = specs["tier_go"], specs["tier_py"]
    # the go tier hosts only HotWorker: it must carry HotWorker's config and
    # NEVER ControlPlane's secret
    assert set(go["config"]) == {"HotWorker"}
    assert "S3CRET" not in json.dumps(go)
    # the reader's tier still gets its own config
    assert "ControlPlane" in py["config"]
    assert py["config"]["ControlPlane"]["db_url"].endswith("h/db")


# --------------------------------------------------------------------------
# F5 — a probe tiered away from the default refuses, not a silent green
# --------------------------------------------------------------------------


def test_f5_probe_with_no_default_tier_process_is_refused(tmp_path):
    plc = {"default_tier": "py",
           "tiers": {"HotWorker": "go", "ControlPlane": "go"},
           "probe": ["work.compute('ping')"]}
    _, err = expand_tiers(plc, ["HotWorker", "ControlPlane"])
    assert err is not None
    assert "probe" in err and "default tier" in err


def test_f5_probe_with_a_default_tier_component_still_attaches(tmp_path):
    plc = {"default_tier": "py", "tiers": {"HotWorker": "go"},
           "probe": ["work.compute('ping')"]}
    expanded, err = expand_tiers(plc, ["HotWorker", "ControlPlane"])
    assert err is None
    assert expanded["processes"]["tier_py"]["probe"] == ["work.compute('ping')"]


# --------------------------------------------------------------------------
# F6 — a parameter named like a py-only extern must not mis-refuse
# --------------------------------------------------------------------------

_F6_APP = """
extern pure fn py_only(x: Str) -> Str = @py { return x }
service Ctl { async fn go(py_only: Str) -> Str }
service Log { async fn write(x: Str) -> Str }
component Innocent provides ctl: Ctl {
  provide ctl { async fn go(py_only) = py_only }
}
component RealPy provides log: Log {
  provide log { async fn write(x) = py_only(x) }
}
"""


def test_f6_param_name_collision_does_not_pull_in_the_extern(tmp_path):
    ir = _ir(tmp_path, _F6_APP)
    # Innocent's method only has a PARAMETER named `py_only`; it never calls the
    # extern, so its slice must not include it
    sliced = placement_slice(ir, {"Innocent"})
    assert "py_only" not in {e.get("name") for e in sliced.get("externs") or []}
    # so Innocent is fine on a native tier (not mis-refused blaming it)
    assert tier_capability_gate(
        ir, {"Innocent": "pg", "RealPy": "pp"}, {"pg": "go", "pp": "py"}) is None


def test_f6_a_genuine_extern_reach_is_still_counted(tmp_path):
    ir = _ir(tmp_path, _F6_APP)
    # RealPy genuinely CALLS py_only -> it stays in the slice (no false drop)
    sliced = placement_slice(ir, {"RealPy"})
    assert "py_only" in {e.get("name") for e in sliced.get("externs") or []}
    # and RealPy on a native tier is correctly refused for it
    problem = tier_capability_gate(
        ir, {"Innocent": "pp", "RealPy": "pn"}, {"pp": "py", "pn": "node"})
    assert problem is not None and "RealPy" in problem and "py_only" in problem
