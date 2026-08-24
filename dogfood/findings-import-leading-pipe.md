# Findings — leading-pipe unions (finding #37)

Probe: verifying item 137 (Branded->Str, literal-union->variant) against
the real DSH `PluginInventoryGateway`. Both parts verified with non-
leading-pipe syntax; the gateway still refuses because DSH formats its
union with a leading pipe per member.

## Verified (item 137)

```ts
type EntryId = Branded<'EntryId'>
type Phase = 'pending' | 'active' | null
```
-> `type Snapshot = { id: Str, phase: Opt[Phase] }` with
`type Phase = Pending | Active` (synthesized variant, null -> Opt).

## The remaining gap

DSH's actual `PluginFiberPhase` is:

```ts
export type PluginFiberPhase =
  | 'pending'
  | 'loading'
  | 'active'
  | 'failed'
  | 'unloading'
  | null
```

The leading `|` on the first member makes the importer capture the type as
`| 'pending'` -> `no revl spelling for this type`. The same union without
the leading pipe imports fine (verified). Fix: when splitting union
members on `|`, drop an empty/leading fragment — TS allows
`type X =\n  | 'a'\n  | 'b'` (the DSH formatter's style).

## Update — multiline unions collapse to the first member

Also verified: a multiline union whose FIRST member has no pipe still
collapses — `type Phase =\n  'pending'\n  | 'loading'\n  | null` parses as
just `'pending'` (`phase: Str`, no variant, no Opt). The type scan stops at
the newline. Same fix area as the leading pipe: split on `|` over the whole
declaration, treating newlines as whitespace within a type.
