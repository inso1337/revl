"""The temporal target binds effect-position externs and pushes a witnessed
extern's declared inverse on the saga - issues #3393 and #3386.

Before the fix, `revl emit --target temporal` rendered `await zqWrite("a")`
calling an extern the module never bound (only `recordResidue` was
destructured from `proxyActivities`, and `RevlActivities` listed only it), and
pushed NO saga step for the witnessed extern's declared `undo` - the inverse
was silently lost on abort (the one outcome the issue forbids). Now:

- every extern reached in an effect/undo position is registered as an
  activity: destructured from `proxyActivities` and typed in `RevlActivities`
  (#3386);
- a `witnessed` extern with a declared `undo` gets a saga step on the
  forward's `Ok` branch, with `result` bound to the `Ok` payload (#3393);
- inverse/compensation `run` bodies `await` their calls, so a failed release
  reaches the residue record instead of floating (#3386's second point).

A component-written `undo` on the step IS the declared inverse (lower.py marks
it `inverse: "declared"`) and is pushed write-ahead exactly once - no second
Ok-branch push is emitted for it.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "backends" / "typescript"
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402


def _load_ts_emitter():
    spec = importlib.util.spec_from_file_location(
        "revl_temporal_3393_ts_emit", TS_DIR / "emit.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    added = str(TS_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(TS_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        if added and str(TS_DIR) in sys.path:
            sys.path.remove(str(TS_DIR))
    return module


_EMITTER = None


def _render(src: str, name: str) -> str:
    global _EMITTER
    if _EMITTER is None:
        _EMITTER = _load_ts_emitter()
    out = _EMITTER.emit(compile_source(src, name), target="temporal")
    assert out, "the temporal target rendered nothing"
    return out


# #3393's smallest source: a witnessed extern called in bare effect position.
WITNESSED = (
    "type WT = { id: Str }\n"
    'extern pure fn zqChk(w: WT) -> Int = @ts { return 1 }\n'
    'extern witnessed fn zqWrite(x: Str) -> Result[WT, Str] '
    'undo pure zqChk(result) = @ts { return x }\n'
    "component Res {\n  effect zqWrite(\"a\")\n}\n"
)

# #3386's smallest source: an acquire extern with its declared inverse
# written on the component bracket.
ACQUIRE = (
    "extern pure fn zqClose(h: RHandle) = @ts { return }\n"
    'extern acquire fn zqOpen(n: Int) -> RHandle undo zqClose(result) = @ts { return "h" }\n'
    "component Job {\n  let fetch = effect zqOpen(0) undo zqClose(fetch)\n}\n"
)


def test_witnessed_extern_and_its_inverse_are_bound_activities():
    out = _render(WITNESSED, "w.rvl")
    assert "const { recordResidue, zqChk, zqWrite } = proxyActivities" in out
    iface = out.split("export interface RevlActivities {", 1)[1]
    assert "zqChk(w: WT): Promise<bigint>" in iface
    assert 'zqWrite(x: string): Promise<{ kind: "Ok"; value: WT } | { kind: "Err"; value: string }>' in iface


def test_witnessed_declared_inverse_is_pushed_on_the_ok_branch():
    out = _render(WITNESSED, "w.rvl")
    assert 'const wit1 = await zqWrite("a")' in out
    assert "if (wit1.kind === 'Ok') {" in out
    assert ('saga.push({ name: "zqWrite.undo", run: async () => { await zqChk(wit1.value) }, '
            'args: () => [wit1.value] })') in out


def test_acquire_externs_are_bound_and_the_inverse_is_awaited():
    out = _render(ACQUIRE, "a.rvl")
    assert "const { recordResidue, zqClose, zqOpen } = proxyActivities" in out
    assert "saga.push({ name: \"fetch_.undo\", run: async () => { if (fetch_ !== undefined) { await zqClose(fetch_) } }" in out
    assert "fetch_ = await zqOpen(0n)" in out
    iface = out.split("export interface RevlActivities {", 1)[1]
    assert "zqClose(h: unknown): Promise<void>" in iface
    assert "zqOpen(n: bigint): Promise<unknown>" in iface


def test_a_component_written_undo_is_pushed_exactly_once():
    """The step's own `undo` IS the declared inverse (`inverse: "declared"`),
    pushed write-ahead; no second Ok-branch push may appear."""
    out = _render(ACQUIRE, "a.rvl")
    assert out.count("saga.push(") == 1
    assert ".undo\"" in out
    assert "=== 'Ok'" not in out
