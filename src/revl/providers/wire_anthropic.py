"""The Anthropic Messages wire format (`POST {base_url}/v1/messages`).

The credential goes in the `x-api-key` header, never in the URL. The API
version is the `anthropic-version` header, `2023-06-01` unless the binding
sets `anthropic_version`.

The reply is a list of content blocks. `text` blocks are the answer and are
joined in order; `thinking` blocks are the reasoning channel and go to
`Completion.reasoning`, never into the answer. Under structured output (issue
#1462) the adapter forces one tool whose `input_schema` is the crossing's wire
schema, and the `tool_use` block's input is the value. The API has no sampling seed,
so `CompletionRequest.seed` is not sent.
"""

from __future__ import annotations

from .completion import Completion, CompletionRequest
from .structured import TOOL_NAME

#: The `provider` value a binding names to use this wire format, and the
#: fields it accepts beyond the common ones. `revl.providers.config` builds
#: its closed vocabulary from these, so the format list is declared once.
PROVIDER = "anthropic"
FIELDS = frozenset({"anthropic_version"})


def build(binding, request: CompletionRequest, credential: str | None):
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": binding.anthropic_version,
    }
    if credential:
        headers["x-api-key"] = credential
    body = {
        "model": binding.model,
        "max_tokens": request.max_tokens or binding.max_tokens,
        "messages": [{"role": "user", "content": request.prompt}],
        "temperature": (binding.temperature if request.temperature is None
                        else request.temperature),
    }
    if request.system:
        body["system"] = request.system
    if request.top_p is not None:
        body["top_p"] = request.top_p
    structured = request.structured
    if structured is not None and structured.mode == "tool":
        input_schema, _wrapped = structured.artifact
        body["tools"] = [{
            "name": TOOL_NAME,
            "description": "Give your answer as this tool's input.",
            "input_schema": input_schema,
        }]
        body["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
    return binding.base_url + "/v1/messages", headers, body


def parse(raw: dict, request: CompletionRequest | None = None) -> Completion:
    blocks = raw["content"]
    if not isinstance(blocks, list):
        raise TypeError("content is not a list of blocks")
    value, has_value = None, False
    structured = getattr(request, "structured", None)
    if structured is not None and structured.mode == "tool":
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") == TOOL_NAME:
                value, has_value = block.get("input"), True
                _schema, wrapped = structured.artifact
                if wrapped:
                    # the root was wrapped as {"value": ...}; a reply without
                    # the key is passed on as-is for the validator to refuse
                    if isinstance(value, dict) and "value" in value:
                        value = value["value"]
                break
    text = "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
    reasoning = "".join(b.get("thinking", "") for b in blocks
                        if isinstance(b, dict) and b.get("type") == "thinking")
    usage = raw.get("usage") or {}
    return Completion(
        text=text,
        model=raw.get("model"),
        tokens_in=usage.get("input_tokens"),
        tokens_out=usage.get("output_tokens"),
        finish_reason=raw.get("stop_reason"),
        reasoning=reasoning,
        value=value,
        has_value=has_value,
    )
