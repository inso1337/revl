"""TEMPORARY microVM boot diagnostic (remove before merge).

Mirrors MicroVMDriver._run_probe exactly (same ctl dir, canary.sh, mounts
manifest, argv, net=none, mounts=[("/tmp","rw")] as the real test uses) but
prints the FULL serial console + stderr, which the driver only tails to 240
chars. Lets us see the whole boot -> 9p mount -> canary -> poweroff in one run.
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

# the same shape the real test drives: seam_dir=/tmp, no spec_path -> one rw 9p
# grant of /tmp (tag revl-fs-0).
mounts = [("/tmp", "rw")]
ctl = Path("/tmp/revl-microvm-dbg")
ctl.mkdir(exist_ok=True)
(ctl / "canary.sh").write_text(sb._canary_script("none"), encoding="utf-8")
(ctl / "mounts").write_text(
    "".join(f"{sb.microvm_mount_tag(i)} {p} {m}\n"
            for i, (p, m) in enumerate(mounts)), encoding="utf-8")

argv = sb.microvm_vm_argv(QEMU, kernel=KERNEL, rootfs=ROOTFS,
                          ctl_dir=str(ctl), mounts=mounts, net="none")
print("ARGV:", " ".join(argv), flush=True)
print("MOUNTS MANIFEST:\n" + (ctl / "mounts").read_text(), flush=True)
try:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    print("RC:", p.returncode, flush=True)
    print("----- STDOUT (serial) -----\n", p.stdout, flush=True)
    print("----- STDERR -----\n", p.stderr, flush=True)
    print("CANARY_DONE:", "CANARY=done" in p.stdout, flush=True)
except subprocess.TimeoutExpired as e:
    print("TIMEOUT (guest did not power off)", flush=True)
    out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
    print("----- partial STDOUT (serial) -----\n", out, flush=True)
    print("----- partial STDERR -----\n", err, flush=True)
    print("CANARY_DONE:", "CANARY=done" in out, flush=True)
