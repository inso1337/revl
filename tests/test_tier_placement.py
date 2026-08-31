"""Per-component tier placement (roadmap item 363).

A composition declares which backend tier each component emits to, in the
placement manifest, as a `[tiers]` table + `default_tier` (no `.rvl` source
change). The conductor expands `[tiers]` into one synthesized process per
distinct tier and rides the EXISTING cross-process seam for cross-tier calls:
there is no new in-process cross-tier FFI. Emission narrows to a per-process
placement slice (the general form of `ts_safe_ir`), so each tier's whole-
document `emit(ir)` sees only what it hosts and multiple artifacts are built.

Levels, coarsest-runtime-need last:

1. surface + expansion — `[tiers]` -> synthesized `[processes]`, the both-forms
   refusal, the wasm refusal, `--backend`+`--placement` refusal (pure);
2. the placement slice — additive (a whole-hosting slice is the full IR
   byte-identical) and unblocking (a py-only extern never reaches rust) (pure);
3. the tier-capability gate — a component on an incapable tier refused at plan
   time naming component+tier, never a toolchain error (pure, emitters as the
   oracle);
4. the boundary checks — a resource-type crossing refused, a sync crossing
   named (pure);
5. the full two-tier boot — a py control plane probes a go hot worker across
   the seam; skips cleanly when the go toolchain or cordis is absent.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.placement import (  # noqa: E402
    cross_tier_boundary_check,
    expand_tiers,
    placement_slice,
    tier_capability_gate,
)

# --------------------------------------------------------------------------
# a two-tier composition: a hot worker + a control plane that reaches it
# --------------------------------------------------------------------------

_APP = """
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
    """A stand-in child that comes up and tears down immediately, so a plan-time
    (`once=True`) conductor run exercises expansion + wiring without a runtime."""

    def __init__(self, name: str):
        self.name = name
        self._lines = [f"[{name}] UP", f"[{name}] DOWN"]
        self.stdin = self
        self.stdout = iter(list(self._lines))

    def write(self, _t):
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


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _ir(tmp: Path, app: str = _APP) -> dict:
    return compile_files([_write(tmp, "app.rvl", app)])


def _names(ir: dict) -> list[str]:
    return [c["name"] for c in ir.get("components") or []]


# --------------------------------------------------------------------------
# 1. surface + expansion
# --------------------------------------------------------------------------


def test_tiers_expands_to_one_process_per_tier(tmp_path):
    ir = _ir(tmp_path)
    plc = {"default_tier": "py", "tiers": {"HotWorker": "go"},
           "probe": ["work.compute('ping')"]}
    expanded, err = expand_tiers(plc, _names(ir))
    assert err is None
    procs = expanded["processes"]
    assert procs["tier_go"] == {"backend": "go", "components": ["HotWorker"]}
    assert procs["tier_py"]["backend"] == "py"
    assert procs["tier_py"]["components"] == ["ControlPlane"]
    # the top-level probe drives the default-tier (control-plane) process
    assert procs["tier_py"]["probe"] == ["work.compute('ping')"]
    # tiers/default_tier/probe are consumed, not left dangling
    assert "tiers" not in expanded and "default_tier" not in expanded


def test_no_tiers_manifest_is_returned_unchanged(tmp_path):
    # a classic [processes] manifest has neither key: byte-identical, a no-op
    plc = {"processes": {"p": {"components": ["HotWorker"]}}}
    expanded, err = expand_tiers(plc, ["HotWorker"])
    assert err is None and expanded is plc


def test_both_forms_in_one_file_is_refused(tmp_path):
    plc = {"tiers": {"HotWorker": "go"}, "processes": {"p": {}}}
    _, err = expand_tiers(plc, ["HotWorker", "ControlPlane"])
    assert err and "both" in err and "[processes]" in err


def test_wasm_is_not_a_placement_tier(tmp_path):
    _, err = expand_tiers({"tiers": {"HotWorker": "wasm"}},
                          ["HotWorker", "ControlPlane"])
    assert err and "wasm" in err and ("rust" in err and "go" in err)


def test_tiers_names_unknown_component(tmp_path):
    _, err = expand_tiers({"tiers": {"Ghost": "go"}}, ["HotWorker"])
    assert err and "Ghost" in err and "unknown" in err


def test_tiers_form_equals_hand_written_processes_form(tmp_path, monkeypatch):
    """The item's equivalence: a `[tiers]` manifest and the hand-written
    `[processes]` form grouping the same components onto the same tiers produce
    identical specs (only the tmp socket/module paths differ)."""
    app = _write(tmp_path, "app.rvl", _APP)

    def wire(monkeypatch):
        specs: dict = {}
        real_popen = _placement.subprocess.Popen

        def fake_popen(cmd, **kwargs):
            if not str(cmd[-1]).endswith(".spec.json"):
                return real_popen(cmd, **kwargs)
            spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
            specs[spec["name"]] = spec
            return _FakeProc(spec["name"])

        monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
        monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
        monkeypatch.setattr(_placement, "_build_go", lambda ir, tmp: "/fake/go/bin")
        monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
        return specs

    def _norm(spec: dict) -> dict:
        out = {k: v for k, v in spec.items()
               if k not in ("files", "refRoot", "stdlibRefRoot")}
        # drop tmp socket/module paths from proxies and serve
        out["proxies"] = {k: {kk: vv for kk, vv in v.items() if kk != "socket"}
                          for k, v in (spec.get("proxies") or {}).items()}
        if "serve" in out:
            out["serve"] = {k: v for k, v in out["serve"].items() if k != "socket"}
        return out

    tiers_specs = wire(monkeypatch)
    tiers_toml = _write(tmp_path, "tiers.toml",
                        'default_tier = "py"\n[tiers]\nHotWorker = "go"\n')
    assert _placement.run_placement([app], tiers_toml, once=True) == 0
    # the synthesized process names are tier_<backend>; map them back to a
    # canonical (backend -> normalized spec) view for the comparison
    tiers_by_backend = {s["backend"]: _norm(s) for s in tiers_specs.values()}

    hand_specs = wire(monkeypatch)
    hand_toml = _write(
        tmp_path, "hand.toml",
        "[processes.tier_py]\ncomponents = [\"ControlPlane\"]\n"
        "[processes.tier_go]\nbackend = \"go\"\ncomponents = [\"HotWorker\"]\n")
    assert _placement.run_placement([app], hand_toml, once=True) == 0
    hand_by_backend = {s["backend"]: _norm(s) for s in hand_specs.values()}

    # same wiring by construction: same provides/proxies/serve/components/depends
    for backend in ("py", "go"):
        t, h = tiers_by_backend[backend], hand_by_backend[backend]
        assert t["components"] == h["components"], backend
        assert t["provides"] == h["provides"], backend
        assert t["proxies"] == h["proxies"], backend
        assert t.get("serve") == h.get("serve"), backend


# --------------------------------------------------------------------------
# 2. the placement slice
# --------------------------------------------------------------------------


def test_whole_hosting_slice_is_the_full_ir_byte_identical(tmp_path):
    ir = _ir(tmp_path)
    # a process hosting every component gets the identical object back — the
    # additivity guarantee: a single-tier placement builds today's artifact.
    assert placement_slice(ir, set(_names(ir))) is ir
    assert placement_slice(ir, {"HotWorker", "ControlPlane"}) is ir


def test_slice_keeps_only_the_hosted_components_services_and_types(tmp_path):
    ir = _ir(tmp_path)
    sliced = placement_slice(ir, {"HotWorker"})
    assert [c["name"] for c in sliced["components"]] == ["HotWorker"]
    # services and types are kept whole (the seam needs the interface)
    assert sliced.get("services") == ir.get("services")


def test_slice_unblocks_a_py_only_extern(tmp_path):
    """A `@py`-only extern reached only by a py-placed component must NOT reach
    a native tier's slice, so the composition can place its hot worker on a
    native tier at all (the roadmap's motivating gap)."""
    app = _APP + """
extern pure fn py_only(x: Str) -> Str = @py { return x }
component Logger provides log: Work {
  provide log { async fn compute(x) = py_only(x) }
}
"""
    ir = compile_files([_write(tmp_path, "app.rvl", app)])
    assert "py_only" in {e["name"] for e in ir.get("externs") or []}
    # HotWorker on a native tier never reaches py_only -> it is not in the slice
    worker = placement_slice(ir, {"HotWorker"})
    assert "py_only" not in {e.get("name") for e in worker.get("externs") or []}
    # but a slice that DOES host the reaching component keeps it
    logger = placement_slice(ir, {"Logger"})
    assert "py_only" in {e.get("name") for e in logger.get("externs") or []}


# --------------------------------------------------------------------------
# 3. the tier-capability gate
# --------------------------------------------------------------------------

_FN_TYPE_APP = """
service Middleware { async fn wrap(next: (Int) -> Int) -> Int }
service Ctl { async fn run() -> Int }
component Handler provides mw: Middleware {
  provide mw { async fn wrap(next) = next(1) }
}
component Boss requires mw: Middleware provides ctl: Ctl {
  provide ctl { async fn run() = mw.wrap(v => v) }
}
"""


def test_incapable_tier_refused_at_plan_naming_component_and_tier(tmp_path):
    ir = compile_files([_write(tmp_path, "fn.rvl", _FN_TYPE_APP)])
    # a declared function type is refused on java
    problem = tier_capability_gate(
        ir, {"Handler": "pj", "Boss": "pp"}, {"pj": "java", "pp": "py"})
    assert problem is not None
    assert "Handler" in problem and "java" in problem
    # the emitter's own tier-limit reason rides through (no javac stderr)
    assert "function type" in problem


def test_the_same_component_is_fine_on_py(tmp_path):
    ir = compile_files([_write(tmp_path, "fn.rvl", _FN_TYPE_APP)])
    # placed on py in the same manifest, nothing is refused
    assert tier_capability_gate(
        ir, {"Handler": "pp", "Boss": "pp"}, {"pp": "py"}) is None


def test_capability_gate_is_a_noop_for_an_all_py_placement(tmp_path):
    ir = _ir(tmp_path)
    assert tier_capability_gate(
        ir, {"HotWorker": "p", "ControlPlane": "p"}, {"p": "py"}) is None


# --------------------------------------------------------------------------
# 4. the boundary checks
# --------------------------------------------------------------------------

_RESOURCE_APP = """
type Sock = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(0) = @py { return {"fd": 1} }
service Db { async fn run() -> Sock }
service Ctl { async fn go() -> Str }
component Store provides db: Db {
  provide db { async fn run() = open_sock() }
}
component Front requires db: Db provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""


def test_resource_type_crossing_a_tier_boundary_is_refused(tmp_path):
    ir = compile_files([_write(tmp_path, "res.rvl", _RESOURCE_APP)])
    requires = {"c": {"db": "Db"}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"db": "Db"}}
    owner = {"db": "w", "ctl": "c"}
    backends = {"c": "py", "w": "go"}   # cross-tier: py <- go
    problem, report = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert problem is not None
    assert "Sock" in problem and "Db" in problem


def test_resource_crossing_a_same_tier_seam_is_untouched(tmp_path):
    ir = compile_files([_write(tmp_path, "res.rvl", _RESOURCE_APP)])
    requires = {"c": {"db": "Db"}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"db": "Db"}}
    owner = {"db": "w", "ctl": "c"}
    backends = {"c": "py", "w": "py"}   # same tier: byte-identical to today
    problem, _ = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert problem is None


def test_sync_cross_tier_seam_is_permitted_and_named(tmp_path):
    sync_app = """
service Work { fn compute(x: Str) -> Str }
service Ctl { async fn go() -> Str }
component HotWorker provides work: Work {
  provide work { fn compute(x) = x }
}
component ControlPlane requires work: Work provides ctl: Ctl {
  provide ctl { async fn go() = "z" }
}
"""
    ir = compile_files([_write(tmp_path, "sync.rvl", sync_app)])
    requires = {"c": {"work": "Work"}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"work": "Work"}}
    owner = {"work": "w", "ctl": "c"}
    backends = {"c": "py", "w": "go"}
    problem, report = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert problem is None                       # permitted
    assert any("address-space-bound" in line for line in report)  # named


# --------------------------------------------------------------------------
# run.py: --backend and --placement are mutually exclusive
# --------------------------------------------------------------------------


def test_backend_and_placement_are_mutually_exclusive(tmp_path, capsys):
    from types import SimpleNamespace

    from revl.run import run_command
    args = SimpleNamespace(files=["app.rvl"], placement="p.toml",
                           backend="rust", once=False)
    assert run_command(args) == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


# --------------------------------------------------------------------------
# 5. the full two-tier boot (real go worker + py control plane)
# --------------------------------------------------------------------------


def _two_tier_ready() -> str | None:
    if shutil.which("go") is None:
        return "no go toolchain on PATH"
    if not _placement._cordis_py_installed():
        return "cordis-py runtime not installed (sh backends/python/setup.sh)"
    return None


def test_two_tier_composition_boots_and_a_call_crosses_the_seam(tmp_path, capfd):
    """The headline: a `[tiers]` manifest places HotWorker on go and the
    control plane on py; `revl run --placement --once` emits two artifacts,
    both boot, and the control-plane probe's call crosses the seam to the go
    worker and returns the value."""
    reason = _two_tier_ready()
    if reason:
        pytest.skip(reason)
    app = _write(tmp_path, "app.rvl", _APP)
    toml = _write(tmp_path, "tiers.toml",
                  'default_tier = "py"\n'
                  "probe = [\"work.compute('ping')\"]\n"
                  '[tiers]\nHotWorker = "go"\n')
    rc = _placement.run_placement([app], toml, once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    # both tiers named in the boot summary, one process each
    assert "tier_py[py]" in out and "tier_go[go]" in out
    # the probe ran on the py control plane and the value crossed back from go
    assert "'ping'" in out or "ping" in out
