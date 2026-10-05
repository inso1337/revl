"""A service method named after a reserved word crosses on the wasm tier
(issue #1512, the wasm row; rust is #1642, ts #1552, go and java #1558).

MEASURED, NO SPLIT. The tier was run end to end with every candidate as a
service method name: the WAT text-format keywords and instruction mnemonics
that are revl identifiers, the Python and JavaScript reserved words (the
cordis-wasm host is Python, and wasm modules are also hosted from JS), and
the names the emitter and the runtime use for their own exports and helpers
(`activate_step`, `deactivate`, `alloc`, `memory`, `req`, `route`, ...). The
frontend admits 124 of the 148; the other 24 are revl keywords or reserved
spellings and are refused at compile time by name. Every one of the 124
crosses: a provider exports it, a consumer imports it through a required
service, and a call reaches the provider, alone and all 124 in one
composition. A `lifecycle test` that calls each directly and through the
consumer passes under `revl test --backend wasm`, and fails when one expected
value is wrong.

WHY. A method name never becomes a WAT token. It is the field of the import
`(import "coeffect:<key>" "<op>" ...)` and of the export `"provide:<key>.<op>"`,
which are WAT strings, and the internal identifier `$req.<key>.<op>` (issue #1756) sits in
the `$` sigil namespace, which no keyword can reach. So these tests pin the
property rather than fix a defect: they pass on main by design, and they turn
red if a later change starts spelling a method name as a bare token.

The emit assertions run everywhere. The executed ones need the `wasmtime`
Python package and a cordis-wasm checkout (CORDIS_WASM, default
~/Projects/cordis-wasm), and skip with a reason without them.
"""

from __future__ import annotations

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
# tests/ is APPENDED, so its modules resolve only names nothing earlier on
# sys.path provides; `_load_by_path` is the one wanted here (see
# backends/java/test_reserved_word_idents_java.py).
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from _load_by_path import load_by_path  # noqa: E402
from revl.errors import RevlError  # noqa: E402

emit = load_by_path("revl_wasm_emit_kw", BACKEND / "emit.py")

#: WAT text-format keywords and plain instruction mnemonics (the dotted ones,
#: `i32.add` and so on, are not revl identifiers).
WAT = ["module", "func", "param", "result", "local", "global", "table", "memory",
       "type", "import", "export", "start", "elem", "data", "offset", "item",
       "declare", "mut", "funcref", "externref", "anyfunc", "i32", "i64", "f32",
       "f64", "v128", "block", "loop", "if", "then", "else", "end", "br", "br_if",
       "br_table", "return", "call", "call_indirect", "return_call", "drop",
       "select", "unreachable", "nop", "try", "catch", "catch_all", "throw",
       "rethrow", "delegate", "tag", "ref", "rec", "sub", "field", "struct",
       "array", "binary", "quote", "register", "invoke", "get", "assert_return",
       "assert_trap", "shared", "unshared", "align", "memory_size", "memory_grow"]
#: the cordis-wasm host's language
PY = ["False", "None", "True", "and", "as", "assert", "async", "await", "break",
      "class", "continue", "def", "del", "elif", "else", "except", "finally",
      "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
      "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
      "match", "case", "type", "_"]
#: a JS embedder's reserved words and the Object.prototype names
JS = ["arguments", "await", "break", "case", "catch", "class", "const",
      "continue", "debugger", "default", "delete", "do", "else", "enum", "eval",
      "export", "extends", "false", "finally", "for", "function", "if",
      "implements", "import", "in", "instanceof", "interface", "let", "new",
      "null", "package", "private", "protected", "public", "return", "static",
      "super", "switch", "this", "throw", "true", "try", "typeof", "var",
      "void", "while", "with", "yield", "constructor", "prototype", "__proto__"]
#: the emitter's and the runtime's own export and helper names
INTERNAL = ["activate_step", "deactivate_step", "deactivate", "alloc", "alloc_str",
            "memory", "str_concat", "int_to_str", "host", "dispose", "spawn",
            "config", "route", "provide", "coeffect", "req", "__heap_grow"]

CANDIDATES = sorted(set(WAT + PY + JS + INTERNAL))


def _accepted(name: str) -> bool:
    try:
        compile_source(f"service S {{ fn {name}(k: Int) -> Int }}\n")
        return True
    except RevlError:
        return False


ADMITTED = [n for n in CANDIDATES if _accepted(n)]


def _program(names: list[str]) -> str:
    """`P` provides every name; `C` calls each through its required `s`."""
    svc = "\n".join(f"  fn {n}(k: Int) -> Int" for n in names)
    impl = "\n".join(f"    fn {n}(k) = k + {i + 1}" for i, n in enumerate(names))
    ops = "\n".join(f"  fn go{i}(k: Int) -> Int" for i in range(len(names)))
    calls = "\n".join(f"    fn go{i}(k) = s.{n}(k)" for i, n in enumerate(names))
    return (f"service S {{\n{svc}\n}}\n"
            f"component P provides s: S {{\n  provide s {{\n{impl}\n  }}\n}}\n"
            f"service Ops {{\n{ops}\n}}\n"
            f"component C requires s: S provides ops: Ops {{\n  provide ops {{\n{calls}\n  }}\n}}\n")


def _lifecycle(names: list[str]) -> str:
    lines = ['lifecycle test "every reserved-word method crosses on wasm" {',
             "  load P", "  load C"]
    for i, n in enumerate(names):
        lines += [f"  let d{i} = call s.{n}(10)", f"  assert d{i} == {11 + i}",
                  f"  let t{i} = call ops.go{i}(10)", f"  assert t{i} == {11 + i}"]
    lines += ["  unload C", "  unload P", "  assert no_residue", "}"]
    return _program(names) + "\n".join(lines) + "\n"


def test_the_frontend_hands_the_emitter_these_names():
    """Pinned so a frontend change that admits or refuses another name is seen
    here: the emitter must carry every name it is handed."""
    assert len(CANDIDATES) == 148 and len(ADMITTED) == 123
    refused = set(CANDIDATES) - set(ADMITTED)
    assert {"if", "return", "match", "type", "provide", "config", "spawn"} <= refused
    assert {"func", "module", "memory", "call", "end", "class", "def",
            "deactivate", "alloc", "req", "_"} <= set(ADMITTED)


def _tokens(wat: str) -> str:
    """The WAT with every string literal and every `$` identifier blanked: what
    is left is the module's structure, the only place a keyword could land."""
    no_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', wat)
    return re.sub(r"\$[0-9A-Za-z!#%&'*+\-./:<=>?@\\^_`|~]+", "$", no_strings)


def test_a_method_name_is_only_ever_a_string_or_a_sigil_identifier():
    """Each name is emitted as the import field and the export string, and
    with those and the `$` identifiers blanked the module is token for token
    the one a neutral name produces: the name reaches nothing else."""
    neutral = emit.emit(compile_source(_program(["zq"])))
    for name in ADMITTED:
        modules = emit.emit(compile_source(_program([name])))
        assert f'(export "provide:s.{name}")' in modules["P"], name
        assert f'(import "coeffect:s" "{name}" (func $req.s.{name} ' in modules["C"], name
        for component in ("P", "C"):
            assert _tokens(modules[component]) == _tokens(neutral[component]), (name, component)


def test_the_emitted_modules_assemble():
    wasmtime = pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    modules = emit.emit(compile_source(_program(ADMITTED)))
    for component in ("P", "C"):
        assert wasmtime.wat2wasm(modules[component])[:4] == b"\0asm"


# ---------------------------------------------------------------------------
# executed on the live cordis-wasm runtime

def _cordis_runtime():
    pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    module = load_by_path("cordis_wasm_runtime_kw", path)
    return module


def _wire(names: list[str]):
    runtime = _cordis_runtime()
    ir = compile_source(_program(names))
    modules = emit.emit(ir)
    rt = runtime.Runtime()
    fibers = {c: rt.plug(c, modules[c]) for c in ir["manifest"]["loadOrder"]}
    return rt, fibers


@pytest.mark.parametrize("name", ADMITTED)
def test_each_name_crosses_alone(name):
    rt, fibers = _wire([name])
    assert rt.call(fibers["P"], f"provide:s.{name}", 10) == 11
    assert rt.call(fibers["C"], "provide:ops.go0", 10) == 11


def test_every_name_crosses_in_one_composition():
    rt, fibers = _wire(ADMITTED)
    got = [rt.call(fibers["C"], f"provide:ops.go{i}", 10) for i in range(len(ADMITTED))]
    assert got == [11 + i for i in range(len(ADMITTED))]


def _revl_test(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    _cordis_runtime()
    revl = shutil.which("revl", path=str(Path(sys.executable).parent))
    if revl is None:
        pytest.skip("no `revl` console script beside this interpreter")
    target = tmp_path / "kw_lifecycle.rvl"
    target.write_text(source, encoding="utf-8")
    return subprocess.run([revl, "test", "--backend", "wasm", str(target)], cwd=ROOT,
                          capture_output=True, text=True, timeout=900)


def test_a_lifecycle_call_reaches_every_name(tmp_path):
    done = _revl_test(tmp_path, _lifecycle(ADMITTED))
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "1 lifecycle test(s) ran" in done.stdout, done.stdout[-2000:]


def test_the_lifecycle_run_is_not_vacuous(tmp_path):
    """The same program with one wrong expected value fails, so the pass above
    is a measurement and not an absence."""
    wrong = _lifecycle(ADMITTED).replace("assert t5 == 16", "assert t5 == 999")
    assert "999" in wrong
    done = _revl_test(tmp_path, wrong)
    assert done.returncode != 0
    assert "assertion failed" in done.stdout + done.stderr


# ---------------------------------------------------------------------------
# the one place a name did reach past a string: the dead-helper sweep

_SWEEPABLE = (
    '(module\n'
    '  (func $used (result i64) (i64.const 1))\n'
    '  (func $dead (result i64) (i64.const 2))\n'
    '  (func (export "provide:s.call_indirect") (result i64) (call $used))\n'
    ')\n'
)


def test_a_name_spelled_like_an_instruction_does_not_switch_the_sweep_off():
    """`_UNSWEEPABLE` was matched against the raw text, so the export string
    `"provide:s.call_indirect"` declined the sweep and the module kept every
    helper. Only a real instruction token declines it now."""
    pruned = emit.prune_unreachable_funcs(_SWEEPABLE)
    assert "$dead" not in pruned and "$used" in pruned
    named = _SWEEPABLE.replace("$used", "$call_indirect")
    assert "$dead" not in emit.prune_unreachable_funcs(named)
    commented = _SWEEPABLE.replace("(module\n", '(module\n  ;; no "call_indirect here\n')
    assert "$dead" not in emit.prune_unreachable_funcs(commented)


def test_a_real_indirect_call_still_declines_the_sweep():
    real = _SWEEPABLE.replace("(call $used)", "(call_indirect (type 0) (i32.const 0))")
    assert emit.prune_unreachable_funcs(real) == real


def test_a_fn_named_call_indirect_is_swept_like_any_other():
    """The same check, reached by a pure module: `$call_indirect` is a name."""
    src = ("pub fn call_indirect(k: Int) -> Int { return k + 1 }\n"
           "pub fn other(k: Int) -> Int { return k }\n")
    plain = emit.emit(compile_source(src.replace("call_indirect", "zq")))["functions"]
    named = emit.emit(compile_source(src))["functions"]
    assert _tokens(named) == _tokens(plain)
