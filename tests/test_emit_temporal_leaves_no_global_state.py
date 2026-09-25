"""Rendering the typescript `--target temporal` must not leave process-global state.

`backends/typescript/emit.py::_emit_temporal` binds `sys.modules["emit"]` to a
proxy of the typescript module and puts that backend's directory on `sys.path`,
so its sibling sink can `from emit import ...`. Both are process-global. When
they outlived the call, the next bare `import emit` anywhere in the process got
the typescript proxy: `run.py` reaches the py backend exactly that way, so the
py runtime tests loaded the typescript emitter and ran its output as python
(`SyntaxError: invalid character '—'` on the generated banner, and
`extern ... has no @ts body` on every witnessed/WAL test after it).

It was invisible to any single test file and red on main in `frontend-cordis`
for 28 tests, because it needs a temporal render and a py load in ONE process,
in that order. This file reproduces that order directly rather than relying on
collection order to line two other files up.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from revl.compiler import compile_source

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "backends" / "typescript"

# An emission crossing is inside the temporal target's Slice-1 mappable subset
# (docs/design/253-temporal-target.md §5), so this renders rather than being
# refused before the global-state assertions are ever reached.
_PROGRAM = (
    'extern emission[model] idempotent(key: card) '
    'fn ask(card: Str) -> Str = @ts { return "ok"; }\n'
    'component Pay {\n  emit ask("visa")\n}\n'
)


def _load_ts_emitter():
    spec = importlib.util.spec_from_file_location(
        "revl_temporal_global_state_ts_emit", TS_DIR / "emit.py")
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


def test_a_temporal_render_restores_the_emit_binding_it_found():
    sentinel = type(sys)("emit")
    sentinel.EmitError = type("NotTheTypescriptEmitError", (Exception,), {})
    previous = sys.modules.get("emit")
    sys.modules["emit"] = sentinel
    try:
        module = _load_ts_emitter()
        rendered = module.emit(compile_source(_PROGRAM, "a.rvl"), target="temporal")
        assert rendered, "the temporal target rendered nothing, so this test proves nothing"
        assert sys.modules.get("emit") is sentinel, (
            "rendering --target temporal left sys.modules['emit'] pointing at "
            f"{sys.modules.get('emit')!r}; a later `import emit` gets the typescript proxy")
    finally:
        if previous is None:
            sys.modules.pop("emit", None)
        else:
            sys.modules["emit"] = previous


def test_a_temporal_render_leaves_no_emit_binding_when_there_was_none():
    previous = sys.modules.pop("emit", None)
    try:
        module = _load_ts_emitter()
        module.emit(compile_source(_PROGRAM, "a.rvl"), target="temporal")
        assert "emit" not in sys.modules, (
            "rendering --target temporal registered a global `emit` module that "
            "outlives the call")
    finally:
        if previous is not None:
            sys.modules["emit"] = previous


def test_a_temporal_render_does_not_leave_the_typescript_backend_on_sys_path():
    before = list(sys.path)
    module = _load_ts_emitter()
    module.emit(compile_source(_PROGRAM, "a.rvl"), target="temporal")
    assert str(TS_DIR) not in [p for p in sys.path if p not in before], (
        "the typescript backend directory is still on sys.path after a temporal "
        "render, ahead of any later bare import")
