"""Which MCP verbs need the cordis-py runtime, and what the server does without
it (issue #1692).

`revl mcp serve` started under an interpreter that cannot import `cordis` used
to come up with no warning, and every verb that needs a live composition then
failed one call at a time: `revl_load` with an import error, and everything
after it with "nothing is loaded". An agent benchmark lost 11 of 51 verbs for a
whole series that way.

Measured by running every verb in a fresh session under a cordis-less venv and
under one with the pinned cordis (the first step of each run is a recording
`revl_load`):

* `RUNTIME_VERBS` cannot work at all without the runtime. `revl_load` is the
  only way to get a live composition, and it needs cordis; every other verb
  here acts on that composition, or on its recording.
* `LIMITED_VERBS` still answer, with less: the gauntlet and quarantine skip
  their substrate battery, the history verbs answer only from an inline
  `timeline`/`trace`, and `revl_ship` runs check/admit/plan but cannot
  `apply`.

Every other verb is static (it compiles, checks or reads the request) and is
unaffected.

What the server does: `revl mcp serve` first looks for the repository's runtime
venv (`backends/python/.venv`, built by `backends/python/setup.sh`). If that
interpreter can import cordis, the server re-executes under it and says so on
stderr. Otherwise it starts, names the unavailable verbs on stderr and in the
`initialize` instructions, and answers each of them with a refusal whose
`next` field is the fix, instead of an import error.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from .._paths import backends_root
from . import remedy

#: Verbs that need a live composition, which only the runtime can boot.
RUNTIME_VERBS = frozenset({
    "revl_load", "revl_call", "revl_swap", "revl_unload", "revl_snapshot",
    "revl_restore", "revl_rollback", "revl_undo", "revl_commit",
    "revl_commit_confirm", "revl_abort", "revl_estop", "revl_estop_report",
    "revl_edit", "revl_live_query", "revl_timeline", "revl_inspect_step",
    "revl_step_back", "revl_replay_bisect", "revl_replay_forward",
    "revl_fork", "revl_fork_confirm",
})

#: Verbs that still answer without the runtime, and what they lose.
LIMITED_VERBS = {
    "revl_ship": "`apply: true` is refused; check, admit and plan still run",
    "revl_gauntlet": "the substrate battery (boot and unload) is skipped",
    "revl_quarantine": "the substrate battery (boot and unload) is skipped",
    "revl_history_emitted_between": "answers only from an inline `timeline`",
    "revl_history_lifetime": "answers only from an inline `trace`/`traceFile`",
}

#: Set in the environment of a re-executed server, so it never loops.
REEXEC_ENV = "REVL_MCP_REEXECED"
#: Set to any value to stay on this interpreter even when the venv exists.
NO_REEXEC_ENV = "REVL_MCP_NO_REEXEC"


def cordis_importable() -> bool:
    try:
        return importlib.util.find_spec("cordis") is not None
    except (ImportError, ValueError):
        return False


def setup_command() -> str:
    return f"sh {backends_root() / 'python' / 'setup.sh'}"


def venv_python() -> Path:
    """Where `backends/python/setup.sh` puts the runtime venv's interpreter."""
    return backends_root() / "python" / ".venv" / "bin" / "python"


def venv_has_cordis(python: Path) -> bool:
    """Whether *python* exists and can import cordis (and revl, to serve)."""
    if not python.is_file():
        return False
    try:
        probe = subprocess.run(
            [str(python), "-P", "-c", "import cordis, revl"],
            capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def is_refused(name: str, arguments: dict) -> bool:
    """Whether this call needs the runtime this interpreter does not have."""
    if name in RUNTIME_VERBS:
        return True
    return name == "revl_ship" and bool((arguments or {}).get("apply"))


def refusal(name: str) -> dict:
    """The named refusal a runtime verb answers with, `next` naming the fix."""
    fix = (f"run `{setup_command()}`, then restart the server: `revl mcp "
           f"serve` re-executes under {venv_python()} once it can import cordis")
    return {
        "ok": False,
        "refused": True,
        "unavailable": "cordis-py runtime",
        # one `next` schema across every refusal (issue #1691): no MCP call
        # installs a runtime, so this remedy is an operator step
        "next": remedy.operator_step(fix),
        "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "runtime",
            "message": (f"`{name}` needs the cordis-py runtime, which this "
                        f"server's interpreter ({sys.executable}) cannot "
                        f"import, so no composition can be loaded here. "
                        f"Fix: {fix}"),
        }],
    }


def announcement() -> str:
    """Which verbs are unavailable or limited, and how to fix it."""
    limited = "; ".join(f"`{name}`: {why}" for name, why in sorted(LIMITED_VERBS.items()))
    return (f"The cordis-py runtime is not importable by this server "
            f"({sys.executable}), so these {len(RUNTIME_VERBS)} verbs are "
            f"unavailable and refuse with a `next` fix: "
            f"{', '.join(sorted(RUNTIME_VERBS))}. Limited: {limited}. Fix: run "
            f"`{setup_command()}` and restart the server.")


def reexec_target() -> Path | None:
    """The interpreter to re-execute under, or None to stay on this one."""
    if cordis_importable():
        return None
    if os.environ.get(REEXEC_ENV) or os.environ.get(NO_REEXEC_ENV):
        return None
    python = venv_python()
    # compare the venv, not the interpreter path: every venv's `bin/python`
    # resolves to the same base interpreter
    if Path(sys.prefix).resolve() == python.parents[1].resolve():
        return None
    return python if venv_has_cordis(python) else None


def reexec(python: Path, argv: list[str]) -> None:
    """Replace this process with the server under *python* (does not return)."""
    print(f"revl mcp serve: the cordis-py runtime is not importable by "
          f"{sys.executable}; re-executing under {python} "
          f"(backends/python/.venv), where it is (issue #1692)",
          file=sys.stderr, flush=True)
    env = dict(os.environ)
    env[REEXEC_ENV] = "1"
    # `-P` (PYTHONSAFEPATH) keeps the working directory off sys.path, so a
    # checkout's `src/` or a stray `cordis.py` in it cannot shadow the venv's
    # install (issue #317).
    os.execve(str(python), [str(python), "-P", "-m", "revl", *argv], env)
