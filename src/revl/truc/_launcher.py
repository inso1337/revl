"""The `truc` launcher — a thin Python shim around the revl composition.

All of truc's dispatch, help text, resolution, and refusal logic live in the
`.rvl` components beside this file; Python does only what revl cannot: be a
console script. The shim (1) finds truc's own component sources relative to
this package, (2) compiles them — that compile *is* an admission, so every
boot of truc is truc passing its own gate (docs/design/truc-architecture.md
§7, stage 0), and (3) boots the composition in-process and calls
`cli.run(argv)`, exactly the `Session.load`/`Session.call` machinery behind
`revl_call` (src/revl/mcp/session.py).

truc runs against the current working directory: `truc add`/`truc assemble`
read and write truc.toml / truc.lock / trucs/ / build/ under `.`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

_HERE = Path(__file__).resolve().parent

#: The bootstrap lock format truc will boot from. Bumped from 0 (a bare file
#: list) when the lock gained per-file hashes; a lock stamped with any other
#: version is refused rather than guessed at, because an older lock carries no
#: pins and a newer one may mean something this launcher cannot check.
LOCK_VERSION = 1

_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


class BootstrapLockError(RuntimeError):
    """truc's own stage-0 lock does not describe the components on disk.

    Always a refusal to boot, never a warning: the whole point of the lock is
    that stage 0 compiles exactly the bytes that were released.
    """


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refuse(reason: str) -> BootstrapLockError:
    return BootstrapLockError(
        f"{reason}\n"
        f"  lock: {_HERE / 'truc.lock'}\n"
        "  truc will not boot on a composition its own lock does not "
        "describe. If this change to truc's components is intended, "
        "regenerate the lock (`python -m revl.truc._relock`) and commit it.")


def _contained_path(rel: str) -> Path:
    """Resolve one lock-named path INSIDE truc's package directory, refusing
    anything that leaves it.

    The trust root this lock rests on is the installed revl package's own
    integrity, and that claim is only true if every byte stage 0 compiles is
    actually *in* the package. So the path is checked, not assumed: it must be
    relative, carry no `..`, and reach a regular file through no symlink at any
    segment. `Path.resolve()` would happily follow a link planted in
    `components/` straight out of the package, which is why the walk below
    tests each segment with `is_symlink()` before the file is accepted.
    """
    if not isinstance(rel, str) or not rel:
        raise _refuse(f"truc.lock names a component path that is not a "
                      f"non-empty string: {rel!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or "\\" in rel:
        raise _refuse(f"truc.lock names an absolute component path: {rel!r} — "
                      "stage-0 sources must live inside truc's package")
    parts = pure.parts
    if any(part in ("..", ".") for part in parts):
        raise _refuse(f"truc.lock names a component path that leaves truc's "
                      f"package: {rel!r}")
    cur = _HERE
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise _refuse(f"truc.lock names a component path that reaches "
                          f"truc's package through a symlink ({cur}) — a "
                          "symlinked source is bytes the package does not own")
    if not cur.is_file():
        raise _refuse(f"truc.lock names a component file that is missing or is "
                      f"not a regular file: {rel!r}")
    return cur


def _lock_rows(data: object) -> list[dict]:
    """The validated `files` rows of a parsed bootstrap lock."""
    if not isinstance(data, dict):
        raise _refuse("truc.lock is not a JSON object")
    version = data.get("lockVersion")
    if version != LOCK_VERSION:
        raise _refuse(
            f"truc.lock is stamped lockVersion {version!r}, and this truc "
            f"boots only lockVersion {LOCK_VERSION} (a version-0 lock is a "
            "bare file list carrying no hashes, so it pins nothing)")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise _refuse("truc.lock names no component files; truc will not fall "
                      "back to compiling whatever is in components/")
    rows: list[dict] = []
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            raise _refuse(f"truc.lock has a `files` entry that is not an "
                          f"object: {row!r}")
        path = row.get("path")
        if not isinstance(path, str) or not path:
            raise _refuse(f"truc.lock has a `files` entry with no path: {row!r}")
        if path in seen:
            raise _refuse(f"truc.lock names {path!r} twice")
        seen.add(path)
        digest = row.get("sha256")
        if not isinstance(digest, str) or not _SHA256_HEX.match(digest):
            raise _refuse(
                f"truc.lock carries no usable pin for {path!r} "
                f"(sha256={digest!r}) — a pin is required, not optional")
        rows.append({"path": path, "sha256": digest})
    return rows


def component_files() -> list[str]:
    """truc's own composition file list, from truc's committed `truc.lock` (§7).

    The lock is truc's stage-0 self-description and it is MANDATORY. There is
    no glob fallback: a lock that is absent, unparseable, stamped with an
    unknown version, or that names no files is a tampering signal, and quietly
    compiling whatever happens to sit in `components/` instead is exactly what
    someone who removed the lock would want. Deleting the lock is no harder
    than corrupting it, so both refuse — the same posture `planner.plan_drift`
    takes on a project truc: a missing pin refuses exactly like a drifting one,
    because a check that can be turned off by deleting its input is not a check.

    Each row is verified before it is compiled: the path must resolve inside
    truc's package through no symlink, and the file's sha256 must equal the
    recorded pin. The trust root here really is the installed package's own
    integrity — anyone who can rewrite these sources can rewrite `registry.py`
    beside them — but that claim is now CHECKED rather than assumed: nothing
    outside the package can be dragged into stage 0 by a lock entry or a
    planted link, and a component whose bytes moved without the lock moving
    with them refuses to boot. Regenerate with `python -m revl.truc._relock`.
    """
    lock = _HERE / "truc.lock"
    if not lock.is_file():
        raise _refuse("truc.lock is missing")
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _refuse(f"truc.lock could not be read: {error}") from error

    out: list[str] = []
    for row in _lock_rows(data):
        path = _contained_path(row["path"])
        actual = _sha256_file(path)
        if actual != row["sha256"]:
            raise _refuse(
                f"hash drift: {row['path']} ({actual}) does not match its "
                f"truc.lock pin ({row['sha256']})")
        out.append(str(path))
    return out


def lock_document() -> dict:
    """The bootstrap lock as it should be for the components on disk — the
    regenerator behind `python -m revl.truc._relock` and the
    regenerate-or-red test in `tests/test_truc_bootstrap.py`. It reads the
    committed lock only for the file LIST (which components make up truc is a
    decision, not something to be discovered by globbing) and recomputes every
    hash."""
    lock = _HERE / "truc.lock"
    data = json.loads(lock.read_text(encoding="utf-8"))
    files = []
    for row in data.get("files") or []:
        rel = row["path"] if isinstance(row, dict) else row
        files.append({"path": rel, "sha256": _sha256_file(_contained_path(rel))})
    return {"lockVersion": LOCK_VERSION, "note": data.get("note", ""),
            "files": files}


def main(argv: list[str] | None = None) -> int:
    from revl.compiler import compile_files  # noqa: PLC0415 — lazy, pulls cordis
    from revl.errors import RevlError  # noqa: PLC0415
    from revl.mcp.session import Session, SessionError  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)

    # `reproduce` is an out-of-band verifier, not a truc state change: it
    # recompiles a published component and compares the recomputed hashes
    # against what was recorded, mutating nothing. It runs frontend-only
    # (`compile_files` + `registry`/`attest`, no runtime) and lives in Python
    # for the same reason `revl attest` does: the launcher does what revl
    # cannot, and hash-comparing a rebuild against a recorded lock/attestation
    # is that. Intercepted here so `truc reproduce` and `revl truc reproduce`
    # are one engine (both reach this launcher).
    if args and args[0] == "reproduce":
        from .reproduce import run as _reproduce_run  # noqa: PLC0415
        return _reproduce_run(args[1:])

    try:
        sources = component_files()
    except BootstrapLockError as error:
        # Stage 0 refuses. truc does not boot on a composition its own lock
        # does not describe, and it never degrades to compiling whatever is
        # on disk instead.
        print(f"truc: refusing to boot: {error}", file=sys.stderr)
        return 70

    try:
        ir = compile_files(sources)
    except RevlError as error:  # truc's own components no longer admit — a bug
        print(f"truc: internal composition failed to compile:\n{error}",
              file=sys.stderr)
        return 70

    session = Session()
    code: int = 0
    try:
        session.load(ir)
        result = session.call("cli", "run", [args])
        value = result.get("result")
        code = value if isinstance(value, int) else 0
    except (SessionError, RevlError) as error:
        print(f"truc: {error}", file=sys.stderr)
        code = 70
    finally:
        try:
            if session.loaded:
                session.unload()
        except Exception:  # noqa: BLE001 — teardown must not mask the exit code
            pass
    return code


if __name__ == "__main__":  # pragma: no cover — `python -m revl.truc`
    raise SystemExit(main())
