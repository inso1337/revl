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

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def component_files() -> list[str]:
    """truc's own composition file list, from truc's committed `truc.lock` (§7).

    The glob fallback is for a checkout with no lock at all. It is NOT a
    fallback for a lock that is present but unreadable: a lock that exists and
    cannot be parsed, or that names no files, is a tampering signal, and quietly
    compiling whatever happens to sit in `components/` instead is exactly what
    someone who corrupted the lock would want. That case fails loudly.

    This list still carries no hashes (slice S5 turns it into truc's full
    self-description). That is acceptable for one reason and no other: these
    files ship *inside the installed revl package*, so the trust root is the
    package's own integrity. Anyone able to rewrite them can rewrite
    `registry.py` beside them, and a hash list living in the same directory
    would not survive that either, so pins here buy nothing the package does not
    already have to guarantee. The project-side `truc.lock` pins bytes fetched
    from somewhere ELSE - a different question, and there a pin is now mandatory
    (`planner.plan_drift`).
    """
    lock = _HERE / "truc.lock"
    if lock.exists():
        data = json.loads(lock.read_text(encoding="utf-8"))
        files = data.get("files")
        if not files:
            raise ValueError(
                f"{lock} names no component files; truc will not fall back to "
                "compiling whatever is in components/")
        return [str((_HERE / f).resolve()) for f in files]
    return sorted(str(p) for p in (_HERE / "components").glob("*.rvl"))


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
        ir = compile_files(component_files())
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
