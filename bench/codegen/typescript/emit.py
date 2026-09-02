#!/usr/bin/env python3
"""Regenerate `emitted/*.ts` from `src/*.rvl` with the TypeScript backend.

Run from anywhere:

    PYTHONPATH=<repo>/src python3 bench/codegen/typescript/emit.py

Every file under `emitted/` is machine output. The cases under `cases/` quote
fragments of it verbatim, and `run.mjs` refuses to measure a fragment it cannot
find in the corresponding emitted file, so a re-emit that changes the shape
fails the harness loudly instead of silently measuring stale code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
OUT = HERE / "emitted"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    sources = sorted(SRC.glob("*.rvl"))
    if not sources:
        print(f"no .rvl sources under {SRC}", file=sys.stderr)
        return 1
    failed = 0
    for source in sources:
        target = OUT / (source.stem + ".ts")
        proc = subprocess.run(
            [sys.executable, "-m", "revl", "emit", "--backend", "typescript",
             str(source)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"FAIL {source.name}: {proc.stderr.strip()}", file=sys.stderr)
            failed += 1
            continue
        target.write_text(proc.stdout, encoding="utf-8")
        print(f"ok   {source.name} -> emitted/{target.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
