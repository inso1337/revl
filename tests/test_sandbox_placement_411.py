"""Capability-enforced sandbox placement (roadmap item 411, Slice 1).

Slice 1 is the STATIC surface: the `[processes.<p>.sandbox]` and `[tiers]`-form
`[sandbox]` manifest tables (isolation + fs/net envelope) as a fourth placement
dimension over the 363 seam, the `[sandbox.needs]` table, the plan-time gate
(the advisory declared-need refusal, the fail-closed unmappable-need refusal,
the cell opaque-residue refusal), per-process config narrowing so a sibling's
secret never enters the boundary, and the boot-summary / `revl audit` envelope
print. No runtime jail is launched here (a sandboxed process still boots on the
ordinary runner; the isolation is DECLARED + gated, not yet ENFORCED); the
container/microVM/wasm-cell driver is Slice 2.

Levels:

1. surface + expansion; `[sandbox]` sugar splits a component into its own
   `sandbox_<component>` process; the both-forms refusal; table validation;
   additivity (no sandbox == byte-identical) (pure);
2. the plan-time gate; a net need against `net = "none"` refuses (advisory); an
   `env`/`exec` need refuses (fail-closed unmappable); a `*` reach under a cell
   refuses; the default (no entry) is admitted (pure);
3. narrowing + boot summary + linker blindness; driven through `run_placement`
   with a fake runner (no Docker): a sibling's secret is absent from the
   sandboxed spec, the boot summary prints the envelope + per-key reach, and the
   composition links identically with and without the sandbox assignment.
"""

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl.placement import (  # noqa: E402
    _fs_covers,
    _normalize_sandbox_table,
    _parse_need,
    expand_tiers,
    render_seam_transport_summary,
    sandbox_approval_rows,
    sandbox_capability_gate,
    sandbox_crossing_check,
    sandbox_relay_table,
)
from revl import sandbox_runtime as _sb  # noqa: E402

# --------------------------------------------------------------------------
# a two-component composition: a plain provider + a host-code-reaching component
# a placement can drop into a sandbox, consuming the provider across the seam.
# --------------------------------------------------------------------------

_APP = """
service Work { async fn compute(x: Str) -> Str }
service Job { emission fn run() -> Str }
extern emission fn fetch(u: Str) -> Str = @py { return u }
component Provider provides work: Work {
  provide work { async fn compute(x) = x }
}
component Untrusted requires work: Work provides job: Job {
  provide job { fn run() = fetch("x") }
}
"""

_COMP_NAMES = ["Provider", "Untrusted"]


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _ir(tmp: Path) -> dict:
    return compile_files([_write(tmp, "app.rvl", _APP)])


# ==========================================================================
# 1. surface + expansion
# ==========================================================================


def test_sandbox_sugar_splits_component_into_its_own_process():
    plc = {"default_tier": "py",
           "sandbox": {"Untrusted": {"isolation": "container", "image": "img:1"}}}
    expanded, err = expand_tiers(plc, _COMP_NAMES)
    assert err is None
    procs = expanded["processes"]
    # Untrusted is split OUT into its own sandbox process carrying the table;
    # Provider stays on the default tier.
    assert procs["sandbox_Untrusted"]["components"] == ["Untrusted"]
    assert procs["sandbox_Untrusted"]["sandbox"]["isolation"] == "container"
    assert procs["tier_py"]["components"] == ["Provider"]
    assert "Untrusted" not in procs["tier_py"]["components"]


def test_sandbox_component_keeps_its_tier():
    # isolation composes WITH the tier: a sandboxed component named in [tiers]
    # still runs on that tier (the sandbox is the jail, not the runtime).
    plc = {"default_tier": "py", "tiers": {"Untrusted": "go"},
           "sandbox": {"Untrusted": {"isolation": "container", "image": "i"}}}
    expanded, err = expand_tiers(plc, _COMP_NAMES)
    assert err is None
    assert expanded["processes"]["sandbox_Untrusted"]["backend"] == "go"


def test_sandbox_needs_table_is_preserved_through_expansion():
    plc = {"default_tier": "py",
           "sandbox": {"Untrusted": {"isolation": "container", "image": "i"},
                       "needs": {"fetch": ["net"]}}}
    expanded, err = expand_tiers(plc, _COMP_NAMES)
    assert err is None
    # the needs table is form-independent and survives for the gate; the
    # component ASSIGNMENT has been consumed into the synthesized process.
    assert expanded["sandbox"] == {"needs": {"fetch": ["net"]}}
    assert "Untrusted" not in expanded["sandbox"]


def test_both_forms_refusal_sandbox_alongside_processes():
    plc = {"processes": {"p": {"components": ["Provider"]}},
           "sandbox": {"Untrusted": {"isolation": "container", "image": "i"}}}
    _, err = expand_tiers(plc, _COMP_NAMES)
    assert err is not None
    assert "[sandbox]" in err and "[processes]" in err
    assert "mutually exclusive" in err


def test_sandbox_names_unknown_component_is_refused():
    plc = {"default_tier": "py",
           "sandbox": {"Nope": {"isolation": "container", "image": "i"}}}
    _, err = expand_tiers(plc, _COMP_NAMES)
    assert err is not None
    assert "unknown component" in err and "Nope" in err


def test_no_sandbox_is_byte_identical_expansion():
    # additivity: a [tiers] manifest with no sandbox expands exactly as before.
    plc = {"default_tier": "py", "tiers": {"Untrusted": "go"}}
    expanded, err = expand_tiers(plc, _COMP_NAMES)
    assert err is None
    assert "sandbox" not in expanded
    assert set(expanded["processes"]) == {"tier_py", "tier_go"}
    assert all("sandbox" not in p for p in expanded["processes"].values())


# --- table validation ------------------------------------------------------


def test_container_requires_image():
    norm, err = _normalize_sandbox_table({"isolation": "container"})
    assert norm is None and "image" in err


def test_unknown_isolation_rung_refused():
    norm, err = _normalize_sandbox_table({"isolation": "jail", "image": "i"})
    assert norm is None and "isolation" in err


def test_net_must_be_none_or_all():
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "net": "api.example.com"})
    assert norm is None and "net" in err


def test_envelope_defaults_to_deny_all():
    norm, err = _normalize_sandbox_table({"isolation": "container", "image": "i"})
    assert err is None
    assert norm["net"] == "none" and norm["fs"] == []


def test_cell_refuses_non_default_fs_net_as_unmappable():
    norm, err = _normalize_sandbox_table(
        {"isolation": "wasm-cell", "net": "all"})
    assert norm is None
    assert "wasm-cell" in err and "import set" in err
    # a cell with no fs/net envelope is fine
    ok, err2 = _normalize_sandbox_table({"isolation": "wasm-cell"})
    assert err2 is None and ok["isolation"] == "wasm-cell"


def test_platform_accepted_on_container_and_carried():
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "platform": "linux/arm64"})
    assert err is None and norm["platform"] == "linux/arm64"


def test_no_platform_normalizes_to_none():
    norm, err = _normalize_sandbox_table({"isolation": "container", "image": "i"})
    assert err is None and norm["platform"] is None


def test_platform_with_an_unconfirmable_arch_refused():
    # an arch the in-sandbox canary could not confirm by `uname -m` is refused
    # at plan time, not trusted to the runtime.
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "platform": "linux/sparc"})
    assert norm is None and "platform" in err


def test_platform_refused_on_the_wasm_cell_rung():
    # the cell is in-process: there is no container-of-an-arch to run.
    norm, err = _normalize_sandbox_table(
        {"isolation": "wasm-cell", "platform": "linux/arm64"})
    assert norm is None and "wasm-cell" in err and "platform" in err


def test_parse_need_vocabulary():
    assert _parse_need("net") == ("net", None, None)
    assert _parse_need("fs:/scratch:rw") == ("fs", "/scratch", "rw")
    assert _parse_need("fs:/data") == ("fs", "/data", "ro")
    # anything outside fs/net is the fail-closed unmappable case
    assert _parse_need("env")[0] == "?"
    assert _parse_need("exec")[0] == "?"


# ==========================================================================
# 2. the plan-time gate (pure)
# ==========================================================================

def _gate_setup(tmp: Path, envelope: dict, needs: dict):
    ir = _ir(tmp)
    processes = {
        "tier_py": {"backend": "py", "components": ["Provider"]},
        "sandbox_Untrusted": {"backend": "py", "components": ["Untrusted"],
                              "sandbox": envelope},
    }
    norm, err = _normalize_sandbox_table(envelope)
    assert err is None, err
    sandboxes = {"sandbox_Untrusted": norm}
    provides = {"tier_py": {"work": "Work"}, "sandbox_Untrusted": {"job": "Job"}}
    requires = {"tier_py": {}, "sandbox_Untrusted": {"work": "Work"}}
    owner = {"work": "tier_py", "job": "sandbox_Untrusted"}
    return ir, processes, sandboxes, needs, requires, provides, owner


def test_gate_net_need_against_no_net_is_refused(tmp_path):
    args = _gate_setup(tmp_path,
                       {"isolation": "container", "image": "i", "net": "none"},
                       {"fetch": ["net"]})
    err = sandbox_capability_gate(*args)
    assert err is not None
    # the headline refusal names component, capability, need, and grant
    assert "Untrusted" in err and "fetch" in err
    assert "needs net" in err and 'net = "none"' in err


def test_gate_net_need_with_net_all_boots(tmp_path):
    args = _gate_setup(tmp_path,
                       {"isolation": "container", "image": "i", "net": "all"},
                       {"fetch": ["net"]})
    assert sandbox_capability_gate(*args) is None


def test_gate_out_of_vocabulary_need_is_fail_closed(tmp_path):
    for bad in ("env", "exec"):
        args = _gate_setup(tmp_path,
                           {"isolation": "container", "image": "i"},
                           {"fetch": [bad]})
        err = sandbox_capability_gate(*args)
        assert err is not None, bad
        assert "[sandbox.needs]" in err and bad in err
        assert "cannot enforce" in err


def test_gate_missing_needs_entry_is_admitted_advisory_default(tmp_path):
    # the gate is advisory: a host-rooted extern with NO needs entry defaults to
    # "needs nothing" and is admitted even in a deny-all sandbox. The envelope,
    # not this gate, is the security boundary (Slice 2).
    args = _gate_setup(tmp_path,
                       {"isolation": "container", "image": "i", "net": "none"},
                       {})
    assert sandbox_capability_gate(*args) is None


def test_gate_fs_need_covered_by_mount(tmp_path):
    args = _gate_setup(
        tmp_path,
        {"isolation": "container", "image": "i", "fs": ["/scratch:rw"]},
        {"fetch": ["fs:/scratch:rw"]})
    assert sandbox_capability_gate(*args) is None
    # the same need against an empty fs grant refuses
    args2 = _gate_setup(tmp_path,
                        {"isolation": "container", "image": "i", "fs": []},
                        {"fetch": ["fs:/scratch:rw"]})
    err = sandbox_capability_gate(*args2)
    assert err is not None and "no covering mount" in err


def test_gate_no_sandbox_is_a_noop(tmp_path):
    ir = _ir(tmp_path)
    processes = {"p": {"backend": "py", "components": ["Provider", "Untrusted"]}}
    assert sandbox_capability_gate(ir, processes, {}, {}, {}, {}, {}) is None


# ==========================================================================
# 2b. the structural crossing-type walk (item 411 T4, pure)
# ==========================================================================
#
# A seam that crosses a SANDBOXED process's isolation boundary may carry only
# value-copyable types. `sandbox_crossing_check` walks each crossing type
# structurally (every record field and variant arm, transitively) and refuses an
# embedded resource handle -- naming the field path -- or an unresolvable type.
# It is the sandboxed-seam rule that pre-empts the tier-agnostic 363 check, so a
# handle two records deep is refused as clearly as one at the surface.

# every crossing shape a sandboxed seam might carry, declared once. No component
# provides the resource services (item 308 B1 refuses returning a handle across
# a signature in pure revl), so a resource seam is a host/remote-provided or
# bridged surface -- exactly the declaration surface these plan-layer checks
# read, wired below the same way `_gate_setup` wires requires/provides by hand.
_CROSSING_APP = """
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock(p: Int) -> Sock undo close_sock(0) = @py { return 0 }
type Conn = { sock: Sock, name: Str }
type Deep = { inner: Conn }
type Env = A(Str) | B(Conn)
service ResRet { async fn dial(x: Str) -> Conn }
service ResTop { async fn raw(x: Str) -> Sock }
service ResDeep { async fn deep(d: Deep) -> Str }
service ResVar { async fn ev(e: Env) -> Str }
service ResList { async fn many(x: Str) -> List[Conn] }
service CleanSvc { async fn compute(x: Str) -> Str }
component P provides clean: CleanSvc {
  provide clean { async fn compute(x) = x }
}
component U requires clean: CleanSvc provides job: CleanSvc {
  provide job { async fn compute(x) = x }
}
"""


def _crossing_setup(tmp: Path, iface: str, *, sandboxed_consumer: bool = True,
                    sandbox_present: bool = True):
    """A two-process placement whose sandboxed process shares the `svc` seam
    (interface `iface`) with an unsandboxed peer. `sandboxed_consumer` toggles
    which end is the sandbox; `sandbox_present=False` drops the sandbox entirely
    (the additive no-op case)."""
    ir = compile_files([_write(tmp, "app.rvl", _CROSSING_APP)])
    norm, err = _normalize_sandbox_table({"isolation": "container", "image": "i"})
    assert err is None, err
    if not sandbox_present:
        processes = {"a": {"backend": "py", "components": ["P"]},
                     "b": {"backend": "py", "components": ["U"]}}
        sandboxes: dict = {}
    elif sandboxed_consumer:
        processes = {"host": {"backend": "py", "components": ["P"]},
                     "sandbox_U": {"backend": "py", "components": ["U"],
                                   "sandbox": {}}}
        sandboxes = {"sandbox_U": norm}
    else:
        processes = {"sandbox_P": {"backend": "py", "components": ["P"],
                                   "sandbox": {}},
                     "host": {"backend": "py", "components": ["U"]}}
        sandboxes = {"sandbox_P": norm}
    a, b = list(processes)
    # `a` provides `svc: iface`, `b` requires it -> the seam crosses a <-> b.
    provides = {a: {"svc": iface}, b: {}}
    requires = {a: {}, b: {"svc": iface}}
    owner = {"svc": a}
    backends = {a: "py", b: "py"}
    return ir, sandboxes, processes, requires, provides, owner, backends


def test_crossing_resource_in_a_record_names_the_field_path(tmp_path):
    args = _crossing_setup(tmp_path, "ResRet")
    err = sandbox_crossing_check(*args)
    assert err is not None
    assert "resource handle" in err
    assert "ResRet.dial return is 'Conn'" in err
    # the load-bearing exit: the walk descends the record and names the leaf path
    assert "handle type 'Sock' (at Conn.sock)" in err
    assert "sandbox_U" in err


def test_crossing_top_level_resource_is_refused_identically(tmp_path):
    # "identically to the top-level resource": a bare handle return refuses with
    # no field-path suffix, the record case's leaf spelled at depth zero.
    args = _crossing_setup(tmp_path, "ResTop")
    err = sandbox_crossing_check(*args)
    assert err is not None
    assert "ResTop.raw return is 'Sock'" in err
    assert "embeds the handle type 'Sock'." in err  # no "(at ...)" suffix
    assert "(at " not in err


def test_crossing_walk_is_transitive_through_two_records(tmp_path):
    # Deep -> Conn -> Sock: the refusal names the full nested path, proving the
    # walk is transitive rather than one level deep.
    args = _crossing_setup(tmp_path, "ResDeep")
    err = sandbox_crossing_check(*args)
    assert err is not None
    assert "at Deep.inner.sock" in err


def test_crossing_walk_descends_a_variant_arm(tmp_path):
    args = _crossing_setup(tmp_path, "ResVar")
    err = sandbox_crossing_check(*args)
    assert err is not None
    assert "at Env.B.sock" in err


def test_crossing_walk_unwraps_a_builtin_carrier(tmp_path):
    # List[Conn]: a structural carrier is unwrapped to its element, which then
    # carries the handle.
    args = _crossing_setup(tmp_path, "ResList")
    err = sandbox_crossing_check(*args)
    assert err is not None
    assert "at Conn.sock" in err


def test_crossing_a_value_typed_seam_is_admitted(tmp_path):
    # CleanSvc.compute(Str) -> Str crosses cleanly by value copy.
    args = _crossing_setup(tmp_path, "CleanSvc")
    assert sandbox_crossing_check(*args) is None


def test_crossing_refuses_when_the_sandbox_is_the_provider(tmp_path):
    # the boundary is symmetric: a sandboxed PROVIDER serving a resource out is
    # refused the same as a sandboxed consumer taking one in.
    args = _crossing_setup(tmp_path, "ResRet", sandboxed_consumer=False)
    err = sandbox_crossing_check(*args)
    assert err is not None and "at Conn.sock" in err and "sandbox_P" in err


def test_crossing_an_unresolvable_type_fails_closed(tmp_path):
    # a nominal crossing type the plan cannot resolve to a primitive, builtin
    # carrier, declared record/variant, or handle is refused rather than assumed
    # copyable. Aliases are inlined before this layer, so `Ghost` is a real gap.
    ir = {"services": {"X": {"methods": {"m": {
              "params": [{"name": "a", "type": "Ghost"}],
              "returns": "Str", "async": True}}}},
          "types": {}, "externs": []}
    norm, _ = _normalize_sandbox_table({"isolation": "container", "image": "i"})
    sandboxes = {"sandbox_U": norm}
    processes = {"host": {"backend": "py"}, "sandbox_U": {"sandbox": {}}}
    provides = {"host": {"svc": "X"}, "sandbox_U": {}}
    requires = {"host": {}, "sandbox_U": {"svc": "X"}}
    owner = {"svc": "host"}
    err = sandbox_crossing_check(ir, sandboxes, processes, requires, provides,
                                 owner, {"host": "py", "sandbox_U": "py"})
    assert err is not None
    assert "unresolvable type" in err and "Ghost" in err


def test_crossing_check_is_a_noop_without_a_sandbox(tmp_path):
    # additive: with no sandboxed process the walk never runs, and the 363 check
    # (unchanged) owns every seam. A resource seam here yields None from T4.
    args = _crossing_setup(tmp_path, "ResRet", sandbox_present=False)
    assert sandbox_crossing_check(*args) is None


def test_crossing_check_leaves_a_non_sandboxed_seam_alone(tmp_path):
    # a sandbox exists, but the resource seam is between two UNSANDBOXED peers;
    # T4 only walks seams that touch a sandboxed process, so this one is left to
    # the 363 verdict (T4 returns None for it).
    ir = compile_files([_write(tmp_path, "app.rvl", _CROSSING_APP)])
    norm, _ = _normalize_sandbox_table({"isolation": "container", "image": "i"})
    # three processes: a sandboxed one with a clean seam, plus two unsandboxed
    # peers sharing the resource seam `svc`.
    processes = {
        "sandbox_U": {"backend": "py", "components": ["U"], "sandbox": {}},
        "host_a": {"backend": "py", "components": ["P"]},
        "host_b": {"backend": "py", "components": []},
    }
    sandboxes = {"sandbox_U": norm}
    provides = {"sandbox_U": {}, "host_a": {"svc": "ResRet"}, "host_b": {}}
    requires = {"sandbox_U": {}, "host_a": {}, "host_b": {"svc": "ResRet"}}
    owner = {"svc": "host_a"}
    backends = {"sandbox_U": "py", "host_a": "py", "host_b": "py"}
    assert sandbox_crossing_check(ir, sandboxes, processes, requires, provides,
                                  owner, backends) is None


# ==========================================================================
# 3. narrowing + boot summary + linker blindness (driven through run_placement
#    with a fake runner; no Docker, no real jail)
# ==========================================================================


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


class _StubDriver:
    """A sandbox runtime driver DOUBLE (item 411 Slice 2).

    These tests are about the static half — the split, the config narrowing, the
    boot summary — so they must not need a container runtime, exactly as they
    already stand in for the cordis-py runtime and the toolchain preflight. The
    real `container` driver (launch + in-sandbox canary + teardown, and every
    refusal that replaces a silent downgrade) is exercised against a real
    runtime in tests/test_sandbox_container_rung_411.py."""

    torn_down = 0

    def preflight(self, pname, env, ctx):
        return {"rung": env["isolation"], "runtime": "stub driver (test double)",
                "enforced": True, "image": env.get("image"), "mounts": [],
                "runtime_mounts": [], "pythonpath": None,
                "evidence": ["stub driver: no boundary established"]}, None

    def wrap(self, pname, cmd, proc_env, achieved):
        return cmd, proc_env

    def teardown(self, pname=None):
        type(self).torn_down += 1


def _drive(tmp_path, toml_text, driver=None):
    app = _write(tmp_path, "app.rvl", _APP)
    toml = _write(tmp_path, "t.toml", toml_text)
    specs: dict = {}
    real = _placement.subprocess.Popen

    def fake_popen(cmd, **k):
        if not str(cmd[-1]).endswith(".spec.json"):
            return real(cmd, **k)
        s = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        specs[s["name"]] = s
        return _FakeProc(s["name"])

    stub = _StubDriver() if driver is None else driver
    # item 411 T3: the seam relay is resolved through a factory, patched here to
    # a manager that resolves NO container runtime — so `_establish_seam_transport`
    # short-circuits and these plan-layer tests never touch a live daemon, the
    # same discipline `resolve_sandbox_driver` is stubbed under.
    from revl import sandbox_runtime as _sbrt
    with mock.patch.object(_placement, "_cordis_py_installed", lambda: True), \
         mock.patch.object(_placement, "_preflight", lambda *a, **k: None), \
         mock.patch.object(_placement, "resolve_sandbox_driver", lambda rung: stub), \
         mock.patch.object(_placement, "_new_relay_manager",
                           lambda pid, img: _sbrt.SeamRelayManager(pid, img, docker="")), \
         mock.patch.object(_placement.subprocess, "Popen", fake_popen):
        rc = _placement.run_placement([app], toml, once=True)
    return rc, specs


_SANDBOX_TOML = (
    'default_tier = "py"\n'
    '[sandbox]\n'
    'Untrusted = { isolation = "container", image = "revl-runner-py:3.12" }\n'
    '[config.Provider]\n'
    'secret = "postgres://S3CRET@h/db"\n'
    '[config.Untrusted]\n'
    'tuning = "ok"\n')


def test_sibling_secret_is_not_in_the_sandboxed_spec(tmp_path):
    # the load-bearing narrowing requirement: a secret in a NON-sandboxed
    # component's config must never enter the boundary. The split gives the
    # sandboxed component its own process, and per-process config narrowing
    # (363 F4) then carries only its own config.
    rc, specs = _drive(tmp_path, _SANDBOX_TOML)
    assert rc == 0, specs
    sb = specs["sandbox_Untrusted"]
    assert set(sb["config"]) == {"Untrusted"}
    assert "S3CRET" not in json.dumps(sb)
    # the provider's own (unsandboxed) process still carries its secret
    assert "Provider" in specs["tier_py"]["config"]


def test_boot_summary_prints_the_envelope(tmp_path, capsys):
    rc, _ = _drive(tmp_path, _SANDBOX_TOML)
    assert rc == 0
    out = capsys.readouterr().out
    # the placement tag carries the rung + envelope
    assert "sandbox_Untrusted[py, container: net=none fs=none]" in out
    # the per-process detail: the scope note, the seam-served provider reach,
    # and the net=none egress caveat
    assert "envelope confines fs+net only" in out
    assert "seam-served: work -> tier_py[py, unsandboxed: full host reach]" in out
    assert "bounds this process's own egress" in out
    # Slice 2: the boundary is ESTABLISHED by a runtime driver (here a double),
    # and the summary reports the rung actually achieved plus its evidence,
    # rather than a declaration nothing backs.
    assert "isolation ESTABLISHED by a runtime driver" in out
    assert "enforcement: rung container ACHIEVED via stub driver (test double)" in out


def test_unmappable_need_refuses_end_to_end(tmp_path):
    toml = (_SANDBOX_TOML
            + '[sandbox.needs]\n'
              'fetch = ["env"]\n')
    rc, _ = _drive(tmp_path, toml)
    assert rc == 1


def test_net_need_refuses_end_to_end(tmp_path):
    # Untrusted reaches `fetch` which the author declares needs net, but the
    # sandbox grants net = "none" -> a clean plan-time refusal (rc 1), nothing
    # spawns.
    toml = (_SANDBOX_TOML
            + '[sandbox.needs]\n'
              'fetch = ["net"]\n')
    rc, _ = _drive(tmp_path, toml)
    assert rc == 1


def test_no_sandbox_summary_is_byte_identical(tmp_path, capsys):
    # additivity end-to-end: the same composition with a plain [tiers] placement
    # prints the pre-411 tag and NO sandbox lines.
    rc, _ = _drive(tmp_path,
                   'default_tier = "py"\n'
                   '[config.Provider]\nsecret = "x"\n')
    assert rc == 0
    out = capsys.readouterr().out
    assert "tier_py[py]=[Provider,Untrusted]" in out
    assert "sandbox placement" not in out
    assert "envelope confines" not in out


def test_composition_links_identically_with_and_without_sandbox(tmp_path):
    # linker blindness (G2/G3/G4 run over the whole composition before the
    # split): the IR compiled from the source is the same regardless of the
    # placement's sandbox assignment; the sandbox keys never reach the checker.
    src = _write(tmp_path, "app.rvl", _APP)
    ir_a = compile_files([src])
    ir_b = compile_files([src])
    # the source links to the same manifest/services/components regardless of
    # any placement's sandbox assignment (linking runs before, and blind to,
    # the split).
    assert ir_a["manifest"] == ir_b["manifest"]
    assert (ir_a.get("services") or {}) == (ir_b.get("services") or {})
    assert [c["name"] for c in ir_a["components"]] == [c["name"] for c in ir_b["components"]]
    # and the two placements (with / without [sandbox]) both drive to rc 0,
    # proving the sandbox assignment changes no admission decision.
    rc_plain, _ = _drive(tmp_path, 'default_tier = "py"\n')
    rc_sandbox, _ = _drive(tmp_path,
                           'default_tier = "py"\n'
                           '[sandbox]\n'
                           'Untrusted = { isolation = "container", image = "i" }\n')
    assert rc_plain == 0 and rc_sandbox == 0


def test_processes_form_sandbox_table_validates_and_narrows(tmp_path):
    # the full-control [processes.<p>.sandbox] form (not sugar): parsed +
    # validated in run_placement, the same envelope print, and the same
    # config narrowing.
    toml = (
        '[processes.provider]\n'
        'components = ["Provider"]\n'
        '[config.Provider]\n'
        'secret = "S3CRET"\n'
        '[processes.worker]\n'
        'components = ["Untrusted"]\n'
        '[processes.worker.sandbox]\n'
        'isolation = "container"\n'
        'image = "revl-runner-py:3.12"\n'
        'net = "all"\n')
    rc, specs = _drive(tmp_path, toml)
    assert rc == 0, specs
    assert "S3CRET" not in json.dumps(specs["worker"])


def test_processes_form_invalid_sandbox_table_refuses(tmp_path):
    toml = (
        '[processes.worker]\n'
        'components = ["Provider", "Untrusted"]\n'
        '[processes.worker.sandbox]\n'
        'isolation = "container"\n')  # no image
    rc, _ = _drive(tmp_path, toml)
    assert rc == 1


def test_audit_view_surfaces_envelope_reach_and_vouched(tmp_path):
    from revl.placement import sandbox_audit_view
    ir = _ir(tmp_path)
    placement = {"default_tier": "py",
                 "sandbox": {"Untrusted": {"isolation": "container", "image": "i"},
                             "needs": {"fetch": ["net"]}}}
    lines, err = sandbox_audit_view(ir, placement)
    assert err is None
    blob = "\n".join(lines)
    assert "sandbox_Untrusted[py, container: net=none fs=none]" in blob
    assert "seam-served: work -> tier_py[py, unsandboxed: full host reach]" in blob
    assert "vouched self-contained (claimed, unverified): fetch" in blob


def test_audit_view_empty_without_sandbox(tmp_path):
    from revl.placement import sandbox_audit_view
    ir = _ir(tmp_path)
    lines, err = sandbox_audit_view(ir, {"default_tier": "py"})
    assert err is None and lines == []


# ==========================================================================
# 4. sandbox seam identity + certificate auto-promotion (item 411 T2), driven
#    through run_placement with a capturing driver double (no Docker, no jail)
# ==========================================================================


def test_sandbox_seam_participants_are_both_ends_of_a_boundary_seam():
    # both a sandboxed process and its host-side peer on a cross-boundary seam
    # need an identity; a remote pseudo-peer and a purely internal seam do not.
    processes = {"prov": {}, "cons": {}}
    requires = {"prov": {}, "cons": {"work": "Work"}}
    provides = {"prov": {"work": "Work"}, "cons": {}}
    owner = {"work": "prov"}
    backends = {"prov": "py", "cons": "py"}
    # only the consumer is sandboxed; the host-side provider is still a participant
    parts = _placement.sandbox_seam_participants(
        {"cons": {"isolation": "container"}}, processes, requires, provides,
        owner, backends, {}, 3.0)
    assert parts == {"prov", "cons"}
    # a sandbox with no cross-process seam names nobody
    solo_req, solo_prov = {"solo": {}}, {"solo": {"k": "K"}}
    assert _placement.sandbox_seam_participants(
        {"solo": {"isolation": "container"}}, {"solo": {}}, solo_req, solo_prov,
        {"k": "solo"}, {"solo": "py"}, {}, 3.0) == set()


class _CapturingDriver(_StubDriver):
    """A driver double that snapshots, per process, exactly what the sandbox gate
    handed it — while the placement directory (and the minted certs) still live.
    T3 has not landed, so the real driver would refuse a cross-boundary seam;
    this double lets the plan-layer cert wiring be seen end to end regardless."""

    def __init__(self):
        self.seen: dict = {}

    def preflight(self, pname, env, ctx):
        rec = {"seam_tls": ctx.get("seam_tls"),
               "mounts": _sb.seam_dir_mounts(ctx)}
        tls = ctx.get("seam_tls")
        if tls:
            pdir = Path(tls["dir"])
            rec["key_bytes"] = Path(tls["key"]).read_bytes()
            rec["dir_files"] = sorted(p.name for p in pdir.iterdir())
            rec["ca_key_in_view"] = (pdir / "seam_ca.key").exists()
            rec["ca_key_in_mint_root"] = (pdir.parent / "seam_ca.key").exists()
        self.seen[pname] = rec
        return super().preflight(pname, env, ctx)


_TWO_SANDBOX_TOML = (
    'default_tier = "py"\n'
    '[sandbox]\n'
    'Provider = { isolation = "container", image = "img:1" }\n'
    'Untrusted = { isolation = "container", image = "img:1" }\n')


def test_each_sandboxed_seam_peer_gets_its_own_identity_and_no_siblings_key(tmp_path):
    # two sandboxed processes on a seam between them: each is minted its own leaf
    # + key, and each sees ONLY its own key in its narrowed mount view.
    drv = _CapturingDriver()
    rc, _ = _drive(tmp_path, _TWO_SANDBOX_TOML, driver=drv)
    assert rc == 0, drv.seen
    prov, cons = drv.seen["sandbox_Provider"], drv.seen["sandbox_Untrusted"]
    # each end has its own conductor-minted material
    assert prov["seam_tls"] and cons["seam_tls"]
    assert prov["seam_tls"]["identity"] == "sandbox_Provider"
    assert cons["seam_tls"]["identity"] == "sandbox_Untrusted"
    # the two keys are distinct material, not a shared one
    assert prov["key_bytes"] != cons["key_bytes"]
    # each per-process directory holds only leaf + key + CA CERT (never the CA key)
    assert prov["dir_files"] == ["seam.crt", "seam.key", "seam_ca.crt"]
    assert cons["dir_files"] == ["seam.crt", "seam.key", "seam_ca.crt"]
    assert prov["ca_key_in_view"] is False and cons["ca_key_in_view"] is False
    # the CA key WAS minted — it just never enters a sandbox's view
    assert prov["ca_key_in_mint_root"] is True


def test_the_narrowed_mount_view_carries_own_spec_and_identity_only(tmp_path):
    drv = _CapturingDriver()
    rc, _ = _drive(tmp_path, _TWO_SANDBOX_TOML, driver=drv)
    assert rc == 0
    mounts = drv.seen["sandbox_Untrusted"]["mounts"]
    paths = [p for p, _ in mounts]
    tls = drv.seen["sandbox_Untrusted"]["seam_tls"]
    # the whole placement directory is NOT mounted; the spec and own identity are
    assert any(p.endswith("sandbox_Untrusted.spec.json") for p in paths)
    assert tls["cert"] in paths and tls["key"] in paths and tls["ca"] in paths
    assert all(mode == "ro" for _, mode in mounts)
    # no sibling's key and no CA key anywhere in the view
    assert drv.seen["sandbox_Provider"]["seam_tls"]["key"] not in paths
    assert not any(p.endswith("seam_ca.key") for p in paths)


def test_a_seam_free_sandbox_mints_nothing(tmp_path):
    # both components in ONE sandboxed process: the work seam is internal, so
    # nothing crosses the boundary and no identity is minted.
    toml = (
        '[processes.worker]\n'
        'components = ["Provider", "Untrusted"]\n'
        '[processes.worker.sandbox]\n'
        'isolation = "container"\n'
        'image = "img:1"\n')
    drv = _CapturingDriver()
    rc, _ = _drive(tmp_path, toml, driver=drv)
    assert rc == 0, drv.seen
    rec = drv.seen["worker"]
    assert rec["seam_tls"] is None
    # the mount view is just the process's own spec, read-only
    assert [m[1] for m in rec["mounts"]] == ["ro"]
    assert rec["mounts"][0][0].endswith("worker.spec.json")


def test_an_undeclared_sandbox_seam_identity_is_refused_under_an_operator_profile(tmp_path):
    # item 55 attribution: with an operator_profile named, a sandbox-seam
    # participant whose identity is not a declared operator is refused.
    opfile = tmp_path / "ops.txt"
    opfile.write_text("operator alice may swap on *\n", encoding="utf-8")
    toml = ('default_tier = "py"\n'
            f'operator_profile = "{str(opfile).replace(chr(92), "/")}"\n'
            '[sandbox]\n'
            'Untrusted = { isolation = "container", image = "img:1" }\n')
    drv = _CapturingDriver()
    rc, _ = _drive(tmp_path, toml, driver=drv)
    assert rc == 1
    # nothing reached the driver — the refusal is at plan time, before preflight
    assert drv.seen == {}


# ==========================================================================
# roadmap 422 F5: `_fs_covers` did no path normalization
# ==========================================================================

def test_fs_covers_refuses_a_traversing_need_that_a_mount_does_not_grant():
    """The executed finding: `_fs_covers` compared raw strings, so
    `/scratch/../../etc/shadow` was COVERED by a `/scratch:rw` mount while the
    same file spelled `/etc/shadow` was refused. Bounded while Slice 1 launches
    no jail and this gate is advisory, but `_fs_covers` is what Slice 2 inherits
    for deriving real mounts."""
    assert _fs_covers(["/scratch:rw"], "/etc/shadow", "rw") is False
    assert _fs_covers(["/scratch:rw"], "/scratch/../../etc/shadow", "rw") is False
    # the same escape through the other non-canonical spellings
    assert _fs_covers(["/scratch:rw"], "/scratch/./../etc/shadow", "rw") is False
    assert _fs_covers(["/scratch:rw"], "/scratch//../etc/shadow", "rw") is False
    assert _fs_covers(["/scratch:rw"], "/scratch/..", "rw") is False


def test_fs_covers_still_grants_what_the_mount_really_grants():
    """Canonicalizing must not cost the honest cases: prefix coverage, the mount
    itself, and the ro/rw ordering all read exactly as before."""
    assert _fs_covers(["/scratch:rw"], "/scratch", "rw") is True
    assert _fs_covers(["/scratch:rw"], "/scratch/sub/file", "rw") is True
    assert _fs_covers(["/data"], "/data/x", "ro") is True
    assert _fs_covers(["/data"], "/data/x", "rw") is False       # ro mount, rw need
    # a sibling that merely shares the prefix is not covered
    assert _fs_covers(["/scratch:rw"], "/scratch-evil/x", "rw") is False


def test_a_non_canonical_mount_covers_nothing_rather_than_widening():
    """The two sides fail closed differently, on purpose. Canonicalizing a
    traversing MOUNT could only WIDEN it (`/scratch/..` denotes `/`), and a
    defense-in-depth pass must never be the thing that widens a grant, so such a
    mount covers nothing here, and is refused outright where it enters."""
    assert _fs_covers(["/scratch/..:rw"], "/etc/shadow", "rw") is False
    assert _fs_covers(["/scratch/..:rw"], "/scratch/x", "rw") is False
    # a relative spelling has no meaning at this layer either
    assert _fs_covers(["scratch:rw"], "scratch/x", "rw") is False


def test_a_traversing_mount_is_refused_by_the_envelope_normalizer():
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "fs": ["/scratch/../..:rw"]})
    assert norm is None
    assert "canonical spelling" in err
    assert "'/'" in err            # item 274: names what it actually denotes
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "fs": ["scratch:rw"]})
    assert norm is None and "absolute path" in err
    # the canonical spelling is admitted unchanged
    norm, err = _normalize_sandbox_table(
        {"isolation": "container", "image": "i", "fs": ["/scratch:rw"]})
    assert err is None and norm["fs"] == ["/scratch:rw"]


def test_gate_refuses_a_traversing_fs_need_naming_the_canonical_spelling(tmp_path):
    """The gate refuses the SPELLING before comparing it, because the comparison
    is literal: an author whose need was silently normalized would believe the
    gate had checked the path they wrote."""
    args = _gate_setup(tmp_path,
                       {"isolation": "container", "image": "i",
                        "fs": ["/scratch:rw"]},
                       {"fetch": ["fs:/scratch/../../etc/shadow:rw"]})
    err = sandbox_capability_gate(*args)
    assert err is not None
    assert "[sandbox.needs]" in err and "fetch" in err
    assert "/etc/shadow" in err                      # item 274: the canonical form


def test_gate_admits_a_canonical_need_the_mount_really_covers(tmp_path):
    args = _gate_setup(tmp_path,
                       {"isolation": "container", "image": "i",
                        "fs": ["/scratch:rw"]},
                       {"fetch": ["fs:/scratch/work:rw"]})
    assert sandbox_capability_gate(*args) is None


# ==========================================================================
# T3: the relay forwarding table, derived purely from the seam graph
# ==========================================================================


def _relay_table(sandboxes, processes, provides, requires, owner, backends,
                 remotes=None):
    return sandbox_relay_table(
        "plc", sandboxes, processes, requires, provides, owner, backends,
        remotes or {}, 3.0)


def test_relay_row_for_a_sandboxed_provider_and_host_consumer():
    # the relay PUBLISHES a host loopback port and forwards it to the sandboxed
    # provider by network alias; the host consumer dials 127.0.0.1:<published>.
    plan = _relay_table(
        {"P": {"isolation": "container", "net": "none"}},
        {"P": {"seam_deadline": 5.0}, "C": {"seam_deadline": 5.0}},
        {"P": {"work": "Work"}, "C": {}}, {"P": {}, "C": {"work": "Work"}},
        {"work": "P"}, {"P": "py", "C": "py"})
    assert len(plan["rows"]) == 1
    row = plan["rows"][0]
    assert row["direction"] == "sandboxed-provider"
    assert "publish" in row["listen"] and row["target"] == "P:9443"
    ep = plan["consumer_endpoints"][("C", "work")]
    assert ep["via"] == "loopback" and ep["host"] == "127.0.0.1"
    assert plan["networks"] == {"P": "revl-sb-plc-P"}
    assert plan["relay"] == "revl-sb-plc-relay"


def test_relay_row_for_a_sandboxed_consumer_and_host_provider():
    # the relay listens on the consumer's network and forwards to the host
    # provider via host.docker.internal; the consumer dials <relay>:<port>.
    plan = _relay_table(
        {"C": {"isolation": "container", "net": "none"}},
        {"H": {"seam_deadline": 5.0}, "C": {"seam_deadline": 5.0}},
        {"H": {"work": "Work"}, "C": {}}, {"H": {}, "C": {"work": "Work"}},
        {"work": "H"}, {"H": "py", "C": "py"})
    assert len(plan["rows"]) == 1
    row = plan["rows"][0]
    assert row["direction"] == "sandboxed-consumer"
    assert row["listen"]["network"] == "revl-sb-plc-C"
    assert row["target"].startswith("host.docker.internal:")
    ep = plan["consumer_endpoints"][("C", "work")]
    assert ep["via"] == "relay" and ep["host"] == "revl-sb-plc-relay"


def test_relay_row_for_two_sandboxed_processes():
    plan = _relay_table(
        {"P": {"isolation": "container", "net": "none"},
         "C": {"isolation": "container", "net": "none"}},
        {"P": {"seam_deadline": 5.0}, "C": {"seam_deadline": 5.0}},
        {"P": {"work": "Work"}, "C": {}}, {"P": {}, "C": {"work": "Work"}},
        {"work": "P"}, {"P": "py", "C": "py"})
    assert len(plan["rows"]) == 1
    row = plan["rows"][0]
    assert row["direction"] == "both-sandboxed"
    assert row["listen"]["network"] == "revl-sb-plc-C"   # listens on consumer net
    assert row["target"] == "P:9443"                      # forwards to provider
    assert set(plan["networks"]) == {"P", "C"}


def test_an_internal_seam_needs_no_relay_row():
    # both components in one sandboxed process: the seam is internal, so the
    # table is empty and nothing is established (byte-identical to before T3).
    plan = _relay_table(
        {"W": {"isolation": "container", "net": "none"}},
        {"W": {"seam_deadline": 5.0}},
        {"W": {"work": "Work"}}, {"W": {"work": "Work"}},
        {"work": "W"}, {"W": "py"})
    assert plan["rows"] == []


def test_a_remote_consumed_key_is_not_this_relays_row():
    # a sandboxed consumer dialing a [remotes] provider: that provider serves
    # its own transport in another composition, not this relay.
    plan = _relay_table(
        {"C": {"isolation": "container", "net": "none"}},
        {"C": {"seam_deadline": 5.0}},
        {"C": {}}, {"C": {"rem": "Work"}}, {},
        {"C": "py"}, remotes={"rem": {"service": "Work"}})
    assert plan["rows"] == []


# ==========================================================================
# T5: the approval-across-boundary channel
# ==========================================================================


def test_approval_rows_reach_the_conductor_via_the_relay():
    plan = sandbox_approval_rows("plc", {"P"}, {"P": "revl-sb-plc-P"})
    assert len(plan["rows"]) == 1
    row = plan["rows"][0]
    assert row["direction"] == "approval" and row["id"] == "approval:P"
    assert row["listen"]["network"] == "revl-sb-plc-P"
    # the request is forwarded to the CONDUCTOR's own listener, not a direct escape
    assert row["target"] == f"host.docker.internal:{plan['listen_port']}"
    assert plan["endpoints"]["P"]["host"] == "revl-sb-plc-relay"


def test_a_process_with_no_class_c_gets_no_approval_row():
    plan = sandbox_approval_rows("plc", set(), {})
    assert plan["rows"] == [] and plan["endpoints"] == {}


def test_approval_row_ports_are_disjoint_from_seam_row_ports():
    # a placement with a seam row (port base 15000) and an approval row (base
    # 16001) must never collide the two.
    appr = sandbox_approval_rows("plc", {"P", "Q"}, {"P": "n1", "Q": "n2"})
    ports = [r["listen"]["port"] for r in appr["rows"]]
    assert all(p >= 16001 for p in ports)
    assert len(set(ports)) == len(ports)


# ==========================================================================
# T3/T5: the boot-summary lines
# ==========================================================================


def test_seam_transport_summary_prints_the_table_and_posture():
    plan = _relay_table(
        {"P": {"isolation": "container", "net": "none"}},
        {"P": {"seam_deadline": 5.0}, "C": {"seam_deadline": 5.0}},
        {"P": {"work": "Work"}, "C": {}}, {"P": {}, "C": {"work": "Work"}},
        {"work": "P"}, {"P": "py", "C": "py"})
    appr = sandbox_approval_rows("plc", set(), {})
    lines = render_seam_transport_summary(
        {"P": {"isolation": "container", "net": "none"}}, plan, appr)
    text = "\n".join(lines)
    assert "relay-mtls" in text and "holds no key" in text
    assert "P:9443" in text
    assert "posture P: net=none" in text
    assert "loopback network seam with cryptographic admission" in text


def test_seam_transport_summary_says_all_is_all():
    plan = _relay_table(
        {"P": {"isolation": "container", "net": "all"}},
        {"P": {"seam_deadline": 5.0}, "C": {"seam_deadline": 5.0}},
        {"P": {"work": "Work"}, "C": {}}, {"P": {}, "C": {"work": "Work"}},
        {"work": "P"}, {"P": "py", "C": "py"})
    lines = render_seam_transport_summary(
        {"P": {"isolation": "container", "net": "all"}}, plan,
        sandbox_approval_rows("plc", set(), {}))
    text = "\n".join(lines)
    assert "posture P: net=all" in text and "narrows NOTHING" in text


def test_seam_transport_summary_counts_the_approval_channel():
    appr = sandbox_approval_rows("plc", {"P"}, {"P": "revl-sb-plc-P"})
    lines = render_seam_transport_summary(
        {"P": {"isolation": "container", "net": "none"}},
        {"rows": [], "relay": "revl-sb-plc-relay"}, appr)
    text = "\n".join(lines)
    assert "approval-across-boundary channel (item 411 T5)" in text
    assert "via the conductor" in text


def test_seam_transport_summary_is_empty_without_a_cross_boundary_seam():
    empty = {"rows": [], "relay": "revl-sb-plc-relay"}
    assert render_seam_transport_summary(
        {}, empty, {"rows": [], "endpoints": {}}) == []
