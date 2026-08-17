"""Source-to-source rewriter for migration §9 (docs/syntax-2.0.md §9).

This is deliberately a small mechanical scanner, not a parser.  It rewrites
1.x ``"$name"`` interpolation and ``"$$"`` literal-dollar escapes inside
double-quoted strings, and leaves everything else — including already
migrated backtick templates — untouched.
"""

from __future__ import annotations


def migrate_source(source: str, filename: str = "<source>") -> tuple[str, list[str]]:
    """Rewrite 1.x ``$`` interpolation in *source* to 2.0 backtick templates.

    Returns ``(new_source, warnings)``.  Warnings are human-readable strings
    identifying the file, line, and why a string was left unchanged.
    """
    out: list[str] = []
    warnings: list[str] = []
    i, n = 0, len(source)

    while i < n:
        c = source[i]
        if c == '"':
            end, terminated = _double_quote_end(source, i)
            raw = source[i:end]
            if not terminated:
                out.append(raw)
                warnings.append(
                    _warning(filename, source, i, "unterminated string literal; leaving unchanged")
                )
                i = end
                continue

            content = raw[1:-1]
            if "$" not in content:
                out.append(raw)
            else:
                result = _rewrite_string_content(content)
                if result is None:
                    out.append(raw)
                    warnings.append(
                        _warning(filename, source, i, "ambiguous `$` in string; leaving unchanged")
                    )
                elif result[0] == "template":
                    out.append("`" + result[1] + "`")
                elif result[0] == "plain_string":
                    out.append('"' + result[1] + '"')
                else:
                    out.append(raw)
            i = end
        elif c == "`":
            # Already-migrated backtick template: copy it through verbatim.
            end = source.find("`", i + 1)
            if end == -1:
                out.append(source[i:])
                i = n
            else:
                out.append(source[i:end + 1])
                i = end + 1
        elif c == "/" and i + 1 < n and source[i + 1] == "/":
            # Line comments are not revl source; do not interpret quotes in them.
            end = source.find("\n", i)
            if end == -1:
                out.append(source[i:])
                i = n
            else:
                out.append(source[i:end])
                i = end
        else:
            out.append(c)
            i += 1

    return "".join(out), warnings


def _double_quote_end(source: str, start: int) -> tuple[int, bool]:
    """Return ``(index_after_string, terminated)`` for a double-quoted string.

    The old 1.x lexer had no backslash escapes, so the first ``"`` closes the
    string and a newline before that closer is invalid.
    """
    n = len(source)
    j = start + 1
    while j < n:
        if source[j] == '"':
            return j + 1, True
        if source[j] == "\n":
            return j, False
        j += 1
    return n, False


def _rewrite_string_content(content: str) -> tuple[str, str | None] | None:
    """Rewrite the inside of a double-quoted string.

    Returns one of:

    - ``("template", text)`` — use a backtick template
    - ``("plain_string", text)`` — keep double quotes, but with literal ``$``
    - ``("plain", None)`` — already a plain 2.0 string; leave it unchanged
    - ``None`` — ambiguous old/new syntax; caller should leave it and warn
    """
    out: list[str] = []
    i, n = 0, len(content)
    saw_old = False
    saw_interp = False
    saw_bare = False

    while i < n:
        c = content[i]
        if c == "$":
            if i + 1 < n and content[i + 1] == "$":
                out.append("$")
                saw_old = True
                i += 2
                continue
            j = i + 1
            if j < n and (content[j].isalpha() or content[j] == "_"):
                j += 1
                while j < n and (content[j].isalnum() or content[j] == "_"):
                    j += 1
                out.append("${" + content[i + 1:j] + "}")
                saw_old = True
                saw_interp = True
                i = j
                continue
            saw_bare = True
            out.append("$")
            i += 1
            continue
        out.append(c)
        i += 1

    if saw_bare and saw_old:
        return None
    if saw_old:
        return ("template" if saw_interp else "plain_string", "".join(out))
    return ("plain", None)


def _warning(filename: str, source: str, index: int, message: str) -> str:
    line = source.count("\n", 0, index) + 1
    return f"{filename}:{line}: {message}"
