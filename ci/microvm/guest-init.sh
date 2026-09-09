#!/bin/sh
# revl microVM guest init (PID 1), roadmap item 411 T6.
#
# The kernel boots this as `/sbin/init` over a read-only virtio-blk ext4 root
# (root=/dev/vda ro), with no initramfs and no init= override. Its whole job is
# the rootfs half of the driver's boot contract (src/revl/sandbox_runtime.py,
# MicroVMDriver): mount the pseudo-filesystems and the 9p shares, run the boot
# canary the driver staged into the control share, let its report stream to the
# serial console the driver captures, then power the guest off so qemu (started
# with -no-reboot) exits and the driver reads the report.
#
# The control share (mount tag revl-ctl) carries two files the driver wrote:
#   canary.sh  the POSIX-sh boot canary (the driver owns it; init only runs it)
#   mounts     one "TAG PATH MODE" line per fs-grant 9p share, because a 9p
#              mount tag is opaque and does not carry the identity path the
#              share must be mounted at.
set -u

BB=/bin/busybox

# pseudo-filesystems. devtmpfs is auto-mounted by the kernel
# (CONFIG_DEVTMPFS_MOUNT), but mount it defensively too.
$BB mount -t proc proc /proc 2>/dev/null
$BB mount -t sysfs sysfs /sys 2>/dev/null
$BB mount -t devtmpfs dev /dev 2>/dev/null

# the control share: canary in, report out. Mounted read-only, matching the
# readonly=on fsdev the driver passes for it.
$BB mkdir -p /revl-ctl
$BB mount -t 9p -o trans=virtio,version=9p2000.L,ro,access=any \
    revl-ctl /revl-ctl 2>/dev/null

# the fs-grant shares, each at its identity path in the declared mode.
if [ -f /revl-ctl/mounts ]; then
  while read -r tag path mode; do
    [ -n "$tag" ] || continue
    $BB mkdir -p "$path"
    if [ "$mode" = "rw" ]; then
      $BB mount -t 9p -o trans=virtio,version=9p2000.L,access=any \
          "$tag" "$path" 2>/dev/null
    else
      $BB mount -t 9p -o trans=virtio,version=9p2000.L,ro,access=any \
          "$tag" "$path" 2>/dev/null
    fi
  done < /revl-ctl/mounts
fi

# run the driver's boot canary; its stdout is the serial console (fd 1 of PID 1
# is /dev/console == ttyS0), so the report reaches the monitor's -serial stdio.
if [ -f /revl-ctl/canary.sh ]; then
  $BB sh /revl-ctl/canary.sh
else
  echo "CANARY_INIT_ERR=no /revl-ctl/canary.sh (control share not mounted?)"
fi

# power the guest off so the -no-reboot monitor exits and the driver reads the
# report. Try the clean ACPI paths first; fall back to an immediate reboot,
# which -no-reboot turns into a qemu exit rather than a boot loop.
$BB sync
$BB poweroff -f 2>/dev/null
$BB halt -f 2>/dev/null
$BB reboot -f 2>/dev/null
echo o > /proc/sysrq-trigger 2>/dev/null
echo b > /proc/sysrq-trigger 2>/dev/null
while true; do $BB sleep 1; done
