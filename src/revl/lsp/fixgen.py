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
    "G4": _g4_site_undo,
    "REVL": _fix_zero_arg_sized_receiver,
}


# ----------------------------------------------------------- candidate repairs

@dataclass
class Repair:
    """One verified way out of a refusal (issue #2759).

    The sibling of `Fix`: where `fix` is the single quick fix an editor applies,
    a `Repair` is one candidate in the verified list a capability or admission
    refusal carries. ``kind`` is the closed vocabulary (`diagnostics.REPAIR_KINDS`);
    ``action`` is the one sentence a person or a model reads; ``edits`` are LSP
    ``TextEdit`` dicts that this engine has applied to the source and re-checked,
    so every entry it returns demonstrably clears the refusal and introduces no
    new one. ``widens_authority`` marks a candidate that enlarges the authority
    the source DECLARES: it is never returned unless the caller asks for widening
    candidates, and it is never ordered ahead of a narrowing one.

    The record shape is #2684's: an entry names its `kind` and `action`, and
    carries the verified edit under `fix_code`, so one `repairs` list vocabulary
    serves both slices."""

    kind: str
    action: str
    edits: list[dict]
    widens_authority: bool = False

    def as_dict(self) -> dict:
        """The record shape a `diagnostics.report` `repairs` entry carries.

        `kind` and `action` are the same pair issue #2684's G4 entries use, so
        one list vocabulary serves both. `fix_code` holds the verified edit in
        the same `{title, edits}` shape a G4 `site-undo` entry carries, and
        `widens_authority` is the authority half this slice adds."""
        return {"kind": self.kind, "action": self.action,
                "widens_authority": bool(self.widens_authority),
                "fix_code": {"title": self.action, "edits": self.edits}}


class CompileBudget:
    """How many verification compiles a caller may still spend.

    Each candidate check is a full compile, so `diagnostics.report` makes ONE
    budget per call and hands it to every record's `generate_repairs`: a report
    with many refusals then spends at most that many compiles in all, not that
    many per record (issue #2759 review)."""

    def __init__(self, compiles: int) -> None:
        self.remaining = compiles

    def take(self) -> bool:
        """Spend one compile, or say there is none left."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def generate_repairs(text: str, diagnostic: dict,
                     filename: str = "<lsp>.rvl", *,
                     widens_authority: bool = False, verify=None,
                     baseline: list[tuple[str, int]] | None = None,
                     limit: int = 8,
                     budget: CompileBudget | None = None) -> list[Repair]:
    """Every verified way out of one admission refusal, in a fixed order.

    One candidate generator per covered code (see ``_REPAIR_GENERATORS``) emits
    *seeds*; this engine is the judge. Each seed is applied to ``text`` and the
    patched source is re-checked, exactly as `generate_fix` does for its single
    fix. A seed survives only when the patched compile no longer reports the
    refusal AND reports no diagnostic the refused compile did not already
    report: a candidate that trades one refusal for another is dropped, not
    returned.

    ``baseline`` is the refused compile's own ``(code, zero-based line)`` set
    (``diagnostics.report`` passes its records here), so a pre-existing, separate
    rejection elsewhere in the file does not disqualify a candidate that really
    cleared THIS one. From a caller that holds only one diagnostic, the default
    is that diagnostic, so any other diagnostic counts as a new refusal.

    ``widens_authority`` opts in to the candidates that enlarge the declared
    authority. They are omitted otherwise, which is the whole point: nothing
    here suggests widening authority by default. At most ``limit`` candidates
    are re-checked (each check is a full compile, so the cap is a cost bound as
    well); a shared ``budget`` bounds the compiles across several calls, and a
    candidate past it is not checked and not returned. Survivors are ordered narrowest authority first, then smallest
    edit, then kind, so the list is deterministic and a learned ordering can
    only reorder these verified entries later (issue #2759)."""
    code = diagnostic.get("code")
    generator = _REPAIR_GENERATORS.get(code)
    if generator is None or not covers_repairs(diagnostic):
        return []
    target = (code, diagnostic["range"]["start"]["line"])
    known = {(target[0], target[1])}
    known.update(baseline or ())
    survivors: list[Repair] = []
    seen: set[tuple] = set()
    seeds = [candidate for candidate in generator(text, diagnostic, filename) or ()
             if candidate.edits
             and (widens_authority or not candidate.widens_authority)]
    for candidate in seeds[:limit]:
        signature = (candidate.kind, candidate.widens_authority,
                     tuple(_edit_signature(edit) for edit in candidate.edits))
        if signature in seen:
            continue
        seen.add(signature)
        if budget is not None and not budget.take():
            break
        patched = apply_edits(text, candidate.edits)
        if verify is not None:
            patched_diagnostics = verify(patched)
            if patched_diagnostics is None:
                continue
        else:
            patched_diagnostics = compute_diagnostics(patched, filename)
        if not _repair_clears(target, known, patched_diagnostics,
                              candidate.edits):
            continue
        survivors.append(candidate)
    survivors.sort(key=lambda repair: (repair.widens_authority,
                                       _edit_weight(text, repair.edits),
                                       repair.kind))
    return survivors[:limit]


def _edit_signature(edit: dict) -> tuple:
    span = edit["range"]
    return (span["start"]["line"], span["start"]["character"],
            span["end"]["line"], span["end"]["character"], edit["newText"])


def _edit_weight(text: str, edits: list[dict]) -> int:
    """How much text the candidate rewrites: inserted characters plus replaced
    span. The fixed ordering's "smaller edit first" key."""
    weight = 0
    for edit in edits:
        weight += len(edit["newText"])
        weight += abs(_offset(text, edit["range"]["end"])
                      - _offset(text, edit["range"]["start"]))
    return weight


def _repair_clears(target: tuple[str, int], known: set[tuple[str, int]],
                   patched: list[dict], edits: list[dict] = ()) -> bool:
    """Whether a patched compile clears `target` and adds no new refusal.

    `known` already holds the refused compile's diagnostics (and `target`), so a
    patched diagnostic outside it is a refusal the candidate introduced. Both
    are moved to the patched file's line numbers first: an edit that deletes
    or adds lines shifts every record below it, and a shifted pre-existing
    record is not a new one."""
    moved = {(code, _patched_line(edits, line)) for code, line in known
             if (code, line) != target}
    for diag in patched:
        pair = (diag.get("code"), diag["range"]["start"]["line"])
        if pair not in moved:
            return False                 # the refusal itself, or a new one
    return True


def _patched_line(edits, line: int) -> int:
    """Where original zero-based `line` sits after `edits`: each edit ending on
    or above it moves it by the lines that edit adds minus the lines it spans."""
    shift = 0
    for edit in edits:
        span = edit["range"]
        if span["end"]["line"] <= line:
            shift += edit["newText"].count("\n") - (
                span["end"]["line"] - span["start"]["line"])
    return line + shift


_REQUIREMENT_MESSAGE = re.compile(
    r"^`(?P<key>[^`]+)` is not a declared requirement of (?P<comp>\w+)$")

_G2_CONFLICT_MESSAGE = re.compile(
    r"^provision conflict: key `(?P<key>[^`]+)`(?: in realm `[^`]+`)? "
    r"is provided by both \w+ and (?P<second>\w+) \(G2\)$")

_R2_UNGRANTED_MESSAGE = re.compile(
    r"^admission refused: component `(?P<comp>[^`]+)` reaches service "
    r"`(?P<service>[^`]+)` \(via `requires (?P<key>[^`]+)`\)")

_WHOLE_STATEMENT_CALL = re.compile(
    r"^(?:let\s+[A-Za-z_]\w*\s*=\s*)?"
    r"[A-Za-z_]\w*\.[A-Za-z_]\w*\((?:[^()]|\([^()]*\))*\)$")

#: code -> the message shape its generator understands. A code is repairable in
#: some failure modes and not others (G1 `binding`, G2 `admission`), so the
#: message shape, not the code alone, decides whether a record is covered.
_REPAIR_SHAPES = {
    "G1": _REQUIREMENT_MESSAGE,
    "G2": _G2_CONFLICT_MESSAGE,
    "R2": _R2_UNGRANTED_MESSAGE,
}


def covers_repairs(diagnostic: dict) -> bool:
    """Whether the repair engine understands this refusal's failure mode.

    `diagnostics.report` asks this before it attaches a list, so a covered code
    in an uncovered mode (G1 `binding`, G2 `differs from the running manifest`)
    keeps the record it always had rather than an empty list that would read as
    "checked, nothing found"."""
    shape = _REPAIR_SHAPES.get(diagnostic.get("code"))
    if shape is None:
        return False
    lines = (diagnostic.get("message") or "").splitlines()
    return bool(lines) and shape.match(lines[0]) is not None


def _g1_requirement_repairs(text: str, diagnostic: dict,
                            filename: str) -> list[Repair]:
    """G1, ``requirement`` mode: a body reads or calls a key its component does
    not declare. Two ways out, and the split is a question of authority:

    * `add-requires` names the declaration the body already relied on. It is
      always a WIDENING candidate: a new `requires` row enlarges the authority
      the component declares, whether a sibling component or an ambient
      provider satisfies it, so it is returned only on opt-in.
    * `remove-call` drops the declaration that reaches the undeclared key. It is
      offered only when the line is a whole statement (`let x = key.m()`, or a
      bare call line), so the deletion is a syntactic statement rather than a
      guess; the verify gate then checks the removal did not leave a binding
      dangling or a hole in the body."""
    lines = (diagnostic.get("message") or "").splitlines()
    match = _REQUIREMENT_MESSAGE.match(lines[0]) if lines else None
    if match is None:
        return []
    key, comp_name = match.group("key"), match.group("comp")
    program = _parse(text, filename)
    if program is None:
        return []
    component = next((c for c in program.components if c.name == comp_name), None)
    if component is None:
        return []
    repairs: list[Repair] = []
    for service, _internal in _services_for_key(program, key):
        edit = _add_requires_edit(text, component, key, service)
        if edit is None:
            continue
        repairs.append(Repair(
            "add-requires",
            f"Declare `{key}: {service}` in {comp_name}'s `requires` clause",
            [edit], widens_authority=True))
    edit = _remove_call_edit(text, diagnostic)
    if edit is not None:
        repairs.append(Repair(
            "remove-call",
            f"Remove the statement that reaches `{key}`",
            [edit]))
    return repairs


def _services_for_key(program: Program, key: str) -> list[tuple[str, bool]]:
    """The services `key` could be declared with, as ``(service, internal)``.

    An internal service is one some component in this program provides under
    `key`; otherwise the same-named declared service is the interface the
    ambient provider satisfies. Each is verified before it is offered, so a
    wrong guess is dropped rather than handed back."""
    found: list[tuple[str, bool]] = []
    for comp in program.components:
        for provided, service, _line in comp.provides:
            if provided == key and (service, True) not in found:
                found.append((service, True))
    internal = {service for service, _internal in found}
    for service in program.services:
        if service.name.lower() == key.lower() and service.name not in internal:
            found.append((service.name, False))
            internal.add(service.name)
    return found


def _add_requires_edit(text: str, component, key: str, service: str) -> dict | None:
    """Insert `key: service` into the component header's `requires` clause."""
    if any(existing == key for existing, _service, _line in component.requires):
        return None
    line0 = component.line - 1
    row = line_text(text, line0)
    header = re.search(rf"\bcomponent\s+{re.escape(component.name)}\b", row)
    if header is None:
        return None
    if component.requires:
        clause = re.search(r"\brequires\b", row)
        if clause is None:
            return None
        boundary = re.search(r"\bprovides\b|\{", row[clause.end():])
        if boundary is None:
            return None
        end = clause.end() + boundary.start()
        while end > clause.end() and row[end - 1] == " ":
            end -= 1
        return _replace_edit(line0, end, end, f", {key}: {service}")
    return _replace_edit(line0, header.end(), header.end(),
                         f" requires {key}: {service}")


def _remove_call_edit(text: str, diagnostic: dict) -> dict | None:
    """Delete the line holding the refused call, when it is the whole statement."""
    line0 = diagnostic["range"]["start"]["line"]
    row = line_text(text, line0)
    if _WHOLE_STATEMENT_CALL.match(row.strip()) is None:
        return None
    rows = lines_of(text)
    if line0 + 1 < len(rows):
        return {"range": {"start": {"line": line0, "character": 0},
                          "end": {"line": line0 + 1, "character": 0}},
                "newText": ""}
    return {"range": {"start": {"line": line0, "character": 0},
                      "end": {"line": line0, "character": len(row)}},
            "newText": ""}


def _g2_conflict_repairs(text: str, diagnostic: dict,
                         filename: str) -> list[Repair]:
    """G2 provision conflict: two components install the same key (in the same
    realm). The mechanical way out is to rename ONE provider's key to a fresh
    one, so the other provider stays the provider of the contested key.

    Verified means the refusal is cleared and the program compiles; it does NOT
    promise the rename preserves behaviour. A call site that reached the
    renamed provider through the original key still binds that key, so it now
    reaches the surviving provider. Consumers of the original key keep working,
    which is why the candidate can verify; the author chooses between the two
    providers by deciding which one keeps the key."""
    lines = (diagnostic.get("message") or "").splitlines()
    match = _G2_CONFLICT_MESSAGE.match(lines[0]) if lines else None
    if match is None:
        return []
    key, second = match.group("key"), match.group("second")
    program = _parse(text, filename)
    if program is None:
        return []
    component = next((c for c in program.components if c.name == second), None)
    if component is None:
        return []
    provided = [k for c in program.components for k, _service, _line in c.provides]
    new_key = _fresh_provision_key(key, provided)
    edits = _rename_provides_edits(text, component, key, new_key)
    if not edits:
        return []
    return [Repair("rename-key",
                   f"Rename {second}'s provision `{key}` to `{new_key}`",
                   edits)]


def _fresh_provision_key(base: str, taken: list[str]) -> str:
    """The first `base_vN` no component in this program provides."""
    suffix = 2
    while f"{base}_v{suffix}" in taken:
        suffix += 1
    return f"{base}_v{suffix}"


def _rename_provides_edits(text: str, component, key: str,
                           new_key: str) -> list[dict]:
    """Rewrite one component's `provides key: Svc`, its `provide key {` block,
    and any `isolate key in realm(...)` that pins that provision."""
    entry = next(((k, service) for k, service, _line in component.provides
                  if k == key), None)
    if entry is None:
        return []
    service = entry[1]
    line0 = component.line - 1
    row = line_text(text, line0)
    matches = list(re.finditer(rf"\b{re.escape(key)}\s*:\s*{re.escape(service)}\b",
                               row))
    if len(matches) != 1:
        return []
    span = matches[0]
    edits = [_replace_edit(line0, span.start(), span.end(),
                           f"{new_key}: {service}")]
    from ..parser import IsolateStmt, ProvideStmt  # noqa: PLC0415

    nodes = list(_walk_nodes(component.body))
    blocks = [node for node in nodes
              if isinstance(node, ProvideStmt) and node.key == key]
    if len(blocks) != 1:
        return []
    block_line0 = blocks[0].line - 1
    block_row = line_text(text, block_line0)
    block = re.search(rf"\bprovide\s+{re.escape(key)}\b", block_row)
    if block is None:
        return []
    start = block.end() - len(key)
    edits.append(_replace_edit(block_line0, start, start + len(key), new_key))
    # a provision pinned to a realm names the key again (`isolate s in
    # realm("r")`); leaving it behind would rename the provision out from under
    # its own placement, so the rename covers it or the candidate is dropped.
    for node in nodes:
        if not isinstance(node, IsolateStmt) or node.key != key:
            continue
        isolate_line0 = node.line - 1
        isolate_row = line_text(text, isolate_line0)
        isolate = re.search(rf"\bisolate\s+{re.escape(key)}\b", isolate_row)
        if isolate is None:
            return []
        isolate_start = isolate.end() - len(key)
        edits.append(_replace_edit(isolate_line0, isolate_start,
                                   isolate_start + len(key), new_key))
    return edits


def _r2_ungranted_repairs(text: str, diagnostic: dict,
                          filename: str) -> list[Repair]:
    """R2, the untrusted-author reach allowlist: a `requires` row binds a
    service the operator did not grant. The author-enactable way out that keeps
    the call sites is `route-via-granted-service`: bind the same key to a
    granted service instead. One candidate per granted service is emitted and
    the verify gate drops every one whose interface does not satisfy the call
    sites, so this never hands back an edit the compiler would refuse.

    The granted set comes from the refusal's own `navigate` record, which
    already carries it (`_granted_navigate`); it is the author's own contract,
    never a reconstruction of the operator's policy."""
    lines = (diagnostic.get("message") or "").splitlines()
    match = _R2_UNGRANTED_MESSAGE.match(lines[0]) if lines else None
    if match is None:
        return []
    comp_name = match.group("comp")
    service = match.group("service")
    key = match.group("key")
    navigate = diagnostic.get("navigate") or {}
    granted = (navigate.get("refused") or {}).get("granted") or ()
    program = _parse(text, filename)
    if program is None:
        return []
    component = next((c for c in program.components if c.name == comp_name), None)
    if component is None:
        return []
    entry = next(((k, svc) for k, svc, _line in component.requires if k == key), None)
    if entry is None or entry[1] != service:
        return []
    line0 = component.line - 1
    row = line_text(text, line0)
    matches = list(re.finditer(rf"\b{re.escape(key)}\s*:\s*{re.escape(service)}\b",
                               row))
    if len(matches) != 1:
        return []
    span = matches[0]
    repairs = []
    for alternative in granted:
        if alternative == service:
            continue
        repairs.append(Repair(
            "route-via-granted-service",
            f"Bind `{key}` to the granted service `{alternative}` instead",
            [_replace_edit(line0, span.start(), span.end(),
                           f"{key}: {alternative}")]))
    return repairs


# code -> the generator of its candidate repairs. A code absent here carries no
# mechanical candidate, and its prose `fix` (item 286) still stands.
_REPAIR_GENERATORS = {
    "G1": _g1_requirement_repairs,
    "G2": _g2_conflict_repairs,
    "R2": _r2_ungranted_repairs,
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
