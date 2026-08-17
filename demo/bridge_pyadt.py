#!/usr/bin/env python
"""py<->py ADT/Result crossing (docs/interop-bridge.md "Canonical value
encoding"): a provider returns a user ADT (`Found`) and a `Result` across the
seam, and the consumer rebuilds them as native case instances.

  provider (Python): DirSvc, serves `dir` (lookup -> Found, check -> Result)
  consumer (Python): AskClient, proxies `dir`, provides `ask`

Both run examples/outcome.rvl via `python -m revl._process_runner`. The probes
prove the consumer got back native ADT instances (right tag + payload), not raw
JSON. Run under the backend venv:
  backends/python/.venv/bin/python demo/bridge_pyadt.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RVL = str(ROOT / "examples" / "outcome.rvl")
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}


def _runner(spec: dict, spec_path: pathlib.Path) -> subprocess.Popen:
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "revl._process_runner", str(spec_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV,
    )


def _wait_up(proc: subprocess.Popen, tag: str) -> str:
    collected = []
    for line in proc.stdout:
        collected.append(line)
        if line.strip() == f"[{tag}] UP":
            break
    return "".join(collected)


def main() -> int:
    tmp = pathlib.Path(f"/tmp/revl_pyadt_{os.getpid()}")
    tmp.mkdir(exist_ok=True)
    sock = str(tmp / "dir.sock")

    provider = _runner(
        {"name": "prov", "files": [RVL], "components": ["DirSvc"],
         "serve": {"socket": sock, "keys": ["dir"]}},
        tmp / "prov.json",
    )
    consumer = _runner(
        {"name": "cons", "files": [RVL], "components": ["AskClient"], "provides": ["ask"],
         "proxies": {"dir": {"socket": sock, "methods": ["lookup", "check"]}},
         "probe": [
             "type(ask.who(1)).__name__",     # -> 'Hit'   (user ADT tag rebuilt)
             "ask.who(1).value",              # -> Row dict (payload survived)
             "type(ask.verify(1)).__name__",  # -> 'Ok'    (Result tag rebuilt)
             "ask.verify(1).value",           # -> Row dict
         ]},
        tmp / "cons.json",
    )

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<18} {detail}")
        if not ok:
            failures.append(name)

    try:
        _wait_up(provider, "prov")
        print("== provider up (DirSvc serving `dir`) ==")
        out = _wait_up(consumer, "cons")
        sys.stdout.write(out)
        check("adt-tag", "=> 'Hit'" in out, "user ADT `Found` rebuilt as native Hit")
        check("adt-payload", "'id': 1" in out and "'name': 'ada'" in out, "Hit payload (Row) survived the crossing")
        check("result-tag", "=> 'Ok'" in out, "built-in Result rebuilt as native Ok")
    finally:
        for proc in (consumer, provider):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for stale in tmp.glob("*"):
            stale.unlink()
        tmp.rmdir()

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} check(s) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
