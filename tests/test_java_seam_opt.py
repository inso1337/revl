"""An `Opt[T]` crosses a seam into and out of the java tier (issue #1627).

WHAT WAS WRONG. The java tier's `Opt` is `java.util.Optional`. A REPLY was
already decoded by the method's generic return type (`BridgeCodec.decode`), so
a py provider's `None` reached a java consumer as `Optional.empty()`. The other
two directions were not:

* a java consumer's ARGUMENTS were written raw, so an `Optional` crossed as its
  `toString()`: a py provider received the string `"Optional.empty"`;
* a java provider's served ARGUMENTS were coerced by raw class, so a wire
  `null` for an `Opt[T]` parameter arrived as a `null` Optional (an NPE on
  `?? d`), a record argument arrived as a Map (`argument type mismatch`), and a
  `null` list element stayed `null`.

WHAT HAPPENS NOW. A proxy encodes each argument with `BridgeCodec.encode`, as a
reply is encoded, and a served call decodes each argument by the method's
generic parameter type, as a reply is decoded. These run real placements, two
processes and a UDS seam, on the java stub runtime (any JDK on PATH) and
cordis-py. With `REVL_REQUIRE_TIERS` naming py and java, a missing runtime
FAILS instead of skipping.
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
  fn wrap(k: Str) -> Box
  fn boxname(b: Box) -> Str
  fn names(k: Str) -> List[Opt[Str]]
  fn first(xs: List[Opt[Str]]) -> Str
}
component P provides s: S {
  provide s {
    fn find(k) {
      if (k == "a") { return Some("A") } else { return None }
    }
    fn echo(o) { return o ?? "none" }
    fn wrap(k) {
      if (k == "a") { return { name: Some("A") } } else { return { name: None } }
    }
    fn boxname(b) { return b.name ?? "none" }
    fn names(k) {
      if (k == "a") { return [Some("A")] } else { return [None] }
    }
    fn first(xs) { return xs[0] ?? "none" }
  }
}
service Ops {
  fn look(k: Str) -> Str
  fn lookbox(k: Str) -> Str
  fn looklist(k: Str) -> Str
  fn sendnone(k: Str) -> Str
  fn sendsome(k: Str) -> Str
  fn sendbox(k: Str) -> Str
  fn sendboxsome(k: Str) -> Str
  fn sendlist(k: Str) -> Str
  fn sendlistsome(k: Str) -> Str
}
component C requires s: S provides ops: Ops {
  provide ops {
    fn look(k) { return match s.find(k) { Some(v) => "some:" + v, None => "none" } }
    fn lookbox(k) { return s.wrap(k).name ?? "none" }
    fn looklist(k) { return s.names(k)[0] ?? "none" }
    fn sendnone(k) { return s.echo(None) }
    fn sendsome(k) { return s.echo(Some(k)) }
    fn sendbox(k) { return s.boxname({ name: None }) }
    fn sendboxsome(k) { return s.boxname({ name: Some(k) }) }
    fn sendlist(k) { return s.first([None]) }
    fn sendlistsome(k) { return s.first([Some(k)]) }
  }
}
"""

#: probe -> the answer, whichever tier answers it
EXPECTED = {
    'ops.look("a")': "some:A",
    'ops.look("z")': "none",
    'ops.lookbox("a")': "A",
    'ops.lookbox("z")': "none",
    'ops.looklist("a")': "A",
    'ops.looklist("z")': "none",
    'ops.sendnone("z")': "none",
    'ops.sendsome("b")': "b",
    'ops.sendbox("z")': "none",
    'ops.sendboxsome("b")': "b",
    'ops.sendlist("z")': "none",
    'ops.sendlistsome("b")': "b",
}

_REQUIRED = {t.strip() for t in os.environ.get("REVL_REQUIRE_TIERS", "").split(",") if t.strip()}


def _need(tier: str, reason: str | None) -> None:
    if reason is None:
        return
    if tier in _REQUIRED:
        pytest.fail(f"REVL_REQUIRE_TIERS names {tier}, but {reason}")
    pytest.skip(reason)


def _need_tiers() -> None:
    java = shutil.which("javac")
    if java is not None and subprocess.run(
            ["javac", "-version"], capture_output=True).returncode != 0:
        java = None
    _need("java", None if java else "no working JDK (javac) on PATH")
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
         "--once"], capture_output=True, text=True, timeout=900, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    out = {}
    for line in (proc.stdout + proc.stderr).splitlines():
        m = re.match(r"^\[\w+\] probe\s*\|\s*(.+?)\s*\|\s*(.*)$", line)
        if m:
            value = m.group(2)
            out[m.group(1)] = (value[3:] if value.startswith("=> ") else value).strip("'\"")
    assert out, (proc.stdout + proc.stderr)[-3000:]
    return out


@pytest.mark.parametrize("provider, consumer", [("py", "java"), ("java", "py")],
                         ids=["py-to-java", "java-to-py"])
def test_an_opt_crosses_the_java_seam_in_every_position(tmp_path, provider, consumer):
    _need_tiers()
    assert _probes(tmp_path, provider, consumer) == EXPECTED
