"""The reference analysis worker the rust language server drives.

Slice 1 of roadmap item 336 owns the LSP protocol in rust and the ANALYSIS in
the reference front end (design `docs/design/336-native-single-binary-tooling.md`,
A1): a native checker outside the self-host frontier returns no diagnostic where
the reference refuses, which in an editor is a silent false-admit. So the rust
binary computes nothing about a document itself; it asks this worker, which
calls exactly the functions `python -m revl.lsp` calls, and forwards the answer
verbatim.

The protocol is one JSON object per line in, one JSON object per line out, in
order:

    {"op": "diagnostics", "text": ..., "filename": ...}
      -> {"ok": true, "result": [Diagnostic, ...]}
    {"op": "hover", "text": ..., "filename": ..., "line": N, "character": N}
      -> {"ok": true, "result": Hover | null}
    {"op": "definition", "text": ..., "filename": ..., "uri": ..., "line": N,
     "character": N}
      -> {"ok": true, "result": Location | null}
    {"op": "codeAction", "text": ..., "filename": ..., "uri": ..., "range": R}
      -> {"ok": true, "result": [CodeAction, ...]}
    {"op": "version"}
      -> {"ok": true, "result": {"language": ..., "python": ...}}

Any failure answers `{"ok": false, "error": "..."}` rather than a plausible
empty result: the server turns that into a visible engine diagnostic, never
into a clean document (the missing-squiggle rule).

The worker is embedded in the rust binary as a string and run with `-c`, so it
has no install step and no path to resolve at runtime.
"""

from __future__ import annotations

import json
import sys


def _dispatch(request: dict):
    from revl.lsp.analysis import (
        compute_code_actions,
        compute_definition,
        compute_diagnostics,
        compute_hover,
    )
    from revl.lsp.document import Position

    op = request.get("op")
    text = request.get("text", "")
    filename = request.get("filename", "<lsp>.rvl")

    if op == "diagnostics":
        return compute_diagnostics(text, filename)
    if op == "hover":
        position = Position(request.get("line", 0), request.get("character", 0))
        return compute_hover(text, position, filename)
    if op == "definition":
        position = Position(request.get("line", 0), request.get("character", 0))
        return compute_definition(text, request.get("uri"), position, filename)
    if op == "codeAction":
        empty = {"start": {"line": 0, "character": 0},
                 "end": {"line": 0, "character": 0}}
        return compute_code_actions(text, request.get("uri"),
                                    request.get("range") or empty, filename)
    if op == "version":
        return {"language": _language_version(), "python": sys.version.split()[0]}
    raise ValueError(f"unknown op: {op!r}")


def _language_version() -> str:
    """The reference `revl` version this engine analyses with, so a client can
    detect a stale binary/engine pair (design: skew is made detectable, not
    solved)."""
    try:
        from importlib.metadata import version

        return version("revl")
    except Exception:
        return "unknown"


def main() -> int:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = {"ok": True, "result": _dispatch(json.loads(line))}
        except BaseException as exc:  # noqa: BLE001 - report, never guess a result
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(payload))
        out.write("\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
