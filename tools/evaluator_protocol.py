#!/usr/bin/env python3
"""The evaluator protocol as the tools read it (issue #2802).

`EVALUATOR_PATHS` and its three views live next to `KERNEL_PATHS` in
`src/revl/kernel_boundary.py`, because one enumeration covering the whole judge
is the point, and the kernel is one view of it. The tools do not import `revl`
for it. Importing `revl.kernel_boundary` executes `revl/__init__.py`, which
imports the compiler: a scorer that loads the candidate's compiler to read a
constant has widened its own input surface for nothing, and on this tree the
package import costs seconds against milliseconds for the one file. So this
module loads that one file by path and re-exports what the tools use. There is
still exactly one enumeration, and it is still in `src/revl/`.

`AUTHORITY_FENCE` and `HELD_OUT_FENCE` are `evaluator_fence(...)` results, not
copies: adding a path to the enumeration adds it to the refusal that names it.
`protocol_digest` is re-exported rather than reimplemented, so a scorecard and
a controller verdict can never be hashing two different definitions of the
protocol.

The path loader is `spec_from_file_location` with the module registered in
`sys.modules` first, because `kernel_boundary.py` declares a dataclass and
`from __future__ import annotations` resolves annotations through
`sys.modules[cls.__module__]`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The private name the one-file load is registered under. Not `kernel_boundary`
#: and not `revl.kernel_boundary`: the controller imports the package's copy and
#: this loader must not replace it in `sys.modules`. The root is part of the
#: name, so two checkouts loaded in one process (the selector does that) each
#: get their own kernel view instead of the first one's.
_NAME = "_revl_kernel_boundary_evaluator_" + hashlib.sha1(
    str(ROOT).encode("utf-8")).hexdigest()[:12]


def _load():
    existing = sys.modules.get(_NAME)
    if existing is not None:
        return existing
    path = ROOT / "src" / "revl" / "kernel_boundary.py"
    spec = importlib.util.spec_from_file_location(_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the evaluator enumeration from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_NAME, None)
        raise
    return module


_KERNEL_BOUNDARY = _load()

#: Every file or tree the evaluator is made of (issue #2802).
EVALUATOR_PATHS = _KERNEL_BOUNDARY.EVALUATOR_PATHS

#: The kernel view of `EVALUATOR_PATHS` (issue #1223).
KERNEL_PATHS = _KERNEL_BOUNDARY.KERNEL_PATHS

#: The evaluator paths the evolution controller refuses by name.
AUTHORITY_FENCE = _KERNEL_BOUNDARY.evaluator_fence("authority")

#: The evaluator paths the held-out scorer refuses by name.
HELD_OUT_FENCE = _KERNEL_BOUNDARY.evaluator_fence("held_out")

#: `sha256:<hex>` over the evaluator bytes in a checkout (issue #2802).
protocol_digest = _KERNEL_BOUNDARY.protocol_digest

#: The view function itself, for a caller that wants a named fence.
evaluator_fence = _KERNEL_BOUNDARY.evaluator_fence
