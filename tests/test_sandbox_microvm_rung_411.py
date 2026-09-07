"""The `microvm` isolation rung's runtime driver (roadmap item 411 T6, toward #107).

The strongest rung of the ladder: the confined body runs under its OWN kernel in
a hardware-accelerated VM, not merely in a namespace on the host kernel. This
file covers the rung at the level this slice implements it — the driver GATES on
`/dev/kvm` via `microvm_runtime_reason` (refusing with a named gap wherever the
accelerator is absent, which is every host in reach today), and where KVM IS
present it boots a microVM and CONFIRMS the boundary from inside with the boot
canary before refusing to host the component (the in-guest py runner + host-mode
seam relay are the step after the live boot is verified on a KVM lane). It never
downgrades to the container rung or to an unconfined process.

Levels:

1. plan-layer, with NO /dev/kvm at all: the rung resolves to a driver, the
   `microvm_runtime_reason` gate and its ordered diagnostics, the PURE
   `evaluate_microvm` judge over synthetic in-VM canary reports (good and every
   tampered shape), the PURE `microvm_vm_argv` monitor argv, and `preflight`'s
   refusals — wrong backend, unmet seam preconditions, the KVM-absent named gap,
   a boundary that fails to confirm, and the headline "boundary verified in-VM
   but hosting is the remaining step" refusal that carries the evidence. These
   need no hypervisor and run everywhere.
2. against a REAL microVM, gated on `run.microvm_runtime_reason()` being None
   (the same shape the wasm tier's `wasm_runtime_reason` gate uses): a KVM guest
   is booted and the in-VM canary CONFIRMS the read-only root and the net=none
   posture from inside, and preflight refuses carrying that in-VM evidence. This
   needs `/dev/kvm` plus a guest kernel + rootfs, which neither a GitHub-hosted
   runner nor a developer laptop has, so it SKIPS everywhere except the
   `sandbox-microvm` CI lane on a self-hosted KVM runner. It is stated plainly
   rather than hidden behind a green run: verifying this live boot on a KVM lane
   is the last requirement to close item 411's microVM rung.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import sandbox_runtime as _sb  # noqa: E402

_ENV_NONE = {"isolation": "microvm", "image": "img", "fs": [], "net": "none"}
_ENV_ALL = {"isolation": "microvm", "image": "img", "fs": [], "net": "all"}

# An in-VM boot canary report a correctly-confined net=none microVM produces —
# the same POSIX-sh canary shape the container rung reads.
_GOOD_NONE = {"ARCH": "x86_64", "ROUTES": "0", "EGRESS": "blocked:101",
              "ROOTFS": "ro", "PY": "yes", "RUNTIME": "image",
              "MOUNTS": {}, "CANARY": "done"}


def _seam(self_role="consumer", *, key="work", remote=False,
          provider="prov", provider_backend="py", provider_deadline=5.0,
          consumer="p", consumer_backend="py", consumer_deadline=5.0):
    """One cross-boundary seam edge (item 411 T1), the shape ctx['seams'] holds."""
    return {"key": key, "self_role": self_role, "remote": remote,
            "provider": provider, "provider_backend": provider_backend,
            "provider_deadline": provider_deadline, "consumer": consumer,
            "consumer_backend": consumer_backend, "consumer_deadline": consumer_deadline}


# ==========================================================================
# 1. plan-layer: no hypervisor needed
# ==========================================================================

def test_the_microvm_rung_now_resolves_to_a_driver():
    driver = _sb.resolve_driver("microvm")
    assert isinstance(driver, _sb.MicroVMDriver)
    assert driver.rung == "microvm"
    # it is not quietly the container rung's driver under another name.
    assert not isinstance(driver, _sb.ContainerDriver)


# -- the /dev/kvm availability gate ----------------------------------------

def test_runtime_reason_names_a_missing_dev_kvm_first():
    reason = _sb.microvm_runtime_reason(kvm_device="/nonexistent/kvm", environ={})
    assert reason is not None
    assert "/nonexistent/kvm is not present" in reason
    assert "Linux + /dev/kvm only" in reason


def test_runtime_reason_orders_monitor_then_assets(tmp_path):
    # a real, openable stand-in for /dev/kvm so the gate moves past clause 1.
    kvm = tmp_path / "kvm"
    kvm.write_bytes(b"")

    # clause 2: no monitor (a bogus override that does not resolve).
    r = _sb.microvm_runtime_reason(
        kvm_device=str(kvm), environ={"REVL_MICROVM_MONITOR": "/nonexistent/qemu"})
    assert r is not None and "no VM monitor resolved" in r

    # clause 3: monitor resolves (point the override at a real file), assets unset.
    monitor = tmp_path / "qemu-system-x86_64"
    monitor.write_bytes(b"")
    monitor.chmod(0o755)
    r = _sb.microvm_runtime_reason(
        kvm_device=str(kvm), environ={"REVL_MICROVM_MONITOR": str(monitor)})
    assert r is not None
    assert "boot assets are not configured" in r
    assert "REVL_MICROVM_KERNEL" in r and "REVL_MICROVM_ROOTFS" in r

    # clause 3b: assets set but the paths do not exist.
    r = _sb.microvm_runtime_reason(
        kvm_device=str(kvm),
        environ={"REVL_MICROVM_MONITOR": str(monitor),
                 "REVL_MICROVM_KERNEL": "/nonexistent/vmlinux",
                 "REVL_MICROVM_ROOTFS": "/nonexistent/rootfs.img"})
    assert r is not None and "kernel configured at" in r and "does not exist" in r


def test_runtime_reason_is_none_when_everything_is_present(tmp_path):
    kvm = tmp_path / "kvm"; kvm.write_bytes(b"")
    monitor = tmp_path / "qemu"; monitor.write_bytes(b""); monitor.chmod(0o755)
    kernel = tmp_path / "vmlinux"; kernel.write_bytes(b"")
    rootfs = tmp_path / "rootfs.img"; rootfs.write_bytes(b"")
    r = _sb.microvm_runtime_reason(
        kvm_device=str(kvm),
        environ={"REVL_MICROVM_MONITOR": str(monitor),
                 "REVL_MICROVM_KERNEL": str(kernel),
                 "REVL_MICROVM_ROOTFS": str(rootfs)})
    assert r is None


# -- the pure in-VM canary judge -------------------------------------------

def test_evaluate_microvm_confirms_a_well_formed_net_none_report():
    evidence, err = _sb.evaluate_microvm("p", _ENV_NONE, [], dict(_GOOD_NONE))
    assert err is None
    assert any("net=none confirmed in-VM" in ln for ln in evidence)
    assert any("root filesystem read-only" in ln for ln in evidence)


def test_evaluate_microvm_refuses_a_report_that_never_completed():
    report = dict(_GOOD_NONE); report.pop("CANARY")
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, [], report)
    assert err is not None and "did not complete" in err


def test_evaluate_microvm_refuses_a_guest_without_python():
    report = dict(_GOOD_NONE); report["PY"] = "no"
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, [], report)
    assert err is not None and "no `python3`" in err


def test_evaluate_microvm_refuses_a_net_none_that_kept_a_route():
    report = dict(_GOOD_NONE); report["ROUTES"] = "1"
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, [], report)
    assert err is not None and "route(s) inside the guest" in err
    assert "never silently downgraded" in err


def test_evaluate_microvm_refuses_a_net_none_whose_egress_was_not_blocked():
    report = dict(_GOOD_NONE); report["EGRESS"] = "open"
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, [], report)
    assert err is not None and "unconfirmed boundary is refused" in err


def test_evaluate_microvm_refuses_a_writable_root():
    report = dict(_GOOD_NONE); report["ROOTFS"] = "rw"
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, [], report)
    assert err is not None and "read-only virtio root did not take" in err


def test_evaluate_microvm_confirms_and_refuses_9p_mounts_by_mode():
    mounts = [("/data", "ro")]
    # not present in the guest mount table -> refuse
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, mounts, dict(_GOOD_NONE))
    assert err is not None and "does not see the 9p share" in err
    # present but rw where ro was declared -> refuse
    report = dict(_GOOD_NONE); report["MOUNTS"] = {"/data": "rw,relatime"}
    _, err = _sb.evaluate_microvm("p", _ENV_NONE, mounts, report)
    assert err is not None and "did not take" in err
    # present and ro -> confirmed
    report = dict(_GOOD_NONE); report["MOUNTS"] = {"/data": "ro,relatime"}
    evidence, err = _sb.evaluate_microvm("p", _ENV_NONE, mounts, report)
    assert err is None and any("/data ro, confirmed in-VM (9p)" in ln for ln in evidence)


def test_evaluate_microvm_refuses_a_platform_that_did_not_take():
    env = dict(_ENV_NONE); env["platform"] = "linux/arm64"
    report = dict(_GOOD_NONE); report["ARCH"] = "x86_64"  # host arch, not arm64
    _, err = _sb.evaluate_microvm("p", env, [], report)
    assert err is not None and "unconfirmed platform is refused" in err


def test_evaluate_microvm_net_all_confines_nothing():
    evidence, err = _sb.evaluate_microvm("p", _ENV_ALL, [], dict(_GOOD_NONE))
    assert err is None
    assert any("net=all" in ln for ln in evidence)


# -- the pure monitor argv --------------------------------------------------

def test_microvm_vm_argv_net_none_has_no_nic_and_a_readonly_root():
    argv = _sb.microvm_vm_argv("qemu", kernel="/k", rootfs="/r", ctl_dir="/c",
                               mounts=[], net="none")
    assert "-netdev" not in argv                      # no NIC at all -> T3 posture
    assert "microvm,accel=kvm" in argv                # the accelerator, not TCG
    assert any(a.startswith("file=/r,") and "readonly=on" in a for a in argv)
    assert "-serial" in argv and "stdio" in argv      # the canary's report path
    # the control share carrying the canary is always mounted read-only.
    assert any("mount_tag=" + _sb._MICROVM_CTL_TAG in a for a in argv)


def test_microvm_vm_argv_net_all_adds_a_user_nic():
    argv = _sb.microvm_vm_argv("qemu", kernel="/k", rootfs="/r", ctl_dir="/c",
                               mounts=[], net="all")
    assert "-netdev" in argv and any("user,id=net0" in a for a in argv)


def test_microvm_vm_argv_shares_each_fs_grant_over_9p_by_mode():
    argv = _sb.microvm_vm_argv("qemu", kernel="/k", rootfs="/r", ctl_dir="/c",
                               mounts=[("/ro", "ro"), ("/rw", "rw")], net="none")
    joined = " ".join(argv)
    assert "path=/ro,security_model=none,readonly=on" in joined
    assert "path=/rw,security_model=none" in joined and "path=/rw,security_model=none,readonly=on" not in joined
    assert "mount_tag=" + _sb.microvm_mount_tag(0) in joined
    assert "mount_tag=" + _sb.microvm_mount_tag(1) in joined


# -- preflight refusals -----------------------------------------------------

def _driver(reason="/dev/kvm is not present (test).", probe=None, monitor=None):
    return _sb.MicroVMDriver(runtime_reason=lambda: reason, probe=probe,
                             monitor=monitor)


def test_preflight_refuses_a_non_py_backend():
    achieved, err = _driver().preflight(
        "p", _ENV_NONE, {"backend": "rust", "seam_dir": "/t"})
    assert achieved is None
    assert "microvm" in err and "py" in err


def test_preflight_refuses_an_unmet_seam_precondition_before_touching_kvm():
    # a rust consumer holds only the UDS-only client — refused at the plan layer,
    # naming the tier, BEFORE the /dev/kvm gate is consulted (runtime_reason here
    # would refuse too, but the seam precondition is the one blamed).
    reached = []
    driver = _sb.MicroVMDriver(
        runtime_reason=lambda: (reached.append(1) or "kvm"))
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t",
                         "seams": [_seam(self_role="provider", provider="p",
                                         consumer="c", consumer_backend="rust")]})
    assert achieved is None
    assert "rust" in err and ("UDS-only" in err or "socket" in err)
    assert reached == [], "the KVM gate must not be reached before seam preconditions"


def test_preflight_refuses_with_the_named_gap_when_no_kvm():
    achieved, err = _driver(reason="/dev/kvm is not present here.").preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t"})
    assert achieved is None
    assert "microvm" in err and "cannot be booted here" in err
    assert "/dev/kvm is not present here." in err
    assert "never downgraded to a container or to an unconfined process" in err


def test_preflight_boots_and_verifies_then_refuses_hosting_carrying_evidence():
    # KVM is present (reason None) and the injected probe returns a confirmed
    # boundary; preflight refuses naming the remaining hosting step and carries
    # the in-VM evidence, exactly like the wasm-cell rung's substrate refusal.
    # seam_dir_mounts adds the placement dir as an rw mount, so the confirmed
    # report must show it in the guest mount table.
    report = dict(_GOOD_NONE); report["MOUNTS"] = {"/t": "rw,relatime"}
    driver = _sb.MicroVMDriver(runtime_reason=lambda: None,
                               probe=lambda *a, **k: (report, None))
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t"})
    assert achieved is None
    assert "boundary is established and verified in-VM" in err
    assert "net=none confirmed in-VM" in err
    assert "in-guest py runner" in err and "host-mode seam relay" in err
    assert "item 411 T6" in err


def test_preflight_refuses_when_the_booted_boundary_does_not_confirm():
    bad = dict(_GOOD_NONE); bad["ROOTFS"] = "rw"
    driver = _sb.MicroVMDriver(runtime_reason=lambda: None,
                               probe=lambda *a, **k: (bad, None))
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t"})
    assert achieved is None
    assert "read-only virtio root did not take" in err


def test_preflight_refuses_when_the_canary_could_not_run():
    driver = _sb.MicroVMDriver(
        runtime_reason=lambda: None,
        probe=lambda *a, **k: ({}, "the in-VM boot canary did not run (test)"))
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t"})
    assert achieved is None and "did not run" in err


def test_wrap_is_unreachable_and_fails_loudly():
    with pytest.raises(AssertionError, match="without the confinement"):
        _driver().wrap("p", ["python3"], None, {})


def test_teardown_is_a_no_op_without_a_booted_vm():
    driver = _driver()
    driver.teardown("p")
    driver.teardown()  # both forms are safe with nothing tracked


# ==========================================================================
# 2. against a real microVM (gated: run.microvm_runtime_reason() is None)
# ==========================================================================

_REASON = _sb.microvm_runtime_reason()


@pytest.mark.skipif(_REASON is not None,
                    reason=f"no bootable microVM here: {_REASON}")
def test_a_real_microvm_boots_and_confirms_the_boundary_in_vm():
    # Only on a KVM-capable lane with a guest kernel + rootfs provisioned (the
    # `sandbox-microvm` CI job on a self-hosted runner). The driver boots a real
    # microVM, the in-VM canary confirms the read-only root and the net=none
    # posture from inside, and preflight refuses carrying that evidence — the
    # live-boot milestone that closes item 411's microVM rung.
    driver = _sb.MicroVMDriver()
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/tmp"})
    assert achieved is None, "hosting is the remaining step; preflight refuses"
    assert "boundary is established and verified in-VM" in err
    assert "net=none confirmed in-VM" in err
    driver.teardown()
