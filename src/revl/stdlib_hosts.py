"""Trusted backend support modules, imported before the loader path is withdrawn.

Install-origin stdlib modules reach their py support code from `@py` bodies
(`from revl_fs_workspace import ...`, `from revl_net import perform`, ...). Those
bodies execute at CALL time, after `revl run` and the placement process runner
have removed `backends/python` from `sys.path`: the directory is a trusted
loader path for the runtime's own first-party modules, not an ambient import
capability for generated code (`src/revl/run.py`, `src/revl/_process_runner.py`,
and the rationale in `src/revl/compiler.py`'s user-body import refusal).

A generated body's `import` therefore has to resolve from `sys.modules`, not
from the path. This module is the one place that imports the trusted support
modules while the path still exists. Both call sites call
:func:`preload_stdlib_host_modules` immediately before the removal, so a stdlib
module added later cannot re-introduce the ordering dependency by accident.

`src/revl/fs.py` performs the same import eagerly for `revl_fs_workspace`; the
shared list here is what a new support module joins.

A module absent from the backend tree is skipped: the tree that ships the
stdlib ships its support modules, so an absent one means no installed stdlib
module imports it. An import that raises is NOT swallowed: the call sites are
booting a composition, and an install fault must be visible rather than the
body failing later at call time.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from ._paths import backends_root

#: Backend support modules importable from the `@py` bodies of install-origin
#: stdlib modules. A new stdlib module with a py support module adds its name
#: here and a row to `backends/python/README.md`.
STDLIB_HOST_MODULES: tuple[str, ...] = (
    "revl_fs_workspace",
    "revl_net",
    "revl_shell_classify",
    "revl_shell_host",
)


def preload_stdlib_host_modules() -> list[str]:
    """Import the trusted support modules from the backend tree.

    Returns the module names that were loaded. The backend directory is added
    to `sys.path` only if it is not already there, and it is NOT removed: the
    caller owns that decision, because the removal is the trust boundary this
    preload exists for.
    """
    backend = str(backends_root() / "python")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    loaded: list[str] = []
    for name in STDLIB_HOST_MODULES:
        if not (Path(backend) / f"{name}.py").is_file():
            continue
        importlib.import_module(name)
        loaded.append(name)
    return loaded
