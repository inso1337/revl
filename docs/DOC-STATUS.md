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

Front-door files outside `docs/` were also refreshed this pass: `README.md`
(stale `self_hosted: in_progress` badge replaced; 8 missing CLI verbs added;
self-hosting status added; em-dash-free), `site/index.html`, `site/README.md`,
`assets/banner.svg` / `architecture.svg` / `logo.svg` (em-dash-free).

| doc | status | em-dashes | tier-limit notes |
|---|---|---|---|
| apply.md | needs-work | 19 |  |
| arithmetic.md | needs-work | 69 |  |
| audit-diff.md | needs-work | 12 |  |
| auto-mocks.md | needs-work | 16 |  |
| backend-go-v3.md | needs-work | 15 | yes |
| backend-ir-v1.md | needs-work | 16 |  |
| backend-ir-v3.md | needs-work | 15 |  |
| backend-ir.md | needs-work | 11 |  |
| backends-roadmap.md | needs-work | 44 |  |
| bench-selfhost.md | live-owned (not audited) | 14 |  |
| boundary-policy.md | needs-work | 18 |  |
| capabilities.md | needs-work | 23 |  |
| capability-attenuation.md | needs-work | 21 |  |
| capability-realm-placement.md | needs-work | 11 |  |
| collections.md | needs-work | 34 | yes |
| component-leases.md | needs-work | 24 |  |
| conformance.md | needs-work | 32 |  |
| contract-errata.md | needs-work | 90 | yes |
| crash-recovery.md | needs-work | 32 |  |
| dash.md | current | 11 |  |
| delivery-semantics.md | needs-work | 7 |  |
| derived-versioning.md | current | 9 |  |
| design-v2-instances.md | needs-work | 69 |  |
| design-v2-realms.md | needs-work | 17 |  |
| distribution-model.md | stale-fixed | 0 |  |
| erase-report.md | needs-work | 19 |  |
| evolve-loop.md | current | 18 |  |
| expressible-iteration.md | needs-work | 18 |  |
| fault-tests.md | needs-work | 48 |  |
| federation.md | current | 16 |  |
| fmt.md | needs-work | 13 |  |
| function-types.md | needs-work | 27 |  |
| gate-as-a-service.md | needs-work | 18 | yes |
| gauntlet.md | needs-work | 17 |  |
| generation-history.md | current | 22 |  |
| generics.md | needs-work | 9 |  |
| guide-ai-agents.md | stale-fixed | 0 |  |
| guide-humans.md | stale-fixed | 0 | yes |
| holes.md | needs-work | 29 |  |
| import-cordis.md | needs-work | 58 |  |
| import-openapi.md | needs-work | 39 |  |
| import-wit.md | needs-work | 28 |  |
| int32-proposal.md | needs-work | 8 |  |
| integer-proposal.md | needs-work | 20 |  |
| interchange-format.md | needs-work | 7 |  |
| interop-bridge.md | needs-work | 51 |  |
| mcp-bridge.md | needs-work | 68 |  |
| namespacing.md | needs-work | 8 |  |
| network-path.md | needs-work | 25 |  |
| network-placement.md | needs-work | 16 |  |
| opentelemetry.md | stale-fixed | 0 |  |
| operator-capabilities.md | needs-work | 24 |  |
| parallel-activation.md | needs-work | 17 |  |
| persistence.md | needs-work | 15 |  |
| plan.md | needs-work | 35 |  |
| prompt-injection-resistance.md | needs-work | 38 |  |
| prop-test.md | needs-work | 23 |  |
| quarantine-tier.md | needs-work | 26 |  |
| queries.md | needs-work | 43 |  |
| records.md | needs-work | 11 |  |
| registry-probe.md | needs-work | 15 |  |
| registry.md | needs-work | 24 | yes |
| rejections.md | needs-work | 58 |  |
| repair-loop.md | needs-work | 24 |  |
| replay.md | needs-work | 51 |  |
| revl-attest.md | current | 20 |  |
| revl-diff.md | current | 19 |  |
| revl-metrics.md | current | 16 |  |
| revl-profile.md | current | 15 |  |
| router.md | stale-fixed | 0 |  |
| seam-deadlines.md | needs-work | 15 |  |
| selfhost-compile.md | stale-fixed | 0 |  |
| selfhost-findings.md | live-owned (not audited) | 241 |  |
| service-compat.md | needs-work | 17 |  |
| signals-and-queries.md | needs-work | 44 |  |
| stability.md | needs-work | 17 |  |
| state-handoff.md | needs-work | 22 |  |
| stdlib-2.0.md | current | 66 | yes |
| stdlib-json.md | needs-work | 22 |  |
| stdlib-list.md | current | 14 |  |
| stdlib-str.md | current | 11 |  |
| stdlib-value.md | current | 24 |  |
| strings.md | needs-work | 39 |  |
| swap.md | needs-work | 11 |  |
| syntax-2.0.md | needs-work | 87 |  |
| threat-model.md | needs-work | 27 |  |
| time-coeffect.md | needs-work | 35 | yes |
| token-economy.md | needs-work | 19 |  |
| truc.md | needs-work | 43 |  |
| v2.0-roadmap.md | needs-work | 851 | yes |
| verified-canary.md | needs-work | 22 |  |
| verified-effect.md | needs-work | 18 |  |
| vision.md | current | 8 |  |
| wasm-capabilities.md | needs-work | 20 | yes |
| why-runtime.md | needs-work | 29 |  |
| why-traces.md | needs-work | 26 |  |
| wit-bridge.md | needs-work | 41 |  |