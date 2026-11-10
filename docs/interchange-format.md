# revl composition interchange format (manifest + G8 audit)

**Status:** implemented (2026-08-23) · roadmap item 28 · answers DESIGN §10
("how much of the linker's manifest should be a stable, documented format?")

`revl audit --json` emits, for a composition, the linker's **manifest** and
the **G8 audit** — what every component *provides*, *requires*, and *reaches*.
That data has always been there. What this document adds is the missing piece:
a **stable, versioned, documented interchange format** so another tool can
consume a revl gate's verdicts *without running revl*.

The format's identity lives in one module, `src/revl/interchange.py`, and its
shape is published as a machine-readable JSON Schema at
[`schema/revl-interchange-v1.schema.json`](../schema/revl-interchange-v1.schema.json).

This is the linker's manifest, a snapshot of an already-compiled composition.
A different sense of "manifest" shows up when a composition declares its own
file list from inside a revl document (`manifest.rvl`); that document and the
host bootstrap it needs are [composition-bootstrap.md](composition-bootstrap.md).

---

## The document

`revl audit --json <files…>` prints one JSON object:

```json
{
  "schema_version": "1.0",
  "kind": "revl.interchange",
  "manifest": {
    "components": [
      { "name": "PgDatabase", "file": "…", "inject": [], "provides": ["db"] },
      { "name": "UserCache",  "file": "…", "inject": ["db"], "provides": ["cache"] }
    ],
    "loadOrder": ["PgDatabase", "UserCache"]
  },
  "boundary": {
    "PgDatabase": { "emissions": [], "capabilities": {}, "compensated": 0, "awaits": 0, "externs": [] },
    "UserCache":  { "emissions": ["db.execute"], "capabilities": { "db.execute": ["*"] },
                    "compensated": 0, "awaits": 0, "externs": [] }
  },
  "externs": [],
  "distributability": {
    "Database": { "verdict": "address-space-bound", "reasons": ["query: not async fn", "execute: emission (sync)"] },
    "Cache":    { "verdict": "address-space-bound", "reasons": ["get: not async fn", "put: emission (sync)"] }
  }
}
```

| member | what it is |
| --- | --- |
| `schema_version` | `MAJOR.MINOR` of this format (see the promise below). |
| `kind` | the constant `"revl.interchange"`, so a consumer can identify the document. |
| `manifest` | the static composition: `components` (each with `inject` = **requires** and `provides`, plus optional `isolate` / `intercept`), `loadOrder` (providers first; teardown is the exact reverse), and optional `templates` (runtime spawn targets). |
| `boundary` | the **reaches** half, per component: `emissions` (irreversible call sites), `capabilities` (the scope each emission's declaration carries: `["*"]` means it promises nothing), `compensated` (how many of the `emissions` labels carry a compensating undo, counted over distinct labels so it can never exceed `emissions`; a label compensated at more than one site counts once), optional `compensatedHostEmissions` (emitting host externs compensated, which cross the boundary but carry no `key.method` label and so are absent from both `emissions` and `compensated`; the key is absent when there are none), `awaits`, and `externs` (host code reached transitively; each entry carries `name`, `class`, `inverse`, optional `capabilities`, and `backends`; see [the boundary extern entry](#the-boundary-extern-entry) below; a first-class dispatch is shown as name `"*"`). |
| `externs` | every declared host-code extern: the trust surface a proof cannot cover. Each entry carries `name`, `class`, `inverse` (the derived inverse-provenance class, see [the inverse class](#the-inverse-class)), and `backends` (the tiers it has a body for), plus, **only when declared**, `reach` (item 373: what a confined crossing is bounded to: `{"kind": "confined", "target": "<param>"}`) and `deferred` (item 484: `true` for an `emission` declared `deferred`). A **deferred** emission is the one crossing whose timing differs: its host call does NOT fire at the call site; it is held and flushed at the commit prompt, and an abort drops the queue. An immediate emission carries no `deferred` key, so its entry is byte-identical to before the field existed. |
| `distributability` | per service, whether it may cross a process seam (`transport-safe` / `address-space-bound`) and why (interop-bridge §4). |
| `retention` | optional (item 308 F10). The report-only retention surface: one row per resource-carrying parameter of a non-inverse extern or of a service method — every declared position at which a resource handle leaves revl's sight. Present only when there is one, so a handle-free composition omits the key. Each row is a MAY-retain: it does not prove a host body keeps the handle, and an absent row does not prove one does not. A declared inverse is excluded (teardown closing a handle is the contract working). |
| `runtime_ceilings` | optional, present only with `revl audit --json --wal FILE`. It identifies the supplied WAL, reports whether its activation completed or its tail is torn, and copies each budgeted emission outcome's `record`, `seq`, and `ceiling` map. `seq` points back to the exact WAL outcome record; the map carries the observed calls, bytes, and elapsed values. |

`--wal` adds runtime evidence without changing the static audit body. The
`runtime_ceilings.records` entries intentionally omit effect arguments and
error details. Without `--wal`, the interchange document is unchanged. The
option is available only on the plain JSON audit, not with `--policy` or
`--diff`.

Provides / requires / reaches — the three things another admission harness
needs — are `manifest.components[].provides`, `manifest.components[].inject`,
and `boundary[name]`.

### The boundary extern entry

Each entry in `boundary[name].externs[]` is the machine-readable classification
of one host-code crossing a component reaches. It is the supported way to read
what the human render prints on its `host code:` line, and it carries:

| field | what it is |
| --- | --- |
| `name` | the extern's name, or `"*"` for a first-class dispatch (a reach through a value whose target is not statically nameable). |
| `class` | the G4 classification, one of the five boundary classes: `"pure"` (no observable effect), `"acquire"` (takes a resource that must be released), `"emission"` (an irreversible crossing out of the system), `"witnessed"` (a reversible host mutation that names its inverse), or `"local"` (a durable write to local state, which crosses no boundary and so asks no owner — item 2146). `"first-class dispatch"` is the pseudo-class for a `"*"` entry. May be `null` when the class is not known. |
| `inverse` | the derived inverse-provenance class of this crossing, one value of the closed vocabulary in [the inverse class](#the-inverse-class) below. This version emits it on **every** entry; `unknown` is a value, never the way this version renders a missing field. A document captured before the field existed has no `inverse`, and that absence is not `unknown` (the two are not comparable). |
| `capabilities` | the declared capability scope of a scoped `emission[...]` / `witnessed[...]` extern, as a sorted token list (e.g. `["fs"]`). **This token, not the name, is what a `capability <glob>` policy rule keys on.** Absent for an unscoped extern, whose token is its own name. A token carrying resource parameters (`fs.write(path="/data")`) appears with its parameters; a capability's own DECLARED dimensions (issue #1938, design 294) appear here too, which is additive to the schema and not to an existing token's bytes. |
| `backends` | the backend tiers this extern has a body for (sorted), e.g. `["py"]`. |

So the human line

```
host code: … write [fs] (witnessed, py)
```

is exactly this JSON entry:

```json
{ "name": "write", "class": "witnessed", "inverse": "declared", "capabilities": ["fs"], "backends": ["py"] }
```

The `capabilities` and `backends` fields carry the `[fs]` token and the `py`
tier the render folds into that one line, and `inverse` answers how this
crossing can be undone. A consumer that needs to know a crossing's
classification, its declared capability, its tier, or its inverse provenance
reads these fields, never the prose.

### The inverse class

Issue #2692: `inverse` is the one derived answer to "how do we know this
crossing can be undone, and how far can the undo be trusted?". It replaces the
scattered facts a reviewer used to read one document at a time (the
crash-recovery register, the approval classes, a `compensate` offset, the
emission rule, `undo_idempotent`) with a single closed vocabulary carried on
every extern entry:

| `inverse` | meaning | read from |
| --- | --- | --- |
| `declared` | an exact inverse the author declared | an `acquire`/`witnessed` extern's `undo` slot |
| `generated` | an exact inverse derived from trusted metadata | reserved: no current declaration produces one |
| `compensation` | a best-effort offset, not a restoration | a `compensate` slot on an `emission` |
| `forward` | irreversible, but recoverable forward by re-issuing the crossing, where the declaration carries an idempotency register | a `deferred` emission with `idempotent` or `idempotent(key: ...)`. The register carried beside the class says how strong the claim is: `read`/`keyed` are free to re-issue, `declared` is fenced and operator-gated |
| `irreversible` | an emission with no inverse, offset or forward claim | the emission rule (`docs/backend-ir-v3.md`) |
| `unknown` | no provenance fact at all | anything else: a `pure` read or a `local` write (both are forbidden a slot), a class `null`, or the `"*"` first-class dispatch |

The class is **derived, never inferred from a name or a shape.** Pairing an
`open` with a `close`, or matching two verbs by spelling, is not evidence; an
inverse is accepted only from a declaration (the `undo` slot) or from trusted
metadata, and anything else is `unknown`. Whether an `unknown` crossing may run
is a separate policy decision made later, with this field in hand; this slice
adds no refusal.

`unknown` is a first-class value, not an omission: every entry this version
emits carries the field. The vocabulary is closed, so a consumer may switch on it
and must treat a value it does not recognise as `unknown`. A change of class
between two generations is authority drift: `revl audit --diff` reports a weaker
class as a weakening and fails the gate unless acknowledged
([audit-diff.md](audit-diff.md#inverse-provenance-drift)). Externs from a
document captured before the field existed are not comparable and are skipped by
that diff, never read as `unknown`.

## The human render is not a stability contract

`revl audit` **without** `--json` prints a human-readable render: the
composition, each component's boundary, the externs, and the distributability
verdicts, for a person reading a review. **That render is not a stability
contract.** Its wording, spacing, and line shape may change between releases to
read better, and such a change is not a semver break.

This is not hypothetical. PR #257 added the declared capability token to the
boundary line, so a witnessed `fs` extern that rendered

```
host code: … write (witnessed, py)
```

began rendering

```
host code: … write [fs] (witnessed, py)
```

The classification was byte-identical at every sha; only the render gained the
` [fs]` token, which is a strict improvement (the token is what a capability
rule targets). But a downstream consumer matching the old prose string stopped
matching, and read the failed match as "no longer witnessed". It was not; the
consumer was parsing an output that was never a contract.

**`revl audit --json` is the supported surface for consumers.** It is the
versioned, schema-published document this file describes, with the additive-only
compatibility promise below. Everything a consumer might have scraped from the
prose (every classification, capability token, and tier) is a documented
field of that document (`boundary[name].externs[]` above); parse the JSON.

A wording change to the prose is still worth reviewing, so it does not surprise
someone reading a diff: the human boundary line is pinned by a golden
(`tests/test_audit_human_boundary_golden.py`) over a corpus that exercises all
four classifications, scoped and bare. The golden does not freeze the render;
it makes changing it a deliberate, reviewable diff rather than a silent break.
Re-applying #257's ` [fs]` addition turns that golden red on purpose.

## The compatibility promise

`schema_version` is `MAJOR.MINOR`.

- **Within a MAJOR, changes are additive only.** New optional members may
  appear; every existing member keeps its name, meaning, and shape. A consumer
  written for `1.m` keeps reading every `1.n` with `n ≥ m`.
- **A MAJOR bump is the only thing that may remove or re-shape a member.** It
  signals a break; a consumer should refuse a MAJOR it was not written for.
- **Ignore unknown members.** Every level of the schema is
  `additionalProperties: true` for exactly this reason: a forward-compatible
  consumer gates on MAJOR and skips what it does not recognise, so a MINOR
  bump never breaks it.

This mirrors the IR's own `ir_version` discipline (`src/revl/lower.py`), one
axis up: the IR version tracks the compiled-artifact shape; the interchange
version tracks the *verdict* shape that tools outside revl depend on.

## Composing with the gauntlet dossier (item 31)

The envelope reserves an optional `gauntlet` member. The gauntlet
(`docs/gauntlet.md`) grades a candidate into `proved` / `tested` / `claimed` /
`pending` sections, and those sections already speak the same
manifest-and-boundary vocabulary this schema defines (its `candidate` summary
is a manifest slice; its `claimed.boundary` is a boundary entry). So a dossier
drops into the same document without a schema change:

```python
from revl.interchange import stamp
document = stamp(audit_report, gauntlet=dossier)   # one envelope, both payloads
```

When items 26 and 30 fill in the dossier's `pending` sections, only their
`status` changes — the interchange format carries them unchanged.

## Worked example: an external harness, no revl in the loop

A harness in any language reads the document and answers provides / requires /
reaches without compiling anything:

```python
import json, subprocess

# produced once by the gate; the harness only ever reads JSON
doc = json.loads(subprocess.check_output(
    ["python", "-m", "revl", "audit", "examples/user_cache.rvl", "--json"]))

assert doc["kind"] == "revl.interchange"
assert doc["schema_version"].split(".")[0] == "1"      # gate on MAJOR only

# who provides `cache`, and what does it require + reach?
for comp in doc["manifest"]["components"]:
    if "cache" in comp["provides"]:
        name = comp["name"]
        reaches = doc["boundary"][name]
        print(name, "requires", comp["inject"],
              "emits", reaches["emissions"],
              "reaches externs", [e["name"] for e in reaches["externs"]])
        # UserCache requires ['db'] emits ['db.execute'] reaches externs []
```

Because the schema is published, a consumer written in another language can
validate the document the same way — no revl process, no Python. The Python
validator ships in-repo:

```python
from revl.interchange import validate
assert validate(doc) == []      # [] means it satisfies the published schema
```

`validate` uses `jsonschema` when it is installed and otherwise falls back to a
small built-in checker, so validating the format needs no third-party
dependency.
