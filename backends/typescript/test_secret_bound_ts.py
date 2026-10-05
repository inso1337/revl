"""Issue #1936 (c): the ts tier injects bound secrets, as the py tier does.

A `secret NAME for CAP` binding (item 256) is injected as the first local of
every emission extern body that serves CAP. The py emitter has done it since
item 256 Slice 1: a `_REVL_SECRETS` map, a fail-loud `_revl_secret` helper,
and `NAME = _revl_secret("NAME")` at the head of each bound body, filled by
the driver at plug from `REVL_SECRET_<NAME>`. The ts emitter emitted none of
it, so a bound `@ts` body that read its key failed with a ReferenceError, and
a bound capability could only refuse on ts.

Now the ts emitter emits the same seam (`_REVL_SECRET_NAMES`,
`_REVL_SECRETS`, `_revlSecret`), and the node runner (placement_runner.ts,
behind `revl run --backend ts` and a ts placement process) resolves each name
from its environment at plug and installs it before any component loads,
refusing the plug when one is missing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

# The key reaches the body: it returns the message length plus the key length.
_BOUND = (
    "secret api_key for net.send\n"
    "extern emission[net.send] fn send(m: Str) -> Int "
    "= @py { return len(m) + len(api_key) } "
    "= @ts { return BigInt(m.length + api_key.length); }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component A provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = emit send(u)\n      return 0\n    }\n  }\n}\n"
)

_SECRET_FREE = (
    "extern emission[net.send] fn send(m: Str) -> Int "
    "= @py { return 0 } = @ts { return 0n; }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component A provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = emit send(u)\n      return 0\n    }\n  }\n}\n"
)

# A composition whose provide method fires the bound extern; a placement probe
# calls it through the node runner. The body throws unless the installed key is
# the expected one. (An activation-body `emit` of a bound extern is refused at
# compile today, which is issue #1936 (a).)
_PROBED = (
    "secret api_key for net.send\n"
    "extern emission[net.send] fn send(m: Str) -> Int "
    "= @py { return 0 } "
    '= @ts { if (api_key !== "sk-secret-value") { throw new Error("wrong key"); } '
    "return BigInt(m.length); }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component A provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = emit send(u)\n      return 7\n    }\n  }\n}\n"
)

_PLACEMENT = """\
[processes.solo]
backend = "ts"
components = ["A"]
probe = ["ops.go(\\"hi\\")"]
"""


def _tsemit():
    spec = importlib.util.spec_from_file_location("_tsemit_1936", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_module(src: str, tail: str) -> subprocess.CompletedProcess:
    """Run the emitted module under plain node with a stub runtime, then `tail`."""
    code = _tsemit().emit(compile_source(src, "bound.rvl"))
    d = Path(tempfile.mkdtemp())
    (d / "runtime.ts").write_text("export const host: any = {};\n", encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    (pkg / "mod.ts").write_text(code + "\n" + tail + "\n", encoding="utf-8")
    return subprocess.run(["node", str(pkg / "mod.ts")],  # noqa: S603
                          capture_output=True, text=True, timeout=120)


def test_the_bound_extern_reads_its_key_as_its_first_local():
    code = _tsemit().emit(compile_source(_BOUND, "bound.rvl"))
    assert 'export const _REVL_SECRET_NAMES: string[] = ["api_key"];' in code
    assert "export const _REVL_SECRETS: Record<string, string> = {};" in code
    assert "function _revlSecret(name: string): string {" in code
    head = code[code.index("export function send("):]
    assert head.splitlines()[1] == '  const api_key = _revlSecret("api_key");'


def test_a_secret_free_program_emits_no_secret_seam():
    code = _tsemit().emit(compile_source(_SECRET_FREE, "free.rvl"))
    assert "_REVL_SECRET" not in code and "_revlSecret" not in code


@needs_node
def test_the_injected_key_reaches_the_ts_body():
    proc = _run_module(_BOUND, '_REVL_SECRETS["api_key"] = "sk-secret-value";\n'
                               'console.log(JSON.stringify(String(send("hi"))));')
    assert proc.returncode == 0, proc.stderr
    # len("hi") + len("sk-secret-value") == 2 + 15, as on py
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == "17"


@needs_node
def test_an_uninstalled_key_fails_loud_and_never_names_a_value():
    proc = _run_module(_BOUND, 'send("hi");')
    assert proc.returncode != 0
    assert "capability-bound secret `api_key` was not installed" in proc.stderr
    assert "sk-" not in proc.stderr


def _revl_run_ts(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    sys.path.insert(0, str(ROOT / "src"))
    from revl.run_ts import ts_runtime_reason  # noqa: PLC0415
    reason = ts_runtime_reason()
    if reason:
        pytest.skip(reason)
    path = tmp_path / "bound.rvl"
    path.write_text(_PROBED, encoding="utf-8")
    placement = tmp_path / "solo.toml"
    placement.write_text(_PLACEMENT, encoding="utf-8")
    full = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    full.pop("REVL_SECRET_API_KEY", None)
    full.update(env)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "revl", "run", str(path), "--placement", str(placement),
         "--once"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=600, env=full, cwd=ROOT)


@needs_node
def test_the_node_runner_installs_the_key_from_the_environment_at_plug(tmp_path):
    ran = _revl_run_ts(tmp_path, {"REVL_SECRET_API_KEY": "sk-secret-value"})
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "wrong key" not in out and "ERROR" not in out, out
    assert '=> 7' in out, out
    assert "sk-secret-value" not in out  # never logged


@needs_node
def test_the_node_runner_refuses_the_plug_without_the_key(tmp_path):
    ran = _revl_run_ts(tmp_path, {})
    out = ran.stdout + ran.stderr
    assert ran.returncode != 0, out
    assert "capability-bound secret `api_key` has no value at plug" in out, out
    assert "REVL_SECRET_API_KEY" in out
