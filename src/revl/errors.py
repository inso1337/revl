"""Compiler diagnostics.

The error messages are a deliverable (DESIGN.md §9): every rejection names
the guarantee it enforces and, where possible, suggests the fix.
"""

from __future__ import annotations


class RevlError(Exception):
    def __init__(self, filename: str, line: int, message: str, hint: str | None = None):
        self.filename = filename
        self.line = line
        self.message = message
        self.hint = hint
        rendered = f"{filename}:{line}: {message}"
        if hint:
            rendered += f"\n  {hint}"
        super().__init__(rendered)
