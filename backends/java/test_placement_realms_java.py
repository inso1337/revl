"""The java placement runners resolve a key in its provider's realm (issue #1567).

WHAT WAS WRONG. `PlacementRunner` (stub runtime) and `RealPlacementRunner`
(cordis4j) read a served or probed key with a shared-realm `ctx.get`. A
provider placed with `isolate kv in realm("wa")` publishes `kv` in realm `wa`
only, so on main a java placement could neither probe it nor serve it over a
seam: both answered `no provider for revl.Components$Kv under key "kv"`.

WHAT IT DOES NOW. `placement._process_placements` hands each java process the
realm of every provision it makes, and the runners resolve a key in the py
tier's `resolve_key` order: the shared realm when the key is provided there,
else its one isolated realm (a strict single-realm read). A key isolated in
two or more realms is refused naming each provider and realm, because a call
names a key, not a realm.

The runtime tests boot real JVM processes through `revl run --placement
--once` and need a JDK; without one they skip, and a skip is not a pass. CI's
`backend-java` job provisions a JDK.
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

needs_jdk = pytest.mark.skipif(javac_gate.JAVA is None, reason=javac_gate.NO_JDK)

ISOLATED = """\
service Kv { fn get(k: Str) -> Str }
component Walled provides kv: Kv {
  isolate kv in realm("wa")
  provide kv { fn get(k) = "wa:" + k }
}
service Ops { fn run(k: Str) -> Str }
component User requires kv: Kv provides ops: Ops {
  provide ops { fn run(k) = kv.get(k) }
}
"""

TWO_PROCESSES = """\
[processes.provider]
backend = "java"
components = ["Walled"]
probe = ["kv.get('p')"]

[processes.consumer]
backend = "java"
components = ["User"]
probe = ["ops.run('c')"]
"""

TWO_REALMS = """\
service Kv { fn get(k: Str) -> Str }
component StoreA provides kv: Kv {
  isolate kv in realm("tenant_a")
  provide kv { fn get(k) = "a:" + k }
}
component StoreB provides kv: Kv {
  isolate kv in realm("tenant_b")
  provide kv { fn get(k) = "b:" + k }
}
"""

SHARED_AND_ISOLATED = TWO_REALMS.replace(
    'component StoreB provides kv: Kv {\n  isolate kv in realm("tenant_b")\n',
    "component StoreB provides kv: Kv {\n")

ONE_PROCESS = """\
[processes.stores]
backend = "java"
components = ["StoreA", "StoreB"]
probe = ["kv.get('who')"]
"""


def _run(tmp_path: Path, source: str, placement: str) -> str:
    (tmp_path / "p.rvl").write_text(source, encoding="utf-8")
    (tmp_path / "p.toml").write_text(placement, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if javac_gate.JAVA:
        env["JAVA_HOME"] = str(Path(javac_gate.JAVA).parents[1])
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(tmp_path / "p.rvl"),
         "--placement", str(tmp_path / "p.toml"), "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900,
        env=env, cwd=ROOT)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    return out


def _probe(out: str, expr: str) -> str:
    lines = [line for line in out.splitlines() if "] probe" in line and expr in line]
    assert len(lines) == 1, out
    return lines[0].split("| ", 2)[-1].strip()


def test_every_provision_carries_its_realm():
    from revl.placement import _process_placements  # noqa: PLC0415
    ir = compile_source(ISOLATED + TWO_REALMS.split("\n", 1)[1], "p.rvl")
    assert _process_placements(ir, ["Walled", "StoreA", "StoreB"]) == {
        "kv": [{"component": "Walled", "realm": "wa"},
               {"component": "StoreA", "realm": "tenant_a"},
               {"component": "StoreB", "realm": "tenant_b"}],
    }


@pytest.fixture(scope="module")
def two_process_run(tmp_path_factory):
    if javac_gate.JAVA is None:
        pytest.skip(javac_gate.NO_JDK)
    return _run(tmp_path_factory.mktemp("iso"), ISOLATED, TWO_PROCESSES)


@needs_jdk
def test_a_consumer_reaches_an_isolated_provider_across_a_seam(two_process_run):
    """The provider serves `kv` from realm `wa`; the consumer's `ops.run`
    crosses the seam into it."""
    assert _probe(two_process_run, "ops.run('c')") == '=> "wa:c"'


@needs_jdk
def test_a_probe_of_an_isolated_key_reaches_its_realm(two_process_run):
    assert _probe(two_process_run, "kv.get('p')") == '=> "wa:p"'


@needs_jdk
def test_a_key_in_two_realms_is_refused_by_name(tmp_path):
    out = _run(tmp_path, TWO_REALMS, ONE_PROCESS)
    assert _probe(out, "kv.get('who')") == (
        "ERROR RuntimeException: key 'kv' is provided in 2 realms (`StoreA` in "
        "realm `tenant_a`, `StoreB` in realm `tenant_b`); a call names a key, not "
        "a realm, so it has no single provider to reach")


@needs_jdk
def test_the_shared_realm_provision_answers_first(tmp_path):
    """py's order: a key provided in the shared realm resolves there, even when
    another component isolates the same key."""
    out = _run(tmp_path, SHARED_AND_ISOLATED, ONE_PROCESS)
    assert _probe(out, "kv.get('who')") == '=> "b:who"'
