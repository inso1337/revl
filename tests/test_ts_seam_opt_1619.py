"""An `Opt[T]` `None` crosses a seam into the ts tier as `None` (issue #1619).

WHAT WAS WRONG. The ts tier's `Opt` is `value | undefined`: a `match` takes the
`None` arm on `=== undefined`, and `?.` short-circuits on it. The wire carries
`None` as JSON `null`, and `bridge.ts` `decodeAs` passed a declared Opt's
`null` through unchanged. A ts consumer of a py provider, a ts provider called
by a py consumer, and ts on both sides all read `None` as `Some(null)`: the
`match` answered `some:null`.

WHAT HAPPENS NOW. `decodeAs` reads a declared `Opt[T]` `null` as `undefined`,
in a reply, an argument, a record field and a list element. The codec is
pinned in backends/typescript/tests/seam_opt.test.ts; these run the real
placements, two processes and a UDS seam. The py halves need cordis-py; with
`REVL_REQUIRE_TIERS` naming py and ts a missing runtime FAILS, not skips.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

APP = """
type Box = { name: Opt[Str] }
service S {
  fn find(k: Str) -> Opt[Str]
  fn echo(o: Opt[Str]) -> Str
  fn box(k: Str) -> Box
  fn boxname(b: Box) -> Str
}
component P provides s: S {
  provide s {
    fn find(k) {
      if (k == "a") { return Some("A") } else { return None }
    }
    fn echo(o) { return match o { Some(v) => "some:" + v, None => "none" } }
    fn box(k) {
      if (k == "a") { return { name: Some("A") } } else { return { name: None } }
    }
    fn boxname(b) { return match b.name { Some(v) => "some:" + v, None => "none" } }
  }
}
service Ops {
  fn look(k: Str) -> Str
  fn lookbox(k: Str) -> Str
  fn sendnone(k: Str) -> Str
  fn sendbox(k: Str) -> Str
}
component C requires s: S provides ops: Ops {
  provide ops {
    fn look(k) { return match s.find(k) { Some(v) => "some:" + v, None => "none" } }
    fn lookbox(k) { return match s.box(k).name { Some(v) => "some:" + v, None => "none" } }
    fn sendnone(k) { return s.echo(None) }
    fn sendbox(k) { return s.boxname({ name: None }) }
  }
}
"""

#: probe -> the answer, on every tier pair
EXPECTED = {
    'ops.look("a")': "some:A",
    'ops.look("z")': "none",
    'ops.lookbox("a")': "some:A",
    'ops.lookbox("z")': "none",
    'ops.sendnone("z")': "none",
    'ops.sendbox("z")': "none",
}

_REQUIRED = {t.strip() for t in os.environ.get("REVL_REQUIRE_TIERS", "").split(",") if t.strip()}


def _need(tier: str, reason: str | None) -> None:
    if reason is None:
        return
    if tier in _REQUIRED:
        pytest.fail(f"REVL_REQUIRE_TIERS names {tier}, but {reason}")
    pytest.skip(reason)


def _need_tiers(*tiers: str) -> None:
    if "node" in tiers:
        reason = None
        if shutil.which("node") is None:
            reason = "node is not on PATH"
        elif not (ROOT / "backends" / "typescript" / "node_modules" / "cordis").is_dir():
            reason = "cordis-ts is not installed (cd backends/typescript && npm ci)"
        _need("ts", reason)
    if "py" in tiers:
        _need("py", None if importlib.util.find_spec("cordis") is not None
              else "the cordis-py runtime is not installed (sh backends/python/setup.sh)")


def _probes(tmp_path: Path, provider: str, consumer: str) -> dict[str, str]:
    (tmp_path / "app.rvl").write_text(APP, encoding="utf-8")
    probes = ", ".join("'" + p + "'" for p in EXPECTED)
    (tmp_path / "app.toml").write_text(
        f'[processes.prov]\nbackend = "{provider}"\ncomponents = ["P"]\n\n'
        f'[processes.cons]\nbackend = "{consumer}"\ncomponents = ["C"]\n'
        f"probe = [{probes}]\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", "app.rvl", "--placement", "app.toml",
         "--once"], capture_output=True, text=True, timeout=600, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    out = {}
    for line in (proc.stdout + proc.stderr).splitlines():
        m = re.match(r"^\[\w+\] probe\s*\|\s*(.+?)\s*\|\s*(.*)$", line)
        if m:
            value = m.group(2)
            out[m.group(1)] = (value[3:] if value.startswith("=> ") else value).strip("'\"")
    assert out, (proc.stdout + proc.stderr)[-3000:]
    return out


@pytest.mark.parametrize("provider, consumer", [
    ("py", "node"), ("node", "py"), ("node", "node"), ("py", "py")],
    ids=["py-to-ts", "ts-to-py", "ts-to-ts", "py-to-py"])
def test_a_none_crosses_the_seam_as_none(tmp_path, provider, consumer):
    _need_tiers(provider, consumer)
    assert _probes(tmp_path, provider, consumer) == EXPECTED
