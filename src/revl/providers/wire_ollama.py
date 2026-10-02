"""Ollama's native API: completions, and the load and unload a provision makes
(roadmap item 515, slice S2).

The OpenAI-compatible shim (`wire_openai`, `base_url` ending in `/v1`) serves
completions from an Ollama server but cannot say where a model is loaded or
take it out of memory. This wire speaks the native API at the server root
(`base_url` is `http://127.0.0.1:11434`, no version segment), which can:

* **load**: `POST /api/generate` with no prompt, `keep_alive: -1` and the
  load options of the device the schedule chose. The model stays resident
  until it is unloaded; no idle timer takes it out behind the provision's back.
* **unload**: `POST /api/generate` with `keep_alive: 0`.
* **report residency**: `GET /api/ps` lists what is loaded, with the bytes
  held in GPU memory (`size_vram`), which is how a load on a `cpu` device is
  told apart from one on a `gpu`.
* **complete**: `POST /api/chat`. Every completion carries the same
  `keep_alive: -1` and device options as the load, because a request with
  other options makes the server reload the model where IT chooses, and a
  request with the default keep-alive would start an idle timer.

The answer is `message.content`; a reasoning model's separate channel is
`message.thinking` and goes to `Completion.reasoning`, never into the text.
"""

from __future__ import annotations

from .completion import Completion, CompletionRequest

#: The `provider` value a binding names to use this wire format, and the
#: fields it accepts beyond the common ones. `revl.providers.config` builds
#: its closed vocabulary from these, so the format list is declared once.
PROVIDER = "ollama"
FIELDS = frozenset({"devices"})

#: The load options a binding's `devices` table may set per device. CLOSED:
#: an option the server reads differently per version is not guessed at.
#: `num_gpu` is how many layers go to the GPU (0 keeps the model on the CPU);
#: `main_gpu` picks which GPU when there are several.
DEVICE_OPTIONS = ("num_gpu", "main_gpu")

#: `keep_alive` for a provision's lifetime: resident until revl unloads it.
PINNED = -1


def build(binding, request: CompletionRequest, credential: str | None,
          device_options=None):
    headers = _headers(credential)
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    options = dict(device_options or {})
    options["temperature"] = (binding.temperature if request.temperature is None
                              else request.temperature)
    options["num_predict"] = request.max_tokens or binding.max_tokens
    if request.top_p is not None:
        options["top_p"] = request.top_p
    if request.seed is not None:
        options["seed"] = request.seed
    body = {"model": binding.model, "messages": messages, "stream": False,
            "keep_alive": PINNED, "options": options}
    return binding.base_url + "/api/chat", headers, body


def parse(raw: dict, request: CompletionRequest | None = None) -> Completion:
    # `request` is the adapter's uniform wire interface (issue #1462). This
    # wire requests no structured output, so there is nothing to read back.
    del request
    message = raw["message"]
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise TypeError("message.content is not a string")
    thinking = message.get("thinking")
    return Completion(
        text=text,
        model=raw.get("model"),
        tokens_in=raw.get("prompt_eval_count"),
        tokens_out=raw.get("eval_count"),
        finish_reason=raw.get("done_reason"),
        reasoning=thinking if isinstance(thinking, str) else "",
    )


def load_request(binding, credential: str | None, device_options):
    """The request that loads `binding.model` with `device_options`."""
    body = {"model": binding.model, "keep_alive": PINNED,
            "options": dict(device_options or {})}
    return binding.base_url + "/api/generate", _headers(credential), body


def unload_request(binding, credential: str | None):
    """The request that takes `binding.model` out of memory."""
    body = {"model": binding.model, "keep_alive": 0}
    return binding.base_url + "/api/generate", _headers(credential), body


def residency_request(binding, credential: str | None):
    """The request that lists what the server holds loaded (a GET)."""
    return binding.base_url + "/api/ps", _headers(credential)


def resident_entry(raw: dict, model: str) -> dict | None:
    """The `/api/ps` entry for `model`, or None when it is not loaded.

    The server reports a tag-less name with `:latest` appended, so `qwen3`
    and `qwen3:latest` are the same model here.
    """
    wanted = {model, model if ":" in model else model + ":latest"}
    for entry in raw.get("models") or ():
        if not isinstance(entry, dict):
            continue
        if entry.get("name") in wanted or entry.get("model") in wanted:
            return entry
    return None


def _headers(credential: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    return headers
