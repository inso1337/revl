# Provider: Anthropic Messages

`"provider": "anthropic"` speaks the Anthropic Messages API. It is a hosted API,
so its residence is always `off_device` and it can serve only a role declared
`off_device`. The general rules are in [model-providers.md](model-providers.md).

## Config

```json
{"roles": {"cloud": {
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "api_key_env": "ANTHROPIC_API_KEY",
  "max_tokens": 1024
}}}
```

| Field | Notes |
| ----- | ----- |
| `api_key_env` | Required. The NAME of the variable holding the API key. |
| `base_url` | Default `https://api.anthropic.com`. The adapter appends `/v1/messages`. |
| `anthropic_version` | The `anthropic-version` header. Default `2023-06-01`. |
| `max_tokens` | Sent on every request (the API requires it). Default 1024. |

## Env vars

The one `api_key_env` names. Its value is sent in the `x-api-key` header and
nowhere else, and is read at request time.

## The request

`POST {base_url}/v1/messages` with `model`, `max_tokens`, one `user` message,
`temperature`, and `system` when the operation has a `system` parameter. The
Messages API has no sampling seed, so a seed is never sent.

## The reply

- The answer is the concatenation of the `text` content blocks, in order.
- `thinking` blocks go to `Completion.reasoning` and are never returned as the
  value.
- `usage.input_tokens` and `usage.output_tokens` are the token counts;
  `stop_reason` is the finish reason.

## Structured output

`structured_output` is `tool` (default) or `none`. For a `validated` operation
the adapter sends one tool, `respond`, whose `input_schema` is the wire schema
(wrapped as `{"value": ...}` when the root is not an object), and forces it with
`tool_choice`. The `tool_use` block's input is the value. Tool input is
schema-guided rather than grammar-constrained, so the adapter claims nothing and
every type counts as approximated; the value is validated on return. See
[model-providers.md](model-providers.md#structured-output-constrained-decoding-issue-1462).

## What is checked, and what is not

Checked: the key comes from the environment only; the role is `off_device`;
`https://` is required when the host is not this machine; no redirect is
followed; an error body that echoes the key is redacted before it becomes a
message.

Not checked: tool use, extended thinking configuration, prompt caching and
batches, none of which the adapter sends. Rate limits and retries on a 429 or a
5xx are not handled by the adapter; a failed request is a `ProviderError`.
