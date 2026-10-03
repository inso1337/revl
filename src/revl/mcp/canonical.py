"""Terse in, canonical out: the server writes the frame (issue #1700).

Output tokens cost several times what input tokens cost, and they are
decode-bound: an agent pays wall time for every space, every indent and every
line of structure it writes. None of that is a decision. `revl fmt` already
computes the canonical layout of any parseable source, for free, and proves it
changed nothing the compiler sees. So the authoring verbs accept source in any
layout the parser reads, canonicalise it on the server, and answer with a
digest of the canonical text rather than the text itself: the agent need not
re-emit what it wrote, and it never writes indentation at all.

Two halves:

* `canonicalise(text)` formats one source through `revl.formatter`, under the
  same IR-equivalence gate `revl fmt` and `revl_fmt` run. A rewrite the gate
  refuses, or a text the formatter cannot scan, is kept as the agent wrote it:
  canonicalisation is a convenience and never changes what is admitted.
* `method_body_edit(text, edit, modules)` is the body-only edit form for
  `revl_edit`: ``{"method": "<key>.<op>", "body": "<body>"}``. The agent writes
  the body; the server finds the provide method and replaces its body, or,
  when the provide block has no such method yet, writes the method's frame
  from the service's declared signature (`fn <op>(<params>)`) and puts the body
  in it. The parameter names, the arrow, the braces and the indentation are
  the server's.

The body is accepted in three spellings: an expression (`n + 1`, written as
`= n + 1`), an expression with its `=` (`= n + 1`), or a block (`{ let x = n
return x }`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..errors import RevlError


class BodyEditError(RuntimeError):
    """A body-only edit could not be placed: no such provision, no such
    operation on its service, or a buffer the scanner cannot read."""


@dataclass
class Canonical:
    text: str
    changed: bool
    reason: str = ""

    def report(self, *, with_text: bool = False) -> dict:
        """What a verb answers about the stored text: its digest, whether the
        server rewrote what the agent sent, and the text itself only when the
        agent asks for it (`returnCanonical`)."""
        out: dict = {"digest": digest(self.text), "changed": self.changed}
        if self.reason:
            out["kept"] = self.reason
        if with_text:
            out["source"] = self.text
        return out


def digest(text: str) -> str:
    """The digest a verb returns in place of the canonical text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def canonicalise(text: str, filename: str = "<candidate>.rvl") -> Canonical:
    """`text` in canonical layout, when the formatter can prove it changed
    nothing the compiler sees; `text` itself otherwise."""
    from ..formatter import FormatError, format_source, ir_equivalent  # noqa: PLC0415

    try:
        formatted = format_source(text, filename)
    except FormatError as error:
        return Canonical(text, False, f"not formatted: {error.message}")
    if formatted == text:
        return Canonical(text, False)
    try:
        gate = ir_equivalent(text, formatted, filename, token_preserving=True)
    except RevlError as error:
        return Canonical(text, False, f"not formatted: {error.message}")
    if not gate.admitted:
        return Canonical(text, False, f"not formatted: {gate.reason}")
    return Canonical(formatted, True)


def canonical_arguments(arguments: dict) -> tuple[dict, dict | None]:
    """A verb's arguments with an inline `source` and every inline module
    canonicalised, and the report on the main source (None when the call
    carried no inline source). File-backed candidates are left alone: their
    text is the operator's file, not the agent's output."""
    source = arguments.get("source")
    modules = arguments.get("modules")
    if source is None and not modules:
        return arguments, None
    out = dict(arguments)
    report = None
    if isinstance(source, str):
        canon = canonicalise(source)
        out["source"] = canon.text
        report = canon.report(with_text=bool(arguments.get("returnCanonical")))
    if isinstance(modules, dict):
        out["modules"] = {path: canonicalise(text, path).text
                          if isinstance(text, str) else text
                          for path, text in modules.items()}
    return out, report


# ------------------------------------------------------------ body-only edits

@dataclass
class _Tok:
    kind: str
    text: str
    start: int
    end: int


def _tokens(text: str) -> list[_Tok]:
    """The buffer's tokens with their spans, comments and newlines dropped.
    The formatter's scanner, so a string or a host block is one token and a
    brace inside it is not a brace."""
    from ..formatter import FormatError, _scan  # noqa: PLC0415

    try:
        pieces = _scan(text, "<buffer>")
    except FormatError as error:
        raise BodyEditError(f"the buffer cannot be scanned: {error.message}") from None
    return [_Tok(p.kind, p.text, p.start, p.end) for p in pieces
            if p.kind not in ("comment", "newline")]


def _close(toks: list[_Tok], i: int) -> int:
    """Index of the bracket closing the opener at `i`."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    j = i
    while j < len(toks):
        t = toks[j].text
        if toks[j].kind == "punct" and t in pairs:
            stack.append(pairs[t])
        elif toks[j].kind == "punct" and stack and t == stack[-1]:
            stack.pop()
            if not stack:
                return j
        j += 1
    raise BodyEditError("unbalanced brackets in the buffer")


@dataclass
class _Provide:
    key: str
    open: int   # index of the block's `{`
    close: int  # index of its `}`


def _provide_blocks(toks: list[_Tok]) -> list[_Provide]:
    out = []
    for i, t in enumerate(toks):
        if (t.kind == "kw" and t.text == "provide" and i + 2 < len(toks)
                and toks[i + 1].kind == "word" and toks[i + 2].text == "{"):
            out.append(_Provide(toks[i + 1].text, i + 2, _close(toks, i + 2)))
    return out


def _method_span(toks: list[_Tok], block: _Provide, op: str) -> tuple[int, int] | None:
    """The character span of `op`'s body in `block`, from its `=` or `{` to
    the end of the body, or None when the block has no such method."""
    i = block.open + 1
    while i < block.close:
        t = toks[i]
        if t.kind == "punct" and t.text in "{([":
            i = _close(toks, i) + 1
            continue
        if (t.kind == "kw" and t.text == "fn" and toks[i + 1].text == op
                and toks[i + 2].text == "("):
            j = _close(toks, i + 2) + 1
            # an optional written return type, up to the body
            while j < block.close and toks[j].text not in ("=", "{"):
                j += 1
            if toks[j].text == "{":
                end = _close(toks, j)
                return toks[j].start, toks[end].end
            k = j + 1
            while k < block.close:
                if toks[k].kind == "punct" and toks[k].text in "{([":
                    k = _close(toks, k) + 1
                    continue
                if toks[k].kind == "kw" and toks[k].text == "fn":
                    break
                k += 1
            return toks[j].start, toks[k - 1].end
        i += 1
    return None


def _spelled(body: str) -> str:
    body = body.strip()
    if not body:
        raise BodyEditError("a body edit needs a non-empty `body`")
    if body.startswith("{") or body.startswith("="):
        return body
    return "= " + body


def _service_params(texts: list[str], component: str | None, key: str,
                    op: str) -> list[str]:
    """The parameter names `op` declares on the service provided at `key`."""
    from ..parser import Parser  # noqa: PLC0415

    services: dict = {}
    provided: list[tuple[str, str]] = []
    for text in texts:
        try:
            program = Parser(text, "<buffer>").parse()
        except RevlError:
            continue
        services.update({s.name: s for s in program.services})
        for comp in program.components:
            if component is None or comp.name == component:
                provided += [(k, svc) for k, svc, _line in comp.provides if k == key]
    if not provided:
        raise BodyEditError(f"no component provides `{key}`")
    names = {svc for _k, svc in provided}
    if len(names) != 1:
        raise BodyEditError(f"`{key}` is provided as more than one service "
                            f"({', '.join(sorted(names))}); name the component, "
                            f"`<Component>.{key}.{op}`")
    decl = services.get(names.pop())
    if decl is None or op not in decl.methods:
        raise BodyEditError(f"the service provided at `{key}` declares no "
                            f"operation `{op}`")
    return [name for name, _type in decl.methods[op].params]


def method_body_edit(text: str, edit: dict, modules: dict | None = None
                     ) -> tuple[str, dict]:
    """Apply one ``{"method": "[<Component>.]<key>.<op>", "body": ...}`` edit."""
    address = str(edit.get("method", ""))
    parts = address.split(".")
    if len(parts) not in (2, 3) or not all(parts):
        raise BodyEditError("`method` is `<key>.<op>` or `<Component>.<key>.<op>`")
    component = parts[0] if len(parts) == 3 else None
    key, op = parts[-2], parts[-1]
    body = _spelled(str(edit.get("body", "")))
    toks = _tokens(text)
    blocks = [b for b in _provide_blocks(toks) if b.key == key]
    if component is not None:
        blocks = [b for b in blocks if _owner(toks, b) == component]
    if not blocks:
        raise BodyEditError(f"no `provide {key} {{ ... }}` block in the buffer")
    if len(blocks) > 1:
        raise BodyEditError(f"more than one `provide {key}` block; name the "
                            f"component, `<Component>.{key}.{op}`")
    block = blocks[0]
    span = _method_span(toks, block, op)
    if span is not None:
        start, end = span
        return text[:start] + body + text[end:], {
            "form": "method", "method": address, "frame": "kept",
            "replaced": text[start:end]}
    params = _service_params([text, *(modules or {}).values()], component, key, op)
    frame = f"fn {op}({', '.join(params)}) {body}"
    at = toks[block.close].start
    return text[:at] + "\n" + frame + "\n" + text[at:], {
        "form": "method", "method": address, "frame": "written",
        "signature": f"fn {op}({', '.join(params)})"}


def _owner(toks: list[_Tok], block: _Provide) -> str | None:
    """The component whose body holds `block`."""
    owner = None
    for i in range(block.open):
        if toks[i].kind == "kw" and toks[i].text == "component":
            owner = toks[i + 1].text
    return owner


__all__ = ["Canonical", "BodyEditError", "canonicalise", "canonical_arguments",
           "digest", "method_body_edit"]
