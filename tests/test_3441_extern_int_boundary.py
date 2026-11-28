"""Issue #3441: the extern boundary range-checks a returned `Int` (py + ts).

WHAT WAS WRONG. A host body of an extern could return an `Int` outside the
64-bit range and the value was accepted unchecked on the py tier; it then
faulted at the next revl arithmetic on it, far from the cause (`host_prime()`
returned 2**255-19; `host_prime() - 1` then trapped with "Int overflow"). On
the ts tier `revlToBigInt` coerced the type but not the range, so the two
tiers disagreed.

WHAT HOLDS NOW.
  * py: an extern whose declared return can hold an `Int` (bare, in a generic
    argument, or in a record field - `_int_reachable`) is decorated
    `@_revl_int_result('<name>')`; the runtime wrapper walks the returned value
    and raises `RuntimeError("extern `<name>` returned an Int outside the
    64-bit range: ...")` at the boundary. Non-Int externs are byte-identical.
  * ts: a verbatim `@ts` body declared `Int` is wrapped in
    `revlExternInt(..., "<name>")`, which coerces AND range-checks, throwing a
    `RangeError` naming the extern.

Checked here: the issue's repro fails at the boundary naming the extern on both
tiers (py by exec'ing the emitted module, ts by running it under node when
available), in-range values pass, `List[Int]`/record/`Map` returns are
checked, and a non-Int extern carries no decorator.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests", ROOT / "backends" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

PY_EMIT = backend_emitter("python")
TS_EMIT = backend_emitter("typescript")


def _emitted(emit, source: str, name: str = "t3441.rvl") -> str:
    text = emit.emit(compile_source(source, name))
    if isinstance(text, dict):
        return "\n".join(v for v in text.values() if isinstance(v, str))
    return str(text)


def _call_py(source: str, fn: str, *args):
    ns: dict = {}
    exec(compile(_emitted(PY_EMIT, source), "<t3441>", "exec"), ns)
    return ns[fn](*args)


OUT_OF_RANGE = "extern pure fn host_prime() -> Int = @py { return 2**63 }\n"


# ------------------------------------------------------------------ py tier


def test_py_out_of_range_int_fails_at_the_boundary_naming_the_extern():
    with pytest.raises(RuntimeError) as excinfo:
        _call_py(OUT_OF_RANGE, "host_prime")
    assert "host_prime" in str(excinfo.value)
    assert "64-bit" in str(excinfo.value)
    assert "9223372036854775808" in str(excinfo.value)


def test_py_in_range_int_passes():
    assert _call_py("extern pure fn ok() -> Int = @py { return 2**62 }\n", "ok") == 2**62
    assert _call_py("extern pure fn edge() -> Int = @py { return 2**63 - 1 }\n", "edge") == 2**63 - 1


def test_py_negative_out_of_range_fails():
    with pytest.raises(RuntimeError) as excinfo:
        _call_py("extern pure fn lo() -> Int = @py { return -(2**63) - 1 }\n", "lo")
    assert "lo" in str(excinfo.value)


def test_py_list_record_and_map_returns_are_checked():
    with pytest.raises(RuntimeError) as e1:
        _call_py("extern pure fn xs() -> List[Int] = @py { return [1, 2**63] }\n", "xs")
    assert "xs" in str(e1.value)
    with pytest.raises(RuntimeError) as e2:
        _call_py("type R = { v: Int }\nextern pure fn r() -> R = @py { return { \"v\": 2**64 } }\n", "r")
    assert "r" in str(e2.value)
    with pytest.raises(RuntimeError) as e3:
        _call_py("extern pure fn m() -> Map[Str, Int] = @py { return { \"a\": 2**70 } }\n", "m")
    assert "m" in str(e3.value)


def test_py_bool_and_int32_externs_are_not_range_checked():
    assert _call_py("extern pure fn b() -> Bool = @py { return True }\n", "b") is True
    text = _emitted(PY_EMIT, "extern pure fn i32() -> Int32 = @py { return 5 }\n")
    assert "int_result" not in text


def test_py_non_int_extern_carries_no_decorator():
    text = _emitted(PY_EMIT, "extern pure fn s() -> Str = @py { return \"hi\" }\n")
    assert "int_result" not in text
    assert _call_py("extern pure fn s() -> Str = @py { return \"hi\" }\n", "s") == "hi"


def test_py_decorator_is_emitted_for_int_return():
    text = _emitted(PY_EMIT, OUT_OF_RANGE)
    assert "from runtime import int_result as _revl_int_result" in text
    assert "@_revl_int_result('host_prime')" in text


# ------------------------------------------------------------------ ts tier


TS_OUT_OF_RANGE = 'extern pure fn host_prime() -> Int\n= @ts {\n  return 2n ** 63n\n}\n'


def test_ts_wrapper_is_emitted_and_names_the_extern():
    text = _emitted(TS_EMIT, TS_OUT_OF_RANGE, "t3441ts.rvl")
    assert "function revlExternInt(v: unknown, name: string): bigint {" in text
    assert 'revlExternInt((() => {' in text
    assert '"host_prime")' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_ts_out_of_range_int_fails_at_the_boundary_naming_the_extern(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (tmp_path / "runtime.ts").write_text(
        (ROOT / "backends" / "typescript" / "runtime.ts").read_text(encoding="utf-8"),
        encoding="utf-8")
    (out_dir / "app.ts").write_text(_emitted(TS_EMIT, TS_OUT_OF_RANGE, "app.rvl"),
                                    encoding="utf-8")
    probe = (
        "import('./app.ts').then(m => {"
        "  try { m.host_prime(); console.log('NO-ERROR'); }"
        "  catch (e) { console.log(e.constructor.name + '|' + e.message); }"
        "})"
    )
    proc = subprocess.run(
        [shutil.which("node"), "--experimental-strip-types", "-e", probe],
        capture_output=True, text=True, timeout=60, cwd=str(out_dir))
    line = next((l for l in proc.stdout.splitlines() if "|" in l), "")
    assert line.startswith("RangeError|"), (proc.stdout, proc.stderr)
    assert "host_prime" in line and "64-bit" in line, line


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_ts_in_range_int_passes(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (tmp_path / "runtime.ts").write_text(
        (ROOT / "backends" / "typescript" / "runtime.ts").read_text(encoding="utf-8"),
        encoding="utf-8")
    src = 'extern pure fn ok() -> Int\n= @ts {\n  return 42\n}\n'
    (out_dir / "app.ts").write_text(_emitted(TS_EMIT, src, "app.rvl"), encoding="utf-8")
    probe = "import('./app.ts').then(m => console.log('VALUE|' + m.ok().toString()))"
    proc = subprocess.run(
        [shutil.which("node"), "--experimental-strip-types", "-e", probe],
        capture_output=True, text=True, timeout=60, cwd=str(out_dir))
    line = next((l for l in proc.stdout.splitlines() if l.startswith("VALUE|")), "")
    assert line == "VALUE|42", (proc.stdout, proc.stderr)
