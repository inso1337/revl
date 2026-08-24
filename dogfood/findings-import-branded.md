# Findings — DSH's branded strings and phase unions (finding #36)

Probe: verifying items 134/135 (external `*Service` bases + `.ts`-extension
imports; rust unicode literals) against the real DSH
`PluginInventoryGateway`. Item 135 verified (rust now emits literal UTF-8 —
the harness's em-dash timer test name compiles on rust again; the workaround
was reverted). Item 134 verified: the gateway's CLASS surface is recovered
(`extends TypertRemoteService` imported, `@Remote('list')` skipped) — it now
stops at the types.

## The two remaining blockers

```
unrecoverable signature on `list`: a generic type this importer does not
map (known: `Array`, `Promise`, `Record`) — the TypeScript type was
`Branded<'PluginEntryId'>`
```

- **(a) `Branded<'Tag'>`** — DSH's branded-string pattern
  (`type EntryId = Branded<'EntryId'>`, brand from `@deepseek-ai/dsh-brand`).
  A branded string IS a string — map it to `Str` (and resolve the alias
  through it). Verified: `type EntryId = string` in the same file imports
  fine; `Branded<...>` refuses.
- **(b) string-literal unions with null** — `PluginFiberPhase = 'pending' |
  'loading' | 'active' | 'failed' | 'unloading' | null` refuses ("a union
  of several concrete types is a sum type with no tag; revl needs a named
  variant"). For literal-only unions the literals are the tags: synthesize
  a named variant (the reverse of revl's variant->TS-union lowering), null
  -> Opt.

`readonly` fields and `readonly T[]` already strip cleanly (verified).

## Target

With (a)+(b), `PluginInventoryGateway.list(): PluginInventorySnapshot`
(record + branded entryId + phase union) imports, and the harness launches
the real gateway. Corpus: `packages/host/plugin-inventory/src/` in the DSH
checkout.
