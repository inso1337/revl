"""The go placement runner resolves a key in its provider's realm (issue #1567).

On main the go runner served and probed every key through `RevlInvoke(root,
..)`, a shared-realm read. A provider placed with `isolate kv in
realm("wa")` publishes `kv` in realm `wa` only, so the provider's probe
answered `stc: service "kv" not provided` and the consumer process never came
up. The runner now resolves each key in the py tier's `resolve_key` order,
off the spec's `placements`: the shared realm when the key is provided there,
else its one isolated realm (through the emitted `RevlRealmContext`), and a
key isolated in two or more realms is refused by name.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

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
    "component StoreB provides kv: Kv {\n") + (
    "service Ops { fn run(k: Str) -> Str }\n"
    "component User requires kv: Kv provides ops: Ops {\n"
    "  provide ops { fn run(k) = kv.get(k) }\n}\n")


def _two_processes(provider: str) -> str:
    return (f'[processes.provider]\nbackend = "go"\ncomponents = [{provider}]\n'
            "probe = [\"kv.get('p')\"]\n\n"
            '[processes.consumer]\nbackend = "go"\ncomponents = ["User"]\n'
            "probe = [\"ops.run('c')\"]\n")


ONE_PROCESS = ('[processes.stores]\nbackend = "go"\ncomponents = ["StoreA", "StoreB"]\n'
               "probe = [\"kv.get('who')\"]\n")

REFUSAL = ("key 'kv' is provided in 2 realms (`StoreA` in realm `tenant_a`, `StoreB` "
           "in realm `tenant_b`); a call names a key, not a realm, so it has no single "
           "provider to reach")


def _run(tmp_path: Path, source: str, placement: str) -> str:
    (tmp_path / "p.rvl").write_text(source, encoding="utf-8")
    (tmp_path / "p.toml").write_text(placement, encoding="utf-8")
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(tmp_path / "p.rvl"),
         "--placement", str(tmp_path / "p.toml"), "--once"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), cwd=ROOT)
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    return out


def _probe(out: str, call: str) -> str:
    """The answer a probe line reports: `=> v`, `-> v` or `ERROR ...`."""
    lines = [line for line in out.splitlines() if "] probe" in line and call in line]
    assert len(lines) == 1, out
    line = lines[0]
    for mark in ("ERROR ", "=> ", "-> "):
        if mark in line:
            return mark.strip() + " " + line.split(mark, 1)[1].strip()
    raise AssertionError(line)

needs_toolchain = pytest.mark.skipif(
    shutil.which("go") is None,
    reason="go is not installed")


@needs_toolchain
def test_a_consumer_reaches_an_isolated_provider_and_its_probe_reaches_the_realm(tmp_path):
    out = _run(tmp_path, ISOLATED, _two_processes('"Walled"'))
    assert _probe(out, "kv.get(")[-6:] == '"wa:p"', out
    assert _probe(out, "ops.run(")[-6:] == '"wa:c"', out


@needs_toolchain
def test_a_shared_provision_answers_first_across_a_seam(tmp_path):
    """py's order: a key provided in the shared realm resolves there, even when
    another component in the same process isolates the same key."""
    out = _run(tmp_path, SHARED_AND_ISOLATED, _two_processes('"StoreA", "StoreB"'))
    assert _probe(out, "kv.get(")[-5:] == '"b:p"', out
    assert _probe(out, "ops.run(")[-5:] == '"b:c"', out


@needs_toolchain
def test_a_key_in_two_realms_is_refused_by_name(tmp_path):
    out = _run(tmp_path, TWO_REALMS, ONE_PROCESS)
    assert _probe(out, "kv.get(") == 'ERROR ' + REFUSAL, out
