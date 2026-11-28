"""Issue #3442: `Bytes` can be built from numbers in revl (stdlib/bytes.rvl).

WHAT WAS WRONG. `Bytes` could be measured, indexed, sliced, concatenated and
compared, but not constructed: `b.push(129)` needed a List receiver and
`Bytes.from_list([129])` was "no builtin method", so binary framing had to
live in a host body. `b[i].to_int32()` was refused as "value of unknown type".

WHAT HOLDS NOW. stdlib/bytes.rvl provides:
  * `bytes_from_list(xs: List[Int]) -> Bytes` (per-tier extern, @py/@ts) -
    every element checked to be 0..255 at the boundary, failing the call
    naming the function;
  * `bytes_to_list(b: Bytes) -> List[Int]` - the inverse;
  * `byte_at(b: Bytes, i: Int) -> Int` - the typed index read, pure revl
    (Int32 widens to Int at the return), so the byte can be compared, masked
    with `^` via `to_int32()`, and rebuilt.

Checked here (py tier: compile + emit + exec the consumer; ts tier: emit, and
under node when available): the issue's acceptance - a six-byte frame built
from numbers equals a host-body Bytes literal, a payload masked with a
four-byte key by index matches the host-side XOR, round trip and byte_at
hold, and an element of 256 is refused naming `bytes_from_list`.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from revl import compile_files  # noqa: E402
from _backend_import import backend_emitter  # noqa: E402

STDLIB = ROOT / "stdlib" / "bytes.rvl"
PY_EMIT = backend_emitter("python")
TS_EMIT = backend_emitter("typescript")

CONSUMER = """\
use "stdlib/bytes.rvl" { bytes_from_list, bytes_to_list, byte_at }

pub extern pure fn frame_literal() -> Bytes = @py { return bytes([0x81, 0x85, 10, 20, 30, 40]) }

pub fn t_frame() -> Bytes { return bytes_from_list([0x81, 0x85, 10, 20, 30, 40]) }
pub fn t_literal() -> Bytes { return frame_literal() }
pub fn t_to(b: Bytes) -> List[Int] { return bytes_to_list(b) }
pub fn t_at(b: Bytes, i: Int) -> Int { return byte_at(b, i) }
pub fn t_mask(payload: Bytes, key: Bytes) -> Bytes {
  var out: List[Int] = []
  var i = 0
  let n = payload.length()
  while (i < n) {
    let m = byte_at(payload, i).to_int32() ^ byte_at(key, i % key.length()).to_int32()
    out = out.push(m)
    i += 1
  }
  return bytes_from_list(out)
}
pub fn t_from(xs: List[Int]) -> Bytes { return bytes_from_list(xs) }
"""


@pytest.fixture(scope="module")
def ns(tmp_path_factory):
    d = tmp_path_factory.mktemp("bytes_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "bytes.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                            encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    ir = compile_files([str(main)])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_bytes", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "bytes.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


# ------------------------------------------------------------------ surface

def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    for sig in (
        "pub extern pure fn bytes_from_list(xs: List[Int]) -> Bytes =",
        "pub extern pure fn bytes_to_list(b: Bytes) -> List[Int] =",
        "pub fn byte_at(b: Bytes, i: Int) -> Int",
    ):
        assert sig in text, sig


# --------------------------------------------------------------- the acceptance

def test_six_byte_frame_equals_host_literal(ns):
    frame = ns["t_frame"]()
    assert len(frame) == 6
    assert frame == ns["t_literal"]()


def test_mask_payload_with_four_byte_key_by_index(ns):
    payload = ns["t_from"]([0x68, 0x69, 0x21, 0x00, 0xFF])
    key = ns["t_from"]([10, 20, 30, 40])
    masked = ns["t_mask"](payload, key)
    # host-side XOR: 0x68^10=0x62, 0x69^20=0x7D, 0x21^30=0x3F, 0x00^40=0x28,
    # 0xFF^10=0xF5
    assert ns["t_to"](masked) == [0x62, 0x7D, 0x3F, 0x28, 0xF5]


def test_round_trip_and_byte_at(ns):
    b = ns["t_from"]([0, 1, 127, 128, 255])
    assert ns["t_to"](b) == [0, 1, 127, 128, 255]
    assert ns["t_at"](b, 3) == 128


@pytest.mark.parametrize("bad", [[1, 256, 3], [-1], [0x100]])
def test_element_outside_0_255_is_refused(ns, bad):
    with pytest.raises(ValueError) as excinfo:
        ns["t_from"](bad)
    assert "bytes_from_list" in str(excinfo.value)
    assert "0..255" in str(excinfo.value)


# ------------------------------------------------------------------ ts tier

def test_ts_emits_uint8array_constructor():
    d = Path(__file__).parent / "_bytes_ts_probe"
    text = TS_EMIT.emit(compile_files([str(ROOT / "stdlib" / "bytes.rvl")]))
    if isinstance(text, dict):
        text = "\n".join(v for v in text.values() if isinstance(v, str))
    assert "Uint8Array" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_ts_tier_builds_and_refuses(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (tmp_path / "runtime.ts").write_text(
        (ROOT / "backends" / "typescript" / "runtime.ts").read_text(encoding="utf-8"),
        encoding="utf-8")
    main = tmp_path / "main.rvl"
    (tmp_path / "stdlib").mkdir(exist_ok=True)
    (tmp_path / "stdlib" / "bytes.rvl").write_text(
        STDLIB.read_text(encoding="utf-8"), encoding="utf-8")
    main.write_text(
        'use "stdlib/bytes.rvl" { bytes_from_list, bytes_to_list }\n'
        'pub fn t_from(xs: List[Int]) -> Bytes { return bytes_from_list(xs) }\n'
        'pub fn t_to(b: Bytes) -> List[Int] { return bytes_to_list(b) }\n',
        encoding="utf-8")
    text = TS_EMIT.emit(compile_files([str(main)]))
    if isinstance(text, dict):
        text = "\n".join(v for v in text.values() if isinstance(v, str))
    (out_dir / "app.ts").write_text(text, encoding="utf-8")
    probe = (
        "import('./app.ts').then(m => {"
        "  const b = m.t_from([129n, 133n, 10n]);"
        "  console.log('LEN|' + b.length + '|TO|' + m.t_to(b).map(String).join(','));"
        "  try { m.t_from([256n]); console.log('256|NO-ERROR'); }"
        "  catch (e) { console.log('256|' + e.constructor.name + '|' + e.message); }"
        "})"
    )
    proc = subprocess.run(
        [shutil.which("node"), "--experimental-strip-types", "-e", probe],
        capture_output=True, text=True, timeout=60, cwd=str(out_dir))
    lines = [l for l in proc.stdout.splitlines() if "|" in l]
    assert "LEN|3|TO|129,133,10" in lines, (proc.stdout, proc.stderr)
    assert any(l.startswith("256|RangeError|bytes_from_list") for l in lines), lines
