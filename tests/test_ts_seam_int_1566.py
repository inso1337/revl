"""An `Int` crosses a seam into and out of the ts tier as a `bigint` (issue #1566).

WHAT WAS WRONG. The wire carries an `Int` as a JSON number, and the node
runner handed JSON.parse's JS `number` straight to the provider method. The ts
tier's `Int` is a `bigint`, so `n + 1n` threw `Cannot mix BigInt and other
types` for a py consumer, a node consumer and a probe alike. A value past 2^53
lost digits in JSON.parse before any code saw it, and the ts side refused to
send one at all.

WHAT HAPPENS NOW. The conductor hands a node process the declared types of
every key it serves or proxies (`placement.seam_typing`). The runner decodes
each argument, each reply and each probe literal by those types
(`bridge.ts` `decodeAs`), walking lists and record fields. It parses the wire
keeping integers past 2^53 exact, and writes them with `JSON.rawJSON`.

These run real placements: two processes and a UDS seam, one of them on
node. The py halves need the cordis-py runtime; with `REVL_REQUIRE_TIERS`
naming py and ts (the `conformance` CI job) a missing runtime FAILS instead
of skipping.
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
sys.path.insert(0, str(ROOT / "src"))

from revl.placement import seam_typing  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

APP = """
type Pair = { a: Int, b: Str }
service S {
  fn bump(n: Int) -> Int
  fn pair(p: Pair) -> Pair
  fn sum(xs: List[Int]) -> Int
}
component P provides s: S {
  provide s {
    fn bump(n) { return n + 1 }
    fn pair(p) { return { a: p.a + 1, b: p.b } }
    fn sum(xs) { return xs[0] + xs[1] }
  }
}
service Ops {
  fn run(n: Int) -> Int
  fn runpair(n: Int) -> Int
  fn runsum(n: Int) -> Int
}
component C requires s: S provides ops: Ops {
  provide ops {
    fn run(n) { return s.bump(n) * 10 }
    fn runpair(n) { return s.pair({ a: n, b: "x" }).a * 10 }
    fn runsum(n) { return s.sum([n, n + 1]) * 10 }
  }
}
"""

#: 2^53 + 1: the first integer a JS `number` cannot hold.
BIG = 9007199254740993

#: probe -> the value it must print, whichever tier answers it.
CONSUMER_PROBES = {
    "ops.run(41)": "420",
    "ops.runpair(41)": "420",
    "ops.runsum(41)": "830",
    "ops.run(900719925474099)": "9007199254741000",
    f"s.bump({BIG})": str(BIG + 1),
}

_REQUIRED = {t.strip() for t in os.environ.get("REVL_REQUIRE_TIERS", "").split(",") if t.strip()}


def _need(tier: str, reason: str | None) -> None:
    if reason is None:
        return
    if tier in _REQUIRED:
        pytest.fail(f"REVL_REQUIRE_TIERS names {tier}, but {reason}")
    pytest.skip(reason)


def _need_node() -> None:
    reason = None
    if shutil.which("node") is None:
        reason = "node is not on PATH"
    elif not (ROOT / "backends" / "typescript" / "node_modules" / "cordis").is_dir():
        reason = "cordis-ts is not installed (cd backends/typescript && npm ci)"
    _need("ts", reason)


def _need_py() -> None:
    reason = None
    if importlib.util.find_spec("cordis") is None:
        reason = "the cordis-py runtime is not installed (sh backends/python/setup.sh)"
    _need("py", reason)


def _placement(tmp_path: Path, provider: str, consumer: str | None, probes) -> list[str]:
    app = tmp_path / "app.rvl"
    app.write_text(APP, encoding="utf-8")
    probe_list = ", ".join('"' + p + '"' for p in probes)
    if consumer is None:
        toml = (f'[processes.prov]\nbackend = "{provider}"\ncomponents = ["P"]\n'
                f"probe = [{probe_list}]\n")
        app.write_text(APP.split("service Ops")[0], encoding="utf-8")
    else:
        toml = (f'[processes.prov]\nbackend = "{provider}"\ncomponents = ["P"]\n\n'
                f'[processes.cons]\nbackend = "{consumer}"\ncomponents = ["C"]\n'
                f"probe = [{probe_list}]\n")
    plc = tmp_path / "app.toml"
    plc.write_text(toml, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(app), "--placement", str(plc), "--once"],
        capture_output=True, text=True, timeout=600, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    return (proc.stdout + proc.stderr).splitlines()


def _probe_results(lines: list[str]) -> dict[str, str]:
    """`[proc] probe | <expr> | => <value>` lines, as expr -> value (or the
    ERROR text)."""
    out = {}
    for line in lines:
        m = re.match(r"^\[\w+\] probe\s*\|\s*(.+?)\s*\|\s*(.*)$", line)
        if m:
            value = m.group(2)
            out[m.group(1)] = value[3:] if value.startswith("=> ") else value
    return out


def _assert_probes(lines: list[str], expected: dict[str, str]) -> None:
    got = _probe_results(lines)
    assert set(got) == set(expected), "\n".join(lines)
    assert got == expected, "\n".join(lines)


@pytest.mark.parametrize("consumer", ["py", "node"])
def test_a_consumer_calls_a_ts_provider_with_int_arguments(tmp_path, consumer):
    """py -> ts and ts -> ts: every argument, list element and record field
    arrives as a `bigint`, and 2^53 + 1 crosses exact."""
    _need_node()
    if consumer == "py":
        _need_py()
    lines = _placement(tmp_path, "node", consumer, CONSUMER_PROBES)
    _assert_probes(lines, CONSUMER_PROBES)


def test_a_ts_consumer_reads_int_results_from_a_py_provider(tmp_path):
    """ts -> py: the replies are decoded by the declared return types, so the
    consumer's `* 10n` sees a `bigint`, and a result past 2^53 is exact."""
    _need_node()
    _need_py()
    lines = _placement(tmp_path, "py", "node", CONSUMER_PROBES)
    _assert_probes(lines, CONSUMER_PROBES)


def test_a_probe_calls_a_ts_provider_with_int_arguments(tmp_path):
    """probe -> ts: a node process's own probe of its own provider. The probe
    literal is decoded by the method's declared parameter type."""
    _need_node()
    probes = {"s.bump(41)": "42", f"s.bump({BIG})": str(BIG + 1)}
    lines = _placement(tmp_path, "node", None, probes)
    _assert_probes(lines, probes)


def test_seam_typing_names_every_param_and_return_and_the_type_table():
    ir = compile_source(APP, "app.rvl")
    typing = seam_typing(ir, {"s": "S", "ops": "Ops"})
    assert typing["signatures"]["s"] == {
        "bump": {"params": ["Int"], "returns": "Int"},
        "pair": {"params": ["Pair"], "returns": "Pair"},
        "sum": {"params": ["List[Int]"], "returns": "Int"},
    }
    assert typing["signatures"]["ops"]["run"] == {"params": ["Int"], "returns": "Int"}
    assert typing["types"]["Pair"]["fields"] == {"a": "Int", "b": "Str"}
