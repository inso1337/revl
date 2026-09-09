"""TEMPORARY microVM boot diagnostic (remove before merge).

Boots several qemu argv variants against the built kernel+rootfs and prints the
full stdout(serial)+stderr for each, so we can see the real qemu error and which
transport shape reaches CANARY=done. mounts=[] to isolate the boot from the 9p
fs-grant share.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from revl import sandbox_runtime as sb  # noqa: E402

KERNEL = os.environ["REVL_MICROVM_KERNEL"]
ROOTFS = os.environ["REVL_MICROVM_ROOTFS"]
QEMU = "qemu-system-x86_64"

ctl = Path("/tmp/revl-dbg-ctl")
ctl.mkdir(exist_ok=True)
(ctl / "canary.sh").write_text(sb._canary_script("none"), encoding="utf-8")
(ctl / "mounts").write_text("", encoding="utf-8")

base = sb.microvm_vm_argv(QEMU, kernel=KERNEL, rootfs=ROOTFS,
                          ctl_dir=str(ctl), mounts=[], net="none")


def run(label, argv):
    print("\n\n########## VARIANT:", label, "##########", flush=True)
    print("ARGV:", " ".join(argv), flush=True)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        print("RC:", p.returncode, flush=True)
        print("----- STDOUT (serial) -----\n", p.stdout, flush=True)
        print("----- STDERR -----\n", p.stderr, flush=True)
        print("CANARY_DONE:", "CANARY=done" in p.stdout, flush=True)
    except subprocess.TimeoutExpired as e:
        print("TIMEOUT", flush=True)
        print("----- partial STDOUT -----\n", e.stdout, flush=True)
        print("----- partial STDERR -----\n", e.stderr, flush=True)


# A: exact driver argv (baseline — expected to reproduce the failure).
run("A-exact-driver-argv", list(base))

# B: same but microvm with pcie=on (gives if=virtio a PCI bus).
b = list(base)
i = b.index("microvm,accel=kvm")
b[i] = "microvm,accel=kvm,pcie=on"
run("B-pcie-on", b)

# C: replace `-drive ...if=virtio...` with if=none + explicit virtio-blk-device
# (mmio blk, no PCI needed).
c = []
skip = False
for tok in base:
    if tok.startswith("file=") and "if=virtio" in tok:
        # rebuild as if=none,id=vda0 and inject the device after.
        newtok = tok.replace("if=virtio", "if=none,id=vda0")
        c.append(newtok)
        c.append("-device")
        c.append("virtio-blk-device,drive=vda0")
    else:
        c.append(tok)
run("C-virtio-blk-device-mmio", c)
