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
to regenerate and commit the reviewed diff, not to freeze the emitter. One
command regenerates any tier:

    python3 tools/regen_goldens.py --check          # which goldens drifted
    python3 tools/regen_goldens.py <tier>           # regenerate that tier

`tools/regen_goldens.py` is the registry of every checked-in golden and how it
is produced; each assertion below names the target that regenerates it.

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

# What a red here means, and the exact command that resolves it. A golden is a
# review prompt, never a wall — see the module docstring and the golden policy.
_STALE = ("{f} no longer matches what the emitter produces. If the emitter change is "
          "intended: `python3 tools/regen_goldens.py {t}`, review the diff, and commit "
          "it in the same change. Do NOT bend the emitter back to the old bytes "
          "(docs/conformance.md, \"Golden policy: snapshot, not freeze\").")


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
    assert src == golden, _STALE.format(f=golden_rel, t=tier)


def test_wasm_functions_golden_from_v3_source():
    """The one wasm golden not checked from the default suite: `functions.wat`
    lives in backends/wasm/test_v3_emit.py, a separate CI job. Emit the same
    v3 source here so a stale functions.wat fails `pytest tests/` too. The
    source is committed beside the golden (`functions.revl`) rather than inlined
    in two tests, so the emit recipe has one owner."""
    emitter = _load_emitter("revl_golden_wasm_emit", "wasm")
    source = ROOT / "backends" / "wasm" / "golden" / "functions.revl"
    ir = compile_source(source.read_text(encoding="utf-8"))
    wat = emitter.emit(ir)["functions"]
    golden = (ROOT / "backends" / "wasm" / "golden" / "functions.wat").read_text()
    assert wat == golden, _STALE.format(f="backends/wasm/golden/functions.wat", t="wasm")


def test_the_frontend_still_mints_ir_version_1():
    """Roadmap 73(d) / 419d: the v1 emitter bodies (`_emit_v1` and kin) are NOT
    propped up by fixtures: the shipped frontend still MINTS `ir_version: 1`
    for a program that uses no v2 or v3 feature, so they are the live path for
    that whole class of programs.

    That fact is the whole premise of the decision recorded in
    docs/conformance.md ("v1 IR input support: frozen, not retired"): retiring
    the dialect has a precondition, and the precondition is that the frontend
    stops minting it. Pin the premise. If version selection is ever changed to
    always emit v3, this reds and the freeze is reopened deliberately instead of
    the v1 bodies quietly becoming dead code.

    The reference IR the goldens above are emitted from is itself a v1 document,
    which is why this belongs here rather than in a lowering test."""
    ir = compile_source(
        """
        service Cache { fn get(key: Str) -> Opt[Str] }

        component Memo provides cache: Cache {
          provide cache {
            fn get(key) { return None }
          }
        }
        """
    )
    assert ir["ir_version"] == 1, (
        "the frontend no longer mints ir_version 1; re-open the FREEZE decision "
        "in docs/conformance.md before assuming the `_emit_v1` bodies are dead")
    assert json.loads(REFERENCE_IR.read_text(encoding="utf-8"))["ir_version"] == 1
