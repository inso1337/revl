"""The compiler surfaces, projected for a human editor.

Slice 1 reuses the exact machinery `revl` already runs — nothing here
re-implements a check, a message, or a symbol table:

  * diagnostics come from `compile_source` (the CLI's own entry point) run to
    its first rejection, each `RevlError` mapped through `diagnostics.classify`;
  * hover text for a diagnostic is `diagnostics.explain` — the same GUARANTEES
    and FIXES `revl explain` prints;
  * hover and go-to-definition over declared names read the parser's AST.

The module deals only in plain data (dicts and small dataclasses). The wire
protocol lives in `protocol`/`server`; keeping the analysis pure makes every
capability callable — and assertable — from a unit test without a socket.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..compiler import compile_source
from ..diagnostics import classify, explain
from ..errors import RevlError
from ..parser import (
    ExternDecl,
    FnDecl,
    LetPatternStmt,
    LetStmt,
    Parser,
    Program,
    TypeDecl,
)
from .document import Position, find_symbol_column, word_at

# LSP DiagnosticSeverity
_SEVERITY = {"error": 1, "warning": 2, "information": 3, "hint": 4}


# ---------------------------------------------------------------- diagnostics

def compute_diagnostics(text: str, filename: str = "<lsp>.rvl") -> list[dict]:
    """The document's diagnostics as LSP `Diagnostic` objects.

    The checker stops at its first rejection, so slice 1 publishes at most one
    diagnostic — the same one `revl compile` would print for this source. On a
    clean compile the list is empty, which is how the client clears stale
    squiggles.
    """
    try:
        compile_source(text, filename)
    except RevlError as error:
        return [_diagnostic_from(text, error)]
    return []


def _diagnostic_from(text: str, error: RevlError) -> dict:
    """One `RevlError` as an LSP Diagnostic, its structured code and category
    supplied by `diagnostics.classify` (not re-derived here)."""
    record = classify(error)
    return {
        "range": _range_for(text, error),
        "severity": _SEVERITY.get(record.get("severity", "error"), 1),
        "code": record.get("code", "REVL"),
        "source": "revl",
        "message": _message_of(record),
    }


def _message_of(record: dict) -> str:
    message = record.get("message", "")
    hint = record.get("hint")
    return f"{message}\n{hint}" if hint else message


def _range_for(text: str, error: RevlError) -> dict:
    """A range for the rejection. A `RevlError` carries a one-based line but no
    column, so tighten onto the token the message names in backticks when it
    can be found on that line, else cover the whole trimmed line."""
    line0 = max(error.line - 1, 0)
    named = _first_backticked(error.message)
    if named is not None:
        col = find_symbol_column(text, error.line, named)
        if col is not None:
            return _span(line0, col, col + len(named))
    return _whole_line_span(text, line0)


def _first_backticked(message: str) -> str | None:
    start = message.find("`")
    if start < 0:
        return None
    end = message.find("`", start + 1)
    if end < 0:
        return None
    token = message[start + 1:end]
    # only worth locating when it is a bare identifier; a quoted phrase or type
    # would not match as a whole word on the source line
    return token if token and all(c.isalnum() or c == "_" for c in token) else None


def _whole_line_span(text: str, line0: int) -> dict:
    from .document import line_text

    row = line_text(text, line0)
    start = len(row) - len(row.lstrip())
    end = len(row) if row.strip() else start + 1
    return _span(line0, start, end)


def _span(line: int, start_char: int, end_char: int) -> dict:
    return {
        "start": {"line": line, "character": start_char},
        "end": {"line": line, "character": end_char},
    }


# ---------------------------------------------------------------- code actions

def compute_code_actions(text: str, uri: str, lsp_range: dict,
                         filename: str = "<lsp>.rvl") -> list[dict]:
    """Quick fixes for the diagnostics that overlap an editor's requested range.

    Each fixable diagnostic (see `fixgen`) becomes an LSP `CodeAction` of kind
    `quickfix` carrying a `WorkspaceEdit` against this document. A diagnostic
    with no safe mechanical rewrite yields no action, so the list is only the
    fixes the engine could verify. Recomputed from the text, matching slice 1's
    stateless stance — the client's `context.diagnostics` is not required."""
    from .fixgen import generate_fix

    actions: list[dict] = []
    for diag in compute_diagnostics(text, filename):
        if not _ranges_overlap(diag["range"], lsp_range):
            continue
        fix = generate_fix(text, diag, filename)
        if fix is None:
            continue
        actions.append({
            "title": fix.title,
            "kind": "quickfix",
            "diagnostics": [diag],
            "edit": {"changes": {uri: fix.edits}},
        })
    return actions


def _ranges_overlap(a: dict, b: dict) -> bool:
    """Whether two LSP ranges intersect, comparing by (line, character).

    Endpoints touch inclusively so an editor's zero-width cursor sitting on
    either edge of a diagnostic still surfaces its fix: overlap holds unless one
    range ends strictly before the other begins."""
    def point(p: dict) -> tuple[int, int]:
        return (p["line"], p["character"])

    return not (point(a["end"]) < point(b["start"]) or point(b["end"]) < point(a["start"]))


# ---------------------------------------------------------------- symbols

@dataclass
class Symbol:
    """A declared name the editor can hover or jump to."""
    name: str
    line: int          # one-based, the declaration site
    kind: str          # fn | extern | type | service | component | param | let
    detail: str        # the signature/type shown on hover


@dataclass
class _Scope:
    start: int         # one-based, inclusive
    end: int           # one-based, exclusive (next top-level decl, or 1<<30)
    locals: dict[str, Symbol]


class SymbolTable:
    """Declared names from one parsed program, keyed for resolution by name and
    cursor line. Globals are the module's declarations; each fn/component adds a
    lexical scope of parameters and `let` bindings over its span."""

    def __init__(self, globals_: dict[str, Symbol], scopes: list[_Scope]) -> None:
        self._globals = globals_
        self._scopes = scopes

    def resolve(self, name: str, at_line: int) -> Symbol | None:
        """The declaration `name` refers to at a one-based line — the innermost
        enclosing scope wins, falling back to a module-level declaration."""
        best: Symbol | None = None
        best_start = -1
        for scope in self._scopes:
            if scope.start <= at_line < scope.end and name in scope.locals:
                if scope.start > best_start:
                    best, best_start = scope.locals[name], scope.start
        return best or self._globals.get(name)


def build_symbols(text: str, filename: str = "<lsp>.rvl") -> SymbolTable:
    """Parse the document and collect its declarations. A parse failure yields
    an empty table rather than raising: diagnostics report the syntax error,
    while hover and definition simply resolve nothing."""
    try:
        program = Parser(text, filename).parse()
    except RevlError:
        return SymbolTable({}, [])
    return _collect(program)


def _collect(program: Program) -> SymbolTable:
    globals_: dict[str, Symbol] = {}
    starts: list[int] = []  # top-level decl lines, to bound scope spans

    for fn in program.fn_decls:
        globals_[fn.name] = Symbol(fn.name, fn.line, "fn", _fn_signature(fn))
        starts.append(fn.line)
    for ext in program.externs:
        globals_[ext.name] = Symbol(ext.name, ext.line, "extern", _extern_signature(ext))
        starts.append(ext.line)
    for td in program.type_decls:
        globals_[td.name] = Symbol(td.name, td.line, "type", _type_signature(td))
        starts.append(td.line)
    for svc in program.services:
        globals_[svc.name] = Symbol(svc.name, svc.line, "service", f"service {svc.name}")
        starts.append(svc.line)
    for comp in program.components:
        globals_[comp.name] = Symbol(comp.name, comp.line, "component", f"component {comp.name}")
        starts.append(comp.line)

    scopes = _scopes_of(program, sorted(starts))
    return SymbolTable(globals_, scopes)


def _scopes_of(program: Program, starts: list[int]) -> list[_Scope]:
    """A lexical scope per fn/component: parameters (or config/requires) plus
    every `let` binding in the body, spanning from the declaration line to the
    next top-level declaration."""
    scopes: list[_Scope] = []
    for fn in program.fn_decls:
        locals_ = {p.name: Symbol(p.name, p.line, "param", f"{p.name}: {p.type}")
                   for p in fn.params}
        _add_lets(fn.body, locals_)
        scopes.append(_Scope(fn.line, _next_after(fn.line, starts), locals_))
    for comp in program.components:
        locals_: dict[str, Symbol] = {}
        for field in comp.config:
            locals_[field.name] = Symbol(field.name, field.line, "param",
                                         f"{field.name}: {field.type}")
        for local, service, line in comp.requires:
            locals_[local] = Symbol(local, line, "param", f"{local}: {service}")
        _add_lets(comp.body, locals_)
        scopes.append(_Scope(comp.line, _next_after(comp.line, starts), locals_))
    return scopes


def _next_after(line: int, starts: list[int]) -> int:
    for start in starts:
        if start > line:
            return start
    return 1 << 30


def _add_lets(body, locals_: dict[str, Symbol]) -> None:
    """Record every `let` binding reachable in a statement body. A first
    binding wins so the declaration site is the one hover and definition land
    on, not a later shadow."""
    for stmt in _walk_lets(body):
        if isinstance(stmt, LetStmt):
            if stmt.name not in locals_:
                detail = f"let {stmt.name}: {stmt.type}" if stmt.type else f"let {stmt.name}"
                locals_[stmt.name] = Symbol(stmt.name, stmt.line, "let", detail)
        elif isinstance(stmt, LetPatternStmt):
            for name in _pattern_names(stmt.pattern):
                locals_.setdefault(name, Symbol(name, stmt.line, "let", f"let {name}"))


def _walk_lets(node):
    """Yield every LetStmt/LetPatternStmt anywhere under a statement body,
    descending into nested blocks generically so a `let` inside an `if` or
    `for` is still in scope. The AST carries no columns, so this is name-and-
    line resolution, not span resolution — enough for slice 1's three verbs."""
    if isinstance(node, (LetStmt, LetPatternStmt)):
        yield node
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_lets(item)
        return
    fields = getattr(node, "__dataclass_fields__", None)
    if fields:
        for name in fields:
            yield from _walk_lets(getattr(node, name))


def _pattern_names(pattern):
    for attr in ("fields", "names", "elements", "items"):
        seq = getattr(pattern, attr, None)
        if isinstance(seq, (list, tuple)):
            for item in seq:
                if isinstance(item, str):
                    yield item
                else:
                    name = getattr(item, "name", None) or getattr(item, "bind", None)
                    if isinstance(name, str):
                        yield name


def _fn_signature(fn: FnDecl) -> str:
    prefix = ("pub " if fn.public else "") + ("verified " if fn.verified else "")
    params = ", ".join(f"{p.name}: {p.type}" for p in fn.params)
    ret = f" -> {fn.returns}" if fn.returns else ""
    return f"{prefix}fn {fn.name}({params}){ret}"


def _extern_signature(ext: ExternDecl) -> str:
    prefix = "pub " if ext.public else ""
    kind = ext.classification + (" async" if ext.async_ else "")
    params = ", ".join(f"{p.name}: {p.type}" for p in ext.params)
    ret = f" -> {ext.returns}" if ext.returns else ""
    return f"{prefix}extern {kind} fn {ext.name}({params}){ret}"


def _type_signature(td: TypeDecl) -> str:
    head = f"type {td.name}" + (f"[{', '.join(td.params)}]" if td.params else "")
    if td.cases:
        cases = " | ".join(c.name + (f"({c.payload})" if c.payload else "") for c in td.cases)
        return f"{head} = {cases}"
    if td.fields:
        fields = ", ".join(f"{f.name}: {f.type}" for f in td.fields)
        return f"{head} {{ {fields} }}"
    return head


# ---------------------------------------------------------------- hover

def compute_hover(text: str, position: Position, filename: str = "<lsp>.rvl") -> dict | None:
    """Hover for a position: the guarantee behind a diagnostic on that token,
    otherwise the type/signature of the declared symbol under the cursor.
    Returns an LSP `Hover` (`contents` + `range`) or None."""
    hit = word_at(text, position)
    if hit is None:
        return None
    word, start, end = hit
    line1 = position.line + 1

    guarantee = _guarantee_hover(text, position.line, start, end, filename)
    if guarantee is not None:
        return _hover(guarantee, position.line, start, end)

    symbol = build_symbols(text, filename).resolve(word, line1)
    if symbol is not None:
        body = f"```revl\n{symbol.detail}\n```"
        return _hover(body, position.line, start, end)
    return None


def _guarantee_hover(text: str, line0: int, start: int, end: int,
                     filename: str) -> str | None:
    """If a diagnostic covers the hovered token, surface its guarantee and fix
    — `diagnostics.explain`, the same pair `revl explain` prints. A diagnostic
    elsewhere on the line (a valid symbol beside the offending one) does not
    steal the hover from that symbol's own type."""
    for diag in compute_diagnostics(text, filename):
        span = diag["range"]
        if span["start"]["line"] != line0 or span["end"]["line"] != line0:
            continue
        if end <= span["start"]["character"] or start >= span["end"]["character"]:
            continue  # the token and the diagnostic do not overlap
        code = diag.get("code")
        payload = explain(code) if isinstance(code, str) else {"ok": False}
        if not payload.get("ok"):
            continue
        lines = [f"**{payload['code']} — {payload['guarantee']}**"]
        if payload.get("fix"):
            lines.append("")
            lines.append(f"Fix: {payload['fix']}")
        return "\n".join(lines)
    return None


def _hover(markdown: str, line: int, start: int, end: int) -> dict:
    return {
        "contents": {"kind": "markdown", "value": markdown},
        "range": _span(line, start, end),
    }


# ---------------------------------------------------------------- definition

def compute_definition(text: str, uri: str, position: Position,
                       filename: str = "<lsp>.rvl") -> dict | None:
    """Go-to-definition: resolve the symbol under the cursor to its declaration
    site and return an LSP `Location` in the same document, or None."""
    hit = word_at(text, position)
    if hit is None:
        return None
    word, _start, _end = hit
    symbol = build_symbols(text, filename).resolve(word, position.line + 1)
    if symbol is None:
        return None
    decl_line = max(symbol.line - 1, 0)
    col = find_symbol_column(text, symbol.line, symbol.name) or 0
    return {"uri": uri, "range": _span(decl_line, col, col + len(symbol.name))}
