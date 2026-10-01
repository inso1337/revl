# Model providers: running a model crossing against a real endpoint

A revl document declares where its model calls may go (`model role`,
`route model`, `reaches [...]`) and what they must return (`validated`). None of
that names a vendor, an endpoint or a key: "a role is a name bound to a member
by configuration" ([531-model-placement.md](design/531-model-placement.md),
section 5). This page is that configuration, and the runtime adapters it binds
(issue #1461).

Three wire formats are supported, one page each:

| `provider` | Covers | Page |
| ---------- | ------ | ---- |
| `openai-compatible` | vLLM, SGLang, llama.cpp server, LM Studio, Ollama (`/v1`), hosted services that copy the OpenAI shape | [providers-openai-compatible.md](providers-openai-compatible.md) |
| `anthropic` | Anthropic Messages API | [providers-anthropic.md](providers-anthropic.md) |
| `gemini` | Gemini on Google AI, and on Vertex AI | [providers-gemini.md](providers-gemini.md) |

The adapters use the standard library's HTTP client (`urllib`). revl keeps zero
required runtime dependencies, and no provider SDK is needed or used.

## Running it

```
revl run app.rvl --providers providers.json
revl run app.rvl --providers providers.json --plan    # print the bindings, make no request
```

`--providers` is py tier only. Every key a component `requires` that no
component provides, and whose service declares `model.*` operations, is served
by a model host built from the file. The host is an ambient provision, provided
before any component loads and withdrawn at teardown.

## The program side

A model crossing names its role in its capability:

```revl
model role local on_device reaches []
model role cloud off_device reaches []

service Model {
  emission[model.local] fn private_label(system: Str, text: Str) -> Str
  emission[model.cloud] fn public_label(text: Str) -> Str
}
service Answer {
  emission fn classify(text: Str) -> Str
  emission fn summarize(doc: Str) -> Str
}

component Classifier requires llm: Model provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  route model on summarize { * -> local }
  provide out {
    fn classify(text) {
      let a = emit llm.public_label(text)
      return a
    }
    fn summarize(doc) {
      let a = emit llm.private_label("be brief", doc)
      return a
    }
  }
}
```

`emission[model.cloud]` places `public_label` on the role `cloud` (roadmap item
512 slice 4). The compiler already checks that a crossing's role is one its
action's `route model` block names, that a `confidential` value only reaches an
`on_device` role, and that a role's `reaches` fits the component. This page adds
the run-time half.

## The configuration

JSON, or TOML with the same shape:

```json
{
  "roles": {
    "local": {"provider": "openai-compatible",
              "base_url": "http://127.0.0.1:11434/v1",
              "model": "qwen3:8b"},
    "cloud": {"provider": "anthropic",
              "model": "claude-sonnet-4-5",
              "api_key_env": "ANTHROPIC_API_KEY"}
  }
}
```

Fields every provider accepts:

| Field | Meaning | Default |
| ----- | ------- | ------- |
| `provider` | `openai-compatible`, `anthropic` or `gemini` | required |
| `model` | the provider's model name | required |
| `base_url` | the endpoint | per provider |
| `api_key_env` | the NAME of the environment variable holding the credential | none (required for `anthropic` and `gemini`) |
| `max_tokens` | output token cap | 1024 |
| `temperature` | sampling temperature | 0 |
| `timeout` | seconds per request | 120 |
| `residence` | `off_device` to narrow a loopback endpoint (see below) | derived from the endpoint |
| `reaches` | capability tokens the endpoint's model can reach on its own | `[]` |

An unknown field is refused, not ignored: a field that is silently dropped is a
setting the operator believes is in force.

## Credentials

A credential is read from the environment variable `api_key_env` names, at
request time, on every request. It is held only in local variables of the
request path. It is never written into the IR, the manifest, a trace, a log, an
error message or any `repr`, and `tests/test_model_providers_1461.py` searches
all of them for a live key (including a provider error body that echoes the
key back, and a `revl run --trace` of a real crossing).

What the configuration check refuses, without repeating the value:

- a field named like a credential (`api_key`, `token`, `authorization`, ...);
- an `api_key_env` that is not an environment variable name (the usual cause is
  pasting the key itself there);
- a `base_url` with user info (`https://user:key@host`) or a query string
  (`?key=...`);
- a credential sent over plain `http://` to a host that is not this machine.

`revl run` also refuses to boot when a variable a bound role needs is unset.

## What decides which adapter a crossing uses

The program does; the configuration is checked against it before boot, and
every refusal is listed at once. Each is a `G-MODEL-PLACE` refusal:

1. **The crossing names its role.** An operation whose `model.*` token names no
   declared role (`emission[model.complete]` with no role called `complete`)
   is refused: serving it would mean the adapter layer picked a placement.
2. **The role must be bound.** A crossing on a role the file does not bind is
   refused, and so is a binding for a role the program does not declare.
3. **Residence.** A role declared `on_device` may only be bound to an endpoint
   whose residence is `on_device`. The refusal names the `route model` arms that
   send `confidential` values to the role.
4. **Reach.** A binding's `reaches` must be covered by the role's declared
   `reaches [...]` (`cap_order.covers`). An undeclared role reach is `*`, which
   covers only `*`.
5. **Typed returns.** A model operation returning anything other than `Str`
   must be `validated`, so the completion is checked against the type on return.
6. **Model services serve completions only.** A non-model operation on a
   service the composition requires from a model host is refused.

Each operation is wired to its role's adapter once, when the host is built.
There is no fallback and no run-time choice: a call can reach only the endpoint
its role is bound to. Redirects are refused, so the endpoint the check judged is
the endpoint the prompt reaches.

## Residence of an endpoint

| Endpoint | Residence |
| -------- | --------- |
| `openai-compatible` on `localhost`, `127.0.0.0/8` or `::1` | `on_device` |
| `openai-compatible` anywhere else (including a LAN address) | `off_device` |
| `anthropic`, `gemini`, whatever `base_url` says | `off_device` |

Configuration may narrow a residence (`"residence": "off_device"` on a loopback
proxy that forwards to a cloud API) and may never widen one.

## How arguments and replies are mapped

- A parameter named `system` of type `Str` is the system prompt.
- One remaining `Str` parameter is the user turn as written; otherwise the user
  turn is a JSON object of parameter name to value.
- An operation returning `Str` gets the answer text. A `validated` operation
  gets the answer decoded as JSON, and item 257's seam checks it against the
  declared type (and retries under `retry N`). Text that is not JSON is passed
  through unchanged so the seam refuses it. No code fences are stripped.
- A reasoning channel, where a provider separates one, is kept apart from the
  answer (`Completion.reasoning`) and is never returned as the value.
- An `async` operation is served on a worker thread and awaited.

## What is checked, and what is not

Checked: everything under "Credentials" and "What decides which adapter", before
boot and again whenever a host is built through `revl.providers.build_hosts`.

Not checked:

- **That an endpoint is what the configuration says.** A loopback URL can be a
  proxy to a cloud API. revl sees a URL, not a network path; an operator who
  runs such a proxy must declare `"residence": "off_device"`.
- **Roles declared only in a `use`d module.** The check reads the root files. A
  crossing on a role it cannot see is refused as naming no declared role, never
  guessed at.
- **Per-value confidentiality at run time.** The compile-time ceiling
  (item 514) decides which values reach which crossing; the run-time check
  makes the residence those decisions rely on true. The host does not see a
  value's origin.
- **Token usage on the trace hop.** Usage and latency are on the host's
  `last_completion`, not yet on the `llm` hop of a `--trace` record.
- **Multi-turn conversations and tool calls.** An adapter sends one system
  prompt and one user turn.

## Python API

`revl.providers` exposes the same pieces for a host that is not `revl run`:
`load_config`, `placement_of_files` / `placement_of_program`, `build_hosts`
(which runs the check and raises `PlacementRefused`), `Adapter`,
`CompletionRequest`, `Completion`, and `request_json`, the one HTTP client that
`bench/model_pin.py`, `bench/run.py` and `bench/decode_grammar_probe.py` also
use.

## Tests

`tests/test_model_providers_1461.py` runs every adapter against a loopback fake
server per wire format, so CI makes no network call. One test is opt-in: with
`REVL_LIVE_OLLAMA_MODEL` set to a pulled model tag, it sends one completion to
the Ollama on `127.0.0.1:11434` through the OpenAI-compatible adapter, and it
skips cleanly when the variable is unset or no Ollama answers.
