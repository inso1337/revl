#!/usr/bin/env python3
"""Regenerate the canonical-ABI component goldens (item 41 slice-3).

    python3 backends/wasm/golden/regen_canonical.py

Reads the fixed source `canonical_echoer.revl` and rewrites
`canonical_echoer.core.wat` and `canonical_echoer.wit` from the current
emitter. Run after any change to `backends/wasm/emit.py` or `canonical.py`
that legitimately moves the emitted bytes, then re-run
`backends/wasm/test_canonical_abi.py`.
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent      # backends/wasm/golden
BACKEND = HERE.parent                               # backends/wasm
ROOT = BACKEND.parents[1]                           # repo root
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BACKEND))

from revl import compile_source  # noqa: E402
import canonical  # noqa: E402

SERVICE = "Echoer"


def main() -> None:
    src = (HERE / "canonical_echoer.revl").read_text(encoding="utf-8")
    res = canonical.emit_component(compile_source(src), service=SERVICE)
    (HERE / "canonical_echoer.core.wat").write_text(res["core_wat"], encoding="utf-8")
    (HERE / "canonical_echoer.wit").write_text(res["wit"], encoding="utf-8")
    print(f"regenerated canonical goldens (boundary functions: {res['functions']})")


if __name__ == "__main__":
    main()
