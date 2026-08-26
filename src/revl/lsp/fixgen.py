"""Applyable quick fixes (roadmap item 287): a diagnostic plus its source in,
a concrete, verified source edit out.

Item 286 gave a rejection its *prose* fix (``record["fix"]`` from ``FIXES``).
This turns that prose into a mechanical rewrite an agent or an editor can
apply — a text range and its replacement — for the diagnostics whose repair is
unambiguous. One engine, two front doors:

  * the LSP ``textDocument/codeAction`` (``analysis.compute_code_actions``);
  * the agent payload (``fix_code`` below), reusable by a later MCP verb.

The stance is CONSERVATIVE and HONEST. A generator only proposes a candidate
where the rewrite is well defined; the engine then *verifies* the candidate by
applying it and re-checking the patched source, and emits the edit only when
that resolves the diagnostic and introduces no worse one at the same site.
A diagnostic with no mechanical fix — or one whose candidate fails to verify —
yields no code-action, and the prose fix from 286 still stands. We never hand
back an edit that would not actually resolve the diagnostic: no-action beats a
wrong edit.

Every code below is proven end to end in ``tests/test_fixgen.py``: the
diagnostic, the generated edit, and a re-check of the patched source that shows
the rejection is gone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..parser import Parser, Program
from ..errors import RevlError
from .analysis import compute_diagnostics
from .document import find_symbol_column, line_text, lines_of


@dataclass
class Fix:
    """A verified quick fix: a human title and the edits that apply it.

    ``edits`` are LSP ``TextEdit`` dicts (``range`` + ``newText``), the shape
    both the code-action ``WorkspaceEdit`` and the agent ``fix_code`` payload
    carry, so the two front doors serialize the very same edit."""

    code: str
    title: str
    edits: list[dict]


# --------------------------------------------------------------- the engine

def generate_fix(text: str, diagnostic: dict, filename: str = "<lsp>.rvl") -> Fix | None:
    """The shared engine: one diagnostic (an LSP ``Diagnostic``) + its source
    to a verified ``Fix``, or None when no safe mechanical rewrite applies.

    Dispatch is by diagnostic ``code``. A generator returns a *candidate* Fix;
    this function accepts it only if applying its edits makes the diagnostic go
    away (``_resolves``). That check is the honesty gate — a candidate that does
    not actually resolve the rejection is dropped, not returned."""
    generator = _GENERATORS.get(diagnostic.get("code"))
    if generator is None:
        return None
    candidate = generator(text, diagnostic, filename)
    if candidate is None:
        return None
    patched = apply_edits(text, candidate.edits)
    if not _resolves(diagnostic, patched, filename):
        return None
    return candidate


def _resolves(original: dict, patched: str, filename: str) -> bool:
    """Whether the patched source actually resolves ``original``.

    The rejection is resolved when re-checking the patch no longer reports the
    same code on the same line. We additionally refuse a patch that trades the
    rejection for a syntax error (our edit broke the file) or for a fresh
    diagnostic on the edited line (a rewrite that only moved the problem). A
    genuinely separate, pre-existing rejection further down the file is allowed
    to remain — this fix resolved *its* diagnostic, which is all it claims."""
    original_code = original.get("code")
    original_line = original["range"]["start"]["line"]
    for diag in compute_diagnostics(patched, filename):
        code = diag.get("code")
        line = diag["range"]["start"]["line"]
        if code == original_code and line == original_line:
            return False  # the same rejection is still there
        if code in ("SYNTAX", "REVL") and line == original_line:
            return False  # the edit broke the line it touched
        if line == original_line:
            return False  # a different rejection now sits on the fixed site
    return True


# --------------------------------------------------------------- edit algebra

def apply_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP ``TextEdit``s to ``text``. Non-overlapping edits are applied
    from the end backwards so earlier offsets are never disturbed."""
    ordered = sorted(edits, key=lambda e: _offset(text, e["range"]["start"]), reverse=True)
    for edit in ordered:
        start = _offset(text, edit["range"]["start"])
        end = _offset(text, edit["range"]["end"])
        text = text[:start] + edit["newText"] + text[end:]
    return text


def _offset(text: str, position: dict) -> int:
    """Absolute character offset of a zero-based ``{line, character}``."""
    rows = lines_of(text)
    line = position["line"]
    base = sum(len(rows[i]) + 1 for i in range(min(line, len(rows))))
    return base + position["character"]


def _replace_edit(line0: int, start: int, end: int, new_text: str) -> dict:
    return {
        "range": {"start": {"line": line0, "character": start},
                  "end": {"line": line0, "character": end}},
        "newText": new_text,
    }


# --------------------------------------------------------------- generators

def _diag_token(text: str, diagnostic: dict) -> tuple[int, int, int, str] | None:
    """The source token a diagnostic points at, as ``(line0, start, end, s)``.

    The slice-1 diagnostic range is already tightened onto the named token, so
    this reads the covered substring back out of the source — the anchor every
    generator rewrites from."""
    span = diagnostic["range"]
    line0 = span["start"]["line"]
    if span["end"]["line"] != line0:
        return None
    start = span["start"]["character"]
    end = span["end"]["character"]
    row = line_text(text, line0)
    if start > len(row) or end > len(row):
        return None
    return line0, start, end, row[start:end]


def _fix_t2_null(text: str, diagnostic: dict, filename: str) -> Fix | None:
    """T2 — ``null`` has no type; absence is ``Opt[T]``.

    revl has no ``null``: ``None`` is the absent optional (it types as
    ``Opt[Any]``). The mechanical rewrite is to replace the ``null`` literal
    with ``None``. In an optional context this checks clean; in a non-optional
    one it becomes a type mismatch on the same line, which the engine's verify
    gate rejects — so this fires only where it genuinely resolves T2."""
    token = _diag_token(text, diagnostic)
    if token is None:
        return None
    line0, start, end, covered = token
    if covered != "null":
        # the range did not land on the literal (a fallback whole-line span);
        # locate `null` as a bare word on the diagnostic's line instead
        col = find_symbol_column(text, line0 + 1, "null")
        if col is None:
            return None
        start, end = col, col + len("null")
    edit = _replace_edit(line0, start, end, "None")
    return Fix("T2", "Replace `null` with `None` (absence is `Opt[T]`)", [edit])


_A9_MESSAGE = re.compile(
    r"^`(?P<key>[^`]+)` is not declared in the `provides` clause of (?P<comp>\w+)"
)


def _fix_a9_provide_key(text: str, diagnostic: dict, filename: str) -> Fix | None:
    """A9 — a provide block whose key is not in the component's ``provides``.

    When the component declares exactly one provision key, the provide block can
    only have meant that key: rename the block's key to the declared one. With
    zero or several declared keys the intended target is ambiguous, so we emit
    nothing (the prose fix still names both repairs). The verify gate confirms
    the renamed block matches that key's service before the fix is offered."""
    first_line = (diagnostic.get("message", "").splitlines() or [""])[0]
    match = _A9_MESSAGE.match(first_line)
    if match is None:
        return None
    wrong_key, comp_name = match.group("key"), match.group("comp")
    declared = _sole_provides_key(text, filename, comp_name)
    if declared is None or declared == wrong_key:
        return None
    token = _diag_token(text, diagnostic)
    if token is None or token[3] != wrong_key:
        return None
    line0, start, end, _ = token
    edit = _replace_edit(line0, start, end, declared)
    return Fix("A9", f"Rename provide block to the declared key `{declared}`", [edit])


def _sole_provides_key(text: str, filename: str, comp_name: str) -> str | None:
    """The single key a named component's ``provides`` clause declares, or None
    when it declares none or several (nothing unambiguous to rename to)."""
    program = _parse(text, filename)
    if program is None:
        return None
    for comp in program.components:
        if comp.name == comp_name:
            keys = [key for key, _svc, _line in comp.provides]
            return keys[0] if len(keys) == 1 else None
    return None


def _parse(text: str, filename: str) -> Program | None:
    try:
        return Parser(text, filename).parse()
    except RevlError:
        return None


# code -> its generator. Adding a code is adding one entry here plus its
# generator and a proof in tests/test_fixgen.py; the engine and both front
# doors need no change.
_GENERATORS = {
    "T2": _fix_t2_null,
    "A9": _fix_a9_provide_key,
}


# --------------------------------------------------------------- agent payload

def fix_code(rejection: dict, source: str, filename: str = "<lsp>.rvl") -> dict:
    """The agent-facing front door: a structured rejection (``diagnostics.classify``)
    plus its source to an applyable ``fix_code`` payload.

    On success: ``{ok, code, title, edits}`` — the same verified edits the LSP
    code-action carries. When no safe mechanical fix exists: ``{ok: False,
    code, reason}``; the prose ``fix`` from item 286 remains the guidance. The
    rejection carries a code and a line but no span, so the ranged diagnostic is
    recomputed from the source and matched to the rejection, then run through
    the one shared engine."""
    code = rejection.get("code")
    diagnostic = _match_diagnostic(source, rejection, filename)
    if diagnostic is None:
        return {"ok": False, "code": code,
                "reason": "the rejection was not reproduced from this source"}
    fix = generate_fix(source, diagnostic, filename)
    if fix is None:
        return {"ok": False, "code": code, "reason": "no mechanical fix for this diagnostic"}
    return {"ok": True, "code": fix.code, "title": fix.title, "edits": fix.edits}


def _match_diagnostic(source: str, rejection: dict, filename: str) -> dict | None:
    """The recomputed LSP diagnostic that corresponds to a rejection record,
    matched by code and one-based line."""
    code = rejection.get("code")
    line0 = (rejection.get("line") or 0) - 1
    for diag in compute_diagnostics(source, filename):
        if diag.get("code") == code and diag["range"]["start"]["line"] == line0:
            return diag
    return None
