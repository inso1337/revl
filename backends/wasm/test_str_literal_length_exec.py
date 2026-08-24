"""`Str.length` on a multibyte STRING LITERAL, EXECUTED on cordis-wasm.

Regression guard for roadmap item 104. `Str` is a code-point sequence on every
tier (item 51, docs/strings.md): `"café".length` is 4 (four code points), not 5
(its UTF-8 byte count). The wasm tier stores a `Str` as a canonical-ABI cell (a
u32 *byte-length* prefix, then the UTF-8 bytes), so a literal `.length` MUST
route through `$str_cp_length` — the code-point counter that walks continuation
bytes — and never fold to the `(i32.load <ptr>)` byte-length read.

Item 104's real defect was the PROPERTY form `s.length` (no parens): it lowered
as a distinct `len` IR node through `_len_expr`, which read the byte-length
prefix directly (`(i64.extend_i32_u (i32.load <ptr>))`) and so answered 5 for
`"café"`. The METHOD form `s.length()` was always correct (it routes through
`$str_cp_length`) — testing only the method form is what masked the bug. Both
forms are pinned here: the property row would flip (café -> 5, 日本語 -> 9,
naïve -> 6) BY EXECUTION on the real runtime if a future fold reintroduced the
byte-count read.

The proof is execution. The runtime is the first-party cordis-wasm prototype;
point CORDIS_WASM at a checkout (default: ~/Projects/cordis-wasm). Without it (or
without the wasmtime Python package) these skip with a reason — never reported as
passing. A CLI-driven companion (`test_str_literal_length_cli_exec.py`) executes
the same property-form cases through the `wasmtime`/`wasm-tools` binaries so the
guard still runs where only the CLI toolchain is present.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cordis_runtime():
    """Load the cordis-wasm runtime by explicit path (avoids colliding with the
    backends/python `runtime` module) or skip with a reason."""
    pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    spec = importlib.util.spec_from_file_location("cordis_wasm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    # register before exec: the runtime's @dataclass fields resolve their types
    # through sys.modules[cls.__module__], which is None for an unregistered
    # synthetic module (a CPython importlib/dataclasses gotcha).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cordis-wasm runtime failed to import: {exc}")
    if not hasattr(module, "Runtime"):
        pytest.skip("cordis-wasm runtime has no Runtime")
    return module


def _run_length(literal: str, expr: str = ".length()") -> int:
    """Compile+emit `provide s { fn f() = "<literal>"<expr> }`, plug it onto a
    fresh cordis-wasm runtime, and return the executed result. `expr` selects the
    method form (`.length()`) or the property form (`.length`, the item-104
    bug)."""
    mod = _cordis_runtime()
    src = (
        "service S { fn f() -> Int }\n"
        "component C provides s: S {\n"
        f'  provide s {{ fn f() = "{literal}"{expr} }}\n'
        "}\n"
    )
    ir = compile_source(src)
    modules = _emitter().emit(ir)
    rt = mod.Runtime()
    fiber = None
    for entry in ir["manifest"]["loadOrder"]:
        fiber = rt.plug(entry, modules[entry])
    return rt.call(fiber, "provide:s.f")


# Cases where the UTF-8 byte count differs from the code-point count, so a
# byte-count regression is unmistakable. The ASCII case pins that byte==codepoint
# strings stay correct (a fix that broke ASCII would be equally wrong).
#   "café"  -> 4 code points, 5 bytes  (é = 2 bytes)
#   "日本語" -> 3 code points, 9 bytes  (each CJK char = 3 bytes)
#   "naïve" -> 5 code points, 6 bytes  (ï = 2 bytes)
#   "abc"   -> 3 code points, 3 bytes  (ASCII: byte == code point)
@pytest.mark.parametrize("expr,form", [(".length()", "method"), (".length", "property")])
@pytest.mark.parametrize(
    "literal,code_points,byte_len",
    [
        ("café", 4, 5),
        ("日本語", 3, 9),
        ("naïve", 5, 6),
        ("abc", 3, 3),
    ],
)
def test_multibyte_literal_length_counts_code_points(literal, code_points, byte_len, expr, form):
    """A multibyte string-literal `.length` (method AND property form) executed
    on wasmtime answers the code-point count, never the UTF-8 byte count. The
    property form is the item-104 defect; both forms are guarded here."""
    got = _run_length(literal, expr)
    assert got == code_points, (
        f'"{literal}"{expr} ({form} form) executed to {got}; expected '
        f"{code_points} code points (byte-count regression would give {byte_len})"
    )
    if byte_len != code_points:
        # make the specific failure mode loud: it must not be the byte count
        assert got != byte_len, (
            f'"{literal}"{expr} ({form} form) returned its UTF-8 byte count '
            f"{byte_len} — the literal `.length` path folded to the byte-length "
            "load instead of routing through $str_cp_length (item 104)"
        )
