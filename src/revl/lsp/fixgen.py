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
from dataclasses import dataclass, fields, is_dataclass

from ..parser import ExprCall, ExprField, ExprVar, LetEffect, Parser, Program
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

def generate_fix(text: str, diagnostic: dict, filename: str = "<lsp>.rvl",
                 *, verify=None) -> Fix | None:
    """The shared engine: one diagnostic (an LSP ``Diagnostic``) + its source
    to a verified ``Fix``, or None when no safe mechanical rewrite applies.

    Dispatch is by diagnostic ``code``. A generator returns a *candidate* Fix;
    this function accepts it only if applying its edits makes the diagnostic go
    away (``_resolves``). That check is the honesty gate: a candidate that does
    not actually resolve the rejection is dropped, not returned.

    ``verify`` hands the patched text to the CALLER's checker instead of the
    editor's plain ``compute_diagnostics`` compile (issue #2684 review): the
    MCP repair path must verify under the same authoring profile and the same
    in-memory modules the check compiled with. It returns the patched
    program's diagnostics, or None when the patch could not be checked (the
    compile stopped before the refusal's line), which drops the candidate."""
    generator = _GENERATORS.get(diagnostic.get("code"))
    if generator is None:
        return None
    candidate = generator(text, diagnostic, filename)
    if candidate is None:
        return None
    patched = apply_edits(text, candidate.edits)
    if verify is not None:
        patched_diagnostics = verify(patched)
        if patched_diagnostics is None:
            return None
        if not _resolves_against(diagnostic, patched_diagnostics):
            return None
        return candidate
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
    to remain: this fix resolved *its* diagnostic, which is all it claims."""
    return _resolves_against(original, compute_diagnostics(patched, filename))


def _resolves_against(original: dict, diagnostics: list[dict]) -> bool:
    """``_resolves`` over diagnostics a caller already computed (see `verify`)."""
    original_code = original.get("code")
    original_line = original["range"]["start"]["line"]
    for diag in diagnostics:
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


_ZERO_ARG_SIZED_RECEIVER = re.compile(
    r"^builtin `(?P<method>[A-Za-z_]\w*)` needs a Str/Bytes/List receiver, "
    r"got `\(\) -> [^`]+`$"
)


def _fix_zero_arg_sized_receiver(text: str, diagnostic: dict,
                                 filename: str) -> Fix | None:
    """Call a bare zero-argument function used as a sized builtin receiver.

    The typechecker only emits this refusal for a known function value and its
    hint only names the fix when the function returns Str, Bytes or List. Keep
    the edit narrower still: parse the source and require the receiver to be a
    bare identifier in a method call on the diagnostic line. `_resolves` then
    checks the candidate against the actual program before returning it.
    """
    message = (diagnostic.get("message", "").splitlines() or [""])[0]
    match = _ZERO_ARG_SIZED_RECEIVER.match(message)
    if match is None:
        return None
    line0 = diagnostic["range"]["start"]["line"]
    method = match.group("method")
    program = _parse(text, filename)
    if program is None:
        return None
    receivers = []
    for node in _walk_nodes(program):
        if (isinstance(node, ExprCall) and node.line == line0 + 1
                and isinstance(node.callee, ExprField)
                and node.callee.name == method
                and isinstance(node.callee.target, ExprVar)):
            receivers.append(node.callee.target.name)
    if len(receivers) != 1:
        return None

    receiver = receivers[0]
    row = line_text(text, line0)
    receiver_before_method = re.compile(
        rf"(?<![A-Za-z0-9_])({re.escape(receiver)})"
        rf"(?=\s*\.\s*{re.escape(method)}\b)"
    )
    matches = list(receiver_before_method.finditer(row))
    if len(matches) != 1:
        return None
    end = matches[0].end(1)
    edit = _replace_edit(line0, end, end, "()")
    return Fix(
        diagnostic.get("code", "REVL"),
        f"Call `{receiver}()` to use its returned value as the receiver",
        [edit],
    )


def _dotted_target(expr) -> str | None:
    """The dotted name an acquisition expression calls, or None."""
    if isinstance(expr, ExprCall):
        expr = expr.callee
    if isinstance(expr, ExprVar):
        return expr.name
    if isinstance(expr, ExprField):
        head = _dotted_target(expr.target)
        return f"{head}.{expr.name}" if head else None
    return None


def _g4_site_undo(text: str, diagnostic: dict, filename: str) -> Fix | None:
    """G4: a mutation with no inverse; spell the inverse at the site.

    The one mechanical repair on a missing-undo refusal is the `site-undo`
    one: on the bound acquisition (`let h = effect open_h()`), append
    `undo close_h(h)` (or the host family's release, `undo h.close()`). The
    inverse is the DECLARED one, so the repair cannot invent a teardown the
    compiler does not already know: a host acquisition's release comes from
    the host-stub family table, and an extern's from its own declared `undo`
    slot. When the source spells no binding, or the inverse is not the
    single `result`/release shape, no candidate is offered; the prose repair
    stands. `generate_fix` still verifies the candidate by recompiling.

    The parser refuses a dotted acquisition (`Pool.open(...)`) while it parses,
    so the AST is unavailable for that shape; the lexical path reads the one
    line plus the extern declarations instead. The bare-name shape parses (the
    gate is deferred to lowering), so its AST path is the authoritative one;
    the line guard below still declines a multi-line acquisition rather than
    guessing where the call ends."""
    line0 = diagnostic["range"]["start"]["line"]
    row = line_text(text, line0)
    start = row.find("effect")
    if start < 0:
        return None
    # the whole acquisition must close on its own line: appending after a
    # continuation would cut the call in half
    tail = row[start:]
    if tail.count("(") != tail.count(")") or "//" in tail:
        return None
    program = _parse(text, filename)
    inverse = None
    if program is not None:
        node = None
        for candidate in _walk_nodes(program):
            if (isinstance(candidate, LetEffect) and candidate.line == line0 + 1
                    and candidate.undo is None):
                if node is not None:
                    return None        # ambiguous: more than one at this line
                node = candidate
        if node is not None:
            inverse = _site_inverse(node.bind, _dotted_target(node.acquire),
                                    program.externs)
    if inverse is None:
        inverse = _lexical_site_inverse(row, text)
    if inverse is None:
        return None
    end = len(row.rstrip())
    edit = _replace_edit(line0, end, end, f" undo {inverse}")
    return Fix("G4", f"Add the acquisition's inverse: `undo {inverse}`", [edit])


_SITE = re.compile(
    r"\beffect\s+(?:await\s+)?(?P<target>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
_BOUND = re.compile(r"\blet\s+(?P<bind>[A-Za-z_]\w*)\s*=\s*$")


def _lexical_site_inverse(row: str, text: str) -> str | None:
    """The inverse for a refused line whose program does not parse.

    Used for the parser's own domain (a dotted acquisition): the line is read
    for the binding and the call, and a bare extern's declared inverse for a
    dotted name comes from the extern declarations text, not the AST."""
    site = _SITE.search(row)
    if site is None:
        return None
    bound = _BOUND.search(row[:site.start()])
    if bound is None:
        return None
    bind, target = bound.group("bind"), site.group("target")
    from ..typecheck import _HOST_ACQUIRE_VERBS  # noqa: PLC0415
    release = _HOST_ACQUIRE_VERBS.get(target)
    if release is not None:
        return f"{bind}.{release}()"
    if "." in target:
        return None                 # a dotted call is not a bare extern
    for decl in _EXTERN_UNDO.finditer(text):
        if decl.group("name") == target:
            return f"{decl.group('inverse')}({bind})"
    return None


#: an extern declaration that names its inverse in the one site-spellable
#: shape, `undo close_h(result)`, over the acquired value only
_EXTERN_UNDO = re.compile(
    r"extern\s+\w+\s+fn\s+(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)[^\n]*?"
    r"\bundo\s+(?P<inverse>[A-Za-z_]\w*)\s*\(\s*result\s*\)")


def _site_inverse(bind: str | None, target: str | None,
                  externs) -> str | None:
    """The inverse expression a missing-undo site should spell, or None.

    Host stub families carry one release (`Pool.open` -> `close`, item 1859);
    a declared extern carries its own `undo` slot, written `undo close(result)`
    and therefore spelled at the site on the bound handle, `close(bind)`."""
    if target is None or bind is None:
        return None
    from ..typecheck import _HOST_ACQUIRE_VERBS  # noqa: PLC0415
    release = _HOST_ACQUIRE_VERBS.get(target)
    if release is not None:
        return f"{bind}.{release}()"
    if "." in target:
        return None                     # a dotted call is not a bare extern
    for decl in externs:
        if decl.name != target or decl.undo is None:
            continue
        undo = decl.undo
        if not isinstance(undo, ExprCall) or not isinstance(undo.callee, ExprVar):
            return None
        args = undo.args or []
        if (len(args) == 1 and isinstance(args[0], ExprVar)
                and args[0].name == "result"):
            return f"{undo.callee.name}({bind})"
        return None
    return None


_G4_RELEASE_HANDLE = re.compile(
    r"^the `undo` of `let (?P<bind>[A-Za-z_]\w*) = effect [^`]+` must release "
    r"THAT handle: write `undo (?P<inverse>[A-Za-z_][\w.]*\(\))`")
_TRAILING_UNDO = re.compile(
    r"\bundo\s+(?P<expr>[A-Za-z_][\w.]*\s*\([^()]*\))\s*$")


def _g4_release_handle(text: str, diagnostic: dict, filename: str) -> Fix | None:
    """G4: a host acquisition's `undo` that does not release the bound handle.

    The refusal names the one inverse in its message (write `undo
    store.drop()`), so the repair replaces the call after `undo` with it:
    `let store = effect Map.new() undo Map.drop()` becomes `... undo
    store.drop()`. Only the single-line shape is rewritten: the `let`, the
    acquisition and exactly one trailing `undo <call>` on the refused line.
    A multi-line acquisition, a second `undo` or a comment on the line
    declines."""
    first_line = (diagnostic.get("message", "").splitlines() or [""])[0]
    match = _G4_RELEASE_HANDLE.match(first_line)
    if match is None:
        return None
    line0 = diagnostic["range"]["start"]["line"]
    row = line_text(text, line0)
    if "//" in row or len(re.findall(r"\bundo\b", row)) != 1:
        return None
    site = re.search(rf"\blet\s+{re.escape(match.group('bind'))}\s*=\s*effect\b", row)
    trailing = _TRAILING_UNDO.search(row)
    if site is None or trailing is None or trailing.start() < site.end():
        return None
    acquire = row[site.end():trailing.start()]
    if acquire.count("(") != acquire.count(")"):
        return None
    inverse = match.group("inverse")
    start, end = trailing.span("expr")
    edit = _replace_edit(line0, start, end, inverse)
    return Fix("G4", f"Release the bound handle: `undo {inverse}`", [edit])


def _fix_g4(text: str, diagnostic: dict, filename: str) -> Fix | None:
    """G4 dispatch: the wrong-handle `undo` when the message names it, else
    the missing-undo site repair."""
    first_line = (diagnostic.get("message", "").splitlines() or [""])[0]
    if _G4_RELEASE_HANDLE.match(first_line):
        return _g4_release_handle(text, diagnostic, filename)
    return _g4_site_undo(text, diagnostic, filename)


_PROVIDER_EMISSION = re.compile(r"^expected fn, found 'emission'")


def _fix_syntax_provider_emission(text: str, diagnostic: dict,
                                  filename: str) -> Fix | None:
    """SYNTAX: `emission fn` inside a `provide` block.

    Provider methods are plain `fn`; emission-ness comes from the service
    declaration. The repair deletes the one `emission ` before `fn` on the
    refused line. Every other SYNTAX refusal (a top-level `emission fn`
    included) has a different message and gets nothing; a line with zero or
    several `emission fn` spellings declines."""
    first_line = (diagnostic.get("message", "").splitlines() or [""])[0]
    if not _PROVIDER_EMISSION.match(first_line):
        return None
    line0 = diagnostic["range"]["start"]["line"]
    row = line_text(text, line0)
    hits = list(re.finditer(r"\bemission\s+(?=fn\b)", row))
    if len(hits) != 1:
        return None
    start, end = hits[0].span()
    edit = _replace_edit(line0, start, end, "")
    return Fix("SYNTAX", "Provider methods are plain `fn`: drop `emission`", [edit])


def _walk_nodes(value, seen: set[int] | None = None):
    """Yield dataclass nodes in the parser tree without following cycles."""
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    if is_dataclass(value):
        yield value
        for descriptor in fields(value):
            yield from _walk_nodes(getattr(value, descriptor.name), seen)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_nodes(item, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk_nodes(item, seen)


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
    "G4": _fix_g4,
    "REVL": _fix_zero_arg_sized_receiver,
    "SYNTAX": _fix_syntax_provider_emission,
}


# --------------------------------------------------------------- agent payload

def fix_code(rejection: dict, source: str, filename: str = "<lsp>.rvl",
             *, diagnostic: dict | None = None, verify=None) -> dict:
    """The agent-facing front door: a structured rejection (``diagnostics.classify``)
    plus its source to an applyable ``fix_code`` payload.

    On success: ``{ok, code, title, edits}``, the same verified edits the LSP
    code-action carries. When no safe mechanical fix exists: ``{ok: False,
    code, reason}``; the prose ``fix`` from item 286 remains the guidance. The
    rejection carries a code and a line but no span, so the ranged diagnostic is
    recomputed from the source and matched to the rejection, then run through
    the one shared engine.

    ``diagnostic`` supplies that ranged diagnostic directly for a caller that
    must not recompile the text on its own (the MCP repair path, issue #2684
    review); ``verify`` is passed through to `generate_fix`, so the patched
    candidate is checked by the caller's profile-aware compiler."""
    code = rejection.get("code")
    if diagnostic is None:
        diagnostic = _match_diagnostic(source, rejection, filename)
    if diagnostic is None:
        return {"ok": False, "code": code,
                "reason": "the rejection was not reproduced from this source"}
    fix = generate_fix(source, diagnostic, filename, verify=verify)
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
