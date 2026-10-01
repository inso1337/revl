# Provider: Gemini (Google AI and Vertex AI)

`"provider": "gemini"` speaks Gemini's `generateContent`, through either front
door. Both are hosted APIs, so the residence is always `off_device`. The
general rules are in [model-providers.md](model-providers.md).

## Config

Google AI (the default, `"api": "google-ai"`):

```json
{"roles": {"cloud": {
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "api_key_env": "GEMINI_API_KEY"
}}}
```

Vertex AI:

```json
{"roles": {"cloud": {
  "provider": "gemini",
  "api": "vertex",
  "project": "my-project",
  "location": "europe-west4",
  "model": "gemini-2.5-pro",
  "api_key_env": "VERTEX_ACCESS_TOKEN"
}}}
```

| Field | Notes |
| ----- | ----- |
| `api` | `google-ai` (default) or `vertex`. |
| `model` | Goes into the request path, so only letters, digits, `.`, `_` and `-`. A leading `models/` is accepted and dropped. |
| `api_key_env` | Required. Google AI: the API key. Vertex: an OAuth access token. |
| `project`, `location` | Vertex only, and required there. Lower-case letters, digits and `-`. |
| `base_url` | Google AI default `https://generativelanguage.googleapis.com/v1beta`; Vertex default `https://{location}-aiplatform.googleapis.com/v1`. |

## Env vars

The one `api_key_env` names, read at request time.

- Google AI sends it in the `x-goog-api-key` header. The API also accepts
  `?key=` in the URL; the adapter never uses that, because a URL is printed in
  errors, and a `base_url` with a query string is refused.
- Vertex sends it as `Authorization: Bearer ...`. Minting or refreshing the
  token (for example `gcloud auth print-access-token`) is the operator's job;
  an expired token is a `ProviderError` with the HTTP status.

## The request

- Google AI: `POST {base_url}/models/{model}:generateContent`.
- Vertex: `POST {base_url}/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent`.

The body has one `user` turn in `contents`, `systemInstruction` when the
operation has a `system` parameter, and `generationConfig` with `temperature`,
`maxOutputTokens`, and `topP` / `seed` when a caller sets them.

## The reply

- The answer is the concatenation of the first candidate's text parts.
- Parts flagged `"thought": true` go to `Completion.reasoning` and are never
  returned as the value.
- `usageMetadata.promptTokenCount`, `candidatesTokenCount` and
  `thoughtsTokenCount` are the token counts; `finishReason` is the finish
  reason; `modelVersion` is the model.

## What is checked, and what is not

Checked: the credential comes from the environment only and never enters the
URL; the role is `off_device`; the Vertex project, location and model are
path-safe; no redirect is followed; an echoed credential is redacted.

Not checked: safety settings, grounding and other Gemini tools, none of which
the adapter sends. A candidate stopped by a safety filter comes back as an empty
answer with its `finishReason` (a `validated` crossing then refuses it), and a
prompt blocked before any candidate exists is a `ProviderError`.
