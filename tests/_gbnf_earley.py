"""A small character-level recogniser for the GBNF subset `revl grammar
--format gbnf` emits, so the test suite can check the GBNF it ships without a
GBNF engine installed.

GBNF is read the way the llama.cpp server reads it: a context-free grammar over
characters, with no tokenizer and no implicit whitespace. The subset is what
`revl.source_grammar` writes: `name ::= expr` rules, `"literal"` (with `\\\\`,
`\\"`, `\\n`, `\\t`, `\\r` escapes), `[class]` and `[^class]` with ranges,
rule references, `( ... )` groups, `|`, and the `*`, `+`, `?` postfixes.

The recogniser is a plain Earley parser with the Aycock-Horspool fix for
nullable rules. It is a test instrument: slow (pure Python, one item set per
character), and meant for documents of a few kilobytes.
"""

from __future__ import annotations

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "]": "]",
           "[": "[", "^": "^", "-": "-", "f": "\f"}


class _Reader:
    def __init__(self, text):
        self.text, self.i = text, 0

    def ws(self):
        while self.i < len(self.text):
            c = self.text[self.i]
            if c in " \t\r":
                self.i += 1
            elif c == "#":
                while self.i < len(self.text) and self.text[self.i] != "\n":
                    self.i += 1
            else:
                return

    def peek(self):
        return self.text[self.i] if self.i < len(self.text) else ""


def parse_gbnf(text: str) -> dict:
    """`{rule: expr}`; expr is ("alt", [...]) | ("seq", [...]) | ("lit", str) |
    ("class", (negated, ranges)) | ("ref", name) | ("rep", expr, min, max)."""
    rules = {}
    for raw in _logical_lines(text):
        if "::=" not in raw:
            continue
        name, body = raw.split("::=", 1)
        reader = _Reader(body)
        rules[name.strip()] = _alt(reader)
        reader.ws()
        if reader.peek():
            raise ValueError(f"trailing text in rule {name.strip()}: {body[reader.i:][:40]!r}")
    return rules


def _logical_lines(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


def _alt(r):
    options = [_seq(r)]
    r.ws()
    while r.peek() == "|":
        r.i += 1
        options.append(_seq(r))
        r.ws()
    return options[0] if len(options) == 1 else ("alt", options)


def _seq(r):
    items = []
    while True:
        r.ws()
        c = r.peek()
        if c in ("", "|", ")"):
            break
        atom = _atom(r)
        if r.peek() in "*+?" and r.peek():
            op = r.peek()
            r.i += 1
            atom = ("rep", atom, 1 if op == "+" else 0, 1 if op == "?" else None)
        items.append(atom)
    return items[0] if len(items) == 1 else ("seq", items)


def _atom(r):
    c = r.peek()
    if c == '"':
        r.i += 1
        out = []
        while r.peek() != '"':
            ch = r.peek()
            if ch == "\\":
                r.i += 1
                ch = ESCAPES[r.peek()]
            out.append(ch)
            r.i += 1
        r.i += 1
        return ("lit", "".join(out))
    if c == "[":
        r.i += 1
        negated = r.peek() == "^"
        if negated:
            r.i += 1
        chars = []
        while r.peek() != "]":
            ch = r.peek()
            if ch == "\\":
                r.i += 1
                ch = ESCAPES[r.peek()]
            chars.append(ch)
            r.i += 1
        r.i += 1
        ranges, k = [], 0
        while k < len(chars):
            if k + 2 < len(chars) and chars[k + 1] == "-":
                ranges.append((chars[k], chars[k + 2]))
                k += 3
            else:
                ranges.append((chars[k], chars[k]))
                k += 1
        return ("class", (negated, tuple(ranges)))
    if c == "(":
        r.i += 1
        inner = _alt(r)
        r.ws()
        assert r.peek() == ")", r.text[r.i:][:40]
        r.i += 1
        return inner
    start = r.i
    while r.peek() and (r.peek().isalnum() or r.peek() == "-"):
        r.i += 1
    if start == r.i:
        raise ValueError(f"unexpected {c!r} at {r.text[r.i:][:30]!r}")
    return ("ref", r.text[start:r.i])


class Grammar:
    """The GBNF lowered to plain productions over characters."""

    def __init__(self, rules: dict, root: str = "root"):
        self.prods: dict = {}           # nonterminal -> [tuple(symbols)]
        self.n = 0
        for name, expr in rules.items():
            self.prods[name] = self._lower(expr)
        self.root = root
        self.nullable = self._nullable()

    def _fresh(self, prods):
        self.n += 1
        name = f"_g{self.n}"
        self.prods[name] = prods
        return name

    def _lower(self, e):
        """`[production]` for expression `e`; a production is a tuple of
        symbols, each a nonterminal name or ("c", (negated, ranges))."""
        tag = e[0]
        if tag == "alt":
            out = []
            for option in e[1]:
                out.extend(self._lower(option))
            return out
        if tag == "seq":
            return [tuple(self._symbol(x) for x in e[1])]
        return [(self._symbol(e),)] if tag != "lit" else [self._lit(e[1])]

    def _lit(self, text):
        return tuple(("c", (False, ((ch, ch),))) for ch in text)

    def _symbol(self, e):
        tag = e[0]
        if tag == "ref":
            return e[1]
        if tag == "class":
            return ("c", e[1])
        if tag == "lit":
            return self._fresh([self._lit(e[1])])
        if tag in ("alt", "seq"):
            return self._fresh(self._lower(e))
        if tag == "rep":
            inner = self._symbol(e[1])
            lo, hi = e[2], e[3]
            if hi == 1:                      # ?
                return self._fresh([(), (inner,)])
            star = self._fresh([])
            self.prods[star] = [(), (inner, star)]
            if lo == 0:
                return star
            return self._fresh([(inner, star)])
        raise ValueError(tag)

    def _nullable(self):
        nullable = set()
        changed = True
        while changed:
            changed = False
            for name, prods in self.prods.items():
                if name in nullable:
                    continue
                if any(all(isinstance(s, str) and s in nullable for s in p) for p in prods):
                    nullable.add(name)
                    changed = True
        return nullable

    def accepts(self, text: str) -> bool:
        def matches(cls, ch):
            negated, ranges = cls
            hit = any(lo <= ch <= hi for lo, hi in ranges)
            return hit != negated

        # an item is (nonterminal, production index, dot, origin)
        sets = [set() for _ in range(len(text) + 1)]
        start = ("$start", 0, 0, 0)
        self.prods["$start"] = [(self.root,)]
        sets[0].add(start)
        for pos in range(len(text) + 1):
            agenda = list(sets[pos])
            while agenda:
                item = agenda.pop()
                name, k, dot, origin = item
                prod = self.prods[name][k]
                if dot < len(prod):
                    sym = prod[dot]
                    if isinstance(sym, str):
                        for j in range(len(self.prods[sym])):
                            new = (sym, j, 0, pos)
                            if new not in sets[pos]:
                                sets[pos].add(new)
                                agenda.append(new)
                        if sym in self.nullable:
                            new = (name, k, dot + 1, origin)
                            if new not in sets[pos]:
                                sets[pos].add(new)
                                agenda.append(new)
                    elif pos < len(text) and matches(sym[1], text[pos]):
                        sets[pos + 1].add((name, k, dot + 1, origin))
                else:
                    for parent in list(sets[origin]):
                        pname, pk, pdot, porigin = parent
                        pprod = self.prods[pname][pk]
                        if pdot < len(pprod) and pprod[pdot] == name:
                            new = (pname, pk, pdot + 1, porigin)
                            if new not in sets[pos]:
                                sets[pos].add(new)
                                agenda.append(new)
            if pos < len(text) and not sets[pos + 1]:
                self.stopped_at = pos
                return False
        self.stopped_at = len(text)
        return (("$start", 0, 1, 0)) in sets[len(text)]
