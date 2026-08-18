#!/usr/bin/env python
"""Java<->Java ADT/Result codec check (docs/interop-bridge.md canonical encoding).

Two Java (cordis4j stub) processes over a Unix socket, both compiled from
examples/outcome.rvl: a provider running DirSvc (serving `dir`, which returns
the user ADT `Found` and a `Result[Row, Str]`), and a consumer running DirUser
against a proxy for `dir`, whose probes cross those tagged values back and
rebuild the native variants. Proves the `{"$kind","$value"}` codec both
directions on the JVM.

This is the JVM twin of demo/bridge_pyadt.py, and it runs the same two
components every examples/placement/outcome_*.toml pairs across languages —
`DirSvc` and `DirUser` are the only two the fixture defines. (The header here
used to claim outcome.rvl "also has AskClient"; it never has. bridge_pyadt.py
believed that claim and died on `module 'revl_proc_mod' has no attribute
'AskClient'`, which is how the drift was found.)

Run:  backends/python/.venv/bin/python demo/bridge_java_adt.py   (JDK 17 is fine)
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
JAVA = ROOT / "backends" / "java"
PLACEMENT = JAVA / "placement"
OUT = PLACEMENT / "adt_out"
GEN = PLACEMENT / "adt_gen"


# The fixture is examples/outcome.rvl itself, not a copy of it: an inline
# near-duplicate is exactly what let this demo's header drift out of sync with
# the file it names. Both processes emit the same Components.java from it.
RVL = ROOT / "examples" / "outcome.rvl"


def emit_components() -> None:
    spec = importlib.util.spec_from_file_location("revl_java_emit", JAVA / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from revl import compile_files

    pkg = GEN / "revl"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "Components.java").write_text(
        module.emit(compile_files([str(RVL)])), encoding="utf-8")


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


def _spec(path: pathlib.Path, data: dict) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def main() -> int:
    emit_components()
    if not build():
        return 1

    sock = f"/tmp/revl_javaadt_{os.getpid()}.sock"
    prov_spec = _spec(PLACEMENT / "adt_prov.json", {
        "name": "prov", "module": "revl.Components", "components": ["DirSvc"],
        "ifaces": {"dir": "revl.Components$Directory"}, "provides": ["dir"],
        "serve": {"socket": sock, "keys": ["dir"]},
    })
    cons_spec = _spec(PLACEMENT / "adt_cons.json", {
        # `lookup`/`check` take a Str key in outcome.rvl, and DirSvc echoes it
        # back as the Row's `name` — so "ada" coming back out is the payload
        # having survived the crossing, not a constant baked into the fixture.
        "name": "cons", "module": "revl.Components", "components": ["DirUser"],
        "ifaces": {"dir": "revl.Components$Directory"},
        "proxies": {"dir": {"socket": sock}},
        "probe": ["dir.lookup('ada')", "dir.check('ada')"],
    })

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<16} {detail}")
        if not ok:
            failures.append(name)

    provider = subprocess.Popen(["java", "-cp", str(OUT), "PlacementRunner", prov_spec],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    consumer = None
    try:
        for line in provider.stdout:  # wait for the provider to be serving
            sys.stdout.write(line)
            if line.strip() == "[prov] UP":
                break
        consumer = subprocess.Popen(["java", "-cp", str(OUT), "PlacementRunner", cons_spec],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        for line in consumer.stdout:
            sys.stdout.write(line)
            out.append(line)
            if line.strip() == "[cons] UP":
                break
        text = "".join(out)
        check("adt-hit", '"$kind":"Hit"' in text and '"id":1' in text and '"name":"ada"' in text,
              "dir.lookup('ada') rebuilt Found.Hit(Row{id:1,name:ada}) across the seam")
        check("result-ok", '"$kind":"Ok"' in text and text.count('"name":"ada"') >= 2,
              "dir.check('ada') rebuilt Result.Ok(Row) across the seam")
    finally:
        for proc in (consumer, provider):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for stale in (sock, prov_spec, cons_spec):
            if os.path.exists(stale):
                os.unlink(stale)

    print("\n" + ("all checks passed" if not failures else f"{len(failures)} check(s) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
