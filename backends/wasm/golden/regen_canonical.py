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

# (fixture stem, service name): the Str-only slice-3 fixture, the aggregate
# follow-on (records/lists/variants/Opt/Result + non-Str scalars) over top-level
# pure fns, and the service-level fixture — the SAME value surface presented from
# a component's `provide` methods, so real services cross as standard components.
_FIXTURES = [("canonical_echoer", "Echoer"),
             ("canonical_aggregates", "Registry"),
             ("canonical_service", "Registry")]


def main() -> None:
    for stem, service in _FIXTURES:
        src = (HERE / f"{stem}.revl").read_text(encoding="utf-8")
        res = canonical.emit_component(compile_source(src), service=service)
        (HERE / f"{stem}.core.wat").write_text(res["core_wat"], encoding="utf-8")
        (HERE / f"{stem}.wit").write_text(res["wit"], encoding="utf-8")
        print(f"regenerated {stem} goldens (boundary functions: {res['functions']})")


if __name__ == "__main__":
    main()
