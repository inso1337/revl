"""Issue #1911: the `abort` lifecycle step on the TypeScript tier.

`src/revl/lower.py` lowers `abort` to `{"step": "abort"}` in a lifecycle test's
body, and the py tier implements it, but `_emit_ts_lifecycle_tests` had no arm
for it and fell through to

    EmitError: lifecycle test '…': unknown lifecycle step 'abort'

so a witnessed abort path could not be tested on ts at all.

The tier has no `SessionOwner` to call, so the step is implemented natively:
mark EVERY live activation's `Frame` aborting (`frameForCtx`), then dispose the
fibers LIFO — each teardown then replays its witnessed inverse instead of
committing (item 245). The runtime import is gated on the step actually
appearing, so a composition that never aborts keeps the byte-identical module
it had before.

These are toolchain-free checks (the main venv, no `npm`); the end-to-end proof
is the emitted module running under Node in the vitest suite.
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_HEAD = (
    "service Ledger { fn count() -> Int }\n"
    "component Tally provides ledger: Ledger {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide ledger { fn count() = store.size() }\n"
    "}\n"
)

ABORTS = (
    _HEAD
    + 'lifecycle test "aborts" {\n'
    "  load Tally\n"
    "  call ledger.count()\n"
    "  abort\n"
    "  assert no_residue\n"
    "}\n"
)

UNLOADS = (
    _HEAD
    + 'lifecycle test "unloads" {\n'
    "  load Tally\n"
    "  call ledger.count()\n"
    "  unload Tally\n"
    "  assert no_residue\n"
    "}\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_lifecycle_abort", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(src: str) -> str:
    return _load().emit(compile_source(src))


def test_abort_step_is_lowered_and_unwound_not_refused():
    out = _emit(ABORTS)
    assert 'it("aborts"' in out
    assert "unknown lifecycle step" not in out
    assert "frameForCtx(_fiber.ctx)?.abort()" in out
    # every live activation is marked BEFORE any teardown runs, so a teardown
    # that reaches a still-live component already sees it rejecting
    mark = out.index("frameForCtx(_fiber.ctx)?.abort()")
    dispose = out.index("await _fiber.dispose()")
    clear = out.index("_revl_fibers.clear()")
    residue = out.index("assertNoResidue(root, _revl_baseline)")
    assert mark < dispose < clear < residue
    # ...and the unwind is LIFO and tolerates a throwing teardown, like py's step
    assert "Array.from(_revl_fibers.values()).reverse()" in out
    assert "try {" in out and "} catch {" in out
    # the tier implements the step itself; there is no `SessionOwner` here
    assert "begin_abort" not in out


def test_frameForCtx_is_imported_only_by_a_test_that_aborts():
    """The gate keeps every non-aborting composition byte-identical."""
    aborting = _emit(ABORTS)
    plain = _emit(UNLOADS)
    assert "frameForCtx" in aborting
    assert "frameForCtx" not in plain
    assert "frameForCtx(_fiber.ctx)" not in plain


def test_uses_lifecycle_abort_finds_the_step():
    m = _load()
    assert m._uses_lifecycle_abort(compile_source(ABORTS)) is True
    assert m._uses_lifecycle_abort(compile_source(UNLOADS)) is False
    assert m._uses_lifecycle_abort({}) is False
