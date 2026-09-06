# `revl bundle` / `revl verify`: the reproducible production bundle

*Assemble a composition's source, IR, dependency lock, per-backend emitted
artifacts, capability policy, and admission evidence into one directory a
consumer can carry and check offline, then prove that directory rebuilds
bit-for-bit.*

Implementation: `src/revl/bundle.py` (the assembler, the verifier, the CLI
handlers), `src/revl/cli/parser.py` (`revl bundle` / `revl verify`),
`src/revl/__main__.py` (dispatch), `tests/test_bundle.py`. Roadmap item 305.

This is mostly assembly. It invents no hash scheme and no evidence format: it
reuses the item-297 `truc reproduce` primitives, the item-127 attestation, the
item-28 interchange/audit document, and the item-31 gauntlet, and stitches their
output into one artifact.

---

## 1. What it is

Until now a composition's proofs lived in separate places: source and manifest
hashes in the registry index, a signature in an `attest` file, the boundary
surface in an `audit --json` document, a graded dossier from the gauntlet.
`revl bundle` gathers all of it into one directory:

```
$ revl bundle app.rvl --out app.revlbundle
wrote bundle app.revlbundle
```

```
app.revlbundle/
  source/probe.rvl          the .rvl sources, verbatim
  ir/ir.json                the compiled IR (path-normalized, deterministic)
  ir/manifest.json          the item-28 audit/interchange document
  components.lock           the provides/requires surface + source/manifest hashes
  emitted/python/...          each backend's emitted artifact
  emitted/typescript/...
  emitted/rust/...  emitted/java/...  emitted/go/...  emitted/wasm/...
  policy.json               the capability/emission surface the boundary crosses
  attestation.json          the item-127 signed record (when a key is available)
  gauntlet.json             the item-31 graded dossier (evidence it is admissible)
  topology.json             the placement map (only when --topology is given)
  runtime-manifest.json     the bundle's own manifest, the recorded surface
```

`revl verify` recompiles the bundled source through the normal compiler pipeline
and compares, tier by tier, against what the bundle recorded:

```
$ revl verify app.revlbundle
revl verify app.revlbundle

  source                OK        1 file(s) match their hashes
  IR                    OK        manifest b9792cb0bf1aa022
  dependency lock       OK        provides={"cache": "Cache"} requires={...}
  policy surface        OK        emissions=1 capabilities=["*"]
  emitted [go]          OK        go 1 file(s) reproduce
  emitted [java]        OK        java 1 file(s) reproduce
  emitted [python]      OK        python 1 file(s) reproduce
  emitted [rust]        OK        rust 1 file(s) reproduce
  emitted [typescript]  OK        typescript 1 file(s) reproduce
  emitted [wasm]        OK        wasm 2 file(s) reproduce
  backend version       OK        interchange 1.0
  attestation           OK        authentic; IR matches the signed hash
  gauntlet              OK        admissible
  topology              --        no topology provided
  reproducible          OK        the bundle rebuilds bit-for-bit

verified: app.revlbundle rebuilds bit-for-bit to what was bundled
(14 OK, 1 unverifiable, 0 mismatch)
```

---

## 2. The tiers `verify` checks

Every tier reports one of three outcomes, the same vocabulary item 297
established, and the same `Check` type reused verbatim:

* **OK**, the recomputed value equals the recorded one.
* **MISMATCH**, they differ; both values are printed so a consumer sees exactly
  what diverged.
* **cannot verify**, nothing was recorded for that tier (no attestation, no
  topology), or the toolchain needed to check it is absent. Honest degradation:
  a tier with no evidence cannot be a mismatch, and it never silently reads OK.

| tier | what it proves | reuses |
| --- | --- | --- |
| **source** | the committed source bytes re-hash to the recorded hash | `registry._sha256` |
| **IR** | the recompiled audit document is byte-reproducible from the source and hashes to the recorded value | `registry._audit_document` |
| **dependency lock** | the rebuilt provides/requires surface equals `components.lock` | `truc.reproduce._surface` |
| **policy surface** | the rebuilt capabilities/emissions equal `policy.json` | `truc.reproduce._policy_of` |
| **emitted [backend]** | each backend re-emits byte-for-byte to the committed artifact, the emitted output *corresponds to* that backend | the backend emitters |
| **backend version** | the recorded interchange schema version matches the current toolchain | `interchange.INTERCHANGE_VERSION` |
| **attestation** | the item-127 signature is authentic and binds the rebuilt IR | `revl.attest` (item 127) |
| **gauntlet** | the evidence is present and records an `admissible` verdict | `revl.mcp.gauntlet` (item 31) |
| **topology** | a placement map is present and readable, when the bundle carries one |  |
| **reproducible** | the aggregate: source, IR, and every emitted artifact rebuild bit-for-bit |  |

`verify` exits **0** when the bundle reproduces (no MISMATCH, unverifiable tiers
allowed), **1** on any MISMATCH, and **2** when the bundle cannot be opened at
all (a missing directory, a corrupt `runtime-manifest.json`). This mirrors
`truc reproduce` exactly.

---

## 3. Options

```
revl bundle <sources...> --out DIR [--backend B]... [--topology PLACEMENT] [--one-file FILE] [--json]
revl verify <bundle> [--json]
```

* `--out DIR` (required), the bundle directory to write. An existing directory
  at that path is replaced.
* `--backend B`, a backend to emit; repeatable. Omit to emit every backend
  (`python`, `typescript`, `rust`, `java`, `go`, `wasm`).
* `--topology PLACEMENT`, a placement/topology map (TOML or JSON) to carry as
  `topology.json`. Omit for a single-process bundle.
* `--one-file FILE`, also pack the bundle into ONE self-contained file (see §6)
  beside the `--out` directory, so a consumer can carry a single artifact.
* `--json`, machine-readable output (the runtime-manifest for `bundle`, the
  tier-by-tier report for `verify`).
* `revl verify <bundle>` accepts either form: the `.revlbundle` directory or a
  one-file bundle. Both verify identically.

Signing keys resolve exactly as `revl attest` does, in order: `--key` is not a
`bundle` flag today (see §4), so the attestation uses `REVL_ATTEST_KEY_FILE`
(a key file) or `REVL_ATTEST_KEY` (the secret) from the environment, plus an
optional `REVL_ATTEST_SIGNER` label. No key means no `attestation.json`, the
bundle is still produced, and `verify` reports the attestation tier as
`cannot verify`.

---

## 4. Design decisions

Item 305 is assembly, but it has a few genuine forks. Each is resolved to the
simplest defensible option and recorded here.

**Manifest schema.** `runtime-manifest.json` is a plain, versioned JSON document
(`kind: "revl.bundle"`, `version: "1.0"`) that records the surface `verify`
checks against: the source file list and hashes, the IR/manifest/composition
hashes, the per-backend emitted file hashes, the policy surface, and which
evidence is present. It is deterministic, it carries **no timestamp**, so the
whole bundle rebuilds bit-for-bit. Evidence that carries its own timestamp
(`attestation.json`, `gauntlet.json`) lives in its own file and is verified by
signature or verdict, never by byte-reproduction, so those timestamps never
break the reproducibility check.

**IR path normalization.** The compiler stamps each component with a source path
relative to the working directory. A bundle is built from the input tree but
verified from the bundle's own `source/` directory, so those paths differ by
location alone. `bundle` normalizes every such path (`components[*].source`,
`components[*].file`, and the manifest's `components[*].file`) to its basename
before hashing or writing anything, the same normalization
`registry._audit_document` and `truc.reproduce._normalized_ir` already apply.
The result is location-independent: a bundle built in one directory verifies from
any other.

**Signing.** The attestation reuses item 127's HMAC-SHA256 scheme unchanged. A
symmetric keyed signature fits the model a bundle needs (a signer and a verifier
who share a secret, e.g. a CI system and the consumer it publishes to) and pulls
in no crypto dependency. When no key is available the bundle is produced without
`attestation.json`, and `verify` degrades that tier honestly. An asymmetric
upgrade (Ed25519, so a consumer verifies with only a public key) is the same
future migration item 127 already reserves an `alg` member for; `bundle` inherits
it for free when `attest` grows it. A dedicated `revl bundle --key` flag is a
thin follow-up (today the key comes from the environment).

**Backends.** Every backend whose emitter is a pure in-repo function is emitted
by default. An emitter that refuses this IR (for example the wasm backend on a
composition that uses floats) is recorded under `skippedBackends` in the
runtime-manifest with a reason, not treated as a bundle failure, a bundle that
cannot target one tier is still a valid bundle for the others.

**Drafts.** A draft (a composition with open holes) compiles but is never
admitted (`docs/holes.md`), so `bundle` refuses it: a bundle is a production
artifact, and signing or shipping a draft would record a verdict the composition
never earned.

**`revl run <bundle>` (not built here).** The roadmap item also names a
`revl run app.revlbundle` that boots a bundle directly. That is a thin wrapper
over the existing `revl run` once a bundle's `source/` and `topology.json` are in
hand, and is left as a follow-up; `bundle`/`verify`, the produce-and-prove pair
- are the load-bearing half and ship first.

---

## 5. What it does not do

`verify` never runs the bundled code. It recompiles the source and re-emits each
backend through the pure compiler frontend and emitters, the same path
`registry.build_index` and `truc reproduce` run, so it needs no cordis runtime
and it never executes host code. It writes nothing: it reads the bundle and
compares. Booting a bundle is `revl run`'s job, behind the admission gate, and a
future `revl run <bundle>` will reuse it.

---

## 6. One-file bundle

The `.revlbundle/` directory is the canonical form. A **one-file bundle** is that
same directory carried inside ONE self-contained JSON document, so a consumer can
hand around a single artifact instead of a tree (attach it to a release, mail it,
drop it in an object store):

```
$ revl bundle app.rvl --out app.revlbundle --one-file app.revlbundle1
wrote bundle app.revlbundle
wrote one-file bundle app.revlbundle1

$ revl verify app.revlbundle1
verified: app.revlbundle1 rebuilds bit-for-bit to what was bundled (14 OK, ...)
```

The envelope is a plain, self-identifying JSON document (`kind:
"revl.bundle.onefile"`, `version: "1.0"`) whose `files` member maps each file's
POSIX relative path to its verbatim text. Every file a bundle writes is UTF-8
text (`.rvl` source, JSON documents, emitted backend source), so the map is
lossless. It **invents no new hash scheme and re-derives nothing**: the exact
bytes travel, so the source, IR, manifest and attestation hashes the directory
recorded stay bit-for-bit what they were.

* **Round-trip.** `pack` (directory → file) and `unpack` (file → directory) are
  inverses: unpacking reproduces the tree byte-for-byte, so `revl verify` on a
  one-file bundle produces the SAME tier-by-tier report as on the directory it
  packed. `verify` accepts a one-file bundle directly, expanding it into a
  throwaway temporary tree it checks and discards (it still writes nothing
  durable).
* **Deterministic.** The document is emitted with sorted keys and no timestamp,
  so a given directory packs to identical bytes every time; a one-file bundle is
  itself reproducible.
* **Jailed on unpack.** A one-file bundle can arrive from anywhere, so `unpack`
  refuses a document that is not a one-file envelope and jails every embedded
  path inside the extraction directory: an absolute or `..`-bearing entry that
  would escape the bundle root is refused, the same realpath containment the
  stdlib-ref verify tier and `hostref` enforce.
