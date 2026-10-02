"""The Gemini `generateContent` wire format, on Google AI or on Vertex AI.

* **Google AI** (`api = "google-ai"`, the default):
  `POST {base_url}/models/{model}:generateContent`, credential in the
  `x-goog-api-key` header. The API also accepts `?key=` in the URL; this
  adapter never uses it, because a URL is printed in errors.
* **Vertex AI** (`api = "vertex"`):
  `POST {base_url}/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent`,
  credential sent as `Authorization: Bearer`. The variable `api_key_env` names
  holds an OAuth access token (for example the output of
  `gcloud auth print-access-token`); minting or refreshing one is outside this
  adapter.

Parts flagged `thought: true` are the reasoning channel and go to
`Completion.reasoning`; the other text parts are the answer.
"""

from __future__ import annotations

from .completion import Completion, CompletionRequest


def _url(binding) -> str:
    model = binding.model.removeprefix("models/")
    if binding.api == "vertex":
        return (f"{binding.base_url}/projects/{binding.project}/locations/"
                f"{binding.location}/publishers/google/models/{model}"
                f":generateContent")
    return f"{binding.base_url}/models/{model}:generateContent"


def build(binding, request: CompletionRequest, credential: str | None):
    headers = {"Content-Type": "application/json"}
    if credential:
        if binding.api == "vertex":
            headers["Authorization"] = f"Bearer {credential}"
        else:
            headers["x-goog-api-key"] = credential
    config = {
        "temperature": (binding.temperature if request.temperature is None
                        else request.temperature),
        "maxOutputTokens": request.max_tokens or binding.max_tokens,
    }
    if request.top_p is not None:
        config["topP"] = request.top_p
    if request.seed is not None:
        config["seed"] = request.seed
    body = {
        "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
        "generationConfig": config,
    }
    if request.system:
        body["systemInstruction"] = {"parts": [{"text": request.system}]}
    return _url(binding), headers, body


def parse(raw: dict) -> Completion:
    candidate = raw["candidates"][0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts
                   if isinstance(p, dict) and not p.get("thought"))
    reasoning = "".join(p.get("text", "") for p in parts
                        if isinstance(p, dict) and p.get("thought"))
    usage = raw.get("usageMetadata") or {}
    return Completion(
        text=text,
        model=raw.get("modelVersion"),
        tokens_in=usage.get("promptTokenCount"),
        tokens_out=usage.get("candidatesTokenCount"),
        finish_reason=candidate.get("finishReason"),
        reasoning=reasoning,
        reasoning_tokens=usage.get("thoughtsTokenCount"),
    )
