#!/bin/sh
# Build the microVM guest root filesystem for the sandbox-microvm CI lane
# (item 411 T6): a small RAW ext4 image carrying `sh` + `python3` (the canary
# needs both) plus the guest init.
#
# The image is assembled from an Alpine rootfs (busybox `sh`/coreutils + musl
# `python3`) exported out of a throwaway container, with `ci/microvm/guest-init.sh`
# dropped in as `/sbin/init`. It is a RAW disk (mkfs.ext4 on a raw file), mounted
# by the driver as root=/dev/vda ro, exactly what microvm_vm_argv passes.
#
# Usage: build-rootfs.sh <out-dir>   (writes <out-dir>/rootfs.img)
set -eu

OUT="${1:?usage: build-rootfs.sh <out-dir>}"
ALPINE="${ALPINE:-alpine:3.20}"
HERE="$(cd "$(dirname "$0")" && pwd)"
INIT="$HERE/guest-init.sh"
mkdir -p "$OUT"

test -f "$INIT" || { echo "missing $INIT" >&2; exit 1; }

# 1. an Alpine rootfs with python3, exported from a throwaway container.
docker rm -f revl-microvm-rootfs >/dev/null 2>&1 || true
docker run --name revl-microvm-rootfs "$ALPINE" \
  sh -c 'apk add --no-cache python3 >/dev/null'
docker export revl-microvm-rootfs -o "$OUT/rootfs.tar"
docker rm -f revl-microvm-rootfs >/dev/null 2>&1 || true

# 2. a raw ext4 image, populated from that rootfs + our init.
rm -f "$OUT/rootfs.img"
dd if=/dev/zero of="$OUT/rootfs.img" bs=1M count=384 status=none
mkfs.ext4 -F -q "$OUT/rootfs.img"

mnt="$(mktemp -d)"
sudo mount -o loop "$OUT/rootfs.img" "$mnt"
trap 'sudo umount "$mnt" 2>/dev/null || true' EXIT
sudo tar -C "$mnt" -xf "$OUT/rootfs.tar"
sudo mkdir -p "$mnt/proc" "$mnt/sys" "$mnt/dev" "$mnt/tmp" "$mnt/revl-ctl" "$mnt/sbin"
# Alpine ships /sbin/init as a symlink into busybox (whose init then reads
# /etc/inittab and tries to run openrc). `cp` onto that symlink would follow it
# instead of replacing it, leaving busybox-init as PID 1. Remove it first so our
# script becomes /sbin/init as a real regular file, and drop the inittab so no
# stray busybox-init path can run openrc either.
sudo rm -f "$mnt/sbin/init" "$mnt/etc/inittab"
sudo install -m 0755 "$INIT" "$mnt/sbin/init"
# make sure /bin/sh resolves (Alpine ships it as a busybox symlink already).
sudo test -e "$mnt/bin/sh" || sudo ln -sf /bin/busybox "$mnt/bin/sh"
echo "== rootfs /sbin/init =="
sudo ls -l "$mnt/sbin/init"; sudo head -1 "$mnt/sbin/init"
echo "== rootfs /bin/sh, /bin/busybox, python3 =="
sudo ls -l "$mnt/bin/sh" "$mnt/bin/busybox" 2>/dev/null || true
sudo test -x "$mnt/usr/bin/python3" && echo "python3 present" || echo "python3 MISSING"
sudo sync
sudo umount "$mnt"
trap - EXIT

rm -f "$OUT/rootfs.tar"
echo "built rootfs -> $OUT/rootfs.img"
ls -l "$OUT/rootfs.img"
