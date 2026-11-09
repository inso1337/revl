"""Declarations-only context for one component (docs/context.md).

An agent about to write or edit a component needs the services it may call and
the services it must implement, spelled the way the compiler will hold it to
them: names, parameter and return types, and the `emission` markers. It does
not need any provider's body, and reading the whole composition to find those
declarations is where its grounding tokens go. `context` hands it exactly the
declarations, cut verbatim from the source, and nothing else.

The pack is built from PARSED modules, never lowered ones, so a component whose
body does not check yet still grounds: only a parse error stops it. Every
service carries a `hash` over its lexed tokens. The lexer drops comments, so a
doc edit or a reflow leaves the hash where it was and a signature edit moves it.

`budget` shrinks the pack in a fixed order instead of truncating it: first the
doc comments go, from the last service backwards, then whole services collapse
to their header line, again from the last one backwards. Every service stays
named, with its clause and its hash, at every step.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import os
import re

from .compiler import _ModuleLoader, _load_root
from .errors import RevlError
from .lexer import lex
from ._paths import relpath_or_abs

# the BPE-shaped proxy `bench/tokens.py` scores tokens-to-green with, so a
# budget here and a number there are counted by the same rule
_PRETOKEN = re.compile(
    r"""'(?:[sdmt]|ll|ve|re)|[^\r\n\w]?\w+|[^\s\w]+|\s+""",
    re.UNICODE,
)

# what may head a declaration before its keyword (parser.py's top-level loop)
_SERVICE_PREFIX = {"pub", "commutative"}
_COMPONENT_PREFIX = {"boot"}


def count_tokens(text: str) -> int:
    total = 0
    for piece in _PRETOKEN.findall(text):
        stripped = piece.strip()
        total += 1 if not stripped else max(1, math.ceil(len(stripped) / 4))
    return total


def _spelled(text: str) -> list | None:
    try:
        return [(t.kind, t.value) for t in lex(text, "<context>") if t.kind != "eof"]
    except RevlError:
        return None


class _Source:
    """One file's text and tokens, with the indexes the cuts look up: its
    lines split on `\\n` alone (what the lexer counts), each token's rank
    among the tokens spelled the same on its line, and where each keyword sits
    on each line."""

    def __init__(self, text: str, path: str):
        self.text = text
        self.lines = text.split("\n")
        self.tokens = lex(text, path)
        self.rank: list[int] = []
        self.keywords: dict[tuple, list[int]] = {}
        seen: dict[tuple, int] = {}
        for x, t in enumerate(self.tokens):
            spelled = (t.line, str(t.value))
            self.rank.append(seen.get(spelled, 0))
            seen[spelled] = self.rank[-1] + 1
            if t.kind == "kw":
                self.keywords.setdefault((t.line, t.value), []).append(x)


def _spelling(token, bounded: bool) -> str:
    """A pattern for where `token` may stand on its line: its text, held to
    word boundaries when `bounded` and it starts or ends with a word
    character, so `service` is never found inside `service0`."""
    text = str(token.value)
    pattern = re.escape(text)
    if bounded and re.match(r"\w", text[:1]):
        pattern = r"(?<!\w)" + pattern
    if bounded and re.match(r"\w", text[-1:]):
        pattern += r"(?!\w)"
    return pattern


# candidate spans lexed per search before a cut falls back to whole lines
_TRIES = 64


def _cut(src: _Source, i: int, j: int) -> tuple[str, bool]:
    """The exact source text of `tokens[i..j]`, and whether it starts its line.

    Tokens carry a line and no column, so the cut is found, not computed: a
    span from a place the first token's text starts on its line to a place the
    last token's text ends on its line is kept when it lexes back to exactly
    `tokens[i..j]`. A brace in a comment, a second declaration on the same
    line, a body opened on the header's line: each yields a span that lexes to
    something else, and is passed over.

    The search runs twice. First with word tokens held to word boundaries,
    which a token glued to the one before it fails (the `s` of `30s` lexes on
    its own after the `30`); then with the bare text. The lex check keeps
    either one exact; the boundaries only make the first one short."""
    first, last = src.tokens[i].line, src.tokens[j].line
    for bounded in (True, False):
        found = _search(src, i, j, bounded)
        if found is not None:
            return found
    # a token whose value is not its spelling (a float's exponent): whole lines
    return "\n".join(src.lines[first - 1:last]), True


def _search(src: _Source, i: int, j: int,
            bounded: bool) -> tuple[str, bool] | None:
    """One search for `_cut`. Every token is one of the matches of its own
    text, and a lookalike (the text inside a string, or in a `//` comment,
    which only ever follows the tokens of its line) only adds matches, so the
    token is its rank-th match on the line (its rank among the tokens spelled
    the same) or a later one. The search walks up from those ranks: on a line
    with no lookalike its first span is the answer, whatever else shares the
    line. A start whose span already lexes longer than wanted is dropped, and
    at most `_TRIES` spans are lexed."""
    tokens, lines = src.tokens, src.lines
    first, last = tokens[i].line, tokens[j].line
    want = [(t.kind, t.value) for t in tokens[i:j + 1]]
    head, tail = lines[first - 1], lines[last - 1]
    starts = [m.start()
              for m in re.finditer(_spelling(tokens[i], bounded), head)]
    ends = [m.end() for m in re.finditer(_spelling(tokens[j], bounded), tail)]

    def pairs():
        lo_a, lo_b = src.rank[i], src.rank[j]
        for a in range(lo_a, len(starts)):
            # on one line an end at or before the start is no span
            past = bisect.bisect_right(ends, starts[a]) if first == last else 0
            for b in range(max(lo_b, past), len(ends)):
                yield a, b
        # below the ranks: reached when the boundaries hid the token itself
        for a in range(len(starts)):
            for b in range(len(ends)):
                if a < lo_a or b < lo_b:
                    yield a, b

    tried, longer = 0, set()
    for a, b in pairs():
        s, e = starts[a], ends[b]
        if a in longer or (first == last and e <= s):
            continue
        if tried == _TRIES:
            break
        tried += 1
        if first == last:
            piece = head[s:e]
        else:
            piece = "\n".join([head[s:], *lines[first:last - 1], tail[:e]])
        spelled = _spelled(piece)
        if spelled == want:
            return piece, not head[:s].strip()
        if spelled is not None and len(spelled) > len(want):
            longer.add(a)  # a later end from this start only lexes longer
    return None


def _doc_above(lines: list[str], line: int) -> list[str]:
    """The contiguous `//` block directly above `line`: the declaration's doc."""
    first = line
    while first > 1 and lines[first - 2].strip().startswith("//"):
        first -= 1
    return lines[first - 1:line - 1]


def _block(src: _Source, line: int, nth: int) -> str:
    """The `nth` service declared on `line`, with its prefixes and its doc,
    brace-matched over the lexed tokens (so a brace inside a string or a
    comment is never counted)."""
    tokens = src.tokens
    i = src.keywords[(line, "service")][nth]
    while i > 0 and tokens[i - 1].kind == "kw" and tokens[i - 1].value in _SERVICE_PREFIX:
        i -= 1
    depth, j = 0, i
    for j in range(i, len(tokens)):
        if tokens[j].kind == "{":
            depth += 1
        elif tokens[j].kind == "}":
            depth -= 1
            if depth == 0:
                break
    piece, alone = _cut(src, i, j)
    doc = _doc_above(src.lines, tokens[i].line) if alone else []
    return "\n".join([*doc, piece])


def _header(src: _Source, line: int, nth: int) -> str:
    """A component's header, `component X requires ... provides ...`, as
    written: from its keyword (and a `boot` before it) up to the `{` that opens
    the body."""
    tokens = src.tokens
    i = src.keywords[(line, "component")][nth]
    if i > 0 and tokens[i - 1].kind == "ident" and tokens[i - 1].value in _COMPONENT_PREFIX:
        i -= 1
    brace = next(k for k in range(i, len(tokens)) if tokens[k].kind == "{")
    # cut through the brace, whose place on its line is never in doubt (the
    # header's own last token may be the `s` glued to a `30s`), then drop it
    piece = _cut(src, i, brace)[0]
    return piece[:piece.rindex("{")].rstrip() if piece.endswith("{") else piece


def _strip_docs(block: str) -> str:
    return "\n".join(l for l in block.split("\n") if not l.strip().startswith("//"))


def _hash(block: str) -> str:
    # `pub` on a service is documentation (services are pub by default), so it
    # is not part of what a consumer compiles against
    lexed = _spelled(block)
    if lexed is None:
        # a cut that does not lex on its own (it never should): hash what it
        # says without its docs, so two such blocks never share one hash
        return hashlib.sha256(_strip_docs(block).encode()).hexdigest()[:16]
    spelled = repr([t for t in lexed if t != ("kw", "pub")])
    return hashlib.sha256(spelled.encode()).hexdigest()[:16]


def _nth_on_line(decls: list, decl) -> int:
    return [d.line for d in decls[:decls.index(decl)]].count(decl.line)


class _Closure:
    """Every module the roots load, with the text each was parsed from."""

    def __init__(self, paths: list[str], sources: dict[str, str] | None):
        virtual = {os.path.abspath(p): t for p, t in (sources or {}).items()}
        self.loader = _ModuleLoader(virtual or None)
        self.loader.mark_roots(os.path.abspath(p) for p in paths)
        self.roots = [_load_root(self.loader, p) for p in paths]
        self.modules = dict(self.loader._cache)
        self.texts = {**self.loader.disk_reads, **virtual}
        self._sources: dict[str, _Source] = {}

    def source(self, path: str) -> _Source:
        if path not in self._sources:
            self._sources[path] = _Source(self.texts[path], path)
        return self._sources[path]

    def _imported(self, module, use):
        try:
            path = self.loader.resolve_use(module.dir, module.path, use)
        except RevlError:
            return None
        return self.modules.get(os.path.abspath(path))

    def _owner(self, decl, fallback):
        return next((m for m in self.modules.values()
                     if any(d is decl for d in m.program.services)), fallback)

    def services(self, module, name: str):
        """Resolve a service name the way `module` sees it: its own
        declaration, then the module a `use ... { name }` of it names, then
        the roots (linked into one composition), then anything loaded. `None`
        for a type that no `service` declares (a `Stream[T]` capability, a
        typo)."""
        for decl in module.program.services:
            if decl.name == name:
                return module, decl
        for use in module.program.uses:
            if use.names and name in use.names:
                target = self._imported(module, use)
                decl = target.services.get(name) if target else None
                if decl is not None:
                    return self._owner(decl, target), decl
        for candidate in [*self.roots, *self.modules.values()]:
            for decl in candidate.program.services:
                if decl.name == name:
                    return candidate, decl
        return None

    def use_line(self, owner, name: str) -> str | None:
        """The `use` a root writes to see `name` from a non-root module, as
        the first root that already imports it wrote the path."""
        if owner in self.roots:
            return None
        for root in self.roots:
            for use in root.program.uses:
                if self._imported(root, use) is owner:
                    return f'use "{use.path}" {{ {name} }}'
        return None


def context(paths: list[str], *, sources: dict[str, str] | None = None,
            component: str | None = None, budget: int | None = None) -> dict:
    closure = _Closure(paths, sources)
    components = {c.name: (root, c)
                  for root in closure.roots for c in root.program.components}
    providers: dict[tuple, list[dict]] = {}
    for name, (root, comp) in sorted(components.items()):
        for key, service, _line in comp.provides:
            found = closure.services(root, service)
            ident = (found[0].path, found[1].name) if found else (None, service)
            providers.setdefault(ident, []).append({"component": name, "key": key})

    if component is not None and component not in components:
        return {"ok": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "usage",
            "message": f"no component `{component}` in the given sources",
            "available": sorted(components)}]}

    wanted: list[tuple[str, str | None, str, object]] = []  # role, key, service, module
    header = None
    if component is not None:
        module, comp = components[component]
        header = _header(closure.source(module.path), comp.line,
                         _nth_on_line(module.program.components, comp))
        wanted += [("provides", k, s, module) for k, s, _ in comp.provides]
        wanted += [("requires", k, s, module) for k, s, _ in comp.requires]
    else:
        for module in closure.modules.values():
            wanted += [("declared", None, d.name, module)
                       for d in module.program.services]

    entries, drawn = [], set()
    for role, key, service, module in wanted:
        found = closure.services(module, service)
        ident = (found[0].path, found[1].name) if found else (None, service)
        # the component being grounded is not its own provider worth naming
        entry = {"role": role, "key": key, "service": service,
                 "providers": [p for p in providers.get(ident, [])
                               if p["component"] != component]}
        if found is None:
            entries.append({**entry, "declared": False})
            continue
        owner, decl = found
        drawn.add(owner.path)
        text = _block(closure.source(owner.path), decl.line,
                      _nth_on_line(owner.program.services, decl))
        clause_key = key if role != "declared" else (
            entry["providers"][0]["key"] if entry["providers"] else None)
        entries.append({
            **entry, "declared": True,
            "module": relpath_or_abs(owner.path),
            "line": decl.line,
            "hash": _hash(text),
            "clause": (f"{'provides' if role == 'provides' else 'requires'} "
                       f"{clause_key}: {service}") if clause_key else None,
            "use": closure.use_line(owner, decl.name),
            "operations": len(decl.methods),
            "text": text,
        })

    if component is not None:
        drawn.add(components[component][0].path)
    raw = sum(count_tokens(closure.texts[p]) for p in drawn)
    steps = _fit(entries, header, component, budget)
    pack = render(entries, header, component)
    return {
        "ok": True,
        "component": component,
        "header": header,
        "services": entries,
        "pack": pack,
        "tokens": {"pack": count_tokens(pack), "raw": raw,
                   "rawFiles": sorted(relpath_or_abs(p) for p in drawn)},
        "budget": None if budget is None else {
            "limit": budget, "fits": count_tokens(pack) <= budget,
            "steps": steps},
    }


def _fit(entries: list[dict], header: str | None, component: str | None,
         budget: int | None) -> list[str]:
    """Shrink `entries` in place until the rendered pack fits `budget`: docs
    off from the last service backwards, then collapse from the last service
    backwards. A step that would not make its service shorter is skipped.
    Returns the steps taken, in order."""
    steps: list[str] = []
    if budget is None:
        return steps
    shown = [e for e in entries if e.get("text")]
    for stage in ("docs", "collapse"):
        for entry in reversed(shown):
            if count_tokens(render(entries, header, component)) <= budget:
                return steps
            if stage == "docs":
                shorter = _strip_docs(entry["text"])
                label = "docs off"
            else:
                shorter = (f"service {entry['service']} "
                           f"{{ /* {entry['operations']} operation(s), "
                           f"collapsed */ }}")
                label = "collapsed"
            if count_tokens(shorter) < count_tokens(entry["text"]):
                entry["text"] = shorter
                entry["collapsed"] = entry.get("collapsed", False) or stage == "collapse"
                steps.append(f"{label}: {entry['service']}")
    return steps


def render(entries: list[dict], header: str | None, component: str | None) -> str:
    out = []
    if header is not None:
        out += [f"// context for component {component}: declarations only", header, ""]
    for e in entries:
        if not e["declared"]:
            out += [f"// {e['role']} {e['key']}: {e['service']} "
                    f"(no `service` declares it)", ""]
            continue
        by = ", ".join(f"{p['component']} as `{p['key']}`" for p in e["providers"])
        note = [e["clause"] or e["role"], f"#{e['hash']}"]
        if e["role"] != "provides":
            note.append(f"provided by {by}" if by else "no provider")
        out.append("// " + "  ".join(note))
        if e["use"]:
            out.append(e["use"])
        out += [e["text"], ""]
    return "\n".join(out).rstrip() + "\n"
