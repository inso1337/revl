"""The supported local development command for the exemplary web app (#724).

``revl dev`` owns the two processes a contributor previously had to assemble by
hand: Vite serves the external frontend assets while the Python Cordis driver
boots the ``.rvl`` composition.  The WebUI coeffect is a real, scoped Cordis
provision, not a process-global bridge; its small development adapter records
the entry that the composition registered and is withdrawn during normal LIFO
teardown.
"""

from __future__ import annotations

import hashlib
import json
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

    ``dev_source`` is an item-459-F1 ASSET HANDLE — the record
    ``{"path": <root-relative>, "sha256": <hex>}`` an ``asset "..."`` expression
    lowers to — and this adapter is the deploy-time half of that pin, the same
    shape ``hostref.plug_refs`` is for a host-module ref: the compiler checked a
    file, this process opens one, and only re-hashing proves they are the same
    bytes.  Every arm below refuses; none repairs.

    ``prod_manifest`` is the OTHER half of the same pair and used to be the one
    string this adapter never opened: it was stored verbatim, so every arm above
    — confinement, existence, content — applied to the dev source and to nothing
    else, and the asymmetry was invisible because both arrive in one call.  The
    suite's own fixtures passed ``"m"`` for it.  It is now resolved the same way:
    confined to the app root ALWAYS, and, when the frontend has actually been
    built, opened and required to name the very entry the handle pins.  What it
    still cannot be is content-pinned — a Vite manifest does not exist when the
    composition is compiled, so there is no compile-time digest to check it
    against, which is why the check is "does the built manifest name this asset"
    rather than "are these the bytes".
    """

    def __init__(self, app_root: Path) -> None:
        self.app_root = app_root
        self.entries: list[tuple[str, str, tuple[str, ...]]] = []
        #: for each registered entry, in registration order: the production
        #: chunk the built Vite manifest maps the pinned dev source to
        #: (root-relative), or ``None`` when the frontend is not built yet.
        #: ``None`` is the ordinary dev state, not a failure: ``revl dev`` runs
        #: Vite over the SOURCE, so requiring a build here would refuse the
        #: command's own main path.  A manifest that DOES exist is checked.
        self.prod_entries: list[str | None] = []
        #: the reactive state each entry published, in registration order. A
        #: production Cordis WebUI host hands this to ``addEntry`` as the reactive
        #: ``data`` object; locally it is recorded so the dev run shows the channel
        #: the composition actually opened, not only the asset paths.
        self.channels: list[dict] = []
        #: the sha256 each registered entry was pinned to at compile time, in
        #: registration order, so a dev run can state WHICH bytes it served.
        self.digests: list[str] = []

    def add_entry(self, dev_source: object, prod_manifest: str,
                  routes: list[str], data: dict | None = None) -> str:
        path, digest = self._handle(dev_source)
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(
                f"WebUI entry {path!r} must be a relative path under {self.app_root}"
            )
        source = (self.app_root / rel).resolve()
        try:
            source.relative_to(self.app_root.resolve())
        except ValueError:
            raise RuntimeError(
                f"WebUI entry {path!r} escapes the app root {self.app_root}"
            ) from None
        if not source.is_file():
            raise RuntimeError(
                f"WebUI entry {path!r} does not exist under {self.app_root}"
            )
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError(
                f"WebUI entry {path!r} does not match the sha256 it was compiled "
                f"against (pinned {digest}, on disk {actual}); recompile the "
                f"composition or restore the asset"
            )
        if not routes or any(not route.startswith("/") for route in routes):
            raise RuntimeError("WebUI entry routes must be absolute, non-empty paths")
        built = self._prod_entry(prod_manifest, source, path)
        channel = dict(data or {})
        self.entries.append((path, prod_manifest, tuple(routes)))
        self.channels.append(channel)
        self.digests.append(digest)
        self.prod_entries.append(built)
        fields = ", ".join(sorted(channel)) or "(none)"
        print(f"  webui  | entry {path} -> {', '.join(routes)}", flush=True)
        print(f"  webui  | asset sha256 {digest[:12]}", flush=True)
        print(f"  webui  | channel state {fields}", flush=True)
        print(f"  webui  | prod entry {built or '(not built)'}", flush=True)
        return path

    def _prod_entry(self, prod_manifest: object, source: Path,
                    path: str) -> str | None:
        """Resolve the production half of the asset pair.

        The dev source and the built manifest are two names for ONE frontend,
        and nothing checked that they agreed: the handle carries a path relative
        to the compile-tree root (``frontend/entry.client.ts``) while a Vite
        manifest is keyed relative to the VITE root (``entry.client.ts``), so a
        production host that joined them by string would look up a key the
        manifest does not have and serve nothing.  The join is therefore made by
        FILE IDENTITY rather than by string surgery: the Vite root is not
        declared anywhere, so each directory between the manifest and the app
        root is tried as one, and an entry matches when its ``src`` names the
        same file on disk as the pinned handle.  That needs no convention about
        ``outDir`` or ``.vite/``, which are Vite's to change.

        Refusals, all of them naming the file: a manifest that is not a relative
        path under the app root (checked whether or not it exists, because that
        is a property of the declaration), one that is not readable JSON, one
        that names no entry for this asset, and one whose matching entry points
        at a chunk that is not on disk.  A manifest that is simply ABSENT is not
        a refusal — see ``prod_entries``.
        """
        if not isinstance(prod_manifest, str) or not prod_manifest:
            raise RuntimeError(
                f"WebUI entry {path!r} declares no production manifest path; "
                f"`add_entry`'s second argument names the built Vite manifest "
                f"(item 459)")
        rel = Path(prod_manifest)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(
                f"WebUI production manifest {prod_manifest!r} must be a relative "
                f"path under {self.app_root}")
        manifest = (self.app_root / rel).resolve()
        root = self.app_root.resolve()
        try:
            manifest.relative_to(root)
        except ValueError:
            raise RuntimeError(
                f"WebUI production manifest {prod_manifest!r} escapes the app "
                f"root {self.app_root}") from None
        if not manifest.is_file():
            return None
        try:
            records = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"WebUI production manifest {prod_manifest!r} is not readable "
                f"JSON: {exc}") from None
        if not isinstance(records, dict):
            raise RuntimeError(
                f"WebUI production manifest {prod_manifest!r} is not a Vite "
                f"manifest object")
        # every directory from the manifest's own up to the app root is a
        # candidate Vite root; `src` is relative to whichever one it is.
        roots = [manifest.parent, *manifest.parents]
        roots = [d for d in roots if d == root or root in d.parents]
        for key, record in records.items():
            if not isinstance(record, dict) or not record.get("isEntry"):
                continue
            src = record.get("src") or key
            if not isinstance(src, str) or Path(src).is_absolute():
                continue
            if not any((base / src).resolve() == source for base in roots):
                continue
            chunk = record.get("file")
            if not isinstance(chunk, str) or not chunk:
                raise RuntimeError(
                    f"WebUI production manifest {prod_manifest!r} names entry "
                    f"{key!r} for {path!r} with no `file` chunk")
            # `file` is relative to the build's `outDir`, which is no more
            # declared than the Vite root is, so it is located the same way.
            on_disk = next(
                (c for c in ((base / chunk).resolve() for base in roots)
                 if c.is_file() and (c == root or root in c.parents)),
                None,
            )
            if on_disk is None:
                raise RuntimeError(
                    f"WebUI entry {path!r} resolves through {prod_manifest!r} to "
                    f"{chunk!r}, which is not on disk under {self.app_root}; "
                    f"rebuild the frontend")
            return str(on_disk.relative_to(root))
        raise RuntimeError(
            f"WebUI production manifest {prod_manifest!r} names no entry for the "
            f"pinned asset {path!r}; its entries are "
            f"{sorted(k for k, r in records.items() if isinstance(r, dict) and r.get('isEntry')) or '(none)'}. "
            f"The dev source and the built manifest must describe one frontend "
            f"(item 459)")

    @staticmethod
    def _handle(dev_source: object) -> tuple[str, str]:
        """Read the asset handle, refusing anything that is not one.

        A bare string is refused BY NAME rather than accepted as a path: it is
        exactly the unresolved, unpinned `Str` that item 459 F1 replaced, and
        accepting it here would make the pin optional at the one place it is
        checked.
        """
        if isinstance(dev_source, str):
            raise RuntimeError(
                f"WebUI entry {dev_source!r} is a bare path string, not an asset "
                f"handle; declare the parameter as the asset record "
                f"`{{ path: Str, sha256: Str }}` and pass `asset \"...\"` "
                f"(item 459)"
            )
        if not isinstance(dev_source, dict):
            raise RuntimeError(
                f"WebUI entry handle must be the asset record "
                f"`{{ path: Str, sha256: Str }}`, got {type(dev_source).__name__}"
            )
        path = dev_source.get("path")
        digest = dev_source.get("sha256")
        if not isinstance(path, str) or not path:
            raise RuntimeError(
                "WebUI entry handle has no `path` field (item 459 asset handle)")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError(
                f"WebUI entry {path!r} carries no sha256 pin; an asset handle "
                f"without a digest cannot be verified (item 459)")
        return path, digest


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
                f"`npm ci` in {frontend}){hint}",
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
