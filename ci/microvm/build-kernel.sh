#!/bin/sh
# Build the microVM guest kernel for the sandbox-microvm CI lane (item 411 T6).
#
# The driver's argv (src/revl/sandbox_runtime.py, microvm_vm_argv) boots
# `-machine microvm` with NO initramfs, so every driver the guest needs must be
# BUILT IN, not a module:
#   * virtio over virtio-mmio (microvm has no PCI): VIRTIO_MMIO
#   * the read-only root, root=/dev/vda: VIRTIO_BLK + EXT4
#   * the control + fs-grant 9p shares: NET_9P + NET_9P_VIRTIO + 9P_FS
#   * the serial console, console=ttyS0: SERIAL_8250(+_CONSOLE)
#   * /dev populated for the guest: DEVTMPFS(+_MOUNT)
# The microvm machine enumerates its virtio-mmio transports over ACPI, so ACPI
# must be in too. We start from `defconfig` (a known-bootable x86_64 kernel) and
# force exactly these on, so the result is a small delta over a vetted baseline
# rather than a hand-rolled config that might miss a boot essential.
#
# Usage: build-kernel.sh <out-dir>   (writes <out-dir>/bzImage; a no-op if the
# bzImage is already there, so an actions/cache restore skips the rebuild).
set -eu

OUT="${1:?usage: build-kernel.sh <out-dir>}"
KVER="${KVER:-6.6.52}"
mkdir -p "$OUT"

if [ -f "$OUT/bzImage" ]; then
  echo "kernel already present at $OUT/bzImage (cache hit); skipping build"
  exit 0
fi

work="$(mktemp -d)"
cd "$work"
echo "fetching linux-${KVER} source"
curl -fsSL "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${KVER}.tar.xz" \
  -o linux.tar.xz
tar xf linux.tar.xz
cd "linux-${KVER}"

make defconfig
./scripts/config \
  --enable CONFIG_64BIT \
  --enable CONFIG_ACPI \
  --enable CONFIG_PVH \
  --enable CONFIG_HYPERVISOR_GUEST \
  --enable CONFIG_PARAVIRT \
  --enable CONFIG_KVM_GUEST \
  --enable CONFIG_VIRTIO \
  --enable CONFIG_VIRTIO_MENU \
  --enable CONFIG_VIRTIO_MMIO \
  --enable CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES \
  --enable CONFIG_VIRTIO_BLK \
  --enable CONFIG_VIRTIO_NET \
  --enable CONFIG_VIRTIO_PCI \
  --enable CONFIG_NET \
  --enable CONFIG_INET \
  --enable CONFIG_NET_9P \
  --enable CONFIG_NET_9P_VIRTIO \
  --enable CONFIG_9P_FS \
  --enable CONFIG_EXT4_FS \
  --enable CONFIG_SERIAL_8250 \
  --enable CONFIG_SERIAL_8250_CONSOLE \
  --enable CONFIG_SERIAL_8250_PCI \
  --enable CONFIG_DEVTMPFS \
  --enable CONFIG_DEVTMPFS_MOUNT \
  --enable CONFIG_TMPFS \
  --enable CONFIG_BINFMT_ELF \
  --enable CONFIG_BINFMT_SCRIPT \
  --enable CONFIG_BLK_DEV \
  --enable CONFIG_BLOCK
make olddefconfig
make -j"$(nproc)" bzImage

cp arch/x86/boot/bzImage "$OUT/bzImage"
echo "built kernel -> $OUT/bzImage"
ls -l "$OUT/bzImage"
