#!/usr/bin/env bash
# Recompile the crash-recovery producer's IR fixture from its revl source
# (roadmap item 322, Slice 2). The module itself is emitted in RECORD mode at
# runtime by crash_producer.py (the wasm tier emits WAT at run time, exactly as
# revl.run_wasm does — nothing to commit), so this only refreshes the IR.
# Run from anywhere; paths resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../../../.." && pwd)"

PYTHONPATH="$root/src" python3 - "$here/crashproof.rvl" "$here/crashproof.ir.json" <<'PY'
import json, sys
from revl import compile_source
src, out = sys.argv[1], sys.argv[2]
ir = compile_source(open(src, encoding="utf-8").read(), "crashproof.rvl")
open(out, "w", encoding="utf-8").write(json.dumps(ir, indent=1) + "\n")
print(f"regenerated {out}")
PY
