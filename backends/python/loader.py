"""Shared module loader for emitted cordis-py components.

Every consumer of the emitter (demo.py, tests, demo/live.py) used to
hand-roll `types.ModuleType` + `exec`; this is the one place that does it.
"""

from __future__ import annotations

import pathlib
import sys
import types

_BACKEND = pathlib.Path(__file__).resolve().parent

_counter = 0


def load(ir: dict, name: str | None = None) -> types.ModuleType:
    """Emit an IR document and exec it as an importable module object.

    The backend directory joins sys.path so the emitted `from runtime
    import …` resolves regardless of the caller's location.
    """
    global _counter
    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    import emit  # local import: needs the path entry above

    _counter += 1
    module_name = name or f"revl_emitted_{_counter}"
    module = types.ModuleType(module_name)
    source = emit.emit(ir)
    exec(compile(source, f"{module_name}.py", "exec"), module.__dict__)
    return module
