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
    sandbox_capability_gate,
)

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


def _drive(tmp_path, toml_text):
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

    with mock.patch.object(_placement, "_cordis_py_installed", lambda: True), \
         mock.patch.object(_placement, "_preflight", lambda *a, **k: None), \
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
    # honesty: the isolation is declared + gated, not yet enforced
    assert "not yet enforced" in out


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
