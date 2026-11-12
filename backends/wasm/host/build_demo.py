#!/usr/bin/env python3
"""Build the browser host's demo component: `demo.rvl` -> `demo.wat` -> `demo.wasm`.

The WAT is checked in beside the module so the emitted shape is reviewable, and
the wasm is checked in so the page opens and runs without a build step. `--check`
re-generates both and fails when either is stale, the same committed-artifact
gate the rest of the tree uses for generated files.

Run from anywhere:

    python3 backends/wasm/host/build_demo.py            # write the artifacts
    python3 backends/wasm/host/build_demo.py --check    # staleness gate
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "wasm"))

from revl import compile_files  # noqa: E402

import emit  # noqa: E402


def build() -> tuple[str, bytes]:
    modules = emit.emit(compile_files([str(HERE / "demo.rvl")]))
    if "Cart" not in modules:
        raise SystemExit(f"demo.rvl did not emit a Cart component: {sorted(modules)}")
    wat = modules["Cart"]
    return wat, _wat_to_wasm(wat)


def _wat_to_wasm(wat: str) -> bytes:
    """Convert WAT to wasm with wabt's `wat2wasm`, else the wasmtime package.

    Both converters are accepted, and their output is normalized: wabt omits
    the `name` custom section while wasmtime emits it, so the section is
    stripped here. That makes the checked-in artifact byte-identical whichever
    tool built it (CI has the wasmtime package, a laptop may only have wabt).
    """
    if shutil.which("wat2wasm"):
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "demo.wat"
            dst = pathlib.Path(tmp) / "demo.wasm"
            src.write_text(wat, encoding="utf-8")
            subprocess.run(["wat2wasm", str(src), "-o", str(dst)], check=True)
            return _strip_name_section(dst.read_bytes())
    try:
        import wasmtime  # type: ignore
    except ImportError as exc:  # pragma: no cover - toolchain-dependent
        raise SystemExit(
            "neither `wat2wasm` nor the `wasmtime` Python package is available; "
            "install one to build the demo"
        ) from exc
    return _strip_name_section(wasmtime.wat2wasm(wat))


def _read_leb(wasm: bytes, offset: int) -> tuple[int, int]:
    """Read an unsigned LEB128 value; return (value, next offset)."""
    value = shift = 0
    while True:
        byte = wasm[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _strip_name_section(wasm: bytes) -> bytes:
    """Drop the `name` custom section so the artifact is tool-independent."""
    if wasm[:4] != b"\0asm":
        raise SystemExit("the converter did not produce a wasm module")
    out = bytearray(wasm[:8])
    offset = 8
    while offset < len(wasm):
        section_start = offset
        section_id = wasm[offset]
        offset += 1
        size, payload_start = _read_leb(wasm, offset)
        payload_end = payload_start + size
        if section_id == 0:
            name_len, name_start = _read_leb(wasm, payload_start)
            if wasm[name_start:name_start + name_len] == b"name":
                offset = payload_end
                continue
        out += wasm[section_start:payload_end]
        offset = payload_end
    return bytes(out)



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the checked-in artifacts are stale")
    args = parser.parse_args()
    wat, wasm = build()
    wat_path = HERE / "demo.wat"
    wasm_path = HERE / "demo.wasm"
    if args.check:
        stale = []
        if not wat_path.exists() or wat_path.read_text(encoding="utf-8") != wat:
            stale.append(wat_path.name)
        if not wasm_path.exists() or wasm_path.read_bytes() != wasm:
            stale.append(wasm_path.name)
        if stale:
            print(f"stale browser-host demo artifact(s): {', '.join(stale)}; "
                  f"run {pathlib.Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"browser-host demo artifacts are current ({len(wasm)} wasm bytes)")
        return 0
    wat_path.write_text(wat, encoding="utf-8")
    wasm_path.write_bytes(wasm)
    print(f"wrote {wat_path.name} and {wasm_path.name} ({len(wasm)} wasm bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
