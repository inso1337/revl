#!/usr/bin/env python
"""py<->rust bridge demo (docs/interop-bridge.md §3): a Rust process consuming
a Python-provided revl service over the bridge wire.

  provider (Python): PgDatabase serving `db`  (demo/bridge_pypy.py --provider)
  consumer (Rust):   backends/rust/bridge_client, calling db.execute / db.query

Proves the newline-delimited JSON wire is language-neutral into Rust: the value
the Python pool returns marshals back into a typed Rust value, and the
provider's trace shows the call crossed the process (and language) boundary.

Build the client first, then run under the backend venv (the provider needs
cordis-py):
  (cd backends/rust/bridge_client && cargo build)
  backends/python/.venv/bin/python demo/bridge_pyrust.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "demo" / "bridge_pypy.py"
CLIENT = ROOT / "backends" / "rust" / "bridge_client" / "target" / "debug" / "revl_bridge_client"


def main() -> int:
    if not CLIENT.exists():
        print(f"error: build the client first:  (cd {CLIENT.parents[2]} && cargo build)", file=sys.stderr)
        return 2

    sock = f"/tmp/revl_pyrust_{os.getpid()}.sock"
    trace = f"/tmp/revl_pyrust_{os.getpid()}.trace"
    pathlib.Path(trace).write_text("", encoding="utf-8")

    provider = subprocess.Popen(
        [sys.executable, str(PROVIDER), "--provider", sock, trace],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<20} {detail}")
        if not ok:
            failures.append(name)

    try:
        ready = False
        for line in provider.stdout:
            if line.strip() == b"READY":
                ready = True
                break
        if not ready:
            print("provider failed to start:\n" + provider.stderr.read().decode(), file=sys.stderr)
            return 1
        print("== provider up (PgDatabase serving `db` in a Python process) ==")

        result = subprocess.run([str(CLIENT), sock], capture_output=True, text=True, timeout=30)
        sys.stdout.write(result.stdout)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)

        check("rust-consumed", "[rust] OK" in result.stdout,
              "Rust called the Python-provided `db` and got typed values back")
        provider_trace = pathlib.Path(trace).read_text(encoding="utf-8")
        check("crossed", "cache_log VALUES (rust)" in provider_trace and ".execute" in provider_trace,
              "the provider's pool.execute recorded the call that crossed from Rust")
    finally:
        provider.terminate()
        try:
            provider.wait(timeout=5)
        except subprocess.TimeoutExpired:
            provider.kill()
        for stale in (sock, trace):
            if os.path.exists(stale):
                os.unlink(stale)

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} check(s) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
