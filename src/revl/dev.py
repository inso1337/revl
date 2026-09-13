"""The supported local development command for the exemplary web app (#724).

``revl dev`` owns the two processes a contributor previously had to assemble by
hand: Vite serves the external frontend assets while the Python Cordis driver
boots the ``.rvl`` composition.  The WebUI coeffect is a real, scoped Cordis
provision, not a process-global bridge; its small development adapter records
the entry that the composition registered and is withdrawn during normal LIFO
teardown.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from . import lifecycle
from .compiler import compile_files
from .errors import RevlError
from .holes import refuse_admission
from .run import _fail, run_command


_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_APP = _ROOT / "examples" / "app" / "notes.rvl"


class DevWebUI:
    """The development host's declared WebUI coeffect.

    A production Cordis WebUI host consumes the same ``add_entry`` request and
    turns it into its shell registration.  Locally Vite owns serving the source
    module, so this adapter makes the composition's registration visible and
    verifies that it names an external asset rather than silently accepting a
    stale path or an inline substitute.

    ``add_entry``'s fourth argument is the typed reactive state the component
    publishes (the ``data`` channel Cordis WebUI broadcasts).  ``data`` keeps a
    default so an entry declared before the channel landed still registers.
    """

    def __init__(self, app_root: Path) -> None:
        self.app_root = app_root
        self.entries: list[tuple[str, str, tuple[str, ...]]] = []
        #: the reactive state each entry published, in registration order. A
        #: production Cordis WebUI host hands this to ``addEntry`` as the reactive
        #: ``data`` object; locally it is recorded so the dev run shows the channel
        #: the composition actually opened, not only the asset paths.
        self.channels: list[dict] = []

    def add_entry(self, dev_source: str, prod_manifest: str, routes: list[str],
                  data: dict | None = None) -> str:
        rel = Path(dev_source)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(
                f"WebUI entry {dev_source!r} must be a relative path under {self.app_root}"
            )
        source = (self.app_root / rel).resolve()
        try:
            source.relative_to(self.app_root.resolve())
        except ValueError:
            raise RuntimeError(
                f"WebUI entry {dev_source!r} escapes the app root {self.app_root}"
            ) from None
        if not source.is_file():
            raise RuntimeError(
                f"WebUI entry {dev_source!r} does not exist under {self.app_root}"
            )
        if not routes or any(not route.startswith("/") for route in routes):
            raise RuntimeError("WebUI entry routes must be absolute, non-empty paths")
        channel = dict(data or {})
        self.entries.append((dev_source, prod_manifest, tuple(routes)))
        self.channels.append(channel)
        fields = ", ".join(sorted(channel)) or "(none)"
        print(f"  webui  | entry {dev_source} -> {', '.join(routes)}", flush=True)
        print(f"  webui  | channel state {fields}", flush=True)
        return dev_source


def _app_files(args) -> list[str]:
    return list(args.files) if args.files else [str(_DEFAULT_APP)]


def _frontend_dir(args, app: Path) -> Path:
    return Path(args.frontend).resolve() if args.frontend else app.parent / "frontend"


def _preflight(files: list[str]) -> int:
    """Fail before spawning Vite, retaining the source-line + stage contract."""
    try:
        ir = compile_files(files)
    except RevlError as exc:
        return _fail(str(exc), lifecycle.COMPILE)
    except OSError as exc:
        return _fail(f"cannot read app source: {exc}", lifecycle.COMPILE)
    try:
        refuse_admission(ir)
    except RevlError as exc:
        return _fail(str(exc), lifecycle.ADMISSION)
    return 0


def _start_vite(frontend: Path, host: str, port: int) -> subprocess.Popen[str]:
    if not frontend.is_dir():
        raise RuntimeError(f"frontend directory does not exist: {frontend}")
    if not (frontend / "package.json").is_file():
        raise RuntimeError(f"frontend directory has no package.json: {frontend}")
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "npm is not on PATH; install Node.js, then re-run `revl dev`"
        )
    # Vite itself gives the actionable dependency diagnostic if npm install has
    # not yet been run.  Keeping its output attached makes that diagnosis part
    # of the single command rather than a hidden child-process failure.
    #
    # `--strictPort` is what makes the banner truthful: without it Vite answers
    # an occupied port by auto-incrementing to the next free one, so the child
    # stays alive and `revl dev` would advertise a port that belongs to some
    # other process while the composition is served somewhere else.  With it,
    # Vite exits instead of moving and the boot check below reports the port
    # failure on the URL `revl dev` was asked to serve.
    return subprocess.Popen(
        [npm, "run", "dev", "--", "--host", host, "--port", str(port), "--strictPort"],
        cwd=frontend,
        text=True,
    )


def _vite_dead(proc: subprocess.Popen[str] | None) -> bool:
    """True when the Vite child already exited (boot failure, port clash)."""
    if proc is None:
        return True
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return False
    return True


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextmanager
def _closed_stdin():
    """`--once` for a REPL-holding driver: EOF the prompt loop immediately."""
    import os
    import sys

    fd = os.open(os.devnull, os.O_RDONLY)
    new = os.fdopen(fd)
    old = sys.stdin
    sys.stdin = new
    try:
        yield
    finally:
        sys.stdin = old
        try:
            new.close()
        except OSError:
            pass


def dev_command(args) -> int:
    """Run the exemplary app and its Vite frontend under one parent process."""
    files = _app_files(args)
    app = Path(files[0]).resolve()
    if not app.is_file():
        return _fail(f"cannot read app source {app}", lifecycle.COMPILE)
    status = _preflight(files)
    if status:
        return status

    frontend = _frontend_dir(args, app)
    vite: subprocess.Popen[str] | None = None
    if not args.no_frontend:
        # `--port 0` means "bind any free port" to Vite, and only the OS knows
        # which one it picked, so no banner could state where the frontend is
        # served.  Unlike the plainly invalid ports (-1, 99999), which Vite
        # rejects on boot and the fatal boot path reports, 0 is a port whose
        # meaning the command cannot vouch for, so it is refused as a usage
        # error before any child is spawned.  With `--no-frontend` there is no
        # Vite and no banner, so the flag stays inert exactly as it was.
        if args.port == 0:
            print("error: --port 0 asks Vite to bind an arbitrary free port, so "
                  "`revl dev` could not name the URL it printed; pass the port "
                  "you want (default 5173), or --no-frontend to skip Vite",
                  file=sys.stderr)
            return 2
        try:
            vite = _start_vite(frontend, args.host, args.port)
        except RuntimeError as exc:
            return _fail(str(exc), lifecycle.BOOT, code=3)
        if _vite_dead(vite):
            out, _ = vite.communicate(timeout=10)
            vite = None
            tail = "\n".join(out.splitlines()[-8:]) if out else ""
            hint = f"\n{tail}" if tail else ""
            return _fail(
                f"vite exited during boot (port {args.port} in use, or run "
                f"`npm install --legacy-peer-deps` in {frontend}){hint}",
                lifecycle.BOOT,
                code=3,
            )
        # Only now that the child has survived its boot does the URL become a
        # statement about anything: a Vite that died, or moved to another port,
        # never reaches this line.
        print(f"== dev frontend — http://{args.host}:{args.port} ==", flush=True)

    # Re-use the normal lifecycle driver so run/dev have precisely the same
    # compile, admission, config, boot and teardown semantics.  Only the scoped
    # ambient host provision differs.  `revl run --once` does not exist for
    # the py tier (dev always wants a live loop unless --once says otherwise),
    # so the flag is consumed here by closing stdin once boot completes.
    args.files = files
    args.backend = "py"
    args.watch = False
    args.record = False
    args.withdraw = None
    args.wal = None
    args.estop_latch = None
    args.trace = None
    args.config = None
    args.env = None
    args.policy = None
    args.placement = None
    args.plan = False
    args.ambient = {"webui": DevWebUI(app.parent)}
    try:
        if bool(args.once):
            run_args = SimpleNamespace(**{**vars(args), "once": False})
            with _closed_stdin():
                from .run import run_command as run_py  # noqa: PLC0415

                return run_py(run_args, hold_once=True)
        return run_command(args)
    finally:
        _stop(vite)
