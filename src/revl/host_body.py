"""One host-body scanner for every backend tier (#3130, #3131, #3162, #3164, #3166).

An extern body is verbatim host code, but two things about it are decided by
the EMITTER, not by the host compiler:

* the body is spliced between a function header and a closing token the
  emitter writes itself, so an unmatched brace (or, on wasm, an unmatched
  paren) that the host compiler reads as structure closes the extern's
  function early. Everything after it is then emitted at module, package or
  class scope, where it runs at load, outside every call-site wrapper;
* the body is written out line by line. ``str.splitlines`` also breaks on
  U+0085, U+2028, U+2029, VT, FF and U+001C..U+001E, which most host
  languages do not treat as line ends (JavaScript treats only U+2028 and
  U+2029 as line terminators), so a comment holding one of them would be
  cut in two and its second half emitted as a statement.

The revl lexer's own end-of-body scan is unchanged: it counts braces inside
``//`` comments on purpose, because the importers rely on that. This module
re-reads the body the way the HOST language reads it and refuses a body that
would leave its function, and it is the single place the tiers share for
splitting and checking a host body.

Refusals are raised through the CALLER's own ``EmitError`` (each backend
defines its own class, see ``src/revl/refusal.py``), so the tier's existing
refusal channel and the entry points that distinguish a refusal from a fault
are unchanged.
"""

from __future__ import annotations

import heapq
import re
import unicodedata

__all__ = ["HostBodyError", "body_lines", "check_host_body"]

#: Characters ``str.splitlines`` treats as a line end but the host languages
#: here do not: VT, FF, the C0 separators, NEL and the Unicode line and
#: paragraph separators. The emitters split on "\n" only, so a body keeps
#: them where the author wrote them; the scanner refuses them where a reader
#: could mistake the text for a comment (outside a string literal).
_SEPARATORS = (
    "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
)

_SEPARATOR_TEXT = {
    "\x0b": "U+000B (vertical tab)",
    "\x0c": "U+000C (form feed)",
    "\x1c": "U+001C (file separator)",
    "\x1d": "U+001D (group separator)",
    "\x1e": "U+001E (record separator)",
    "\x85": "U+0085 (next line)",
    "\u2028": "U+2028 (line separator)",
    "\u2029": "U+2029 (paragraph separator)",
}

#: The host name each backend's lexical rules belong to, for refusal text.
_HOST = {
    "ts": "JavaScript",
    "rs": "Rust",
    "java": "Java",
    "go": "Go",
    "wasm": "WAT",
}

#: A Rust raw string may open with `r`, `br` or `cr` (the prefix carries the
#: byte/C-string flavour) and any number of `#` before the quote.
_RUST_RAW_PREFIXES = ("br", "cr", "r")

#: The ECMAScript keywords an expression may follow, so a `/` after them
#: opens a regex literal, unless the word follows a member-access `.` or
#: `?.` (then it is a property name; a spread's `...` and the dot ending a
#: number like `1.` are no member access): `case`, `default` (`export
#: default`), `delete`, `do`, `else`, `extends`, `in`, `instanceof`, `new`,
#: `return`, `throw`, `typeof`, `void`, and the contextual `await`,
#: `yield` and `of`, which are also plain identifiers (see
#: `_CONTEXTUAL_KEYWORDS`). The other keywords are followed by `(`, `{`, a
#: name or a line end (`break`, `continue`, `debugger`), or are operands
#: that divide (`this`, `super`, `null`, `true`, `false`); a line end after
#: an operand is read both ways.
#: `_Scan._js_code` covers the punctuation.
_REGEX_KEYWORDS = frozenset({
    "await", "case", "default", "delete", "do", "else", "extends", "in",
    "instanceof", "new", "of", "return", "throw", "typeof", "void", "yield",
})

_REGEX_OPENERS = frozenset("([{,;:=!&|?+-*%^~<>")

_IDENT_CONT = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$"
)

#: Per-backend lexical rules. `open`/`close` are the tokens the emitter's
#: splice must own; `line_ends` is where the host ends a `//` comment;
#: `block_nests` covers Rust and WAT (their block comments nest);
#: `strings` are ordinary quote characters; `char` selects the char-literal
#: flavour (Rust's has to tell a char from a lifetime); `raw` is Rust's
#: raw-string prefix set; `text_block` is Java's `\"\"\"`; `template` is
#: JavaScript's backtick; `regex` is JavaScript's regex literal; and
#: `separators` says whether this host's body is split line by line by its
#: emitter (wasm splices the body whole, so only its parens matter);
#: `unicode_escapes` says the host decodes `\uXXXX` before it lexes (Java).
_PROFILES = {
    "ts": {
        "open": "{", "close": "}",
        "line_comments": ("//",),
        "line_ends": "\n\r\u2028\u2029",
        "block_comments": (("/*", "*/"),),
        "block_nests": False,
        "strings": ('"', "'"),
        "char": None,
        "raw": None,
        "raw_quotes": (),
        "multiline_strings": False,
        "text_block": False,
        "template": True,
        "regex": True,
        "separators": True,
        "unicode_escapes": False,
    },
    "rs": {
        "open": "{", "close": "}",
        "line_comments": ("//",),
        "line_ends": "\n\r",
        "block_comments": (("/*", "*/"),),
        "block_nests": True,
        "strings": ('"',),
        "char": "rust",
        "raw": _RUST_RAW_PREFIXES,
        "raw_quotes": (),
        "multiline_strings": True,
        "text_block": False,
        "template": False,
        "regex": False,
        "separators": True,
        "unicode_escapes": False,
    },
    "java": {
        "open": "{", "close": "}",
        "line_comments": ("//",),
        "line_ends": "\n\r",
        "block_comments": (("/*", "*/"),),
        "block_nests": False,
        "strings": ('"',),
        "char": "java",
        "raw": None,
        "raw_quotes": (),
        "multiline_strings": False,
        "text_block": True,
        "template": False,
        "regex": False,
        "separators": True,
        "unicode_escapes": True,
    },
    "go": {
        "open": "{", "close": "}",
        "line_comments": ("//",),
        "line_ends": "\n\r",
        "block_comments": (("/*", "*/"),),
        "block_nests": False,
        "strings": ('"',),
        "char": "go",
        "raw": None,
        "raw_quotes": ("`",),
        "multiline_strings": False,
        "text_block": False,
        "template": False,
        "regex": False,
        "separators": True,
        "unicode_escapes": False,
    },
    "wasm": {
        "open": "(", "close": ")",
        "line_comments": (";;",),
        "line_ends": "\n\r",
        "block_comments": (("(;", ";)"),),
        "block_nests": True,
        "strings": ('"',),
        "char": None,
        "raw": None,
        "raw_quotes": (),
        "multiline_strings": False,
        "text_block": False,
        "template": False,
        "regex": False,
        "separators": False,
        "unicode_escapes": False,
    },
}


class HostBodyError(ValueError):
    """A host body the emitter must not splice as written.

    The backend emitters pass their own ``EmitError`` instead, so the class
    only marks a direct caller's refusal (the scanner's own tests).
    """


def body_lines(body: str) -> list[str]:
    """Split a verbatim host body into the lines the emitter writes out.

    ``str.splitlines`` also breaks on U+0085, U+2028, U+2029, VT, FF and the
    C0 separators, so a comment holding one of them is cut in two and the
    text after the separator is emitted as its own statement (#3131, #3166).
    Normalise the line endings a host language does end a line with
    (``\\r\\n`` and a lone ``\\r``) and split on ``\\n`` only.

    An empty body yields ``[""]``, matching the ``splitlines() or [""]`` the
    tiers used to write.
    """
    normalised = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    if lines and lines[-1] == "":
        # a trailing newline ends the last line, as splitlines() says
        lines.pop()
    return lines or [""]


class _Scan:
    """Every reading of a host body under the host language's lexical rules.

    The rules are deterministic for every host but JavaScript, where a `/`
    can open a regex literal or be the division operator and the choice
    depends on the grammar, not on the characters around it (`o.return / 1`
    divides, `return /1/` is a regex; `if (a) /x/` is a regex, `f(a) / x`
    divides; `let a` then a line break then `/x/` is a regex, because the
    line break ends the statement). Only a `/` the token before it proves to
    be division is read as division alone; every other `/` that can open a
    regex is read both ways, and the body must be balanced under each
    reading, so the scanner never has to guess which one the host picks.

    The scan is a forward pass over lexical states. A state's key is the
    reading context at a position: what a `/` there may be (``_REGEX``,
    ``_DIV``, ``_EITHER``, ``_DOT`` after a member access, ``_HEAD`` after
    `if`, `while`, `for` or `with`), how many template literals are open,
    whether the position is code or template text, and which open
    parentheses are statement heads (so `if (a) /x/` opens a regex and
    `f(a) / x` divides). Its value is, for the top level and for each open
    template interpolation, the interval of brace depths the readings that
    reach it can have. Readings that reach the same key at the same
    position are merged by widening the intervals, so each key at each
    position is stepped once and the work stays linear in the body. The
    lower bound is exact, so an underflow is never missed; a widened upper
    bound only adds refusals, never removes one. A body whose number of
    steps still exceeds `_STEPS_PER_CHAR` per character (readings that keep
    disagreeing) is refused rather than scanned on, and so is a reading
    that nests more than `_MAX_NESTING` templates or parentheses.
    """

    __slots__ = ("s", "n", "p", "depth", "underflow", "separator", "steps",
                 "limit", "exhausted", "too_deep", "stops", "unterminated")

    def __init__(self, src: str, profile: dict) -> None:
        self.s = src
        self.n = len(src)
        self.p = profile
        self.depth = 0
        self.underflow = False
        self.separator: str | None = None
        self.steps = 0
        self.limit = _STEPS_PER_CHAR * self.n + _STEPS_FLOOR
        self.exhausted = False
        self.too_deep = False
        self.unterminated = False
        self.stops = _plain_stops(profile)

    def run(self) -> None:
        # pending: position -> {key: depth intervals}; a key is
        # (before, open templates, in code, open parens)
        start = (_REGEX, 0, True, ())
        if not self.n:
            return
        pending: dict[int, dict[tuple, tuple]] = {0: {start: ((0, 0),)}}
        heap = [0]
        while heap:
            pos = heapq.heappop(heap)
            for key, depths in pending.pop(pos).items():
                self.steps += 1
                if self.steps > self.limit:
                    self.exhausted = True
                    return
                for nxt, nkey, ndepths in self._step(pos, key, depths):
                    if self.underflow:
                        return
                    if nkey[1] > _MAX_NESTING or len(nkey[3]) > _MAX_NESTING:
                        self.too_deep = True
                        return
                    if nxt >= self.n:
                        self._finish(nxt, nkey, ndepths)
                        continue
                    bucket = pending.get(nxt)
                    if bucket is None:
                        bucket = pending[nxt] = {}
                        heapq.heappush(heap, nxt)
                    known = bucket.get(nkey)
                    bucket[nkey] = (ndepths if known is None
                                    else _widen(known, ndepths))

    def _finish(self, end: int, key: tuple, depths: tuple) -> None:
        # A skipper returns one past the end for a string, comment or
        # template the body leaves open: the emitter writes code after the
        # body, and the host would read that code as part of the literal
        # until a later body closes it.
        if end > self.n or key[1] or not key[2]:
            self.unterminated = True
        lo, hi = depths[0]
        if (lo or hi) and not self.depth:
            self.depth = hi or lo

    def _step(self, i: int, key: tuple, depths: tuple) -> list:
        """The states one token at `i` leads to: `(position, key, depths)`."""
        before, templates, code, parens = key
        s, p = self.s, self.p
        if not code:
            return [self._template_text(i, templates, parens, depths)]
        c = s[i]
        if p["regex"] and c == "/" and not s.startswith(("//", "/*"), i):
            end = None if before == _DIV else self._skip_regex(i)
            out = []
            if end is not None:
                out.append((end, (_DIV, templates, True, parens), depths))
            if end is None or before != _REGEX:
                out.append((i + 1, (_REGEX, templates, True, parens), depths))
            return out
        if p["template"] and c == "`":
            return [(i + 1, (_DIV, templates + 1, False, parens), depths)]
        token = self._token(i)
        if token is not None:
            end, literal = token
            if literal:
                before = _DIV
            else:
                self._note_separators(i, end)
                if before == _DIV and _has_line_end(s, i, end):
                    before = _EITHER
            return [(end, (before, templates, True, parens), depths)]
        lo, hi = depths[-1]
        if c == p["open"]:
            grown = depths[:-1] + ((lo + 1, hi + 1),)
            return [(i + 1, (_REGEX, templates, True, parens), grown)]
        if c == p["close"]:
            return self._close(i, templates, parens, depths)
        if p["separators"] and c in _SEPARATORS and self.separator is None:
            self.separator = c
        if p["regex"]:
            end, before, parens = self._js_code(i, before, parens)
        else:
            end = self._plain_run(i)
        return [(end, (before, templates, True, parens), depths)]

    def _close(self, i: int, templates: int, parens: tuple,
               depths: tuple) -> list:
        lo, hi = depths[-1]
        if not templates:
            if lo == 0:
                # some reading closes the function the body is spliced into
                self.underflow = True
                return []
            return [(i + 1, (_EITHER, 0, True, parens), ((lo - 1, hi - 1),))]
        out = []
        if lo == 0:
            # closes the innermost interpolation: back to its template text
            out.append((i + 1, (_DIV, templates, False, parens), depths[:-1]))
        if hi > 0:
            inner = (max(lo, 1) - 1, hi - 1)
            out.append((i + 1, (_EITHER, templates, True, parens),
                        depths[:-1] + (inner,)))
        return out

    def _template_text(self, i: int, templates: int, parens: tuple,
                       depths: tuple) -> tuple:
        s, n = self.s, self.n
        while i < n:
            c = s[i]
            if c == "\\":
                i += 3 if s.startswith("\r\n", i + 1) else 2
                continue
            if c == "`":
                return i + 1, (_DIV, templates - 1, True, parens), depths
            if c == "$" and s.startswith("{", i + 1):
                return (i + 2, (_REGEX, templates, True, parens),
                        depths + ((0, 0),))
            i += 1
        return n, (_DIV, templates, False, parens), depths

    def _plain_run(self, i: int) -> int:
        """Past a run of characters no rule of a non-JavaScript host reads."""
        s, n, stops = self.s, self.n, self.stops
        i += 1
        while i < n and s[i] not in stops:
            i += 1
        return i

    def _js_code(self, i: int, before: str,
                 parens: tuple) -> tuple[int, str, tuple]:
        """Step over one JavaScript code token at `i`: where it ends, what it
        allows a following `/` to be, and the open parentheses after it
        (True for the head of `if`, `while`, `for` or `with`)."""
        s, n = self.s, self.n
        c = s[i]
        if _js_space(c):
            if before == _DIV and c in _JS_LINE_ENDS:
                # `let a` + line break + `/x/`: the line break may end the
                # statement, so the operand no longer proves division
                before = _EITHER
            return i + 1, before, parens
        number = _JS_NUMBER.match(s, i)
        if number:
            # `1.` ends a number, so `1. in /x/` is the `in` operator; `1..x`
            # is a member access on `1.`
            return number.end(), _DIV, parens
        if s.startswith("...", i):
            # a spread or rest element: an expression follows, and the
            # keyword after it is no property name
            return i + 3, _REGEX, parens
        if _js_word_char(c) or c == "#":
            j = i + 1
            while j < n and _js_word_char(s[j]):
                j += 1
            return j, _word_before_slash(s[i:j], before), parens
        if c == ".":
            # a member access, `.` or the dot of `?.` (a numeric literal and
            # a spread are taken above): the name after it is a property
            return i + 1, _DOT, parens
        if c == "(":
            return i + 1, _REGEX, parens + (before == _HEAD,)
        if c == ")":
            if not parens:
                return i + 1, _EITHER, parens
            # the head of `if (c) /x/` is followed by a statement; any other
            # parenthesis (a call, a group, a parameter list) by an operator
            return i + 1, _REGEX if parens[-1] else _DIV, parens[:-1]
        operand = before in (_REGEX, _HEAD)  # an operand is expected here
        if c in "+-" and i + 1 < n and s[i + 1] == c:
            # a prefix `++` is an operator; a postfix one ends an operand
            return i + 2, _REGEX if operand else _EITHER, parens
        if c == "!":
            # a prefix `!` is an operator; TypeScript's non-null `x!` ends
            # an operand
            return i + 1, _REGEX if operand else _EITHER, parens
        if c == ">" and i and s[i - 1] == "=":
            return i + 1, _REGEX, parens  # `=>`
        if c in _REGEX_OPENERS and c != ">":
            return i + 1, _REGEX, parens
        # `]` ends an operand (division next on the same line); a TypeScript
        # type argument list's `>` can end an operand.
        return i + 1, _DIV if c == "]" else _EITHER, parens

    def _note_separators(self, start: int, end: int) -> None:
        if not self.p["separators"] or self.separator is not None:
            return
        chunk = self.s[start:end]
        for c in _SEPARATORS:
            if c in chunk:
                self.separator = c
                return

    def _token(self, i: int) -> tuple[int, bool] | None:
        """The end of the string or comment starting at `i`, and whether it
        is a string (separators inside one are allowed). None for ordinary
        code. Templates and regex literals are read by `_walk`, since they
        can have more than one end."""
        s, p = self.s, self.p
        for opener, closer in p["block_comments"]:
            if s.startswith(opener, i):
                return self._skip_block(i, opener, closer), False
        for opener in p["line_comments"]:
            if s.startswith(opener, i):
                return self._skip_line(i, opener), False
        if p["text_block"] and s.startswith('"""', i):
            return self._skip_java_text_block(i), True
        if p["raw"] is not None:
            raw = self._skip_rust_raw(i)
            if raw is not None:
                return raw, True
        if s[i] in p["raw_quotes"]:
            return self._skip_raw_quote(i, s[i]), True
        if s[i] in p["strings"]:
            return self._skip_quote(i, s[i]), True
        if p["char"] is not None and s[i] == "'":
            end = self._skip_char(i)
            if end is not None:
                return end, True
        return None

    def _skip_line(self, i: int, opener: str) -> int:
        i += len(opener)
        while i < self.n and self.s[i] not in self.p["line_ends"]:
            i += 1
        return i

    def _skip_block(self, i: int, opener: str, closer: str) -> int:
        s, n = self.s, self.n
        i += len(opener)
        depth = 1
        while i < n:
            if self.p["block_nests"] and s.startswith(opener, i):
                depth += 1
                i += len(opener)
            elif s.startswith(closer, i):
                depth -= 1
                i += len(closer)
                if depth == 0:
                    return i
            else:
                i += 1
        return n + 1  # unterminated

    def _skip_quote(self, i: int, quote: str) -> int:
        s, n = self.s, self.n
        i += 1
        while i < n:
            c = s[i]
            if c == "\\":
                # a backslash before CR LF continues the line as one unit
                i += 3 if s.startswith("\r\n", i + 1) else 2
                continue
            if c == quote:
                return i + 1
            if not self.p["multiline_strings"] and c in "\n\r":
                # unterminated: let the caller read the rest as code, so a
                # brace the host would see structural cannot be swallowed
                return i
            i += 1
        return n + 1  # unterminated

    def _skip_raw_quote(self, i: int, quote: str) -> int:
        # Go's backtick raw string: no escapes and the quote only closes it.
        end = self.s.find(quote, i + 1)
        return self.n + 1 if end < 0 else end + 1

    def _skip_char(self, i: int) -> int | None:
        s, n = self.s, self.n
        if self.p["char"] == "rust":
            # `'a` is a lifetime, `'a'` a char literal; only a closed literal
            # (with an immediate closing quote) is skipped. Mirrors the lexer's
            # `_char_literal_end`, which lives on the reference side only.
            j = i + 1
            if j >= n or s[j] in "\n\r":
                return None
            if s[j] == "\\":
                j += 2
                while j < n and s[j] not in "'\n\r":
                    j += 1
                return j + 1 if j < n and s[j] == "'" else None
            if s[j] != "'" and j + 1 < n and s[j + 1] == "'":
                return j + 2
            return None
        i += 1
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                return i + 1
            if c in "\n\r":
                return None
            i += 1
        return None

    def _skip_rust_raw(self, i: int) -> int | None:
        s, n = self.s, self.n
        if i and s[i - 1] in _IDENT_CONT:
            return None
        for prefix in self.p["raw"]:
            if not s.startswith(prefix, i):
                continue
            j = i + len(prefix)
            hashes = 0
            while j < n and s[j] == "#":
                hashes += 1
                j += 1
            if j >= n or s[j] != '"':
                continue
            close = '"' + "#" * hashes
            end = s.find(close, j + 1)
            return n + 1 if end < 0 else end + len(close)
        return None

    def _skip_java_text_block(self, i: int) -> int:
        s, n = self.s, self.n
        i += 3
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s.startswith('"""', i):
                return i + 3
            i += 1
        return n + 1  # unterminated

    def _skip_regex(self, i: int) -> int | None:
        s, n = self.s, self.n
        i += 1
        in_class = False
        while i < n:
            c = s[i]
            if c == "\\":
                if i + 1 < n and s[i + 1] in _JS_LINE_ENDS:
                    return None  # a regex literal cannot continue a line
                i += 2
                continue
            if c in _JS_LINE_ENDS:
                return None
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                i += 1
                while i < n and s[i].isalpha():
                    i += 1
                return i
            i += 1
        return None


#: What the token before a `/` lets it be in JavaScript: a regex literal, the
#: division operator, either (both readings are scanned), or, after a `.`,
#: whatever the following property name allows.
_REGEX, _DIV, _EITHER, _DOT = "regex", "div", "either", "dot"

#: After one of these a `(` opens a statement head, and the `)` closing it
#: is followed by a statement, so a `/` there opens a regex literal.
_HEAD = "head"
_HEAD_KEYWORDS = frozenset({"if", "while", "for", "with"})

#: Words in `_REGEX_KEYWORDS` that are also ordinary identifiers outside a
#: generator, an async function or a `for` head, so either reading is real.
_CONTEXTUAL_KEYWORDS = frozenset({"await", "yield", "of"})

#: JavaScript white space and line terminators (ECMA-262 12.2, 12.3); the
#: rest of the Zs category is checked through `unicodedata`.
_JS_SPACE = frozenset(" \t\n\r\x0b\x0c\u00a0\ufeff\u2028\u2029")


#: JavaScript line terminators (ECMA-262 12.3).
_JS_LINE_ENDS = frozenset("\n\r\u2028\u2029")

#: A JavaScript numeric literal, with the dot that may end it (`1.`) and the
#: fraction, exponent, radix prefix, separators and `n` suffix it may carry.
#: Over-reading `0x1.f` as one token is harmless: either way an operand ends
#: there.
_JS_NUMBER = re.compile(
    r"(?:[0-9]\w*(?:\.\w*)?|\.[0-9]\w*)(?:(?<=[eE])[+-][0-9]\w*)?")

#: Steps the scan may take per character of body before it refuses the body
#: as too ambiguous to check (see `_Scan`). Ordinary bodies take about one.
_STEPS_PER_CHAR = 8
_STEPS_FLOOR = 4096

#: Open template literals or parentheses one reading may nest; past it the
#: body is refused, so a state's key stays small.
_MAX_NESTING = 64


def _has_line_end(s: str, start: int, end: int) -> bool:
    return any(c in _JS_LINE_ENDS for c in s[start:end])


def _widen(a: tuple, b: tuple) -> tuple:
    """Two readings at one state: the interval hull of their depths."""
    return tuple((min(x[0], y[0]), max(x[1], y[1])) for x, y in zip(a, b))


def _plain_stops(profile: dict) -> frozenset:
    """The characters at which a rule of a non-JavaScript host can start."""
    stops = set(profile["open"] + profile["close"])
    stops.update(opener[0] for opener in profile["line_comments"])
    stops.update(opener[0] for opener, _ in profile["block_comments"])
    stops.update(profile["strings"])
    stops.update(profile["raw_quotes"])
    stops.update(prefix[0] for prefix in profile["raw"] or ())
    if profile["char"] is not None:
        stops.add("'")
    if profile["text_block"]:
        stops.add('"')
    stops.update(_SEPARATORS)
    return frozenset(stops)


def _js_space(c: str) -> bool:
    return c in _JS_SPACE or unicodedata.category(c) == "Zs"


def _js_word_char(c: str) -> bool:
    """A character that can continue an identifier, keyword or number. Any
    non-ASCII character that is not white space counts, so a keyword glued
    to a non-ASCII identifier character is never read as the keyword."""
    if c in _IDENT_CONT or c == "\\":
        return True
    return ord(c) > 127 and c not in _SEPARATORS and not _js_space(c)


def _word_before_slash(word: str, before: str) -> str:
    if before == _DOT:
        return _DIV  # a property name: `o.return / 2` divides
    if word in _HEAD_KEYWORDS or (word == "await" and before == _HEAD):
        return _HEAD  # `for await (`
    if word in _CONTEXTUAL_KEYWORDS:
        return _EITHER
    if word in _REGEX_KEYWORDS:
        return _REGEX
    return _DIV


def _decode_unicode_escapes(src: str) -> str:
    """Java's translation of unicode escapes, done before lexing (JLS 3.3).

    A `\\` starts an escape when an odd number of contiguous raw backslashes
    ends at it (so `\\\\u007d` is a backslash pair followed by text) and it
    is followed by one or more `u` and four hex digits. javac reads the
    decoded character as source, so a `}` spelled as an escape closes a
    block. A decoded character never starts another escape. A decoded line
    or paragraph separator becomes a space: the emitter splits the raw text,
    which holds no separator there.
    """
    out: list[str] = []
    i, n, run = 0, len(src), 0
    while i < n:
        c = src[i]
        if c == "\\" and run % 2 == 0 and src.startswith("u", i + 1):
            j = i + 1
            while j < n and src[j] == "u":
                j += 1
            digits = src[j:j + 4]
            if len(digits) == 4 and all(d in _HEX_DIGITS for d in digits):
                decoded = chr(int(digits, 16))
                out.append(" " if decoded in _SEPARATORS else decoded)
                i, run = j + 4, 0
                continue
        run = run + 1 if c == "\\" else 0
        out.append(c)
        i += 1
    return "".join(out)


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def check_host_body(backend: str, body: str, *, where: str = "",
                    error: type[Exception] = HostBodyError) -> None:
    """Refuse a host body that would leave the function it is spliced into.

    `backend` is the canonical body key (``ts``, ``rs``, ``java``, ``go``,
    ``wasm``); ``py`` bodies are indentation-structured and have nothing to
    brace-balance, so callers with a ``py`` body only use `body_lines`.
    `where` names the extern in the refusal; `error` is the caller's own
    refusal class (``EmitError``), so the tier keeps one error vocabulary.
    """
    if backend == "py":
        # Python bodies are indentation-structured: nothing to brace-balance,
        # and the split is all this module owes them.
        return None
    profile = _PROFILES.get(backend)
    if profile is None:
        raise ValueError(f"unknown host body backend {backend!r}")
    if profile["separators"]:
        # what the emitter writes: `body_lines` ends lines at CR LF and CR
        body = body.replace("\r\n", "\n").replace("\r", "\n")
    if profile["unicode_escapes"]:
        body = _decode_unicode_escapes(body)
    scan = _Scan(body, profile)
    try:
        scan.run()
    except RecursionError:
        scan.too_deep = True
    prefix = f"{where}: " if where else ""
    if scan.unterminated:
        raise error(
            f"{prefix}the @{backend} host body ends inside a string, "
            f"comment or template literal under {_HOST[backend]} lexical "
            f"rules, so the code the emitter writes after the body would be "
            f"read as part of it. Close it inside the body."
        )
    if scan.exhausted:
        raise error(
            f"{prefix}the @{backend} host body has more possible readings "
            f"under {_HOST[backend]} lexical rules than the scanner checks "
            f"({scan.limit} steps for {scan.n} characters): too many `/` "
            f"that may open a regex literal or divide, whose readings "
            f"disagree. Split the body or simplify those expressions."
        )
    if scan.too_deep:
        raise error(
            f"{prefix}the @{backend} host body nests more than "
            f"{_MAX_NESTING} template literals or parentheses in one reading "
            f"under {_HOST[backend]} lexical rules, deeper than the scanner "
            f"follows. Split the body or flatten those expressions."
        )
    if scan.underflow:
        raise error(
            f"{prefix}the @{backend} host body is not "
            f"{_balanced(profile)}-balanced under {_HOST[backend]} lexical "
            f"rules: a '{profile['close']}' closes the function the body is "
            f"spliced into, so the rest of the body would run outside the "
            f"extern's call. Split it across the extern boundary or remove "
            f"the stray '{profile['close']}'."
        )
    if scan.depth:
        raise error(
            f"{prefix}the @{backend} host body is not "
            f"{_balanced(profile)}-balanced under {_HOST[backend]} lexical "
            f"rules: it ends with {scan.depth} unclosed '{profile['open']}', "
            f"so it absorbs the function's closing '{profile['close']}'. "
            f"Split it across the extern boundary or close it."
        )
    if scan.separator is not None:
        raise error(
            f"{prefix}the @{backend} host body contains "
            f"{_SEPARATOR_TEXT[scan.separator]} outside a string literal; "
            f"some readers treat that character as a line end, so text that "
            f"reads as a comment can execute as a statement. Remove it or "
            f"put it inside a string literal."
        )
    return None


def _balanced(profile: dict) -> str:
    return "brace" if profile["open"] == "{" else "parenthesis"
