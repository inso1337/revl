"""The ts `json_parse` refuses the malformed JSON the py tier refuses (#3205).

`json_parse` is `pure`, so it must not diverge per tier. The @ts body is a
hand-written recursive-descent parser; its number and string productions used
to accept documents RFC 8259 (and py's `json.loads`) refuse: a leading zero,
`1e` (which became a Float NaN, defeating every bound comparison), `1.`, `-.5`,
a `\\u` escape without four hex digits (decoded as NUL), and a raw character
below U+0020 inside a string.

The same documents run through the py body and the ts body (node) here, and
each tier must give the same verdict. A document both accept must decode to
the same value.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

#: documents RFC 8259 refuses, each of which the ts body used to accept
REFUSED = [
    "[0123]",  # leading zero
    "0123",
    "-01",
    "[1e]",  # an empty exponent became a Float NaN
    "[1e+]",
    "[1E-]",
    "[1.]",  # no digits after the point
    "[1.e5]",
    "[-.5]",  # no digits before the point
    "[.5]",
    "[-]",  # a sign with no digits
    "-",
    '["\\uZZZZ"]',  # a non-hex escape decoded as NUL
    '["\\u12"]',  # a short escape
    '["\\u12G4"]',
    '["\\u"]',
    '["\\',  # a lone backslash at the end of the input
    "[1e",  # truncated in the exponent
    '["a\nb"]',  # a raw newline in a string
    '["a\tb"]',  # a raw tab in a string
    '["a\x00b"]',  # a raw NUL in a string
    '{"a\nb": 1}',  # the same in an object key
    '{"amount": 1e}',  # the NaN that passes `amount > max`
]

#: documents both tiers accept, which must decode to the same value
ACCEPTED = [
    "0",
    "-0",
    "[0, 10, 100]",
    "[1e5, 1E5, 1e+5, 1e-5, 1.5e3]",
    "[0.5, -0.5, 0e0, 0.0]",
    "-7",
    "9007199254740993",
    '["\\u00e9", "\\uD83D\\uDE00", "\\u0041"]',
    '["\\n\\t\\"\\\\\\/"]',
    '{"a": [1, 2.5, true, null, "x"]}',
    ' \t\r\n[1] \n',
]


def _compile(d: Path) -> dict:
    (d / "stdlib").mkdir()
    (d / "stdlib" / "json.rvl").write_text(
        (ROOT / "stdlib" / "json.rvl").read_text(encoding="utf-8"),
        encoding="utf-8")
    (d / "main.rvl").write_text(
        'use "stdlib/json.rvl" { json_parse, json_stringify }\n'
        "fn roundtrip(s: Str) -> Str { return json_stringify(json_parse(s)) }\n",
        encoding="utf-8")
    return compile_files([str(d / "main.rvl")])


def _emit(backend: str, ir: dict) -> str:
    path = str(ROOT / "backends" / backend)
    sys.path.insert(0, path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_json3205_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = module.emit(ir)
        return "\n".join(out.values()) if isinstance(out, dict) else str(out)
    finally:
        sys.path.remove(path)


def _py_verdicts(ir: dict, docs: list, fn: str) -> list:
    code = _emit("python", ir)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had, previous = "runtime" in sys.modules, sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(code, "emitted.py", "exec"), ns)  # noqa: S102 - our own emitter
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    out = []
    for doc in docs:
        try:
            out.append(["accept", ns[fn](doc)])
        except Exception:  # noqa: BLE001 - any refusal is the verdict
            out.append(["refuse", None])
    return out


def _ts_verdicts(ir: dict, docs: list, fn: str, d: Path) -> list:
    code = _emit("typescript", ir)
    (d / "runtime.ts").write_text("export const host: any = {};\n",
                                  encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    # `fn` is json_parse for the refusal verdict (a NaN must be refused by the
    # parse itself, not by a later json_stringify) and roundtrip for the
    # valid-document comparison.
    tail = (
        f"const docs: string[] = {json.dumps(docs)};\n"
        "const out: any[] = [];\n"
        "for (const doc of docs) {\n"
        f"  try {{ const r: any = {fn}(doc); "
        "out.push(['accept', typeof r === 'string' ? r : null]); }\n"
        "  catch (e) { out.push(['refuse', null]); }\n"
        "}\n"
        "console.log(JSON.stringify(out));\n")
    (pkg / "mod.ts").write_text(code + "\n" + tail, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        ["node", str(pkg / "mod.ts")], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def ir(tmp_path_factory):
    return _compile(tmp_path_factory.mktemp("json3205_src"))


def test_py_tier_refuses_the_malformed_documents(ir):
    """The reference verdict the ts tier is held to."""
    verdicts = _py_verdicts(ir, REFUSED, "json_parse")
    assert [v[0] for v in verdicts] == ["refuse"] * len(REFUSED)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_tier_refuses_what_the_py_tier_refuses(ir, tmp_path):
    # json_parse itself, not roundtrip: json_stringify refuses a non-finite
    # Float, which would hide a NaN the parser let through
    verdicts = _ts_verdicts(ir, REFUSED, "json_parse", tmp_path)
    accepted = [d for d, v in zip(REFUSED, verdicts) if v[0] == "accept"]
    assert not accepted, f"the ts json_parse accepted malformed JSON: {accepted!r}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_tier_still_agrees_with_py_on_valid_documents(ir, tmp_path):
    py = _py_verdicts(ir, ACCEPTED, "roundtrip")
    ts = _ts_verdicts(ir, ACCEPTED, "roundtrip", tmp_path)
    assert [v[0] for v in py] == ["accept"] * len(ACCEPTED)
    # float spelling is json_stringify's business (py `1e-05`, ts `0.00001`),
    # so compare the decoded documents, not the text
    decoded = [[v[0], json.loads(v[1])] for v in py], [[v[0], json.loads(v[1])] for v in ts]
    assert decoded[0] == decoded[1]
