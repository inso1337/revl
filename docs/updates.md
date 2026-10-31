# Updates: check a release, verify it, apply it through the gate

**Status:** language side implemented (`revl update check|fetch|status`). The
operator MCP surface (`revl_update_apply`) follows in the MCP slice of the
same work; this page states the boundary it inherits.

Revl systems already know how to replace a running component: the admission
gate, the swap, the generation history and the undo are the machinery a
running composition evolves with. What was missing was a way to point that
machinery at our own releases: a release identity, artifact hashes, a
compatibility statement, and an operator surface that fetches the release and
runs it through the gate the system already trusts.

## The release feed

Each release on GitHub carries a `release.json` asset beside its build
artifacts. The manifest is the only cross-repository wire format; `revl
update`, the harness update engine and the product launcher all read the same
document.

```json
{
  "schema": "revl.release/1",
  "project": "revl",
  "version": "2.1.0",
  "tag": "v2.1.0",
  "commit": "<40 hex>",
  "channel": "stable",
  "artifacts": [
    {"name": "revl-2.1.0.tar.gz", "kind": "sdist",
     "url": "https://...", "sha256": "<64 hex>", "size": 123456}
  ],
  "compat": {"language": "2.1.0", "gateApi": "1.0.0", "irVersion": 1,
             "frontier": "reference-full:2.1.0", "minPython": "3.11"},
  "live": false,
  "hostChanges": true,
  "pins": {"revl": "<40 hex>"},
  "changes": {"bump": "minor", "headline": "..."}
}
```

Rules the parser enforces, each with its own refusal code:

| field | rule |
| --- | --- |
| `schema` | exactly `revl.release/1`; a newer schema needs a newer updater |
| `project` | the feed may only ship its own project |
| `version` / `tag` | semver; the tag names the version (`v2.1.0`) |
| `commit` | 40 character commit sha |
| `artifacts` | at least one; unique names; http(s) or file URL; 64 character sha256 |
| `compat` | `language` is required semver; `gateApi`, `irVersion`, `minPython`, `frontier` are optional host facts |
| `live` | true only when the release was tested as a live update over the previous release |
| `hostChanges` | true when the release also changes host Python, compiler or venv bytes |

A repository that publishes no manifest still resolves through its newest
semantic tag. The result says `tagFallback: true`, carries a locally computed
hash, has no compatibility statement, and is never applied automatically.
A manifest always wins over a tag.

## The commands

```
revl update check   [--repo OWNER/NAME] [--channel stable|prerelease] [--json]
revl update fetch   [--repo OWNER/NAME] [--home DIR] [--json]
revl update status  [--home DIR] [--json]
```

`check` compares the running toolchain (`compiler_version()`, `gate_version()`,
the IR revision and the interpreter) with the newest release, reports the
semver bump, whether the release declares itself live-safe, and whether the
host satisfies its declared gate, language and Python requirements. Exit
codes are stable for automation:

| exit | meaning |
| --- | --- |
| 0 | up to date |
| 10 | an update is available |
| 2 | error or refusal |

`fetch` downloads the release artifact, verifies it against the manifest
sha256, extracts it safely under the update home
(`$REVL_UPDATE_HOME`, default `~/.revl/updates`) and writes a
`provenance.json` beside the sources. Nothing is compiled and nothing is
executed. Extraction refuses absolute paths, `..`, backslashes, drive paths,
links and device entries, and bounds the archive at 300 MB and 50,000 files.

`status` prints the running identity, the staged releases and the update
journal, which is an append-only JSONL file in the update home.

`--manifest PATH_OR_URL` bypasses the feed and reads that manifest directly.
The same validation runs, so a mirror document is not a second code path.

## Trust boundary

Nothing a feed says is trusted until it is verified, and verification never
replaces the gate:

1. the manifest must validate, and must name the feed's project;
2. the artifact must hash to the manifest's sha256;
3. the running compiler and admission gate must accept the release sources
   before any swap is planned;
4. the operator surface that applies a staged release is a two-stage call
   bound to a plan hash, so what is applied is what was planned against the
   generation that is running;
5. a post-apply health check reverts automatically when the successor does
   not serve its keys.

A release whose bytes are verified is operator-sanctioned code: it may carry
host bodies, exactly as a checkout the operator compiled would. A release
that fails a check is never compiled.

The CLI never applies an update. A CLI process has no handle on a running
session, and the whole point of the mechanism is that the running system's own
gate decides. That half is the operator MCP verb.

## Zero restart, honestly

Everything the running gate can admit (`*.rvl` composition sources) is applied
to a running composition by a swap, with no process or browser reload. Host
bytes (harness Python, the compiler package, the venv) cannot be swapped
in-process today; those are staged and reported as `restartRequired`, and the
launcher completes them at the next start. The boundary shrinks as host code
moves into revl, and the manifest already carries the two flags
(`live`, `hostChanges`) the mechanism needs to keep the two cases apart.

## Publishing a release

`tools/release_manifest.py` writes the manifest from a tagged checkout, using
the same parser the updater reads:

```
python3 tools/release_manifest.py --out release.json dist/*
python3 tools/release_manifest.py --validate release.json
```

`publish.yml` runs it on every `v*` tag and uploads the manifest beside the
sdist and wheel; `release-dryrun.yml` generates and validates it on a schedule
and on PRs that touch the release path, so the writer cannot drift from the
reader without a red run.
