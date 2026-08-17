#!/usr/bin/env python
"""py<->java bridge demo (docs/interop-bridge.md §3): a Java (cordis4j) process
consuming a Python-provided revl service over the bridge wire, in the shape a
`revl run --placement` java process would use.

  provider (Python): PgDatabase serving `db`  (demo/bridge_pypy.py --provider)
  consumer (Java):   UserCache on the cordis4j stub runtime, `db` via a generic
                     java.lang.reflect.Proxy that forwards over the socket

Proves the JSON wire is language-neutral into Java: the Java UserCache's
cache.put crosses `db.execute` into the Python pool, and cache.get returns the
stored value. Uses the in-repo cordis4j stubs (non-reactive), so it verifies
the crossing and clean teardown; peer-death-as-withdrawal needs the real
reactive cordis4j jar (REVL_CORDIS4J_CLASSES) and is the follow-up.

Build is automatic. Run under the backend venv (the provider needs cordis-py):
  backends/python/.venv/bin/python demo/bridge_pyjava.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

JAVA = ROOT / "backends" / "java"
PLACEMENT = JAVA / "placement"
OUT = PLACEMENT / "out"
GEN = PLACEMENT / "gen"
PROVIDER = ROOT / "demo" / "bridge_pypy.py"


def emit_components() -> None:
    spec = importlib.util.spec_from_file_location("revl_java_emit", JAVA / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from revl import compile_files

    ir = compile_files([str(ROOT / "examples" / "user_cache.rvl")])
    pkg = GEN / "revl"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "Components.java").write_text(module.emit(ir), encoding="utf-8")


def build() -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    stubs = [str(p) for p in (JAVA / "stubs").rglob("*.java")]
    runner = subprocess.run(
        ["javac", "--release", "17", "-d", str(OUT), *stubs, str(PLACEMENT / "PlacementRunner.java")],
        capture_output=True, text=True,
    )
    if runner.returncode:
        print(runner.stderr, file=sys.stderr)
        return False
    components = subprocess.run(
        ["javac", "--release", "17", "-cp", str(OUT), "-d", str(OUT), str(GEN / "revl" / "Components.java")],
        capture_output=True, text=True,
    )
    if components.returncode:
        print(components.stderr, file=sys.stderr)
        return False
    return True


def main() -> int:
    emit_components()
    if not build():
        return 1

    sock = f"/tmp/revl_pyjava_{os.getpid()}.sock"
    trace = f"/tmp/revl_pyjava_{os.getpid()}.trace"
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

    runner = None
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

        spec = {
            "name": "java",
            "module": "revl.Components",
            "components": ["UserCache"],
            "ifaces": {"db": "revl.Components$Database", "cache": "revl.Components$Cache"},
            "provides": ["cache"],
            "proxies": {"db": {"socket": sock}},
            "probe": ["cache.put('alice', '42')", "cache.get('alice')"],
        }
        spec_file = PLACEMENT / "spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        runner = subprocess.Popen(
            ["java", "-cp", str(OUT), "PlacementRunner", str(spec_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        collected = []
        for line in runner.stdout:
            sys.stdout.write(line)
            collected.append(line)
            if line.strip() == "[java] UP":
                break
        out = "".join(collected)

        check("java-active", "[java] load  | UserCache" in out,
              "Java UserCache loaded on cordis4j against a cross-process `db`")
        check("cache-get", 'cache.get(\'alice\')| => "42"' in out,
              "cache.get returned the value put through the seam")
        time.sleep(0.2)
        provider_trace = pathlib.Path(trace).read_text(encoding="utf-8")
        check("crossed", "cache_log" in provider_trace and ".execute" in provider_trace,
              "the provider's pool.execute recorded the emit that crossed from Java")
    finally:
        if runner and runner.poll() is None:
            runner.terminate()
            try:
                runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner.kill()
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
