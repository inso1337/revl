"""Every checked-in backend golden, folded into the default `pytest tests/`.

The per-backend suites verify their goldens separately (backends/*/test_emit_*.py,
the vitest emitter test, backends/wasm/test_v3_emit.py) and run as separate CI
jobs — which is exactly how a stale golden used to survive: the *default* entry
point (`pytest tests/`, what the pre-commit hook and this repo's protocol run)
never touched most of them. These tests fold every golden byte-equality run
into that default suite.

Snapshot policy (docs/conformance.md, "Golden policy"): the invariant is
"emitter output never changes *unreviewed*". A failing test here means the
emitter changed without the golden being regenerated and reviewed — the fix is
to regenerate (`python3 backends/typescript/scripts/regen-golden.py` and
siblings) and commit the reviewed diff, not to freeze the emitter.

Every emitter is pure Python, so none of this needs a toolchain.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

REFERENCE_IR = ROOT / "examples" / "user_cache.ir.json"


def _load_emitter(name: str, backend: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The reference-IR user_cache goldens — the same input the per-backend suites
# emit from (backends/python/tests/test_emitter.py, backends/rust and
# backends/java test_emit_*.py, the vitest emitter test).
USER_CACHE_GOLDENS = {
    "python": ("backends/python/golden/user_cache.py", {}),
    "typescript": ("backends/typescript/golden/user_cache.ts", {}),
    "rust": ("backends/rust/golden/user_cache.rs", {}),
    "java": ("backends/java/golden/user_cache.java", {}),
}


@pytest.mark.parametrize("tier", sorted(USER_CACHE_GOLDENS))
def test_user_cache_golden_from_reference_ir(tier):
    """Emit the reference IR with the tier's own emitter and compare to its
    golden — mirroring the per-backend suites' byte-equality run, so a stale
    golden (or a reference IR that no longer matches the frontend) cannot hide
    behind the default suite."""
    golden_rel, kwargs = USER_CACHE_GOLDENS[tier]
    emitter = _load_emitter(f"revl_golden_{tier}_emit", tier)
    ir = json.loads(REFERENCE_IR.read_text(encoding="utf-8"))
    src = emitter.emit(ir, **kwargs)
    golden = (ROOT / golden_rel).read_text(encoding="utf-8")
    assert src == golden


def test_wasm_functions_golden_from_v3_source():
    """The one wasm golden not checked from the default suite: `functions.wat`
    lives in backends/wasm/test_v3_emit.py, a separate CI job. Emit the same
    v3 source here so a stale functions.wat fails `pytest tests/` too."""
    emitter = _load_emitter("revl_golden_wasm_emit", "wasm")
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn add(a: Int, b: Int) -> Int { return a + b }
        fn negate(b: Bool) -> Bool { return !b }
        fn name(row: Row) -> Str { return row.name }
        fn first(xs: List[Int]) -> Int { return xs[0] }
        fn greet() -> Str { return "hi" }
        fn make_row(id: Int, name: Str) -> Row { return { id: id, name: name } }
        fn classify(n: Int) -> Str {
          if (n < 0) return "neg"
          return "pos"
        }
        """
    )
    wat = emitter.emit(ir)["functions"]
    golden = (ROOT / "backends" / "wasm" / "golden" / "functions.wat").read_text()
    assert wat == golden
