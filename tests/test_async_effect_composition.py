"""Item 131 — explicit async/await EFFECT composition: admission + emission.

The runtime PROOF (LIFO teardown across the suspension) lives on the reference
tier at backends/python/tests/test_async_effect_composition.py (py asyncio) and
the ts vitest twin (backends/typescript/tests/async_effect_composition.test.ts).
This suite is the tier-agnostic half that runs under the standard gate: the
frontend admits the three spellings and flags the IR, and the py/ts emitters
render the awaited acquisition/emission in the boundary-atomic shape design §4
clause 1 requires (acquisition awaits, THEN the inverse registers, in one
generator step). The refusal side (the four silent leaks + the two pairing
rules + the block fence) is the rejection sweep in tests/test_frontend.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _tier_emit(tier: str):
    """Load one tier's `emit.py` under a distinct module name so the four
    backends' identically-named modules never shadow one another."""
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"_emit_{tier}", path)
    mod = importlib.util.module_from_spec(spec)
    # each backend's emit.py imports sibling helpers by bare name, so its own
    # directory must lead sys.path while it executes
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    return mod

_PROG = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
  async fn slow_open(sql: Str) -> List[Row]
  emission async fn record(sql: Str) -> Int
}
component PgDatabase provides db: Database {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
    async fn slow_open(sql) { await Job.run("B")  return pool.query(sql) }
    async fn record(sql)    { await Job.run("R")  return pool.execute(sql) }
  }
}
component Consumer requires db: Database {
  let la = effect db.query("ACQ A") undo db.query("UNDO A")
  let lb = effect await db.slow_open("ACQ B") undo db.query("UNDO B")
  await emit db.record("FIRE") compensate db.execute("OFFSET")
}
"""


def _consumer_body():
    ir = compile_source(_PROG, "aec.rvl")
    consumer = next(c for c in ir["components"] if c["name"] == "Consumer")
    return ir, consumer["body"]


def test_frontend_flags_the_awaited_steps_and_only_those():
    """The IR carries the additive `async: true` on exactly the await-marked
    effect/emit steps; the sync acquisition carries no flag (byte-identity)."""
    _, body = _consumer_body()
    la, lb, emit_step = body[0], body[1], body[2]
    assert la["step"] == "let-effect" and "async" not in la, \
        "the sync acquisition stays unflagged (byte-identical to before)"
    assert lb["step"] == "let-effect" and lb.get("async") is True, \
        "`effect await` flags the acquisition async"
    assert emit_step["step"] == "emit" and emit_step.get("async") is True, \
        "`await emit` flags the emission async"
    # rule 3 held at admission: the teardown slots are present and sync
    assert "undo" in lb and "compensate" in emit_step


def test_python_emits_boundary_atomic_awaits():
    """py: the acquisition awaits its LANDED value, then the inverse yields in
    the same generator step (design §4 clause 1); the emission awaits so it
    fires."""
    ir, _ = _consumer_body()
    code = _tier_emit("python").emit(ir)
    body = code[code.index("def _consumer_apply"):]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # the body generator went async because it carries awaited steps
    assert any(ln.startswith("async def _body()") for ln in lines)
    # acquisition awaits, then the inverse registers on the very next line
    i = next(k for k, ln in enumerate(lines) if ln == "lb = await _revl_ctx.db.slow_open('ACQ B')")
    assert lines[i + 1] == "yield lambda: _revl_ctx.db.query('UNDO B')", \
        "the inverse yield is the next action after the awaited acquisition"
    # the awaited emission fires (a bare async emit would build an unawaited coro)
    assert "await _revl_ctx.db.record('FIRE')" in lines


def test_typescript_emits_boundary_atomic_awaits():
    """ts: same boundary-atomic shape on the async generator; the awaited
    emission is keyword-led (`await …`) so it never begins with `(` (ASI)."""
    ir, _ = _consumer_body()
    code = _tier_emit("typescript").emit(ir)
    assert "async function* ()" in code, "an awaited step forces the async generator"
    # acquisition awaits its landed value (const-led, ASI-safe), inverse next
    assert 'const la = ctx.db.query("ACQ A")' in code, \
        "the sync acquisition is byte-identical (no await)"
    assert 'const lb = (await ctx.db.slow_open("ACQ B"))' in code
    assert 'yield () => ctx.db.query("UNDO B")' in code
    # the awaited emission is keyword-led, so ASI cannot glue it to the prior
    # `yield () => <undo>` arrow
    assert 'await ctx.db.record("FIRE")' in code
    assert '(await ctx.db.record("FIRE"))' not in code, \
        "the emission statement must not begin with `(` (ASI hazard)"


def test_wasm_refuses_the_awaited_spellings():
    """wasm awaits only the `Job.run` host op; an awaited acquisition/emission
    has no async host seam, so it is refused honestly rather than silently
    erased (go/java/rust erasure is covered by their per-tier emit goldens)."""
    wasm = _tier_emit("wasm")
    src = """
service Log { emission async fn note(m: Str) -> Int }
service Db { fn go() -> Str }
component A requires log: Log provides db: Db {
  await emit log.note("x")
  provide db { fn go() { return "ok" } }
}
"""
    ir = compile_source(src, "w.rvl")
    with pytest.raises(wasm.EmitError, match="Job.run"):
        wasm.emit(ir)
