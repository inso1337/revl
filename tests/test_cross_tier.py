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
    "str_service": {},              # wasm gained Str at the service boundary — all six tiers now
    "reference_composition": {      # this comp uses host Map.new, a genuine wasm boundary
        "wasm": "host builtin Map.new — the wasm tier expresses state through coeffects",
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


def test_string_typed_services_reach_every_tier():
    """Str-typed services now reach all six tiers, including wasm — which
    carries Str across the service boundary as a canonical-ABI pointer."""
    failures = _emit_all(compile_source(STR_SERVICE), "str_service")
    assert failures == {}, f"unexpected refusals: {failures}"


def test_wasm_now_emits_str_at_the_service_boundary():
    """Was a documented limit (wasm was i32-only at the boundary); wasm gained
    Str via linear-memory pointers, so it must now emit a Str-typed service
    rather than refuse it. The inverse of the old refusal assertion."""
    _emitter("wasm").emit(compile_source(STR_SERVICE))


FOUR_COMPONENT_BODY_CONSTRUCTS = """
type Outcome = Found(Int) | Missing
fn double(n: Int) -> Int { return n * 2 }
service Bus { fn maybe(n: Int) -> Opt[Int] }
service S { fn f(x: Int) -> Int  fn g(x: Int) }
component Env provides bus: Bus {
  provide bus { fn maybe(n) = (n > 0) ? Some(n) : None }
}
component C requires bus: Bus provides s: S {
  provide s {
    fn f(x) {
      let a = bus.maybe(x) ?? 0
      let b = double(a)
      let o = Found(b)
      return match o { Found(v) => v, Missing => 0 }
    }
    fn g(x) { let kept = double(x)  return }
  }
}
"""


def test_four_component_body_constructs_reach_every_tier():
    """docs/v2.0-roadmap.md §1d items 1-4 — the four component-body renderer
    divergences, in one composition: `??`, a pure-fn call, and `match` in a
    provide-method body, and a bare `return` in a void op. Each was a
    well-formed node with no case in some tier's component-body renderer;
    every one is now a real case, so every tier — go and wasm included — must
    emit this without refusing. A regression on any tier reappears here."""
    ir = compile_source(FOUR_COMPONENT_BODY_CONSTRUCTS)
    failures: dict[str, str] = {}
    for tier in TIERS + ("go",):
        module = _emitter(tier)
        try:
            module.emit(ir)
        except Exception as exc:  # noqa: BLE001 — any refusal is the signal
            failures[tier] = f"{type(exc).__name__}: {exc}"
    assert failures == {}, f"tiers that refused a component-body construct: {failures}"


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
    failures = _emit_all(ir, "reference_composition")
    assert failures == {}, f"unexpected refusals: {failures}"
