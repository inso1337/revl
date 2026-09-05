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
| `boundary` | the **reaches** half, per component: `emissions` (irreversible call sites), `capabilities` (the scope each emission's declaration carries — `["*"]` means it promises nothing), `compensated`, `awaits`, and `externs` (host code reached transitively; each entry carries `name`, `class`, optional `capabilities`, and `backends`; see [the boundary extern entry](#the-boundary-extern-entry) below; a first-class dispatch is shown as name `"*"`). |
| `externs` | every declared host-code extern — the trust surface a proof cannot cover. |
| `distributability` | per service, whether it may cross a process seam (`transport-safe` / `address-space-bound`) and why (interop-bridge §4). |
| `retention` | optional (item 308 F10). The report-only retention surface: one row per resource-carrying parameter of a non-inverse extern or of a service method — every declared position at which a resource handle leaves revl's sight. Present only when there is one, so a handle-free composition omits the key. Each row is a MAY-retain: it does not prove a host body keeps the handle, and an absent row does not prove one does not. A declared inverse is excluded (teardown closing a handle is the contract working). |

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
| `class` | the G4 classification, one of the four boundary classes: `"pure"` (no observable effect), `"acquire"` (takes a resource that must be released), `"emission"` (an irreversible crossing out of the system), or `"witnessed"` (a reversible host mutation that names its inverse). `"first-class dispatch"` is the pseudo-class for a `"*"` entry. May be `null` when the class is not known. |
| `capabilities` | the declared capability scope of a scoped `emission[...]` / `witnessed[...]` extern, as a sorted token list (e.g. `["fs"]`). **This token, not the name, is what a `capability <glob>` policy rule keys on.** Absent for an unscoped extern, whose token is its own name. |
| `backends` | the backend tiers this extern has a body for (sorted), e.g. `["py"]`. |

So the human line

```
host code: … write [fs] (witnessed, py)
```

is exactly this JSON entry:

```json
{ "name": "write", "class": "witnessed", "capabilities": ["fs"], "backends": ["py"] }
```

The `capabilities` and `backends` fields carry the `[fs]` token and the `py`
tier the render folds into that one line. A consumer that needs to know a
crossing's classification, its declared capability, or its tier reads these
fields, never the prose.

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
