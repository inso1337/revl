# Findings — importing DSH's real plugins (finding #33)

Probe: `revl import cordis` on DSH's actual plugin sources, to close the
"only typed cordis plugins import" frontier. Two real plugins refuse with
precise diagnostics; a minimal repro isolates the first blocker.

## The two real refusals

- `packages/host/plugin-inventory/src/index.ts` —
  `PluginInventoryGateway extends TypertRemoteService`:
  `the service 'pluginInventory' exposes no method surface this importer
  can read`.
- `packages/llm/llm/src/index.ts` — `LlmRuntime extends Service`, every
  method `AsyncIterable<StreamChunk>` / `unknown` / complex records:
  `every operation on 'llm' had an unrecoverable signature, so no callable
  surface remains`.

## Minimal repro (verified)

```ts
class TypertRemoteService extends Service { ... }
export class Gateway extends TypertRemoteService {
  @Remote('list')
  list(): string[] { return [] }
}
```
→ `exposes no method surface this importer can read`.

Root cause: `src/revl/import_cordis.py:507`'s class regex is
`extends (?:[A-Za-z_$][\w$]*\.)*Service\b` — it matches `extends Service`
and `extends pkg.Service`, but a single identifier ending in `Service`
(`TypertRemoteService`) is NOT matched, so the whole class is invisible
before any method is read.

## The ask (roadmap item 104)

Three gaps, each independently useful, with the real DSH plugins as the
test corpus:
(a) surface recovery matches any base ending in `Service`; `_members`
    skips `@Decorator(...)` lines;
(b) transcribe named record types across the plugin's local imports
    (records/lists/unions, "no guessing");
(c) partial import — recoverable ops import, the rest get
    `// UNRECOVERED`, instead of voiding the whole service.

Immediate workaround (works today): hand-declare the service boundary —
the `tools/dsh_plugin_demo.py` path (service + externs + provider), which
is exactly the importer's own "trusted, not checked" contract.
