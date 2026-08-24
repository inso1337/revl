# Findings — importing DSH's real gateway after item 116 (finding #34)

Probe: verifying item 116 (`revl import cordis` recovers non-Service base
chains, decorated methods, cross-module records) against the REAL DSH
`PluginInventoryGateway`. Item 116's own fixtures pass, but the real plugin
still refuses. Two confirmed gaps, both minimally repro'd.

## Verified working (item 116)

- in-file base chains (`class Sessions extends BaseStore`, `BaseStore
  extends Service`) — `subclassed.ts`;
- decorated methods (`@Remote('list')` skipped, no phantom op);
- extensionless local imports (`from './models/user'`) with nested records
  transcribed dependency-first.

## Gap (a) — an external Service-named base is invisible

`_service_class().chain_of` walks only LOCAL classes (`by_name`); a base
imported from another package terminates the chain, so the plugin class
never becomes a service. The real DSH gateway extends `TypertRemoteService`
(imported from `@deepseek-ai/dsh-typert-protocol`):

```ts
import { TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
export class Gateway extends TypertRemoteService {
  @Remote('list')
  list(): string[] { return [] }
}
```
→ `the service 'gw' exposes no method surface this importer can read`.

Fix: treat a non-local base whose name ends in `Service` (the cordis
convention) as a service root in `chain_of` / `_service_roots`.

## Gap (b) — `.ts`-extension imports resolve nothing

`_resolve_module` appends candidate extensions to the raw spec, so
`from './types.ts'` (real DSH style — the DSH codebase writes extensions)
probes `./types.ts.ts` → no candidates → the nominal type refuses:

```ts
import type { Snapshot } from './types.ts'
list(): Snapshot { ... }
```
→ `the nominal type 'Snapshot' is not defined in this file or a local
import`; the SAME import without the extension (`from './types'`)
transcribes fine (`type Snapshot = { entries: List[Entry] }`).

Fix: strip an existing `.ts`/`.tsx`/`.mts`/`.d.ts` extension before
probing candidates (or probe the bare path).

## The target

With (a)+(b), `PluginInventoryGateway.list(): PluginInventorySnapshot`
(record defined in `./types.ts`) should import. Corpus:
`packages/host/plugin-inventory/src/index.ts` in the DSH checkout.
