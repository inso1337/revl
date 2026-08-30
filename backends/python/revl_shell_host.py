"""Py-tier host support for `stdlib/shell.rvl` (roadmap item 252).

Two host bodies back the shell tool's extern surface:

  * `run_opaque(cmd)` — the honest, irreversible `emission` fallback. A command
    the pure classifier could not prove fs-local (`revl_shell_classify`) is run
    verbatim through the system shell, exactly as `sh -c` always did: one opaque
    crossing, no witness, no inverse. This is the surface item 252 is shrinking,
    not removing — the unrecognized tail stays here, honestly one prompt.

  * `classify(cmd)` — re-exported from `revl_shell_classify` so the `@py` extern
    body in `stdlib/shell.rvl` is a one-line delegation, matching the
    `revl.truc._host` pattern the other stdlib emission externs use.

Kept in `backends/python` (on the emitted module's `sys.path`, like
`revl_fs_workspace`) so an `@py` body can `import revl_shell_host`.
"""

from __future__ import annotations

import subprocess

# Re-export the pure classifier so `stdlib/shell.rvl`'s `classify` extern body is
# a single delegation. The classifier itself lives in its own module so it stays
# importable and testable with zero host/IO dependencies (it is pure).
from revl_shell_classify import classify  # noqa: F401  (re-exported)


def run_opaque(cmd: str) -> str:
    """Run `cmd` through the system shell and return its captured stdout — the
    opaque `emission` path for a command the classifier did not lower.

    This is deliberately the whole, unreduced shell: `shell=True`, no argument
    parsing, no confinement. It is reached ONLY for commands the classifier
    returned an `emission` verdict for, i.e. exactly the residue item 252 leaves
    behind. stderr is folded into the returned text so a failing command's
    diagnostics are visible to the operator who approved the one prompt."""
    completed = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
    )
    out = completed.stdout
    if completed.stderr:
        out = out + completed.stderr
    return out
