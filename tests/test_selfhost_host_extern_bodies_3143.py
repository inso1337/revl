"""Issue #3143: the self-host sources declare host externs, and a tier refuses
a whole file when any extern it declares has no body for that tier.

Every extern in `selfhost/*.rvl` therefore carries a `@py`, a `@ts` and an
`@rs` body. Three checks hold that together:

1. the declarations themselves (no extern is left with a missing tier body);
2. each `@ts` / `@rs` body gives the same text as its `@py` body over a probe
   set, run through node and cargo (the two bodies are the same host
   formatting, written once per tier);
3. the files that used to be refused now emit on the typescript and rust
   tiers.

The probe sets keep to inputs where the tiers agree by construction. Two
documented approximations stay out of the rust comparison: `repr` / the rust
string literal decide `str.isprintable` for U+00AD (a format character) from
`is_control` / `is_whitespace`, and serde_json without `float_roundtrip` reads
a 17 digit float one ulp off.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.lexer import _HOST_TRIVIA, _match_brace  # noqa: E402

SELFHOST = ROOT / "selfhost"
FILES = ["compile", "emit_go", "emit_java", "emit_py", "emit_rust", "emit_ts",
         "emit_wasm", "lower"]

_DECL = re.compile(
    r"^(?:pub )?extern (?:pure )?fn (\w+)\(([^)]*)\)\s*->\s*([^\n=]+?)\s*(?==|\n|$)", re.M)
_ARM = re.compile(r"\s*=\s*@(\w+)\s*\{")


def _externs(path: Path) -> dict[str, tuple[list, str, dict[str, str]]]:
    """name -> (params, return type, {tier: body text}) for each extern declared
    at the start of a line. `lower.rvl` quotes extern declarations inside its own
    test strings, so only `sc_sha256` is read from it."""
    text = path.read_text(encoding="utf-8")
    out = {}
    for m in _DECL.finditer(text):
        name = m.group(1)
        if path.name == "lower.rvl" and name != "sc_sha256":
            continue
        params = []
        for p in m.group(2).split(","):
            if p.strip():
                n, t = p.split(":")
                params.append((n.strip(), t.strip()))
        bodies = {}
        pos = m.end()
        while True:
            a = _ARM.match(text, pos)
            if not a:
                break
            tier = a.group(1)
            close = _match_brace(text, a.end() - 1,
                                 _HOST_TRIVIA[{"rs": "rust"}.get(tier, tier)])
            bodies[tier] = text[a.end():close]
            pos = close + 1
        out[name] = (params, m.group(3).strip(), bodies)
    return out


ALL = {f: _externs(SELFHOST / f"{f}.rvl") for f in FILES}


def test_every_selfhost_extern_has_a_py_ts_and_rs_body():
    missing = [
        f"{f}.rvl: {name} lacks @{tier}"
        for f, externs in ALL.items()
        for name, (_, _, bodies) in externs.items()
        for tier in ("py", "ts", "rs")
        if tier not in bodies
    ]
    assert not missing, "\n".join(missing)
    # the scan found the declarations it is meant to guard
    assert sum(len(e) for e in ALL.values()) >= 38


# ------------------------------------------------------------ probe inputs
BIG = 2**63 - 1
NUMS = [0, 1, 42, -7, 10**18, BIG, 1.5, 3.0, -0.0, 0.0, 1e16, 1e15,
        9999999999999998.0, 0.0001, 0.00001, 1e-7, 123456789.5, 5e-324,
        1.7976931348623157e308, 0.1, 100.0, 1e22, 2.5e-5, -1234.5]
STRS = ["", "a", "plain text", "it's", 'say "hi"', "both ' and \"", "back\\slash",
        "tab\there", "nl\nx\r\ny", "\x00\x01\x1f\x7f", "é", "日本語", "😀 astral",
        "\u00a0nbsp", "\u2028sep", "${x}", "`tick`", "a*/b/*c", "end\n", "\x85ctl",
        "{}[]"]
TS_ONLY_STRS = ["\u00adsoft"]
JSONV = NUMS + STRS + [True, False, None, [], ["a", "b"], [1, [2, [3.5]]], {},
                       {"k": 1, "z": [True, None]}, {"é": "ü", "a b": {"c": 1.0}}]
IDENTS = ["EmitRust", "a1B", "HTTPServer", "emit_rust", "", "x", "Hello", "aB", "A",
          "AbCd9E", "foo_bar", "a-b_c", "__x", "AB_cd", "fooBar", "__a__b", "a_b",
          "foo__bar", "emitTs"]
MANGLE = ["lambda", "lambda_", "len", "len_", "foo", "None", "match", "match__",
          "type", "_", "", "Ok", "Err", "_RevlRouter", "_ov1", "_ov", "_ov12_",
          "_effect_2", "_timer_3_h", "_timer_3x", "_timer_3", "_timer_3_inflight",
          "_FOO_CONFIG", "__CONFIG", "_CONFIG", "_revl", "_REVLx", "__Revl_a",
          "revl", "_bi", "_bi_", "x_", "print", "Self", "self_", "ctx", "plugin__"]
SPACE = ["  a  ", "\t\nx\r\n", "\x1c\x1dz\x1e\x1f", "\u00a0a\u00a0", "\u3000b\u2003",
         "\ufeffc\ufeff", "\x85d\x85", "", "   ", "a b", "\u200bz\u200b"]
LINES = ["", "a", "a\n", "a\n\n", "a\nb", "a\r\nb", "a\rb",
         "a\x0bb\x0cc\x1cd\x1de\x1ef\x85g\u2028h\u2029i", "\n", "\r\n\r\n", "a\r"]
DEDENT = ["", "    a\n    b", "    a\n  b", "\t a\n \tb", "  a\n\n  b\n   \n  c", "a\n  b",
          "  a\r\n  b", "  \n  x", "   \n", " a\n  b\n c", "\tx\n\ty\n", "  a\n  \n",
          "  a\n \n"]
HASH = ["", "abc", "é日本😀", "a" * 1000, "x" * 55, "y" * 56, "z" * 64, "w" * 119]


def _samples(name: str, params: list, tier: str) -> list[list]:
    strs = STRS + (TS_ONLY_STRS if tier == "ts" else [])
    if name in ("tab", "newline", "stdlib_mark", "flag_char"):
        return [[]]
    if name == "num_str":
        return [[v] for v in NUMS]
    if name in ("json_dumps", "py_repr"):
        extra = TS_ONLY_STRS if tier == "ts" and name == "py_repr" else []
        return [[v] for v in JSONV + extra]
    if name in ("go_string", "string_lit"):
        return [[s] for s in (strs if name == "string_lit" else STRS)]
    if name == "template_text":
        return [[s] for s in STRS]
    if name == "comment_text":
        return [[s] for s in STRS + ["a\rb\u2029c", "x */ y /* z"]]
    if name in ("camel", "snake", "pascal", "upper"):
        # `capitalize` title-cases its first character (U+01C6 -> U+01C5, U+00DF ->
        # "Ss"); the ts and rs bodies upper-case it, as emit_rust.rvl's always did.
        extra = {"snake": ["ÀB"], "camel": ["Éa_b", "é_ü"]}.get(name, ["Éa_b", "é_ü", "ß", "ÀB"])
        return [[s] for s in IDENTS + extra]
    if name == "mangle":
        return [[n] for n in MANGLE]
    if name == "mangle_with":
        return [[n, b, t, tm] for n in MANGLE for b in (False, True)
                for t in (False, True) for tm in (False, True)]
    if name in ("py_rstrip", "py_strip"):
        return [[s] for s in SPACE]
    if name == "py_strip_nl":
        return [[s] for s in ["\n\nabc\n\n", "abc", "", "\n", "a\nb", "\n \n"]]
    if name == "py_splitlines":
        return [[s] for s in LINES]
    if name == "py_dedent":
        return [[s] for s in DEDENT]
    if name in ("sha256_hex", "sc_sha256"):
        return [[s] for s in HASH]
    raise KeyError(name)


FS_NAMES = {"module_path", "read_module_text", "ref_probe"}
# `snake` / `camel` / `newline` / ... exist in several files with different
# bodies: probe every declaration, keyed by file.
PROBED = [(f, n) for f, ext in ALL.items() for n in ext if n not in FS_NAMES]


def _py_fn(body: str, params: list):
    src = "def f(%s):\n%s\n" % (", ".join(n for n, _ in params),
                                 textwrap.indent(textwrap.dedent(body.strip("\n")), "    "))
    g: dict = {}
    exec(src, g)  # the @py body, run as the reference it is
    return g["f"]


def _js(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, int):
        return f"{v}n"
    if isinstance(v, float):
        return "(-0)" if str(v) == "-0.0" else repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_js(x) for x in v) + "]"
    return "{" + ", ".join(f"{json.dumps(k)}: {_js(x)}" for k, x in v.items()) + "}"


TS_TYPES = {"Str": "string", "Any": "any", "Bool": "boolean", "List[Str]": "string[]"}
RS_TYPES = {"Str": "String", "Any": "Box<dyn Any>", "Bool": "bool", "List[Str]": "Vec<String>"}


def _node_ok() -> bool:
    """The probe is a `.ts` file run by plain node: it needs type stripping
    (node 23.6+) and `process.getBuiltinModule` (22.3+)."""
    if shutil.which("node") is None:
        return False
    out = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout
    major, minor = (int(x) for x in out.strip().lstrip("v").split(".")[:2])
    return (major, minor) >= (23, 6)


NODE_OK = _node_ok()


def _node_run(cases: list[tuple], env: dict | None = None, cwd: Path | None = None) -> dict:
    out = []
    for cid, body, params, ret, sets in cases:
        sig = ", ".join(f"{n}: {TS_TYPES[t]}" for n, t in params)
        out.append(f"function f_{cid}({sig}): {TS_TYPES[ret]} {{\n{body}\n}}")
    out.append("const results: Record<string, any> = {};")
    for cid, body, params, ret, sets in cases:
        out.append(f"results[{json.dumps(cid)}] = [];")
        for args in sets:
            call = f"f_{cid}({', '.join(_js(a) for a in args)})"
            out.append(f"try {{ results[{json.dumps(cid)}].push({{ok: {call}}}); }} "
                       f"catch (e) {{ results[{json.dumps(cid)}].push({{err: String(e)}}); }}")
    out.append("console.log(JSON.stringify(results));")
    return out


@pytest.fixture(scope="module")
def probe_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("externs3143")


def _expected(sets_by_case: dict, bodies_by_case: dict) -> dict:
    return {cid: [_py_fn(*bodies_by_case[cid])(*a) for a in sets]
            for cid, sets in sets_by_case.items()}


@pytest.mark.skipif(not NODE_OK, reason="node 23.6+ not installed")
def test_ts_bodies_match_the_py_bodies(probe_dir):
    cases, bodies, sets_by = [], {}, {}
    for f, name in PROBED:
        params, ret, b = ALL[f][name]
        cid = f"{f}_{name}"
        sets = _samples(name, params, "ts")
        body = textwrap.dedent(b["ts"].strip("\n"))
        cases.append((cid, body, params, ret, sets))
        bodies[cid] = (b["py"], params)
        sets_by[cid] = sets
    want = _expected(sets_by, bodies)
    script = probe_dir / "probe.ts"
    script.write_text("\n".join(_node_run(cases)), encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout)
    bad = [(cid, a, w, g) for cid, sets in sets_by.items()
           for a, w, g in zip(sets, want[cid], got[cid]) if g.get("ok") != w]
    assert not bad, bad[:5]


def _cargo_probe(crate: Path, entries: list[tuple]) -> dict:
    """Build one crate holding every `@rs` body as a fn and run the probe sets
    through it. entries: (id, params, return type, rs body, argument sets)."""
    wrappers, arms = [], []
    for cid, params, ret, body, _ in entries:
        sig = ", ".join(f"{n}: {RS_TYPES[t]}" for n, t in params)
        wrappers.append(
            "#[allow(unused_variables, unused_mut, unused_parens, unused_assignments)]\n"
            f"fn f_{cid}({sig}) -> {RS_TYPES[ret]} {{\n{textwrap.dedent(body.strip(chr(10)))}\n}}")
        conv = []
        for i, (_, t) in enumerate(params):
            conv.append({"Any": f"Box::new(args[{i}].clone()) as Box<dyn Any>",
                         "Str": f"args[{i}].as_str().unwrap().to_string()",
                         "Bool": f"args[{i}].as_bool().unwrap()"}[t])
        call = f"f_{cid}({', '.join(conv)})"
        res = (f"serde_json::Value::String({call})" if ret == "Str" else
               f"serde_json::Value::Array({call}.into_iter().map(serde_json::Value::String).collect())")
        arms.append(f'"{cid}" => {res},')
    main = """use std::any::Any;
use std::io::Read;
%s
fn main() {
    let mut inp = String::new();
    std::io::stdin().read_to_string(&mut inp).unwrap();
    let cases: Vec<serde_json::Value> = serde_json::from_str(&inp).unwrap();
    let mut out = Vec::new();
    for c in cases {
        let name = c["fn"].as_str().unwrap().to_string();
        let args: Vec<serde_json::Value> = c["args"].as_array().unwrap().clone();
        let r = std::panic::catch_unwind(|| match name.as_str() {
            %s
            _ => panic!("unknown"),
        });
        out.push(match r { Ok(v) => serde_json::json!({"ok": v}), Err(_) => serde_json::json!({"err": "panic"}) });
    }
    println!("{}", serde_json::to_string(&out).unwrap());
}
""" % ("\n".join(wrappers), "\n            ".join(arms))
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n'
        '[dependencies]\nserde_json = { version = "1", features = ["preserve_order"] }\n',
        encoding="utf-8")
    (crate / "src" / "main.rs").write_text(main, encoding="utf-8")
    build = subprocess.run(["cargo", "build", "--quiet"], cwd=crate, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr[-4000:]
    inputs = [{"fn": cid, "args": a} for cid, _, _, _, sets in entries for a in sets]
    run = subprocess.run([str(crate / "target" / "debug" / "probe")], input=json.dumps(inputs),
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-2000:]
    flat, k, got = json.loads(run.stdout), 0, {}
    for cid, _, _, _, sets in entries:
        got[cid] = flat[k:k + len(sets)]
        k += len(sets)
    return got


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_rs_bodies_match_the_py_bodies(probe_dir):
    entries, bodies, sets_by = [], {}, {}
    for f, name in PROBED:
        params, ret, b = ALL[f][name]
        cid = f"{f}_{name}"
        sets = _samples(name, params, "rs")
        entries.append((cid, params, ret, b["rs"], sets))
        bodies[cid] = (b["py"], params)
        sets_by[cid] = sets
    want = _expected(sets_by, bodies)
    got = _cargo_probe(probe_dir / "rs", entries)
    bad = [(cid, a, want[cid][n], got[cid][n]) for cid, _, _, _, sets in entries
           for n, a in enumerate(sets) if got[cid][n].get("ok") != want[cid][n]]
    assert not bad, bad[:5]


def test_go_float_literals_are_num_str_wrapped():
    """`float_lit` / `widen_float_lit` used to be `"revlF(%r)" % v`; they are now
    `"revlF(" + num_str(v) + ")"`, which is the same text because `repr` and
    `str` agree for an int and a float."""
    assert all(repr(v) == str(v) for v in NUMS)
    go = (SELFHOST / "emit_go.rvl").read_text(encoding="utf-8")
    for name in ("float_lit", "widen_float_lit"):
        assert f'fn {name}(v: Any) -> Str {{ return "revlF(".concat(num_str(v)).concat(")") }}' in go
        assert f"extern pure fn {name}" not in go


# ----------------------------------------------------- the filesystem externs
def _fs_tree(root: Path) -> dict[str, list[list[str]]]:
    def w(rel: str, data: bytes | str):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    w("doc/a.rvl", "a\r\nb\rc\n")
    w("doc/sub/m.rvl", "m")
    w("imp/lib/x.rvl", "x")
    w("jail/stdlib/fs.rvl", "fs")
    w("jail/stdlib/sub/doc.rvl", "doc")
    w("jail/stdlib/data.bin", bytes(range(256)) * 5)
    w("jail/stdlib/sub/own.txt", "own")
    w("outside.txt", "out")
    w("plain/doc.rvl", "d")
    w("plain/sub/f.txt", "f")
    os.symlink(root / "outside.txt", root / "jail/stdlib/sub/esc.txt")
    A = lambda p: str(root / p)  # noqa: E731
    doc = A("jail/stdlib/sub/doc.rvl")
    return {
        "module_path": [
            [A("doc/a.rvl"), "sub/m.rvl"], [A("doc/a.rvl"), "lib/x.rvl"],
            [A("doc/a.rvl"), "nope.rvl"], ["", "x"], [A("doc/a.rvl"), ""],
            [A("doc/a.rvl"), "../doc/sub/m.rvl"], ["doc/a.rvl", "sub/m.rvl"],
            [A("doc/a.rvl"), A("imp/lib/x.rvl")], [A("doc/a.rvl"), "./sub/../sub/m.rvl"],
            [A("doc/a.rvl"), "sub"]],
        "read_module_text": [[A("doc/a.rvl")], [""], [A("doc/sub/m.rvl")]],
        "ref_probe": [
            [doc, "own.txt"], [doc, "../fs.rvl"], [doc, "../data.bin"],
            [doc, "../../../outside.txt"], [doc, "esc.txt"], [doc, "missing.txt"],
            [A("plain/doc.rvl"), "sub/f.txt"], [A("plain/doc.rvl"), "../outside.txt"],
            ["", "x"], [A("plain/doc.rvl"), ""], ["plain/doc.rvl", "sub/f.txt"],
            [A("plain/doc.rvl"), A("plain/sub/f.txt")], [A("plain/doc.rvl"), "sub"],
            [A("jail/stdlib/sub/nodoc.rvl"), "own.txt"]],
    }


@pytest.fixture
def fs_probe(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "fs"
    root.mkdir()
    sets = _fs_tree(root)
    monkeypatch.chdir(root)
    # a search-path entry that exists, one that does not, and blanks around both
    monkeypatch.setenv("REVL_IMPORT_PATH",
                       f" {root / 'imp'} {os.pathsep}{os.pathsep}{root / 'nonexistent'}")
    want = {n: [_py_fn(ALL["compile"][n][2]["py"], ALL["compile"][n][0])(*a) for a in s]
            for n, s in sets.items()}
    return root, sets, want


@pytest.mark.skipif(not NODE_OK, reason="node 23.6+ not installed")
def test_ts_filesystem_externs_match_the_py_bodies(fs_probe):
    root, sets, want = fs_probe
    cases = [(n, textwrap.dedent(ALL["compile"][n][2]["ts"].strip("\n")),
              ALL["compile"][n][0], ALL["compile"][n][1], s) for n, s in sets.items()]
    script = root / "probe.ts"
    script.write_text("\n".join(_node_run(cases)), encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout)
    bad = [(n, a, w, g) for n, s in sets.items()
           for a, w, g in zip(s, want[n], got[n]) if g.get("ok") != w]
    assert not bad, bad[:5]


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_rs_filesystem_externs_match_the_py_bodies(fs_probe):
    root, sets, want = fs_probe
    entries = [(n, ALL["compile"][n][0], ALL["compile"][n][1], ALL["compile"][n][2]["rs"], s)
               for n, s in sets.items()]
    got = _cargo_probe(root / "rs", entries)
    bad = [(n, a, want[n][i], got[n][i]) for n, s in sets.items()
           for i, a in enumerate(s) if got[n][i].get("ok") != want[n][i]]
    assert not bad, bad[:5]


# ------------------------------------------------------------ the emission
@pytest.mark.slow
@pytest.mark.parametrize("backend", ["ts", "rust"])
@pytest.mark.parametrize("name", FILES)
def test_a_selfhost_file_no_longer_stops_at_a_missing_extern_body(name, backend):
    """Each file used to be refused at its first extern with no body for the
    tier. The typescript tier now emits all eight. The rust tier still refuses a
    few of them on a functional record update or an uninferable record literal,
    which are other gaps (#2899); only a missing extern body is this issue's.
    The refusal is checked by message, so a rust file stopped at another gap
    first proves nothing about its externs: the declaration test above is the
    guard for that, and this one covers the typescript tier end to end."""
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "emit", "--backend", backend,
         str(SELFHOST / f"{name}.rvl")],
        capture_output=True, text=True, cwd=ROOT)
    assert "has no @" not in proc.stderr, proc.stderr[-600:]
    if backend == "ts":
        assert proc.returncode == 0, proc.stderr[-600:]
