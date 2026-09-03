# revl_gauntlet — the proving ground as one verb

**Status:** v1 implemented — `revl_gauntlet` in `src/revl/mcp/gauntlet.py`,
registered in `server.py`, tested in `tests/test_gauntlet.py`. The lifecycle
battery needs the cordis-py runtime; everything else is pure frontend.

`revl_swap` is binary: it admits a candidate or it refuses. That is the right
shape for *deploy*, but it throws away everything the toolchain learned on the
way to yes/no. The gauntlet is the graded form — **candidate in, dossier
out.** It runs a battery against a candidate component in an **isolated
scratch session the live composition never sees** and returns a structured
verdict.

> Admission proves a candidate *may* run; the gauntlet proves it *does run
> correctly* — before it touches anything.

## The three kinds of knowledge

The dossier's top-level structure is not a flat list of checks. It is a
deliberate split by **epistemic status** — how we know each thing — because an
agent (or item 28's interchange format) needs to treat proof, evidence, and
trust differently:

| section | how we know it | in v1 |
|---|---|---|
| **proved** | deduced by the compiler, before anything runs | admission (§5 structural compatibility, G2/G3, no interface drift) and derived teardown (LIFO) |
| **tested** | observed by a real run, reported *with counts* | the boot/unload no-residue lifecycle |
| **claimed** | cannot be verified — taken on faith, *enumerated* | the G8 extern boundary |

The line between them is the whole point. A proof needs no witness; a test
reports how many witnesses it found; a claim is the surface a proof cannot
reach, so the honest thing is to *list* it rather than assert it holds. Moving
a fact from `claimed` to `tested` (fault sweep) or from a structural claim to
a `tested` observation (inverse round-trips) is exactly what roadmap items 30
and 26 will do — and the schema already has the slots.

## Dossier schema

```jsonc
{
  "ok": true,                 // a dossier was produced (NOT the grade)
  "verdict": "admissible",    // the grade: "admissible" | "rejected"
  "note": "...",
  "candidate": { "loadOrder": [...], "components": [...], "services": [...] },

  "proved": {
    "admission": {
      "kind": "proved",
      "status": "proved",     // "proved" | "refused"
      "against": "session",   // "session" (a live composition) | "cold"
      "note": "...",
      "diagnostics": [ ... ]  // present only when refused
    },
    "teardown": {
      "kind": "proved",
      "status": "derived",
      "teardownOrder": [ ... ],  // LIFO — the reverse of load order
      "note": "..."
    }
  },

  "tested": {
    "lifecycle": {
      "kind": "tested",
      "status": "passed",     // "passed" | "failed" | "unavailable" | "not-run"
      "ran": true,
      "test": "boot/unload no-residue (R4)",
      "counts": { "checks": 4, "passed": 4, "failed": 0 },
      "checks": { "registry": true, "provisions": true,
                  "effects": true, "listeners": true },
      "detail": { "registrySize": 0, "provisions": [], ... }
    }
  },

  "claimed": {
    "boundary": {
      "kind": "claimed",
      "status": "enumerated",
      "externs": [ "sha256_hex", ... ],  // every host op the candidate reaches
      "count": 1,
      "components": { "<name>": { "emissions": [...], "capabilities": {...},
                                  "compensated": 0, "awaits": 0,
                                  "externs": [...] } },
      "note": "..."
    }
  },

  "pending": {
    "faultSweep":       { "status": "pending", "roadmapItem": 30, "counts": null, ... },
    "inverseRoundTrip": { "status": "pending", "roadmapItem": 26, "counts": null, ... }
  },

  "scratch": { "isolated": true, "booted": true, "note": "..." }
}
```

### `ok` vs `verdict`

`ok` reports that the gauntlet *ran and produced a dossier*. The grade lives
in `verdict`. A candidate that fails admission is a **result, not a crash**:
`ok` is `true`, `verdict` is `"rejected"`, and `proved.admission` carries the
compiler diagnostic. This is the difference from `revl_swap`, which returns
`ok: false` on rejection because for a swap, rejection *is* the failure. For
the gauntlet, grading a bad candidate is success.

### `against`: `session` vs `cold`

When the server holds a live composition, admission is checked **against it**
(ambient services in scope, G2/G3 spanning both, interface drift refused) and
`against` is `"session"`. With nothing loaded, the candidate is graded as a
standalone composition and `against` is `"cold"`.

## Isolation

The battery runs against a **throwaway `Session` instance** — the same
in-memory composition machinery `revl_load` drives, but a separate object. The
live composition the server holds (`server.SESSION`) is read for the admission
manifest and **never mutated**. The scratch session boots the candidate, the
no-residue checks tear it down, and it is discarded. A candidate that faults,
leaves residue, or cannot even boot changes nothing the operator can see —
`tests/test_gauntlet.py::test_the_scratch_session_leaves_the_live_composition_untouched`
loads a live composition, grades a *different* candidate against it, and
asserts the live one still answers with its original provider afterward.

### Isolation is not a licence to run what the gate refuses

The scratch session isolates the candidate from the **live composition**. It
does not isolate it from the **host**: booting a candidate runs its activation
body, and a host-body extern the candidate reaches is host code in the server's
own process (item 24 — the gate does not sandbox host code; that is what
[`revl_quarantine`](quarantine-tier.md)'s wasm substrate is for).

So the gauntlet's admission compile carries the session's **authoring trust**,
through `server.compile_under_authoring` — the same door `revl_check`,
`revl_admit` and `revl_swap` compile through. A candidate the admission gate
refuses is graded `rejected` on that refusal and is never lowered, never
booted. Grading a candidate is not permission to run one the gate would not
admit. `revl_quarantine` and `revl_repair`, which run this battery, compile
through the same door for the same reason.

## Graceful degradation

Every branch grades rather than raises:

- **Admission refused** → `verdict: "rejected"`; downstream sections report
  `status: "not-run"`; no scratch session is booted.
- **No runtime / open typed hole / candidate is a fragment** → the lifecycle
  section reports `status: "unavailable"` with the reason, and still carries a
  zeroed `counts` block so the shape is stable. Admission and boundary are
  unaffected — they are pure frontend.

## What is real vs pending in v1

Real ingredients, each **reused** from existing machinery, not reimplemented:

- **admission** — the same `compile_source`/`compile_files` gate the CLI and
  `revl_swap` use, with the live composition passed as the manifest.
- **lifecycle no-residue** — `Session.load` + `Session.unload`, whose R4
  residue checks (registry, provisions, effects, listeners) are reported with
  counts.
- **G8 boundary** — the `__main__._boundary` walk, enumerated per component.

Designed but reporting `pending` until their roadmap items land:

- **faultSweep** (item 30) — inject a fault at each effect boundary and
  confirm the derived teardown still leaves no residue; moves a class of
  recovery claims from `claimed` to `tested`.
- **inverseRoundTrip** (item 26) — run each effect's undo and confirm it
  round-trips the pre-state; moves reversibility from a structural claim to a
  `tested` observation with counts.

When 26/30 land they fill their existing slots; nothing about the dossier
shape changes, only their `status`. That stability is why the schema is
designed now: item 28's interchange format carries this same document.
