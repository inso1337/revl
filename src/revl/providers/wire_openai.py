"""The OpenAI-compatible chat/completions wire format.

One format covers the local servers revl is most often pointed at: vLLM,
SGLang, the llama.cpp server, LM Studio and Ollama's `/v1` shim, as well as
hosted services that copy the OpenAI shape. `base_url` includes the version
segment (`http://127.0.0.1:11434/v1`); the adapter appends
`/chat/completions`.

Two reading rules were measured on a local reasoning model, not guessed, and
came from `bench/run.py`'s old private client:

* the answer is `message.content`. A server that separates a reasoning channel
  puts it under `reasoning` (Ollama) or `reasoning_content` (the name several
  servers copied); both are read, in that order, into `Completion.reasoning`
  and never into `text`;
* `completion_tokens` counts the reasoning channel too, which is the number a
  cost reading wants, and `completion_tokens_details.reasoning_tokens` is kept
  separately when the server reports it.
"""

from __future__ import annotations

from .completion import Completion, CompletionRequest

#: The `provider` value a binding names to use this wire format, and the
#: fields it accepts beyond the common ones. `revl.providers.config` builds
#: its closed vocabulary from these, so the format list is declared once.
PROVIDER = "openai-compatible"
FIELDS = frozenset()

#: Reasoning-channel keys, in the order they are read.
REASONING_KEYS = ("reasoning", "reasoning_content")


def build(binding, request: CompletionRequest, credential: str | None):
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    body = {
        "model": binding.model,
        "messages": messages,
        "temperature": (binding.temperature if request.temperature is None
                        else request.temperature),
        "max_tokens": request.max_tokens or binding.max_tokens,
        "stream": False,
    }
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.seed is not None:
        body["seed"] = request.seed
    return binding.base_url + "/chat/completions", headers, body


def parse(raw: dict) -> Completion:
    choice = raw["choices"][0]
    message = choice["message"]
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise TypeError("message.content is not a string")
    reasoning = ""
    for key in REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            reasoning = value
            break
    usage = raw.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return Completion(
        text=text,
        model=raw.get("model"),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        finish_reason=choice.get("finish_reason"),
        reasoning=reasoning,
        reasoning_tokens=details.get("reasoning_tokens"),
    )
