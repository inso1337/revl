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
