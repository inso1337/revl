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
| `boundary` | the **reaches** half, per component: `emissions` (irreversible call sites), `capabilities` (the scope each emission's declaration carries — `["*"]` means it promises nothing), `compensated`, `awaits`, and `externs` (host code reached transitively, a first-class dispatch shown as name `"*"`). |
| `externs` | every declared host-code extern — the trust surface a proof cannot cover. |
| `distributability` | per service, whether it may cross a process seam (`transport-safe` / `address-space-bound`) and why (interop-bridge §4). |

Provides / requires / reaches — the three things another admission harness
needs — are `manifest.components[].provides`, `manifest.components[].inject`,
and `boundary[name]`.

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
