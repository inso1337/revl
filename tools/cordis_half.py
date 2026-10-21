"""Run the cordis-runtime half of `tests/` for the local pre-merge gate (issue #2207).

The frontend step of tools/pre_merge.sh runs `tests/` from the frontend venv,
which has no cordis-py runtime, so every test that needs it skips there. Before
this step no local gate ran those tests at all: only CI's main-only
`frontend-cordis` job did, so a runtime regression (the issue's mutant turns the
WAL-less abort replay in `SessionOwner.finalize_abort` from LIFO into FIFO)
passed every local gate.

This driver picks the test files to run and the interpreter to run them with:

* Files: when the frontend step ran with `REVL_CORDIS_SKIP_LOG` set,
  tests/conftest.py wrote the node id of every test that skipped naming cordis
  to that log; the files of those node ids are exactly the half that did not
  run, and an empty log means nothing to re-run. Only when the frontend step
  did not run (no --log), the selected test files that mention cordis are used.
* Interpreter: the first candidate that can import the suite's modules AND the
  cordis-py runtime with its `watchdog` extra, with that runtime's checkout at
  CORDIS_PY_PIN (backends/python/setup.sh, or the environment's override). That is the venv
  backends/python/setup.sh builds (the pinned fork, never PyPI `cordis`).

The last line printed is a status the gate reads:

    STATUS ran <n>          pytest ran <n> file(s); the exit code is pytest's
    STATUS none             no selected test needs the runtime (exit 0)
    STATUS missing <why>    files need the runtime and no candidate has it at
                            CORDIS_PY_PIN, or no pin was found at all (exit
                            EXIT_MISSING): the gate reports NOT OK
    STATUS error <why>      the skip log passed with --log is missing (exit 1)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

EXIT_MISSING = 10

# What `pytest tests/` imports hard (tools/pre_merge.sh `suite_ok`), plus the
# runtime itself and the `watchdog` extra the fork's `cordis.hmr` needs.
REQUIRED_MODULES = ("pytest", "pytest_asyncio", "yaml", "cryptography",
                    "llguidance", "cordis", "watchdog")

PIN_SOURCE = "backends/python/setup.sh"

SETUP_HINT = ("run `sh backends/python/setup.sh` (pinned cordis-py fork, "
              "CORDIS_PY_PIN, with watchdog) or set REVL_CORDIS_PYTHON")


def missing_modules(python: str) -> list[str] | None:
    """The required modules `python` cannot import; None if it cannot run."""
    if not python or not os.access(python, os.X_OK) or Path(python).is_dir():
        return None
    probe = ("import importlib.util, sys\n"
             f"mods = {REQUIRED_MODULES!r}\n"
             "print(' '.join(m for m in mods if importlib.util.find_spec(m) is None))\n")
    try:
        done = subprocess.run([python, "-c", probe], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.split()


def pinned_revision(root: Path) -> str | None:
    """CORDIS_PY_PIN: the environment's override, else backends/python/setup.sh's."""
    if os.environ.get("CORDIS_PY_PIN"):
        return os.environ["CORDIS_PY_PIN"]
    try:
        text = (root / PIN_SOURCE).read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r'CORDIS_PY_PIN="\$\{CORDIS_PY_PIN:-([0-9a-f]{7,40})\}"', text)
    return found.group(1) if found else None


def cordis_revision(python: str) -> str | None:
    """The git revision of the cordis-py checkout `python` imports, if any."""
    probe = ("import importlib.util, os\n"
             "spec = importlib.util.find_spec('cordis')\n"
             "print(os.path.dirname(spec.origin) if spec and spec.origin else '')\n")
    try:
        done = subprocess.run([python, "-c", probe], capture_output=True,
                              text=True, timeout=60, check=False)
        where = done.stdout.strip()
        if done.returncode != 0 or not where:
            return None
        head = subprocess.run(["git", "-C", where, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=60,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip() if head.returncode == 0 else None


def resolve_python(candidates: list[str],
                   pin: str | None = None) -> tuple[str | None, list[str]]:
    """The first usable candidate, and why each earlier one was rejected.

    With a `pin`, a candidate whose cordis-py checkout is not at that revision
    is rejected too: an `ok` must mean the runtime revision the suite is
    validated against, not whatever fork an older venv still holds."""
    rejected = []
    for cand in candidates:
        if not cand:
            continue
        missing = missing_modules(cand)
        if missing is None:
            rejected.append(f"{cand}: not runnable")
            continue
        if missing:
            rejected.append(f"{cand}: cannot import {', '.join(missing)}")
            continue
        if pin:
            rev = cordis_revision(cand)
            if rev is None:
                rejected.append(f"{cand}: cannot read its cordis-py revision "
                                f"(not a git checkout), pin is {pin[:12]}")
                continue
            if not rev.startswith(pin) and not pin.startswith(rev):
                rejected.append(f"{cand}: cordis-py at {rev[:12]}, "
                                f"CORDIS_PY_PIN is {pin[:12]}")
                continue
        return cand, rejected
    return None, rejected


def files_from_log(log: Path, root: Path) -> list[str]:
    files = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        path = line.strip().split("::", 1)[0]
        if path and (root / path).is_file():
            files.add(path)
    return sorted(files)


#: The token the fallback selection treats as "this file needs the runtime".
#: It is a text match, not the AST gate scan tests/ owns, because a fresh CI
#: runner has no test dependency to import; the structural invariant in
#: `tests/test_env_gated_skips_run_somewhere.py` holds every runtime-gated file
#: to this token, so `--select`'s `STATUS none` cannot hide a guarded file.
RUNTIME_TOKEN = "cordis"


def names_runtime(text: str) -> bool:
    """Does this file's text make it part of the runtime half of the suite?"""
    return RUNTIME_TOKEN in text


def files_from_selection(selection: list[str], root: Path) -> list[str]:
    files = set()
    for node in selection:
        path = root / node.split("::", 1)[0]
        found = sorted(path.rglob("test_*.py")) if path.is_dir() else [path]
        for f in found:
            try:
                if f.is_file() and names_runtime(f.read_text(encoding="utf-8")):
                    files.add(str(f.relative_to(root)))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--log", help="REVL_CORDIS_SKIP_LOG the frontend step wrote")
    ap.add_argument("--select", default="tests/",
                    help="space-separated pytest selection (fallback without a log)")
    ap.add_argument("--python", action="append", default=[],
                    help="candidate interpreter, in order (repeatable)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    # A log that exists, even empty, is the frontend step's answer: an empty
    # one means nothing skipped for cordis, so there is nothing to re-run. The
    # selection fallback is only for a gate whose frontend step did not run,
    # which passes no --log at all. A --log that is missing is a broken step,
    # never a reason to fall back to hundreds of files.
    if args.log is not None:
        if not Path(args.log).is_file():
            print(f"STATUS error skip log {args.log} does not exist")
            return 1
        files = files_from_log(Path(args.log), root)
    else:
        files = files_from_selection(args.select.split(), root)
    if not files:
        print("STATUS none")
        return 0

    # No pin means no revision to hold the runtime to. Running anyway would
    # turn the pin check off without a word (issue #2257), so it is MISSING,
    # naming the file the pin was read from.
    pin = pinned_revision(root)
    if pin is None:
        print(f"{len(files)} test file(s) need the cordis-py runtime and did not run")
        print(f"STATUS missing no CORDIS_PY_PIN found in {PIN_SOURCE} "
              "(nor in the environment), so no venv can be checked against it")
        return EXIT_MISSING

    python, rejected = resolve_python(args.python, pin)
    if python is None:
        for why in rejected:
            print(f"rejected {why}")
        print(f"{len(files)} test file(s) need the cordis-py runtime and did not run")
        print(f"STATUS missing no cordis-py venv: {SETUP_HINT}")
        return EXIT_MISSING

    cmd = [python, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    env.pop("REVL_CORDIS_SKIP_LOG", None)
    rc = subprocess.run(cmd, cwd=root, env=env, check=False).returncode
    print(f"STATUS ran {len(files)}")
    # pytest's own codes: 5 is "nothing collected", which here would be a
    # silent pass over files that need the runtime, so it is a failure too.
    return rc if rc != 5 else 1


if __name__ == "__main__":
    sys.exit(main())
