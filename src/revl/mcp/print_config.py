"""`revl mcp serve --print-config`: the MCP client config block (issue #2281).

A client needs a command and args to start the server, and the one value a
human cannot guess is the interpreter: it has to import both `revl` and
`cordis`, and which one does is installation specific. This module finds it by
asking each candidate to import both, and prints the block for it. Nothing here
opens stdio or starts a server.

Candidates, in order: the runtime venv `backends/python/setup.sh` builds, then
the interpreter running this command. `revl doctor` reports the same answer
through `resolve`, so the two cannot disagree.

The probe runs with `-E`, so a `PYTHONPATH` or `PYTHONHOME` in the shell that
ran this command cannot vouch for an interpreter: the client starts the server
with only what the block says. A source checkout that is not installed passes
the probe only with the entries `revl doctor` prints for it; those go into the
block as an `env` key, and are probed the same way (`sys.path`, no environment).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, TextIO

from .._paths import backends_root, venv_python

#: What a candidate has to run for the server to be fully usable from it. `-E`
#: drops PYTHONPATH and PYTHONHOME, `-P` the script directory.
PROBE = ("-E", "-P", "-c", "import cordis, revl")

#: Runs one argv and says whether it exited 0.
RunsOk = Callable[[list], bool]

#: Serve flags that open a listener or terminate TLS. A stdio client config
#: cannot carry them, so asking for one is an error, not a silent omission.
LISTENER_FLAGS = {
    "http": "--http", "tls_cert": "--tls-cert", "tls_key": "--tls-key",
    "tls_client_ca": "--tls-client-ca", "allow_host": "--allow-host",
    "allow_origin": "--allow-origin",
}

#: Serve flags carried into the block: (dest, flag, kind, default). Kinds:
#: `path` (made absolute), `value`, `paths` / `values` (repeatable), `switch`.
FORWARDED = (
    ("restore", "--restore", "path", None),
    ("operator_profile", "--operator-profile", "path", None),
    ("operator", "--operator", "value", None),
    ("policy", "--policy", "path", None),
    ("approval_policy", "--approval-policy", "value", None),
    ("approval_record_values", "--approval-record-values", "value", "withheld"),
    ("author_trust", "--author-trust", "value", "untrusted"),
    ("attempt_limit", "--attempt-limit", "value", None),
    ("provider", "--provider", "paths", None),
    ("grant", "--grant", "values", None),
    ("bind_realm", "--bind-realm", "values", None),
    ("profile_settle_ms", "--profile-settle-ms", "value", 1000),
    ("all_tools", "--all-tools", "switch", None),
)


def candidates(python_dir: Path | None = None) -> list[Path]:
    """The interpreters to try, runtime venv first, without duplicates."""
    python_dir = python_dir or backends_root() / "python"
    found: list[Path] = []
    for path in (venv_python(python_dir / ".venv"), Path(sys.executable)):
        if path not in found:
            found.append(path)
    return found


def probe_argv(python: Path, pythonpath: Iterable[str] = ()) -> list:
    """The command that asks *python* whether it imports both. *pythonpath* is
    put on `sys.path` the way the client's `PYTHONPATH` would be, without
    reading this process's environment."""
    argv = [str(python), *PROBE]
    entries = [str(p) for p in pythonpath]
    if entries:
        argv[-1] = f"import sys; sys.path[:0] = {entries!r}; {argv[-1]}"
    return argv


def runs_ok(argv: list) -> bool:
    """Run *argv* and say whether it exited 0. A missing interpreter is False."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=30,
                              check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve(options: Iterable[Path], ok: RunsOk = runs_ok,
            checkout: dict | None = None) -> tuple[Path, dict] | None:
    """`(interpreter, env)` for the first of *options* that imports both, else
    for the first that does with the source checkout's entries (*checkout* is
    `doctor.checkout_pythonpath`'s answer), with `env` then holding exactly that
    `PYTHONPATH`. None when nothing qualifies."""
    options = list(options)
    for python in options:
        if ok(probe_argv(python)):
            return python, {}
    if checkout and checkout.get("cordis", {}).get("found"):
        entries = checkout["pythonpath"]
        for python in options:
            if ok(probe_argv(python, entries)):
                return python, {"PYTHONPATH": os.pathsep.join(entries)}
    return None


def serve_flags(args) -> tuple[list, list]:
    """`(argv to forward, refused flag names)` from the parsed serve flags.
    Paths are made absolute: the client starts the server from elsewhere."""
    refused = [flag for dest, flag in LISTENER_FLAGS.items()
               if getattr(args, dest, None)]
    if getattr(args, "auth", "bearer") != "bearer":
        refused.append("--auth")
    out: list = []
    files = getattr(args, "files", None)
    if files:
        out += ["--files", *(os.path.abspath(f) for f in files)]
    for dest, flag, kind, default in FORWARDED:
        value = getattr(args, dest, None)
        if kind == "switch":
            if value:
                out.append(flag)
        elif kind in ("paths", "values"):
            for item in value or []:
                out += [flag, os.path.abspath(item) if kind == "paths" else item]
        elif value is not None and value != default:
            out += [flag, os.path.abspath(value) if kind == "path" else str(value)]
    return out, refused


def build_config(python: Path, roots: Iterable[str], env: dict | None = None,
                 extra: Iterable[str] = ()) -> dict:
    """The client config block. Every path in it is absolute."""
    argv = ["-P", "-m", "revl", "mcp", "serve"]
    for root in roots:
        argv += ["--root", os.path.abspath(root)]
    entry: dict = {"command": str(python), "args": argv + list(extra)}
    if env:
        entry["env"] = dict(env)
    return {"mcpServers": {"revl": entry}}


def run(args, *, out: TextIO | None = None, err: TextIO | None = None,
        options: list[Path] | None = None, ok: RunsOk = runs_ok,
        checkout: dict | None = None) -> int:
    """Print the block and return 0, or say why there is none and return 1.
    The roots default to the current directory, the project the session works
    on. A flag that cannot go in a stdio config is refused by name."""
    out = out or sys.stdout
    err = err or sys.stderr
    extra, refused = serve_flags(args)
    if refused:
        print(f"error: {', '.join(refused)} cannot go in a stdio client config "
              "(it opens a listener); start `revl mcp serve` with it directly",
              file=err)
        return 1
    options = options if options is not None else candidates()
    if checkout is None:
        from .. import doctor  # noqa: PLC0415 - lazy

        checkout = doctor.checkout_pythonpath(doctor.Prober(), backends_root())
    found = resolve(options, ok, checkout)
    if found is None:
        tried = ", ".join(str(p) for p in options)
        setup = backends_root() / "python" / "setup.sh"
        print(f"error: no interpreter can import both revl and cordis with no "
              f"PYTHON* environment (PYTHONPATH, PYTHONHOME), which is how a "
              f"client starts the server (tried {tried}). Fix: sh {setup}, "
              f"then run this again", file=err)
        return 1
    python, env = found
    roots = list(getattr(args, "root", None) or []) or [os.getcwd()]
    print(json.dumps(build_config(python, roots, env, extra), indent=2), file=out)
    return 0
