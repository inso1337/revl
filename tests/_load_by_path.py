"""Load a repository file as a module under a given name, once per session.

Tools, harnesses and emitters are scripts, not package modules, so tests load
them by path: `spec_from_file_location`, register under a name (dataclasses
with `from __future__ import annotations` resolve their annotations through
`sys.modules[cls.__module__]`), execute. Written inline, that sequence builds a
NEW module every time it runs and REPLACES whatever the name already held.

That is how an order-dependent red reached main (issue #1449):
`tools/evolution_controller.py` imports `evolution_reward` at collection time,
and `tests/test_evolution_reward.py` later re-executed the same file under the
same name. From then on the controller's `Verdict` subclassed one class and
`import evolution_reward` returned another, so a subclass check failed only
when the reward tests happened to run first.

`load_by_path` returns the module already registered under `name` when it was
loaded from the same file, so every loader in the session, and every plain
`import name`, shares one module and one set of classes. A different file under
the same name is still loaded and registered, as before: that is a module-name
collision between two files, which reusing would hide rather than fix.

`tests/test_load_by_path_is_the_only_by_path_loader.py` fails on a new inline
registration anywhere in tests/.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _file_of(module: ModuleType) -> Path | None:
    file = getattr(module, "__file__", None)
    return Path(file).resolve() if file else None


def load_by_path(name: str, path: str | Path) -> ModuleType:
    """The module in `path`, registered in `sys.modules` as `name`."""
    path = Path(path).resolve()
    existing = sys.modules.get(name)
    if existing is not None and _file_of(existing) == path:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A module that failed to execute is not left registered in place of
        # what the name held before.
        if existing is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = existing
        raise
    return module
