# Provider: OpenAI-compatible chat/completions

`"provider": "openai-compatible"` speaks the chat/completions shape that vLLM,
SGLang, the llama.cpp server, LM Studio and Ollama's `/v1` shim all serve. It is
the only provider that can serve an `on_device` role. The general rules
(credentials, placement, argument mapping) are in
[model-providers.md](model-providers.md).

## Config

```json
{"roles": {"local": {
  "provider": "openai-compatible",
  "base_url": "http://127.0.0.1:11434/v1",
  "model": "qwen3:8b",
  "max_tokens": 2048
}}}
```

| Field | Notes |
| ----- | ----- |
| `base_url` | Required. Include the version segment (`/v1`); the adapter appends `/chat/completions`. |
| `model` | The server's model name, sent as-is. |
| `api_key_env` | Optional. A local server usually needs none. When set, the value is sent as `Authorization: Bearer ...`. |
| `residence` | Derived: `on_device` for `localhost`, `127.0.0.0/8` and `::1`, otherwise `off_device`. May be set to `off_device` for a loopback proxy. |

Common servers:

| Server | `base_url` |
| ------ | ---------- |
| Ollama | `http://127.0.0.1:11434/v1` |
| llama.cpp server | `http://127.0.0.1:8080/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |

## Env vars

Only the one `api_key_env` names, if any. It is read at request time.

## The request

`POST {base_url}/chat/completions` with `model`, `messages` (a `system` message
when the operation has a `system` parameter, then one `user` message),
`temperature`, `max_tokens` and `"stream": false`. `top_p` and `seed` are sent
only when a caller sets them (the `bench/` tools do).

## The reply

- The answer is `choices[0].message.content`.
- A reasoning channel under `reasoning` (Ollama) or `reasoning_content` is read
  into `Completion.reasoning` and is never returned as the value.
- `usage.prompt_tokens` and `usage.completion_tokens` are the token counts;
  `completion_tokens` includes reasoning tokens, and
  `completion_tokens_details.reasoning_tokens` is kept when reported.

## Structured output

`structured_output` is `json-schema` (default), `gbnf` or `none`. A `validated`
operation's wire schema goes in `response_format` (`json-schema`), or its GBNF
text in the top-level `grammar` field llama.cpp's server reads (`gbnf`). Both
modes CLAIM the decode, so the completion is held to the artifact on return.
Ollama honours `response_format` and ignores `grammar` without an error, so use
`json-schema` there; `gbnf` is for llama.cpp. See
[model-providers.md](model-providers.md#structured-output-constrained-decoding-issue-1462).

## What is checked, and what is not

Checked: the `on_device` claim against the host in `base_url`; no credential in
the URL; no credential over plain `http://` to another machine; no redirect is
followed.

Not checked: that a loopback server is not itself forwarding to another machine;
that the server honours `seed` or `temperature`; the server's own model
identity (use `bench/model_pin.py` to pin one).
