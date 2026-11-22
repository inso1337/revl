"""Declarations-only context for one component (docs/context.md).

An agent about to write or edit a component needs the services it may call and
the services it must implement, spelled the way the compiler will hold it to
them: names, parameter and return types, and the `emission` markers. It does
not need any provider's body, and reading the whole composition to find those
declarations is where its grounding tokens go. `context` hands it exactly the
declarations, cut verbatim from the source, and nothing else.

The pack is built from PARSED modules, never lowered ones, so a component whose
body does not check yet still grounds. What does stop it is the module loader's
own errors: a parse error, a `use` that cannot be resolved or is not public,
and the file checks the loader makes at load time (an `asset` or a host-body
file that is not there). Every `type` a service signature names, transitively,
is cut beside it. Every service carries a `hash` over its lexed tokens and
those of its types. The lexer drops comments, so a doc edit or a reflow leaves
the hash where it was and a signature or a field edit moves it.

A pack for one component ends with `IDIOMS`, a short compiled illustration of
how a provider body is written. Declarations alone lost rounds-to-green: a
model that never sees a body writes a bare `match`, puts `emission` on a
provider method, or drops an `emit` or a `return`, which the raw composition
had shown it by example (bench/context_arm.py, docs/context.md). The
illustration is the same text for every pack and carries nothing of the
sources.

`budget` shrinks the pack in a fixed order instead of truncating it: first the
doc comments go, from the last service backwards, then the illustration, then
whole services collapse to their header line, again from the last one
backwards. Every service stays named, with its clause and its hash, at every
step.
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
from .parser import Parser
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
_TYPE_PREFIX = {"pub"}

# a type name inside a rendered type (`Opt[Note]`, `(Int) -> Row`): the way
# the compiler reads the types a service names (compiler._service_type_closure)
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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
        self.path = path
        self._heads: dict[tuple, int] | None = None
        self._parser: Parser | None = None
        seen: dict[tuple, int] = {}
        for x, t in enumerate(self.tokens):
            spelled = (t.line, str(t.value))
            self.rank.append(seen.get(spelled, 0))
            seen[spelled] = self.rank[-1] + 1
            if t.kind == "kw":
                self.keywords.setdefault((t.line, t.value), []).append(x)

    def head(self, line: int, name: str) -> int:
        """The token that opens `type NAME` (or `event NAME`) on `line`."""
        if self._heads is None:
            self._heads = {}
            for x, t in enumerate(self.tokens[:-1]):
                opens = (t.kind == "kw" and t.value == "type") or (
                    t.kind == "ident" and t.value == "event")
                nxt = self.tokens[x + 1]
                if opens and nxt.kind == "ident":
                    self._heads.setdefault((t.line, nxt.value), x)
        return self._heads[(line, name)]

    def declaration_end(self, i: int) -> int:
        """The last token of the `type` or `event` declaration opened at token
        `i`, where the parser itself stops reading it: a type has no closing
        mark of its own (a variant or an alias just ends)."""
        if self._parser is None:
            self._parser = Parser(self.text, self.path)
        self._parser.pos = i
        if self.tokens[i].kind == "kw":
            self._parser.type_decl()
        else:
            self._parser.event_decl()
        return self._parser.pos - 1


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


def _type_block(src: _Source, line: int, name: str) -> str:
    """A `type` (or `event`) declaration with its `pub` and its doc, cut the
    way `_block` cuts a service. Where it ends is the parser's call."""
    tokens = src.tokens
    i = src.head(line, name)
    j = src.declaration_end(i)
    while i > 0 and tokens[i - 1].kind == "kw" and tokens[i - 1].value in _TYPE_PREFIX:
        i -= 1
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


def _hash(block: str, types: list[str] = ()) -> str:
    """The hash of a service's tokens and, after them, the tokens of each type
    declaration its signatures name (transitively), so a changed field moves
    the hash of every service that shows it."""
    def spelled(text: str, drop_pub: bool):
        lexed = _spelled(text)
        if lexed is None:
            # a cut that does not lex on its own (it never should): hash what
            # it says without its docs, so two such blocks never share one hash
            return _strip_docs(text)
        return [t for t in lexed if not (drop_pub and t == ("kw", "pub"))]

    # `pub` on a service is documentation (services are pub by default), so it
    # is not part of what a consumer compiles against; on a type it is, since
    # it decides whether a `use` may name the type
    parts = [spelled(block, True), *(spelled(t, False) for t in types)]
    # a service naming no type keeps the hash it had before types were hashed
    payload = parts[0] if len(parts) == 1 else parts
    payload = payload if isinstance(payload, str) else repr(payload)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
        self._types: dict[tuple, dict] = {}
        self._refs_of: dict[tuple, list[tuple]] = {}
        self._home: dict[int, str] = {}

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

    def type_decl(self, module, name: str):
        """Resolve a type name the way `module` sees it, as the compiler does:
        its own declaration, then the module a `use ... { name }` of it names.
        `None` for a builtin (`Str`, `Opt`) or a name nothing in scope
        declares. Never a same-named type of some unrelated loaded module."""
        for decl in module.program.type_decls:
            if decl.name == name:
                return module, decl
        for use in module.program.uses:
            if not use.names or name not in use.names:
                continue
            target = self._imported(module, use)
            decl = next((d for d in target.program.type_decls
                         if d.name == name), None) if target else None
            if decl is not None:
                return target, decl
        return None

    def types_named(self, owner, decl) -> list[dict]:
        """Every `type` declaration the signatures of service `decl` (declared
        in `owner`) name, and those declarations name in turn, in the order
        they are first met. One dict per declaration for the whole pack."""
        pending: list[tuple] = []
        for method in decl.methods.values():
            names = [t for _p, t in method.params] + [method.returns]
            pending += [(owner, n) for t in names
                        for n in _TYPE_NAME.findall(t or "")]
        pending.reverse()
        found: list[dict] = []
        seen: set[tuple] = set()
        while pending:
            module, name = pending.pop()
            resolved = self.type_decl(module, name)
            if resolved is None:
                continue
            home, tdecl = resolved
            key = (home.path, tdecl.name, tdecl.line)
            if key not in self._types:
                self._refs_of[key] = self._refs(home, tdecl)
                self._types[key] = {
                    "name": tdecl.name,
                    "module": relpath_or_abs(home.path),
                    "line": tdecl.line,
                    # only a `pub` type can be named in a `use`; one reached through
                    # another type needs no import line at all
                    "use": (self.use_line(home, tdecl.name)
                            if tdecl.public else None),
                    "text": _type_block(self.source(home.path), tdecl.line,
                                        tdecl.name),
                }
                self._home[id(self._types[key])] = home.path
            if key in seen:
                continue
            seen.add(key)
            found.append(self._types[key])
            pending += reversed(self._refs_of[key])
        return found

    def type_paths(self, types: list[dict]) -> set[str]:
        return {self._home[id(t)] for t in types}

    @staticmethod
    def _refs(home, tdecl) -> list[tuple]:
        written = [f.type for f in tdecl.fields or ()]
        written += [c.payload for c in tdecl.cases or () if c.payload]
        if len(tdecl.cases or ()) == 1 and tdecl.cases[0].payload is None:
            written.append(tdecl.cases[0].name)  # an alias spells its target
        own = set(tdecl.params or ())
        return [(home, n) for t in written for n in _TYPE_NAME.findall(t or "")
                if n not in own and n != tdecl.name]

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


#: How a provider body is written, appended to a component's pack. It is the
#: text the rounds-to-green run measured, and tests/test_context.py compiles it
#: as it stands, so an idiom here is one the checker accepts.
IDIOMS = """\
// How a provider body is written: a compiled illustration, not part of
// these sources. Its names (Log, Kv, Example) mean nothing here.
extern emission fn write_line(line: Str) = @py { pass }
service Log { emission[write_line] fn record(line: Str) -> Int }
service Kv {
  fn get(k: Str) -> Opt[Str]
  fn find(k: Str) -> Result[Str, Str]
  emission[log] fn put(k: Str, v: Str)
  emission[log] fn tag(k: Str) -> Str
}
component Example requires log: Log provides kv: Kv {
  // activation: runs once at load; `effect ... undo ...` is revertible state
  let store = effect Map.new() undo store.drop()

  provide kv {
    // a method is a plain `fn`, never `emission fn`: the service declares that
    fn get(k) = store.get(k)

    // `match` is an expression: return it, or bind it with `let`
    fn find(k) {
      return match store.get(k) {
        Some(v) => Ok(v),
        None    => Err("no entry for " + k),
      }
    }

    // a mutation pairs `effect` with its `undo`; a call to an emission
    // needs `emit`, even inside another emission
    fn put(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
      emit log.record(`put ${k}`)
    }

    // an emission's value is the `emit` expression itself: bind it or
    // return it, never call the emission a second time to get it; a block
    // body that returns a value ends with `return`
    fn tag(k) {
      let line = emit log.record(k)
      return `tag:${k} at ${line}`
    }
  }
}
"""


def context(paths: list[str], *, sources: dict[str, str] | None = None,
            component: str | None = None, budget: int | None = None,
            idioms: bool | None = None) -> dict:
    """`idioms` appends `IDIOMS` to the pack; by default it does when a
    component is named, the case where a body is about to be written."""
    if idioms is None:
        idioms = component is not None
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
        types = closure.types_named(owner, decl)
        drawn.update(closure.type_paths(types))
        hashed = _hash(text, [t["text"] for t in types])
        # each service holds its own copies, so shedding the docs beside one
        # service never changes what another shows
        types = [dict(t) for t in types]
        clause_key = key if role != "declared" else (
            entry["providers"][0]["key"] if entry["providers"] else None)
        entries.append({
            **entry, "declared": True,
            "module": relpath_or_abs(owner.path),
            "line": decl.line,
            "hash": hashed,
            "clause": (f"{'provides' if role == 'provides' else 'requires'} "
                       f"{clause_key}: {service}") if clause_key else None,
            "use": closure.use_line(owner, decl.name),
            "operations": len(decl.methods),
            "text": text,
            "types": types,
        })

    if component is not None:
        drawn.add(components[component][0].path)
    raw = sum(count_tokens(closure.texts[p]) for p in drawn)
    steps, idioms = _fit(entries, header, component, budget, idioms)
    pack = render(entries, header, component, idioms)
    return {
        "ok": True,
        "component": component,
        "header": header,
        "services": entries,
        "pack": pack,
        "idioms": idioms,
        "tokens": {"pack": count_tokens(pack), "raw": raw,
                   "rawFiles": sorted(relpath_or_abs(p) for p in drawn)},
        "budget": None if budget is None else {
            "limit": budget, "fits": count_tokens(pack) <= budget,
            "steps": steps},
    }


def _fit(entries: list[dict], header: str | None, component: str | None,
         budget: int | None, idioms: bool) -> tuple[list[str], bool]:
    """Shrink `entries` in place until the rendered pack fits `budget`: docs
    off from the last service backwards (the docs of the types shown beside
    it go with it), then the illustration, then collapse from the last
    service backwards (a collapsed service shows no operations, so no types).
    A step that would not make the pack shorter is undone and skipped.
    Returns the steps taken, in order, and whether the illustration stayed."""
    steps: list[str] = []
    if budget is None:
        return steps, idioms
    shown = [e for e in entries if e.get("text")]
    for stage in ("docs", "collapse"):
        if stage == "collapse" and idioms and count_tokens(
                render(entries, header, component, idioms)) > budget:
            idioms = False
            steps.append("idioms off")
        for entry in reversed(shown):
            before = count_tokens(render(entries, header, component, idioms))
            if before <= budget:
                return steps, idioms
            saved = (entry["text"], [t["text"] for t in entry["types"]])
            if stage == "docs":
                entry["text"] = _strip_docs(entry["text"])
                for t in entry["types"]:
                    t["text"] = _strip_docs(t["text"])
                label = "docs off"
            else:
                entry["text"] = (f"service {entry['service']} "
                                 f"{{ /* {entry['operations']} operation(s), "
                                 f"collapsed */ }}")
                label = "collapsed"
            was = entry.get("collapsed", False)
            entry["collapsed"] = was or stage == "collapse"
            if count_tokens(render(entries, header, component, idioms)) < before:
                steps.append(f"{label}: {entry['service']}")
            else:
                entry["text"], texts = saved
                for t, text in zip(entry["types"], texts):
                    t["text"] = text
                if "collapsed" in entry and not was:
                    del entry["collapsed"]
    return steps, idioms


def render(entries: list[dict], header: str | None, component: str | None,
           idioms: bool = False) -> str:
    out, shown = [], set()
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
        # the types a service names stand beside it; one that two services
        # name is cut once, beside the first that still shows its operations
        for t in () if e.get("collapsed") else e["types"]:
            if (t["module"], t["line"]) not in shown:
                shown.add((t["module"], t["line"]))
                if t["use"]:
                    out.append(t["use"])
                out += [t["text"], ""]
    if idioms:
        out += [IDIOMS]
    return "\n".join(out).rstrip() + "\n"
