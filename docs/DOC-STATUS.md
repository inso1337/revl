# Documentation staleness inventory

A triage list for the docs refresh (roadmap items 243, 258, 285). The single
source of truth for what has shipped is `docs/v2.0-roadmap.md` (items marked ✅).
This table is the state after the front-door pass; the `needs-work` rows are the
follow-up wave.

**Pass 2 (item 285).** Priority was the self-hosting status, which had gone
stale after item 262 (the fully-native compile of component + extern programs).
Fixed this pass: `README.md` (self-hosting badge + status block now say the
covered surface, functions + components + externs, compiles fully-native
byte-exact on py and rust with no reference in the chain; added the
`python -m revl.lsp` and `python -m revl.otel` entry points), `docs/guide-humans.md`
(the Tooling command table completed to all 28 wired verbs, `revl run` corrected
to all six tiers booting live including ts and go, LSP + otel added),
`docs/guide-ai-agents.md` (LSP added), `docs/selfhost-compile.md` (rewritten:
the typed component body is no longer a reference-only remainder; the item-262
component/extern corpus documented), `docs/opentelemetry.md` (accuracy-verified,
em-dash-free, stray fence + landed coordination note fixed). Still open: the bulk
of the `needs-work` rows below (accuracy + em-dash style pass), and
`docs/selfhost-findings.md` (a large, live-owned chronological log whose past
slice entries record then-current state, not present-tense product claims).

**Status meanings**

- **stale-fixed**: audited and corrected this pass (accuracy + em-dash/AI-tell
  style pass, verified against the roadmap's ✅ state).
- **current**: spot-verified accurate against the roadmap or the code this pass.
  The subject is a landed feature and the doc describes it as landed. A nonzero
  em-dash count still means a style pass is pending.
- **live-owned (not audited)**: actively maintained by other agents this
  session (self-host / bench). Left untouched by this pass on purpose.
- **needs-work**: not deep-audited this pass. Accuracy not individually
  confirmed, and the em-dash/AI-tell style pass is still pending. Not known to
  be wrong, just not yet checked. Sorted by em-dash count is a reasonable order
  for the follow-up wave.

**Columns**

- **em-dashes**: count of `—` remaining (0 = style-clean; em-dashes inside
  ` ```revl ` fences are intentionally preserved).
- **tier-limit notes**: `yes` = the doc contains a genuine "not yet lowerable /
  not yet wired" tier limitation worth confirming against current tier support
  before it is trusted (usually accurate, but the first thing to re-check).

**Naming convention (item 255).** The dogfood workload is named uniformly as
**the lighthouse workload** across `docs/` and `dogfood/`. Public text does not
carry absolute local paths or the workload's internal milestone numbers; a
findings file cites its own finding id, not the workload's build scaffolding.
Item 248 is the single anchor entry for the workload in `docs/v2.0-roadmap.md`.
Forward rule: new text uses the lighthouse-workload framing only.

Front-door files outside `docs/` were also refreshed this pass: `README.md`
(stale `self_hosted: in_progress` badge replaced; 8 missing CLI verbs added;
self-hosting status added; em-dash-free), `site/index.html`, `site/README.md`,
`assets/banner.svg` / `architecture.svg` / `logo.svg` (em-dash-free).

**Item 303 pass (comprehensive command + MCP reference).** Two new reference
docs were authored, grounded in the code and verified command-by-command and
verb-by-verb: `commands-reference.md` (every `revl` subcommand and its flags,
checked against `src/revl/cli/parser.py` + `revl <cmd> --help`) and
`mcp-reference.md` (all 36 verbs `revl mcp serve` advertises, checked against
`src/revl/mcp/server.py` + `query_tools.py`). Both are em-dash-free and carry no
`revl` fences, so the doc-example gate is untouched. The front door was folded
to link them and to add the session's new `doctor` (291) and `scaffold` (288)
commands and truc's `reproduce` (297) verb: `README.md`, `guide-ai-agents.md`
(MCP table extended with ship/undo/lease/canary/repair/quarantine and the
`fix`-in-rejection note from item 286), `guide-humans.md`. `docs/truc.md`
(owned by item 297) and any stdlib-json page (item 281) were left to their
owners; this pass only links to them.

The table below is GENERATED. `doc` and `em-dashes` come from the tree
(`docs/*.md`, and each file's em-dash count); `status` and `tier-limit
notes` are human judgement and are carried across a regeneration. Run
`make docs-gen` after editing any doc, or `tools/docgen.py --check` reds CI.
This is the mechanism the pass-notes above lacked: the 2026-09-02 inventory
was corrected by hand and had drifted back within a day.

<!-- docgen:doc-status begin -->
| doc | status | em-dashes | tier-limit notes |
|---|---|---|---|
| README.md | needs-work | 48 |  |
| apply.md | needs-work | 19 |  |
| arithmetic.md | needs-work | 68 |  |
| audit-diff.md | needs-work | 14 |  |
| authoring-for-agents.md | needs-work | 0 |  |
| auto-mocks.md | needs-work | 16 |  |
| backend-go-v3.md | needs-work | 15 | yes |
| backend-ir-v1.md | needs-work | 16 |  |
| backend-ir-v3.md | needs-work | 18 |  |
| backend-ir.md | needs-work | 11 |  |
| backends-roadmap.md | needs-work | 42 |  |
| bench-selfhost.md | live-owned (not audited) | 49 |  |
| boundary-policy.md | needs-work | 22 |  |
| bundle.md | needs-work | 0 |  |
| capabilities.md | needs-work | 28 |  |
| capability-attenuation.md | needs-work | 21 |  |
| capability-realm-placement.md | needs-work | 11 |  |
| closures.md | needs-work | 7 |  |
| collections.md | needs-work | 34 | yes |
| commands-reference.md | current | 5 |  |
| component-leases.md | needs-work | 24 |  |
| composition-bootstrap.md | needs-work | 1 |  |
| composition-layers.md | needs-work | 11 |  |
| composition-rows.md | needs-work | 13 |  |
| conformance.md | needs-work | 38 |  |
| contract-errata.md | needs-work | 93 | yes |
| crash-recovery.md | needs-work | 44 |  |
| dash.md | current | 11 |  |
| delivery-semantics.md | needs-work | 7 |  |
| deploy.md | needs-work | 37 |  |
| derived-versioning.md | current | 9 |  |
| design-v2-instances.md | needs-work | 69 |  |
| design-v2-realms.md | needs-work | 17 |  |
| distribution-model.md | stale-fixed | 7 |  |
| environment-binding.md | needs-work | 0 |  |
| erase-report.md | needs-work | 19 |  |
| evolve-loop.md | current | 18 |  |
| expressible-iteration.md | needs-work | 18 |  |
| fault-tests.md | needs-work | 70 |  |
| federation.md | current | 16 |  |
| fix-code.md | needs-work | 10 |  |
| fmt.md | needs-work | 13 |  |
| function-types.md | needs-work | 31 |  |
| gate-as-a-service.md | needs-work | 21 | yes |
| gate-dependency-contract.md | needs-work | 28 |  |
| gauntlet.md | needs-work | 17 |  |
| generation-history.md | current | 22 |  |
| generics.md | needs-work | 10 |  |
| guide-ai-agents.md | stale-fixed | 2 |  |
| guide-humans.md | stale-fixed | 0 | yes |
| harness-gate-guide.md | needs-work | 2 |  |
| holes.md | needs-work | 29 |  |
| import-a2a.md | current | 6 |  |
| import-cordis.md | needs-work | 58 |  |
| import-openapi.md | needs-work | 45 |  |
| import-wit.md | needs-work | 29 |  |
| int32-proposal.md | needs-work | 8 |  |
| integer-proposal.md | needs-work | 20 |  |
| interchange-format.md | needs-work | 8 |  |
| interop-bridge.md | needs-work | 51 |  |
| mcp-bridge.md | needs-work | 75 |  |
| mcp-reference.md | current | 3 |  |
| namespacing.md | needs-work | 8 |  |
| network-path.md | needs-work | 25 |  |
| network-placement.md | needs-work | 21 |  |
| opentelemetry.md | stale-fixed | 0 |  |
| operator-capabilities.md | needs-work | 26 |  |
| parallel-activation.md | needs-work | 17 |  |
| persistence.md | needs-work | 15 |  |
| plan.md | needs-work | 35 |  |
| process.md | needs-work | 2 |  |
| prompt-injection-resistance.md | needs-work | 38 |  |
| prop-test.md | needs-work | 23 |  |
| quarantine-tier.md | needs-work | 26 |  |
| queries.md | needs-work | 43 |  |
| records.md | needs-work | 15 |  |
| registry-probe.md | needs-work | 15 |  |
| registry.md | needs-work | 46 | yes |
| rejections.md | needs-work | 66 |  |
| repair-loop.md | needs-work | 24 |  |
| replay.md | needs-work | 51 |  |
| revl-attest.md | current | 9 |  |
| revl-diff.md | current | 19 |  |
| revl-metrics.md | current | 16 |  |
| revl-profile.md | current | 15 |  |
| router.md | stale-fixed | 5 |  |
| scaffold.md | needs-work | 2 |  |
| schedule-testing.md | needs-work | 0 |  |
| seam-deadlines.md | needs-work | 15 |  |
| selfhost-compile.md | stale-fixed | 0 |  |
| selfhost-findings.md | live-owned (not audited) | 251 |  |
| service-compat.md | needs-work | 17 |  |
| signals-and-queries.md | needs-work | 44 |  |
| stability.md | needs-work | 17 |  |
| state-handoff.md | needs-work | 22 |  |
| stdlib-2.0.md | current | 83 | yes |
| stdlib-json.md | needs-work | 41 |  |
| stdlib-list.md | current | 14 |  |
| stdlib-str.md | current | 16 |  |
| stdlib-value.md | current | 25 |  |
| stdlib-version.md | needs-work | 11 |  |
| strings.md | needs-work | 39 |  |
| swap.md | needs-work | 20 |  |
| syntax-2.0.md | needs-work | 104 |  |
| threat-model.md | needs-work | 27 |  |
| time-coeffect.md | needs-work | 35 | yes |
| token-economy.md | needs-work | 19 |  |
| truc.md | needs-work | 47 |  |
| verified-canary.md | needs-work | 22 |  |
| verified-effect.md | needs-work | 18 |  |
| vision.md | current | 8 |  |
| wasm-capabilities.md | needs-work | 35 | yes |
| why-runtime.md | needs-work | 29 |  |
| why-traces.md | needs-work | 26 |  |
| wit-bridge.md | needs-work | 41 |  |
| witnessed-fs.md | needs-work | 6 |  |
<!-- docgen:doc-status end -->
