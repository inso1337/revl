"""The request and the reply every wire format translates to and from."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionRequest:
    """One single-turn completion. `None` means "the binding's default" for
    `max_tokens` and `temperature`, and "not sent" for the rest."""

    prompt: str
    system: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    #: the constraint to attach (issue #1462), a `structured.Structured`, or
    #: None for an unconstrained completion
    structured: object = None


@dataclass(frozen=True)
class Completion:
    """What came back. `text` is the answer channel only; a reasoning channel,
    where the provider separates one, is in `reasoning` and never mixed in."""

    text: str
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    finish_reason: str | None = None
    reasoning: str = ""
    reasoning_tokens: int | None = None
    latency_seconds: float | None = None
    provider: str = ""
    #: a value the provider returned already decoded (the Anthropic adapter's
    #: forced tool input), valid only when `has_value` is True
    value: object = None
    has_value: bool = False

    def usage(self) -> dict:
        """The host-usage mapping `runtime.revl_host_usage` reads."""
        return {"model": f"{self.provider}:{self.model}" if self.model
                else None,
                "usage": {"tokensIn": self.tokens_in,
                          "tokensOut": self.tokens_out}}
