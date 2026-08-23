# Composition persistence

The MCP session (`src/revl/mcp/session.py`) holds an *evolved composition*: a
set of components an agent admitted at runtime through the admission gate —
booted with `revl_load`, then grown and replaced with `revl_swap`. Every one of
those admissions ran the checker (`compile_files` / `compile_source` ->
parse -> check -> lower, then the holes gate and the runtime boot). Nothing a
draft component did touched the disk, and nothing got in that the checker did
not accept.

That state was **in-memory only**. When the server process stopped, the
evolved generation was gone; self-evolution was durable for the life of the
process and no longer. Persistence closes that gap with a snapshot/restore
pair — `revl_snapshot` / `revl_restore`, and `revl mcp serve --restore
SNAPSHOT.json` to boot straight from one.

## What a snapshot is

A snapshot is **not** a pickle of live runtime objects — not the fibers, not
the provisions, not the effect accumulator. It is the *inputs needed to
reproduce the composition by re-admitting it*:

```json
{
  "sources":  { "source": "…rvl text…", "modules": { "./lib.rvl": "…" } },
  "manifest": { "loadOrder": ["MemDb", "L1", "Front"], "components": [ … ] },
  "meta":     { "snapshotVersion": 1, "irVersion": "…", "components": [ … ],
                "loadOrder": [ … ], "record": false, "config": { … },
                "createdAt": "2026-08-23T…Z" }
}
```

- **`sources`** — the text of the currently-admitted components (the same
  `source` / `files` / `modules` a live `revl_load` was given). When the
  admission used files, their text is read *at snapshot time* and carried
  inline, so the snapshot is self-contained JSON that reproduces the
  *snapshotted* sources rather than whatever the paths hold later.
- **`manifest`** — the running composition's manifest (load order, component
  interfaces). It travels as metadata and as a cross-check, never as something
  the runtime is rebuilt from (see below).
- **`meta`** — versioning, config, the record flag, and the component set the
  snapshot claims to reproduce.

It is plain JSON. The sources are text and the manifest is already a dict, so
there is nothing to serialize specially.

## The load-bearing rule: restore replays admission

**Restore re-admits. It does not rehydrate.**

`revl_restore` takes the snapshot's `sources` and compiles them through the
*same* entry points a live `revl_load` uses — `compile_source` /
`compile_files`, which run parse + check + lower — and then boots the result
through `Session.load`, which runs the holes gate and the runtime activation.
Every component in the snapshot goes through the identical admission path a
live admission does.

It **never** rebuilds the runtime from the stored `manifest` dict. That
distinction is the whole point:

> A snapshot taken under an **older** checker must not be able to smuggle a
> now-rejected component past a **newer** one.

If restore trusted the manifest — if it treated the snapshot as a serialized
runtime and loaded it back verbatim — then a component that an older checker
accepted would slip straight back in even after a newer checker learned to
reject it. The guarantee the checker exists to enforce would be silently
undone by a save file. So restore does the opposite: it throws the sources
back through the current checker. A component the current checker rejects
**fails the restore loudly, with the diagnostic**, and *nothing is loaded* —
the same refusal a live `revl_swap` of that component would produce. There is
no code path in restore that loads a component without first re-admitting it.

As defence in depth, after recompiling, restore verifies that the re-admitted
component set matches the set the snapshot claimed. If the sources no longer
reproduce the snapshotted composition, that is a loud refusal too, not a quiet
substitution. The authority is always the freshly compiled IR; the stored
manifest is only ever compared against, never loaded.

## Using it

- `revl_snapshot` — with a composition loaded, returns `{ snapshot: { … } }`.
  Write that JSON wherever you keep durable state.
- `revl_restore` — with nothing loaded, takes `{ snapshot: { … } }` and
  re-admits it. On a rejected component it returns `ok: false`,
  `restored: false`, and the diagnostic; the session stays empty.
- `revl mcp serve --restore SNAPSHOT.json` — re-admits the snapshot into the
  session before serving, so an evolved composition survives a restart. A
  rejected component aborts the boot with the diagnostic on stderr rather than
  starting a server in a smuggled state.

A composition that was loaded *without* its sources (a hand-built IR handed
straight to `Session.load`) cannot be snapshotted: there is nothing to replay
through the gate, and `revl_snapshot` says so rather than emitting a snapshot
that could only be restored by bypassing admission.

## Scope

Persistence covers the **admission** half — the sources and manifest,
re-admitted. It deliberately does not try to replay the **effect-state** half:
the effect accumulator that `backends/python/replay.py` records is a history of
what the running composition *did*, not part of what was admitted, and
reproducing it would mean re-invoking recorded calls, not re-admitting
components. A restored composition boots clean (with `record` re-enabled if the
snapshot had it on) and is driven forward from there.
