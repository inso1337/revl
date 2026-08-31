"""TypeScript tier: the item-233 char-classification builtins (roadmap item
364) — `is_digit`/`is_alpha`/`is_alnum`/`is_space`.

Item 233 landed these on the py tier (backends/python/emit.py) and item 267
ported them to rust (backends/rust/emit.py); the ts emitter had NO case, so a
program using them typechecked and passed `revl test --backend py` but died at
`revl test --backend ts` with `unknown builtin method 'is_space'`. This file
guards the ts lowering added for 364.

The ts lowering mirrors the py/rust native forms EXACTLY (ASCII, single code
point; the receiver is a one-char Str — an empty receiver is false and no input
faults; multi-character input is outside the per-character contract):
  * is_digit()  — `0`-`9`
  * is_alpha()  — `a`-`z` / `A`-`Z` (letters only, NOT `_`)
  * is_alnum()  — is_alpha ∪ is_digit
  * is_space()  — space, tab, LF, CR
JS `<=`/`<` on strings is UTF-16 code-unit lexicographic order, which is
code-point order for ASCII, so it matches python's chained string comparison
byte for byte; the receiver is bound once by an arrow IIFE (`_rc`).

Checked here:
  * the shape: each lowers to its documented inline IIFE, no revl-fn call and
    no `EmitError('unknown builtin method ...')` fall-through;
  * cross-tier agreement: for every byte 0-255 (and the empty/multi-char edge
    inputs), the ts verdict under node equals the py verdict from the py
    emitter — the same string classifies identically on py and ts.

The node leg is gated on node + a resolvable cordis-ts (the emitted module
imports `host` from ../runtime.ts); a machine without them skips with a reason,
never a spurious red — the same honesty rule test_ts_witnessed_parity.py uses.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PY = _ROOT / "backends" / "python"
_BACKEND_TS = _ROOT / "backends" / "typescript"
for _p in (str(_ROOT / "src"), str(_BACKEND_PY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from revl.compiler import compile_source  # noqa: E402

METHODS = ("is_digit", "is_alpha", "is_alnum", "is_space")

# One exported fn per builtin, receiver is the fn's Str param.
FN_SRC = "".join(
    f"pub fn {m}(c: Str) -> Bool {{ return c.{m}() }}\n" for m in METHODS)


def _load_ts_emit():
    """The ts emitter, loaded by path under a unique name so it never shadows
    the py backend's own `emit` module (both backends ship a module named
    `emit`; importing the bare name would poison sys.modules)."""
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_364", _BACKEND_TS / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- node gate

def _node_reason() -> str | None:
    import shutil
    if shutil.which("node") is None:
        return "node is required to run the emitted ts classifier"
    # the emitted module imports `host` from ../runtime.ts, which pulls cordis
    if not (_BACKEND_TS / "node_modules" / "cordis").exists():
        return "cordis-ts (backends/typescript/node_modules) is required"
    return None


_NEEDS_NODE = pytest.mark.skipif(
    _node_reason() is not None, reason=(_node_reason() or ""))


# ------------------------------------------------------------ shape (no node)

def test_ts_char_class_lowers_to_inline_iife_no_fn_call():
    """Each builtin lowers to its documented inline IIFE — no revl-fn call, no
    `unknown builtin method` fall-through (the item-364 bug)."""
    src = _load_ts_emit().emit(compile_source(FN_SRC))
    assert '((_rc: string) => "0" <= _rc && _rc <= "9")(c)' in src
    assert ('((_rc: string) => ("a" <= _rc && _rc <= "z") '
            '|| ("A" <= _rc && _rc <= "Z"))(c)') in src
    assert ('((_rc: string) => ("0" <= _rc && _rc <= "9") '
            '|| ("a" <= _rc && _rc <= "z") '
            '|| ("A" <= _rc && _rc <= "Z"))(c)') in src
    # is_space keeps the tab/LF/CR escapes a revl string literal cannot spell.
    assert ('((_rc: string) => _rc === " " || _rc === "\\t" '
            '|| _rc === "\\n" || _rc === "\\r")(c)') in src


@pytest.mark.parametrize("m", METHODS)
def test_ts_emitter_does_not_refuse_the_builtin(m):
    """The regression itself: emitting a fn that calls the builtin must NOT
    raise the emitter's `unknown builtin method` refusal."""
    _load_ts_emit().emit(
        compile_source(f"pub fn f(c: Str) -> Bool {{ return c.{m}() }}\n"))


# ------------------------------------------------------------- py oracle

def _exec_python(src: str):
    """Execute the py-emitted module; return its namespace (the py tier is the
    cross-tier oracle — item 233's original semantics)."""
    spec = importlib.util.spec_from_file_location(
        "pyemit_char_class_364", _BACKEND_PY / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(compile_source(src)),
                     "char_class.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


# ------------------------------------------------------------- ts under node

# every byte 0-255, plus the edge inputs the per-char contract does not cover
_INPUTS = [chr(n) for n in range(256)] + ["", "ab", "0a", "  ", "aA", "12"]


def _run_ts(inputs: list[str]) -> dict[str, list[bool]]:
    """Emit the four builtins to ts and run them under node over `inputs`,
    returning {method: [bool, ...]} in input order."""
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "char_class_parity.ts"
    module.write_text(
        _load_ts_emit().emit(compile_source(FN_SRC),
                             runtime_import="../../runtime.ts"),
        encoding="utf-8")

    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=generated) as fh:
        json.dump(inputs, fh)
        inputs_path = fh.name

    harness = generated / "_char_class_harness.ts"
    harness.write_text(
        "import { readFileSync } from 'node:fs'\n"
        "import { is_digit, is_alpha, is_alnum, is_space } "
        "from './char_class_parity.ts'\n"
        "const fns: Record<string, (c: string) => boolean> = "
        "{ is_digit, is_alpha, is_alnum, is_space }\n"
        "const inputs: string[] = JSON.parse(readFileSync(process.argv[2], 'utf-8'))\n"
        "const out: Record<string, boolean[]> = {}\n"
        "for (const m of Object.keys(fns)) out[m] = inputs.map(s => fns[m](s))\n"
        "process.stdout.write(JSON.stringify(out))\n",
        encoding="utf-8")

    try:
        proc = subprocess.run(
            ["node", str(harness), inputs_path],
            capture_output=True, text=True, cwd=str(_BACKEND_TS))
    finally:
        Path(inputs_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"ts char-class harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@_NEEDS_NODE
def test_ts_matches_py_over_every_byte_and_edge_input():
    """The core cross-tier claim: for every byte 0-255 and every edge input,
    the ts verdict under node equals the py verdict — the same string
    classifies identically on py and ts."""
    py_ns = _exec_python(FN_SRC)
    ts = _run_ts(_INPUTS)
    mismatches = []
    for m in METHODS:
        for s, ts_val in zip(_INPUTS, ts[m]):
            py_val = py_ns[m](s)
            if bool(py_val) != bool(ts_val):
                mismatches.append((m, s, py_val, ts_val))
    assert not mismatches, "py<->ts char-class divergence:\n" + "\n".join(
        f"  {m}({s!r}): py={p} ts={t}" for m, s, p, t in mismatches)


@_NEEDS_NODE
def test_ts_is_total_empty_false_multichar_never_faults():
    """The lean lowering stays TOTAL on the ts tier: the empty receiver is
    false (not a fault), and multi-char input yields a plain bool."""
    ts = _run_ts(["", "ab", "0a", "  ", "aA", "12"])
    for m in METHODS:
        assert ts[m][0] is False, (m, "empty must be false")
        for v in ts[m]:
            assert v in (True, False), (m, v)
