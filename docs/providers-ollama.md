# Provider: Ollama, loaded and unloaded by revl

`"provider": "ollama"` speaks Ollama's native API at the server root. Unlike
every other provider, revl LOADS the model before the first call and UNLOADS it
after the last one, on the device the model schedule chose (roadmap item 515,
slice S2, issue #1189). The general rules (credentials, placement, argument
mapping) are in [model-providers.md](model-providers.md); the schedule is in
[model-scheduling.md](model-scheduling.md).

Use `openai-compatible` with `base_url` ending in `/v1` when you want the same
Ollama server to manage its own residency. Use `ollama` when the program's
device profile and the host's declared devices should decide where the model
sits and for how long.

## Config

```json
{"roles": {"small": {
  "provider": "ollama",
  "base_url": "http://127.0.0.1:11434",
  "model": "qwen3:0.6b",
  "devices": {"cpu0": {"num_gpu": 0}, "gpu0": {}}
}}}
```

| Field | Notes |
| ----- | ----- |
| `base_url` | Required. The server root, with no `/v1`. |
| `model` | The Ollama model tag, sent as-is. `qwen3` and `qwen3:latest` are the same model. |
| `devices` | Required, non-empty. Each key is a device `name` from the placement's `[[processes.<p>.devices]]`; each value is the load options that put the model on that device. |
| `api_key_env` | Optional, sent as `Authorization: Bearer ...` (a proxy in front of Ollama). |
| `residence` | Derived as for `openai-compatible`: `on_device` on a loopback host, otherwise `off_device`. |
| `structured_output` | `none`, the only value. This adapter sends no constrained format, so a `validated` operation's completion is unconstrained and checked on return. |

The load options are a closed set:

| Option | Meaning |
| ------ | ------- |
| `num_gpu` | Layers placed on the GPU. `0` keeps the whole model on the CPU. |
| `main_gpu` | Which GPU, when there are several. |

`{}` is the server's default placement, which on a machine with a GPU is the
GPU. An unknown option, a negative value, or a device name that is not a plain
name is refused.

## Running it

A managed role is loaded on the device the model schedule chose, and a
single-process run has no schedule. So `ollama` needs a placement:

```
revl run app.rvl --placement placement.toml --providers providers.json --once
```

The placement declares the host's devices and places the components that
route to the role on it (see [model-scheduling.md](model-scheduling.md)). With
`--providers`, a model key that no component provides is served inside each
process that requires it, instead of being refused as provided by no process.

`revl run app.rvl --providers providers.json` without `--placement` refuses the
boot with `ModelPlacementRefused` and loads nothing.

## One provision per role

Every model host whose operations route to a role is a consumer of that role's
provision (`src/revl/providers/provision.py`). In the child, before any
component activates:

1. The first consumer's acquire loads the member: `POST /api/generate` with no
   prompt, `keep_alive: -1` and the load options for the device
   `revl.model_placement.device_for(role)` answers.
2. The server is then asked what it holds (`GET /api/ps`). A model it does not
   report loaded, or one on the wrong class of device, is unloaded and the boot
   refused. The class is the share of the model in GPU memory
   (`size_vram / size`), rounded the way `ollama ps` prints it: a `cpu` device
   must read 0% and a `gpu` device 100%, so a model that only partly fits the
   GPU is refused too. A CPU load still holds a little GPU memory for its
   compute graph (64 MiB of a 22 GB model on Ollama 0.34.4, measured), which
   rounds to 0%. An `npu` device is not checked, because the server reports no
   NPU figure.
3. Later consumers of the same role share that load.

Every completion is `POST /api/chat` carrying the same `keep_alive: -1` and load
options. A request with other options makes the server reload the model where
it chooses, and a request with the default keep-alive starts an idle timer that
would unload it behind the provision's back.

At teardown, after every component is gone, each consumer releases, and the
last release unloads the member (`keep_alive: 0`). The per-process residue
proof then asks the server again:

```
[edge] model | provision       | model role `small`: 2 consumer(s) (llm, tagger), 1 load(s) on cpu0, 1 unload(s)
[edge] residue no residue | registry=0 provisions=[] disposables=1/1 models=unloaded
```

`models=` lists every problem instead when there is one: a consumer still
holding the role, loads and unloads that do not pair, or a model the server
still reports loaded.

## What is refused, and where

| Refusal | When |
| ------- | ---- |
| The schedule places the role on a device the binding has no `devices` entry for | plan time, before anything spawns; again at boot |
| A process would load a managed role but has no model schedule | plan time |
| A model key required by a process on another tier | plan time |
| The process was handed no schedule | boot (`ModelPlacementRefused`) |
| The server reports the model on the wrong class of device, or not loaded | boot, after unloading it |
| A call on a role the schedule placed on another host | call (`ModelPlacementRefused`) |
| A completion before the load | call |
| A consumer acquiring twice, or releasing what it does not hold | at once |

## What is checked, and what is not

Checked: that the load goes to the scheduled device's options, that the server
reports the model loaded and on the right class of device after the load, and
that it reports it gone after the last release.

Not checked:

- **Which GPU.** `size_vram` says how much is in GPU memory, not which GPU. `main_gpu` is
  sent, and not verified.
- **Memory.** The server's `size` is not compared with the role's declared
  `memory`. Item 538 owns comparing the published supply with the demand.
- **Other clients of the same server.** Another program can load, reload or
  unload the same model on the same Ollama. revl sees what the server reports,
  not who caused it.
- **Eviction by the server.** `keep_alive: -1` asks the server to keep the
  model. A server short of memory can still evict it. The next completion then
  loads it again, with the scheduled device's options because every request
  carries them, and the provision does not count that load.

## Tests

`tests/test_model_provision_515.py` runs against a loopback fake that speaks
the four endpoints above. One test is opt-in: with
`REVL_LIVE_OLLAMA_PROVISION_MODEL` set to a pulled tag, it loads that model on
the CPU of the local Ollama through two consumers, checks one load, one unload
and no residue, and skips when the server already holds any model, so it
cannot evict one another program is using.
