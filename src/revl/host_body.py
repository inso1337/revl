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

#: After these words JavaScript reads `/` as the start of a regex literal
#: rather than as division, unless the word follows a `.` (then it is a
#: property name). `_Scan._js_code` covers the punctuation, and scans both
#: readings where the token before the `/` does not decide it.
_REGEX_KEYWORDS = frozenset({
    "await", "case", "delete", "do", "else", "in", "instanceof", "new",
    "of", "return", "throw", "typeof", "void", "yield",
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
    divides). Where the token before a `/` does not settle it, the scan
    follows BOTH readings and the body must be balanced under each of them,
    so the scanner never has to guess which one the host picks.

    A reading is a state `(position, depth, before)`; `before` is what the
    token before the next `/` allows (``_REGEX``, ``_DIV``, ``_EITHER``, or
    ``_DOT`` after a member access). States are deduplicated, so the number
    of readings stays bounded by the body's length.
    """

    __slots__ = ("s", "n", "p", "depth", "underflow", "separator", "_ends")

    def __init__(self, src: str, profile: dict) -> None:
        self.s = src
        self.n = len(src)
        self.p = profile
        self.depth = 0
        self.underflow = False
        self.separator: str | None = None
        self._ends: dict[int, tuple[int, ...]] = {}

    def run(self) -> None:
        work = [(0, 0, _REGEX)]
        seen: set[tuple[int, int, str]] = set()
        while work and not self.underflow:
            state = work.pop()
            if state in seen:
                continue
            seen.add(state)
            self._walk(*state, work, None)

    def _walk(self, i: int, depth: int, before: str,
              work: list, ends: set | None) -> None:
        """Follow one reading from `i` until the body ends or a `/` forks.

        `ends` is None at the top of the body, where a close below depth 0
        leaves the function (an underflow). Inside a template interpolation
        it collects the position after the `}` that closes the
        interpolation, and separators are not noted (the template is a
        literal at the top level).
        """
        s, n, p = self.s, self.n, self.p
        while i < n:
            c = s[i]
            if p["regex"] and c == "/" and not s.startswith(("//", "/*"), i):
                end = None if before == _DIV else self._skip_regex(i)
                if end is None:
                    i, before = i + 1, _REGEX
                    continue
                if before == _REGEX:
                    i, before = end, _DIV
                    continue
                work.append((end, depth, _DIV))
                work.append((i + 1, depth, _REGEX))
                return
            if p["template"] and c == "`":
                after = self._template_ends(i)
                if len(after) == 1:
                    i, before = after[0], _DIV
                    continue
                work.extend((end, depth, _DIV) for end in after)
                return
            token = self._token(i)
            if token is not None:
                end, literal = token
                if literal:
                    before = _DIV
                elif ends is None:
                    self._note_separators(i, end)
                i = end
                continue
            if c == p["open"]:
                depth += 1
                i, before = i + 1, _REGEX
                continue
            if c == p["close"]:
                depth -= 1
                if depth < 0:
                    if ends is None:
                        self.underflow = True
                    else:
                        ends.add(i + 1)
                    return
                i, before = i + 1, _EITHER
                continue
            if (ends is None and p["separators"] and c in _SEPARATORS
                    and self.separator is None):
                self.separator = c
            if p["regex"]:
                i, before = self._js_code(i, before)
            else:
                i += 1
        if ends is not None:
            ends.add(n)
        elif depth and not self.depth:
            self.depth = depth

    def _js_code(self, i: int, before: str) -> tuple[int, str]:
        """Step over one JavaScript code token at `i` and say what it allows
        a following `/` to be."""
        s, n = self.s, self.n
        c = s[i]
        if _js_space(c):
            return i + 1, before
        if _js_word_char(c) or c == "#":
            j = i + 1
            while j < n and _js_word_char(s[j]):
                j += 1
            return j, _word_before_slash(s[i:j], before)
        if c == ".":
            return i + 1, _DOT
        operand = before == _REGEX  # an operand is expected here
        if c in "+-" and i + 1 < n and s[i + 1] == c:
            # a prefix `++` is an operator; a postfix one ends an operand
            return i + 2, _REGEX if operand else _EITHER
        if c == "!":
            # a prefix `!` is an operator; TypeScript's non-null `x!` ends
            # an operand, so `x! / y` divides
            return i + 1, _REGEX if operand else _EITHER
        if c == "]":
            return i + 1, _DIV
        if c == ">" and i and s[i - 1] == "=":
            return i + 1, _REGEX  # `=>`
        if c in _REGEX_OPENERS and c != ">":
            return i + 1, _REGEX
        # `)` ends both a condition (regex next) and an operand (division
        # next); a TypeScript type argument list's `>` can end an operand.
        return i + 1, _EITHER

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
        return n

    def _skip_quote(self, i: int, quote: str) -> int:
        s, n = self.s, self.n
        i += 1
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == quote:
                return i + 1
            if not self.p["multiline_strings"] and c in "\n\r":
                # unterminated: let the caller read the rest as code, so a
                # brace the host would see structural cannot be swallowed
                return i
            i += 1
        return n

    def _skip_raw_quote(self, i: int, quote: str) -> int:
        # Go's backtick raw string: no escapes and the quote only closes it.
        end = self.s.find(quote, i + 1)
        return self.n if end < 0 else end + 1

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
            return n if end < 0 else end + len(close)
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
        return n

    def _template_ends(self, i: int) -> tuple[int, ...]:
        """Every position the template literal opening at `i` can end at.

        An interpolation may hold a `/` with two readings, so a template can
        have more than one end. Memoised by start: the ends do not depend on
        the reading that reached the template."""
        known = self._ends.get(i)
        if known is not None:
            return known
        s, n = self.s, self.n
        ends: set[int] = set()
        work, seen = [i + 1], set()
        while work:
            j = work.pop()
            if j in seen:
                continue
            seen.add(j)
            while j < n:
                c = s[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "`":
                    ends.add(j + 1)
                    break
                if c == "$" and j + 1 < n and s[j + 1] == "{":
                    work.extend(self._interpolation_ends(j + 2))
                    break
                j += 1
            else:
                ends.add(n)
        result = tuple(sorted(ends))
        self._ends[i] = result
        return result

    def _interpolation_ends(self, i: int) -> set[int]:
        """Every position after the `}` closing the interpolation at `i`."""
        ends: set[int] = set()
        work = [(i, 0, _REGEX)]
        seen: set[tuple[int, int, str]] = set()
        while work:
            state = work.pop()
            if state in seen:
                continue
            seen.add(state)
            self._walk(*state, work, ends)
        return ends

    def _skip_regex(self, i: int) -> int | None:
        s, n = self.s, self.n
        i += 1
        in_class = False
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c in "\n\r":
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

#: Words in `_REGEX_KEYWORDS` that are also ordinary identifiers outside a
#: generator, an async function or a `for` head, so either reading is real.
_CONTEXTUAL_KEYWORDS = frozenset({"await", "yield", "of"})

#: JavaScript white space and line terminators (ECMA-262 12.2, 12.3); the
#: rest of the Zs category is checked through `unicodedata`.
_JS_SPACE = frozenset(" \t\n\r\x0b\x0c ﻿  ")


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
    if profile["unicode_escapes"]:
        body = _decode_unicode_escapes(body)
    scan = _Scan(body, profile)
    scan.run()
    prefix = f"{where}: " if where else ""
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
