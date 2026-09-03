"""Drop the working directory from `sys.path` in the CLI entry points.

`python -m revl` is the documented invocation everywhere — README, `setup.sh`,
the `revl run` diagnostic — and `-m` makes CPython put the process's working
directory at `sys.path[0]`, ahead of site-packages. Every bare-name import the
CLI makes from then on resolves there first, so a `cordis.py`, `yaml.py` or
`wasmtime.py` sitting next to a composition is imported instead of the real
module: host Python that runs before any admission check, without the `.rvl`
ever having to compile (issue #317).

The console scripts (`revl`, `truc`, declared in `pyproject.toml`) never had
this, because a script entry point puts the script's own directory on
`sys.path` and not the caller's. `drop_cwd_entry()` gives `-m` the same
property.

This is NOT issue #302. That one is about the three explicit
`sys.path.insert(0, backends/python)` calls in `run.py`, `_process_runner.py`
and `bundle.py`, which shadow with a directory this repo ships; it has a
different cause and is not addressed here.

Scope, stated plainly: this closes the window for every import made after the
entry point starts, which is where the bare-name runtime imports live. It
cannot cover names already resolved while `import revl` itself was running —
`-m` puts the working directory on the path before the package is imported at
all, so nothing inside the package can act earlier than that. Installing and
using the `revl` console script, or `python -P -m revl` (PYTHONSAFEPATH, 3.11+),
is the invocation that has no window at all.
"""

from __future__ import annotations

import os
import sys

__all__ = ["drop_cwd_entry"]


def drop_cwd_entry() -> bool:
    """Remove the `-m`-injected working-directory entry from `sys.path`.

    Returns True when an entry was removed, False when there was nothing to
    remove (a console script, `python -P`, or an already-scrubbed path).

    Only `sys.path[0]` is considered. That is the entry CPython injects for
    `-m`; a working directory the user put on `PYTHONPATH` themselves is a
    deliberate choice and is left alone.
    """
    if sys.flags.safe_path or not sys.path:
        return False
    head = sys.path[0]
    # `-m` writes `""` on some CPython versions and the absolute working
    # directory on others; both mean the same thing.
    if head == "":
        del sys.path[0]
        return True
    try:
        if os.path.isdir(head) and os.path.samefile(head, os.getcwd()):
            del sys.path[0]
            return True
    except OSError:
        # A head entry that cannot be stat'ed is not the working directory.
        pass
    return False
