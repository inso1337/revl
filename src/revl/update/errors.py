"""Structured refusals for the update mechanism (docs/updates.md).

Every failure the updater reports to a caller is a code plus a sentence and,
where one exists, the mechanical fix. The same rule the rest of the operator
surface follows: a refusal names what to change, it never returns a bare
boolean.
"""

from __future__ import annotations


class UpdateError(RuntimeError):
    """A refusal a caller acts on: ``code``, ``message``, optional ``fix``."""

    def __init__(self, code: str, message: str, *, fix: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.details = dict(details or {})

    def as_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.fix:
            payload["fix"] = self.fix
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
