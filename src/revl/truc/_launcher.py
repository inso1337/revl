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
    """truc's own composition file list. Stage 0 reads it from truc's committed
    `truc.lock` (§7); absent or unreadable, it falls back to a sorted glob of
    `components/` so a checkout is never wedged by a stale lock."""
    lock = _HERE / "truc.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            files = data.get("files")
            if files:
                return [str((_HERE / f).resolve()) for f in files]
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(str(p) for p in (_HERE / "components").glob("*.rvl"))


def main(argv: list[str] | None = None) -> int:
    from revl.compiler import compile_files  # noqa: PLC0415 — lazy, pulls cordis
    from revl.errors import RevlError  # noqa: PLC0415
    from revl.mcp.session import Session, SessionError  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)

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
