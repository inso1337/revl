"""A service method named after a Rust keyword has one spelling at every site.

Part of issue #1512 (the rust tier; the ts tier is PR #1552). Of Rust's strict,
reserved and weak keywords, the revl frontend accepts 36 as a method name (the
other 20 are revl keywords too). On main the rust emitter spelled such a method
three ways:

* the trait declaration and a call through a required service moved it onto
  the append-`_` ladder (`box` -> `box_`);
* a provider's impl in the bridge-carrying path named it `box_` but looked its
  declared types up under `box_`, missed, and typed it `(k: Value) -> ()`;
* the bridge proxy (`fn box(&self, ..)`) and the bridge dispatch
  (`svc.box(..)`) kept the raw keyword, which rustc refuses.

So every one of the 32 strict/reserved keywords the frontend accepts broke
`cargo build` of any crate that declared it. `_method_ident` is now the one
spelling, used at the trait, every impl, the proxy, the dispatch, a call
through a required service and a lifecycle `call`. The wire keeps the contract
name. Toolchain-free emit assertions run everywhere. The end-to-end placements
(rust only, py -> rust, rust -> py) need cargo and cordis-py and skip without
them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_rust_emit_kw", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

#: The Rust Reference's keyword table: strict, reserved, weak.
STRICT = ["as", "break", "const", "continue", "crate", "else", "enum", "extern",
          "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
          "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
          "super", "trait", "true", "type", "unsafe", "use", "where", "while",
          "async", "await", "dyn"]
RESERVED = ["abstract", "become", "box", "do", "final", "macro", "override", "priv",
            "typeof", "unsized", "virtual", "yield", "try", "gen"]
WEAK = ["macro_rules", "union", "safe", "raw"]
#: names one rung up the append-`_` ladder, which must stay distinct
LADDER = ["box_", "match_", "type_"]


def _accepted(name: str) -> bool:
    try:
        compile_source(f"service S {{ fn {name}(k: Str) -> Str }}\n")
        return True
    except RevlError:
        return False


ACCEPTED = [n for n in STRICT + RESERVED + WEAK + LADDER if _accepted(n)]
KEYWORDS = [n for n in ACCEPTED if n in STRICT or n in RESERVED]


def _program(names: list[str]) -> str:
    svc = "\n".join(f"  fn {n}(k: Str) -> Str" for n in names)
    impl = "\n".join(f'    fn {n}(k) {{ return "{n}:" + k }}' for n in names)
    ops = "\n".join(f"  fn go{i}(k: Str) -> Str" for i in range(len(names)))
    calls = "\n".join(f"    fn go{i}(k) {{ return s.{n}(k) }}" for i, n in enumerate(names))
    return (f"service S {{\n{svc}\n}}\n"
            f"component P provides s: S {{\n  provide s {{\n{impl}\n  }}\n}}\n"
            f"service Ops {{\n{ops}\n}}\n"
            f"component C requires s: S provides ops: Ops {{\n  provide ops {{\n{calls}\n  }}\n}}\n")


def test_the_frontend_hands_the_emitter_these_names():
    """Pinned so a frontend change that admits or refuses another keyword is
    seen here: the emitter must spell every name it is handed."""
    assert len(KEYWORDS) == 32 and len(ACCEPTED) == 39
    assert {"box", "impl", "Self", "crate", "self", "super", "gen"} <= set(KEYWORDS)


def test_a_keyword_method_has_one_spelling_at_every_site():
    src = emit.emit(compile_source(_program(ACCEPTED)))
    for name in ACCEPTED:
        spelled = emit._method_ident(name)
        if name in KEYWORDS:
            assert spelled != name
            # no site spells the raw keyword as a method
            raw = re.findall(r"(?<![\w#])(?:fn |svc\.|\.)" + re.escape(name) + r"\(", src)
            assert raw == [], (name, raw)
        sites = {
            "declaration": f"fn {spelled}(&self, k: String) -> String",
            "dispatch": f"svc.{spelled}(",
            "call": f".{spelled}(k",
        }
        for site, needle in sites.items():
            assert needle in src, (name, site, needle)
        # the wire keeps the contract name
        assert f'"{name}" => ' in src
        assert f'"{name}", vec![' in src


def test_the_ladder_stays_injective():
    spelled = [emit._method_ident(n) for n in ACCEPTED]
    assert len(set(spelled)) == len(spelled)
    assert emit._method_ident("box") == "box_"
    assert emit._method_ident("box_") == "box__"
    assert emit._method_ident("drop") == "drop_"
    assert emit._method_ident("value") == "value"


def test_a_plain_method_name_is_byte_identical():
    src = emit.emit(compile_source(_program(["value", "lookup"])))
    assert "fn value(&self, k: String) -> String" in src
    assert "svc.value(" in src and "value_" not in src


# ---------------------------------------------------------------------------
# end to end: real placements

def _ready() -> str | None:
    if shutil.which("cargo") is None:
        return "no cargo on PATH"
    if importlib.util.find_spec("cordis") is None:
        return "the cordis-py runtime is not installed (sh backends/python/setup.sh)"
    return None


def _placement(tmp_path: Path, procs: list) -> dict:
    (tmp_path / "kw.rvl").write_text(_program(ACCEPTED), encoding="utf-8")
    probes = ", ".join(f"'ops.go{i}(\"x\")'" for i in range(len(ACCEPTED)))
    blocks = [f'[processes.{p}]\nbackend = "{b}"\ncomponents = {json.dumps(c)}\n'
              for p, b, c in procs]
    blocks[-1] += f"probe = [{probes}]\n"
    (tmp_path / "kw.toml").write_text("\n".join(blocks), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", "kw.rvl", "--placement", "kw.toml", "--once"],
        capture_output=True, text=True, timeout=3000, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    text = proc.stdout + proc.stderr
    got = dict(re.findall(
        r"probe\s*\|\s*ops\.go(\d+)\(.*?\)\s*\|?\s*(?:=>|->)\s*['\"]?([^'\"\n]*)", text))
    assert got, text[-3000:]
    return {ACCEPTED[int(i)]: v for i, v in got.items()}


@pytest.mark.parametrize("procs", [
    [("both", "rust", ["P", "C"])],
    [("prov", "py", ["P"]), ("cons", "rust", ["C"])],
    [("prov", "rust", ["P"]), ("cons", "py", ["C"])],
], ids=["rust", "py-to-rust", "rust-to-py"])
def test_every_keyword_method_is_called_end_to_end(tmp_path, procs):
    reason = _ready()
    if reason:
        pytest.skip(reason)
    assert _placement(tmp_path, procs) == {n: f"{n}:x" for n in ACCEPTED}
