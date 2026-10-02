"""`revl run --backend java --once` proves an ISOLATED provision in its realm
(issue #1550).

WHAT WAS WRONG. The once runner (backends/java/placement/RunOnce.java) read
every provided key in the SHARED realm, for both the UP proof and the
no-residue proof. A key a component isolates (`isolate kv in
realm("tenant_a")`) is published in that realm only, so on main every
composition with an isolated provision loaded all its components Active and
then exited 1 on `no provider for revl.Components$Kv`. `examples/tenants.rvl`,
the realms example, could not run on java at all, and nothing in CI ran it.

WHAT IT DOES NOW. `run_java._placements` hands the runner every provision with
the realm it is published in, and the runner reads each one there, strictly
(`serviceInRealm`, no fallback to the shared realm). Two tenants providing `kv`
are two provisions, `kv@tenant_a` and `kv@tenant_b`; each must be live while
the composition is up and gone after teardown.

This lives under backends/java/ so it runs in the `backend-java` job, which
pins a JDK. Without one it skips, and a skip is not a pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (ROOT / "src", HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import javac_gate  # noqa: E402

from revl.compiler import compile_source  # noqa: E402

TENANTS = ROOT / "examples" / "tenants.rvl"

needs_jdk = pytest.mark.skipif(javac_gate.JAVA is None, reason=javac_gate.NO_JDK)


def _run_once(path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if javac_gate.JAVA:
        env["JAVA_HOME"] = str(Path(javac_gate.JAVA).parents[1])
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", str(path), "--backend", "java", "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900,
        env=env, cwd=ROOT)


def test_every_provision_carries_the_realm_it_is_published_in():
    from revl.run_java import _placements  # noqa: PLC0415
    ir = compile_source(TENANTS.read_text(encoding="utf-8"), str(TENANTS))
    assert [(p["component"], p["key"], p["realm"]) for p in _placements(ir)] == [
        ("TenantAStore", "kv", "tenant_a"),
        ("TenantBStore", "kv", "tenant_b"),
    ]


@needs_jdk
def test_examples_tenants_runs_once_on_java_with_no_residue():
    ran = _run_once(TENANTS)
    out = ran.stdout
    assert ran.returncode == 0, ran.stderr + out
    assert out.count("state=Active") == 4, out
    assert "kv@tenant_a     | live [Kv]" in out, out
    assert "kv@tenant_b     | live [Kv]" in out, out
    assert "0 service(s) still provided" in out, out
    assert "NO-RESIDUE" in out and "[run] DOWN" in out, out


@needs_jdk
def test_a_shared_and_an_isolated_provision_are_both_proved(tmp_path):
    """One key in the shared realm, one isolated: each is read where it lives,
    and the shared read of the isolated key is not what decides it."""
    source = tmp_path / "mixed.rvl"
    source.write_text(
        "service Kv { fn get(k: Str) -> Str }\n"
        "component Shared provides kv: Kv { provide kv { fn get(k) = k } }\n"
        "component Walled provides iso: Kv {\n"
        '  isolate iso in realm("wall")\n'
        "  provide iso { fn get(k) = k }\n}\n",
        encoding="utf-8")
    ran = _run_once(source)
    out = ran.stdout
    assert ran.returncode == 0, ran.stderr + out
    assert "kv              | live [Kv]" in out, out
    assert "iso@wall        | live [Kv]" in out, out
    assert "NO-RESIDUE" in out, out
