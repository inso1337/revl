"""Compiler diagnostics.

The error messages are a deliverable (DESIGN.md §9): every rejection names
the guarantee it enforces and, where possible, suggests the fix.
"""

from __future__ import annotations

from .why import WhyTrace, render as render_why


class RevlError(Exception):
    def __init__(self, filename: str, line: int, message: str, hint: str | None = None,
                 code: str | None = None, category: str | None = None,
                 expected: str | None = None, actual: str | None = None,
                 why: WhyTrace | None = None):
        self.filename = filename
        self.line = line
        self.message = message
        self.hint = hint
        # structured fields for the agent-facing projection (diagnostics.py);
        # all optional — most rejections are classified from their message
        self.code = code
        self.category = category
        self.expected = expected
        self.actual = actual
        # the derivation behind the verdict, where the check ran a search
        # (G4's fixed point, G3's cycle, G2's provider table) — see why.py.
        # It is appended *after* the message and hint so the first line of
        # every rejection is unchanged.
        self.why = why
        rendered = f"{filename}:{line}: {message}"
        if hint:
            rendered += f"\n  {hint}"
        trace = render_why(why)
        if trace:
            rendered += "\n" + trace
        super().__init__(rendered)


class RevlErrors(RevlError):
    """Carrier for a multi-refusal compile (roadmap item 386, Stage 1).

    The frontend used to abort on the FIRST `RevlError`; Stage 1 collects every
    recoverable refusal and raises them together at the end of
    `check_and_lower`. This carrier IS a `RevlError` so all ~70 `except
    RevlError` sites and `classify()` keep working with no change: its primary
    fields (`filename`/`line`/`message`/`code`/`category`/…) MIRROR THE FIRST
    diagnostic, so every legacy single-error consumer sees exactly what it saw
    before. The full ordered list lives on `.errors`; `diagnostics.report`,
    `plan._add` and the LSP iterate it. `__str__` renders the whole list plus a
    census line, so the many `print(f"error: {error}")` sites upgrade for free
    — and, for a lone refusal, renders byte-identically to that one error.
    """

    def __init__(self, errors: "list[RevlError]"):
        if not errors:
            raise ValueError("RevlErrors requires at least one error")
        self.errors: list[RevlError] = list(errors)
        first = self.errors[0]
        super().__init__(first.filename, first.line, first.message,
                         hint=first.hint, code=first.code, category=first.category,
                         expected=first.expected, actual=first.actual, why=first.why)

    def __str__(self) -> str:
        # A single refusal renders exactly as a plain `RevlError` would: no
        # census line, so the text path stays byte-identical for the common case.
        if len(self.errors) == 1:
            return str(self.errors[0])
        files = {e.filename for e in self.errors}
        n, m = len(self.errors), len(files)
        census = (f"{n} refusals across {m} "
                  f"{'file' if m == 1 else 'files'}")
        return "\n".join([str(e) for e in self.errors] + [census])
