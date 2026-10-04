"""A refused acquisition is not an unreleased resource (issue #1859).

The python tier's emitted lifecycle harness pairs each host-stub `open`/`new`
with its `close`/`drop` (`_revl_unreleased`, the R1 half of `assert
no_residue`). A refusing acquisition records `pool.open refused <url>` and
raises, so nothing was acquired, but the pairing read the `.open` and reported
`pool (open() with no close())`. The session teardown report had the same
false positive (fixed beside its `hostResources` check); this is the emitted
harness's copy.

The other tiers count differently: TypeScript keeps a live-resource set filled
on construction, and go, rust and java keep a counter incremented on
construction, so a raise before construction is never counted. Their host
stubs also have no refusing `boom://` hook.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="`revl test --backend py` needs the cordis-py runtime; install it "
           "with `sh backends/python/setup.sh` and run under its venv",
)

SOURCE = """
service S { fn ping() -> Int }
component Db provides s: S {
  config { url: Str }
  let a = effect Map.new() undo a.drop()
  let p = effect Pool.open(config.url, 2) undo p.close()
  provide s { fn ping() = 1 }
}

lifecycle test "a refused open leaves nothing held" {
  load Db with { url: "boom://nope" }
  unload Db
  assert no_residue
}

lifecycle test "an opened pool that is closed leaves nothing held" {
  load Db with { url: "db://orders" }
  unload Db
  assert no_residue
}
"""


def _revl_test(path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, "-P", "-m", "revl", "test", str(path), "--backend", "py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


@needs_cordis
def test_a_refused_open_is_not_reported_as_held(tmp_path):
    path = tmp_path / "refused.rvl"
    path.write_text(SOURCE, encoding="utf-8")
    result = _revl_test(path)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "PASS a refused open leaves nothing held" in out
    assert "PASS an opened pool that is closed leaves nothing held" in out
    assert "open() with no close()" not in out


def _emitted_pairing():
    """`_revl_unreleased` exactly as the python emitter writes it into a
    lifecycle module, with the acquire table it reads."""
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_1859", ROOT / "backends" / "python" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    namespace = {"_REVL_ACQUIRE": dict(emit._LIFECYCLE_ACQUIRE)}
    exec(emit._LIFECYCLE_HARNESS, namespace)
    return namespace["_revl_unreleased"]


def test_the_pairing_skips_a_refusal_and_still_catches_a_real_leak():
    unreleased = _emitted_pairing()
    assert unreleased(["pool.open refused boom://nope"]) == []
    assert unreleased(["pool#1.open db://a", "pool#1.close db://a"]) == []
    # the control: skipping a refusal must not hide a real unreleased pool
    assert unreleased(["pool.open refused boom://x", "pool#2.open db://b"]) == [
        "pool#2 (open() with no close())"]
