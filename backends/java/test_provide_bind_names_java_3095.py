"""A `let-effect` bind name reaches the Java provider through the binding
ladder (issue #3095).

The activation local is already declared through `_ident(.., "binding")`, so a
bind named `ctx` becomes `ctx__` (the frontend escapes revl `ctx` to `ctx_`
first). The provider class field, constructor parameter, constructor argument
and the provide-method capture rename were built from the raw bind name, so
they disagreed with the local: the constructor was handed the emitter's own
`ctx`/`fx`, or a local `apply` never declares, and a raw field name collided
with the provider's own scaffolding field.

Only a name the ladder actually escapes shows the defect, so this file drives
both shapes: the corpus fixture's revl `ctx` (frontend-escaped to `ctx_`, then
backend-escaped to `ctx__`) and a raw `fx` (not frontend-reserved, so only the
backend ladder moves it to `fx_`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
java_emit = load_by_path("revl_java_emit_bind3095", HERE / "emit.py")

from revl.compiler import compile_files, compile_source  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "emit_java_corpus" / "comp_provide_ctx_bind.rvl"

# A bind the frontend leaves alone (`fx` is not in its reserved set) but the
# Java binding ladder escapes, because `fx` is the provider's own effect scope.
_FX_SRC = """
service Store {
  fn get(key: Str) -> Opt[Int]
  emission fn put(key: Str, value: Int)
}

component Ledger provides store: Store {
  let fx = effect Map.new() undo fx.drop()

  provide store {
    fn get(key) = fx.get(key)
    fn put(key, value) {
      effect fx.insert(key, value)
      undo   fx.remove(key)
    }
  }
}
"""


def test_provider_uses_the_escaped_ctx_bind_name():
    src = java_emit.emit(compile_files([str(FIXTURE)]))
    javac_gate.compile_check(src, "provider bind named ctx (#3095)")

    assert "private final Map<java.lang.Long> ctx__;" in src
    assert ("LedgerStore(Context ctx, Context.EffectScope fx, "
            "Map<java.lang.Long> ctx__)") in src
    assert "this.ctx__ = ctx__;" in src
    assert "new LedgerStore(ctx, fx, ctx__)" in src
    assert "this.ctx__.get(key)" in src
    # The raw IR spelling (`ctx_`) names no local in `apply`; it must not
    # survive as a provider capture or constructor argument.
    assert re.search(r"\bctx_\b", src) is None


def test_provider_ladders_a_bind_the_frontend_left_raw():
    src = java_emit.emit(compile_source(_FX_SRC))
    javac_gate.compile_check(src, "provider bind named fx (#3095)")

    # The provider's own `fx` field stays the effect scope; the bind's field is
    # the laddered spelling, not a duplicate that javac rejects.
    assert "private final Context.EffectScope fx;" in src
    assert "private final Map<java.lang.Long> fx_;" in src
    assert "this.fx = fx;" in src
    assert "this.fx_ = fx_;" in src
    assert "new LedgerStore(ctx, fx, fx_)" in src
    assert "this.fx_.get(key)" in src
