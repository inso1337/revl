#!/usr/bin/env python
"""py<->java bridge on the REAL reactive cordis4j runtime (docs/interop-bridge.md
section 3): a Java consumer that reactively deactivates when the Python provider
dies (peer death = withdrawal), not the non-reactive stub path of
demo/bridge_pyjava.py.

  provider (Python): PgDatabase serving `db`  (demo/bridge_pypy.py --provider)
  consumer (Java):   UserCache on the real cordis4j, `db` via a generic
                     reflection proxy, loaded through ctx.inject so a withdrawal
                     deactivates it (Theorem 63)

Verifies two things: (a) the crossing still works on the real runtime
(cache.get -> "42", provider trace shows the emit), and (b) killing the Python
provider deactivates the Java consumer reactively (its inject fiber unloads and
its inverses run), with no exception.

Needs JDK 21 (cordis4j targets 21) and a cordis4j checkout. This script clones +
compiles cordis4j on first run. Run under the backend venv:
  backends/python/.venv/bin/python demo/bridge_pyjava_real.py
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

JAVA_DIR = ROOT / "backends" / "java"
PLACEMENT = JAVA_DIR / "placement"
CORDIS4J = JAVA_DIR / ".cordis4j"
CLASSES = JAVA_DIR / ".cordis4j-classes"
OUT = PLACEMENT / "real_out"
GEN = PLACEMENT / "real_gen"
PROVIDER = ROOT / "demo" / "bridge_pypy.py"

JDK21 = pathlib.Path("/opt/homebrew/opt/openjdk@21")
JAVAC = str(JDK21 / "bin" / "javac") if JDK21.exists() else "javac"
JAVA = str(JDK21 / "bin" / "java") if JDK21.exists() else "java"


def ensure_cordis4j() -> bool:
    if not CORDIS4J.exists():
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/1na-ko/cordis4j", str(CORDIS4J)],
            capture_output=True, text=True,
        )
        if clone.returncode:
            print(clone.stderr, file=sys.stderr)
            return False
    if not (CLASSES / "io" / "cordis4j" / "core" / "Context.class").exists():
        CLASSES.mkdir(exist_ok=True)
        srcs = [str(p) for p in (CORDIS4J / "cordis4j-core" / "src" / "main" / "java").rglob("*.java")
                if p.name != "module-info.java"]
        result = subprocess.run(
            [JAVAC, "--release", "21", "-d", str(CLASSES), *srcs],
            capture_output=True, text=True,
        )
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            return False
    return True


def build() -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    pkg = GEN / "revl"
    pkg.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("revl_java_emit", JAVA_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    from revl import compile_files

    ir = compile_files([str(ROOT / "examples" / "user_cache.rvl")])
    (pkg / "Components.java").write_text(emit_module.emit(ir), encoding="utf-8")

    result = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(CLASSES), "-d", str(OUT),
         str(PLACEMENT / "RealPlacementRunner.java"), str(pkg / "Components.java")],
        capture_output=True, text=True,
    )
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return False
    return True


def main() -> int:
    if not ensure_cordis4j() or not build():
        return 1

    sock = f"/tmp/revl_pyjava_real_{os.getpid()}.sock"
    trace = f"/tmp/revl_pyjava_real_{os.getpid()}.trace"
    pathlib.Path(trace).write_text("", encoding="utf-8")

    provider = subprocess.Popen(
        [sys.executable, str(PROVIDER), "--provider", sock, trace],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {detail}")
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
            print("provider failed:\n" + provider.stderr.read().decode(), file=sys.stderr)
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
        spec_file = PLACEMENT / "real_spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        runner = subprocess.Popen(
            [JAVA, "-cp", f"{CLASSES}{os.pathsep}{OUT}", "RealPlacementRunner", str(spec_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        up_lines: list[str] = []
        for line in runner.stdout:
            sys.stdout.write(line)
            up_lines.append(line)
            if line.strip() == "[java] UP":
                break
        up = "".join(up_lines)
        check("crossing-real", 'cache.get(\'alice\')| => "42"' in up,
              "Java UserCache on the REAL cordis4j got the value across the seam")
        time.sleep(0.2)
        provider_trace = pathlib.Path(trace).read_text(encoding="utf-8")
        check("crossed", "cache_log" in provider_trace and ".execute" in provider_trace,
              "the provider's pool.execute recorded the emit that crossed from Java")

        # peer death: kill the provider; the consumer must deactivate reactively.
        print("== killing the Python provider ==")
        provider.terminate()
        tail_lines: list[str] = []
        deadline = time.time() + 15
        while time.time() < deadline:
            line = runner.stdout.readline()
            if not line:
                break
            sys.stdout.write(line)
            tail_lines.append(line)
            if line.strip() == "[java] DOWN":
                break
        tail = "".join(tail_lines)
        check("reactive-withdrawal", "deactivated" in tail or "withdraw" in tail,
              "the Java consumer unloaded reactively when the provider died (R2/R3)")
        check("no-exception", "Exception" not in tail and "\tat " not in tail,
              "reactive teardown, not a thrown exception")
    finally:
        if runner and runner.poll() is None:
            runner.terminate()
            try:
                runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner.kill()
        if provider.poll() is None:
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
