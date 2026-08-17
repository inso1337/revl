"""One source, every emitter — the portability floor.

Backend divergence is this project's recurring bug class: a construct lands,
four tiers take it, one refuses, and nobody notices until someone targets
that tier by hand. It happened with the stale rust golden, and again when
provide-method bindings shipped to python/ts/java/wasm while the rust
backend rejected them.

These tests run one composition through all five emitters. They need no
toolchain — every emitter is pure Python — so the floor is cheap to hold.
A tier that genuinely cannot express something belongs in EXPECTED_LIMITS
with the reason, which keeps a deliberate restriction distinct from an
oversight.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

TIERS = ("python", "typescript", "rust", "java", "wasm")

# tier -> reason, for constructs a backend deliberately cannot express
EXPECTED_LIMITS = {
    "method_bindings": {},          # every tier must take this one
    "str_service": {
        "wasm": "the cordis-wasm tier is i32-only (docs/v2.0-roadmap.md)",
    },
}


def _emitter(tier: str):
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_{tier}_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit_all(ir: dict, case: str) -> dict[str, str]:
    """Emit through every tier; returns {tier: failure} for unexpected ones."""
    failures: dict[str, str] = {}
    for tier in TIERS:
        module = _emitter(tier)
        try:
            module.emit(ir)
        except Exception as exc:  # noqa: BLE001 — any refusal is the signal
            if tier not in EXPECTED_LIMITS.get(case, {}):
                failures[tier] = f"{type(exc).__name__}: {exc}"
    return failures


INT_SERVICE = """
service Counter { fn bump(n: Int) -> Int }
component C provides counter: Counter {
  provide counter {
    fn bump(n) {
      let step = 1
      let next = n + step
      return next
    }
  }
}
"""

STR_SERVICE = """
service Greet { fn hello(name: Str) -> Str }
component G provides greet: Greet {
  provide greet {
    fn hello(name) {
      let prefix = "hi, "
      return prefix + name
    }
  }
}
"""


def test_method_bindings_are_expressible_on_every_tier():
    """The regression this file exists for: `let` in a provide-method body
    reached four tiers before the fifth could express it."""
    failures = _emit_all(compile_source(INT_SERVICE), "method_bindings")
    assert failures == {}, f"tiers that refused a portable construct: {failures}"


def test_string_typed_services_reach_every_tier_but_wasm():
    failures = _emit_all(compile_source(STR_SERVICE), "str_service")
    assert failures == {}, f"unexpected refusals: {failures}"


def test_wasm_still_refuses_str_for_the_documented_reason():
    """An expected limit must stay expected — if wasm gains string support
    this test fails and EXPECTED_LIMITS gets updated deliberately."""
    with pytest.raises(Exception, match="i32-only|not lowerable"):
        _emitter("wasm").emit(compile_source(STR_SERVICE))


def test_the_reference_composition_emits_on_every_hosted_tier():
    ir = compile_source(
        """
service Cache { fn get(k: Str) -> Opt[Str]
                emission fn put(k: Str, v: Str) }
service Log { emission fn note(m: Str) -> Int }
component MemCache requires log: Log provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn get(k) = store.get(k)
    fn put(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
      emit log.note(k)
    }
  }
}
"""
    )
    failures = _emit_all(ir, "str_service")
    assert failures == {}, f"unexpected refusals: {failures}"
