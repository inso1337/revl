---
id: P47
title: Oracles Prevent Silent Divergence, Not Wrong Agreement: Coverage Blind Spots, Independence Decay and a Cooking-Resistant Census
theme: compiler
kind: methods
maturity: shipped-evidence
status: The coverage ledgers, ratchets, provenance sidecar and census are on main with tests and CI steps; the headline coverage figure is dated 2026-09-02 and needs a re-take, and the yield study (do closures find real bugs) has not been done.
fused_with: P70
---

## Pitch
A byte-for-byte differential oracle between two compilers certifies only what its corpus executes: in one project, 53.8% of the reference emitter's statements were never run by the corpus behind a green oracle, and the tooling that now measures, ratchets and fails closed on that gap is open and reproducible.

## Abstract
Differential testing between two independently written implementations is the standard answer to "who tests the compiler". It prevents silent divergence on the inputs it is given. It does not say which side is right when both agree, does not demand inputs nobody wrote, and cannot see that the two sides mirror different sets of source-of-truth data. We report on a compiler with a second, self-hosted implementation held to byte-agreement across six code emitters, and on the same-day failures (three by the roadmap item's header, five in its body) that showed the oracle was green over features the self-host lacked. Measuring in statements instead of dispatch arms, 8633 of 16037 reference-emitter statements (53.8%) were never executed by the corpus the oracle runs, 2797 of 11217 port statements were unrun, and 59 of 569 self-host functions were never entered; the construct-level table had said about 19%. We describe the mechanisms built in response: a shrink-only ledger of unreached constructs with a written disposition per entry; a line-level ledger; a ratchet that fails when an entry is stale as well as when a gap is new; per-document generation provenance, with an undeclared document counted as model-authored so that adding a file can never raise measured independence; and a published gate-versus-reference census whose false-admission bucket is dropped when the baseline is written and failed by the check step even against a hand-edited baseline. The tests named in the evidence table exercise the mechanisms; the secret-marking ablation constructs a known gap and checks that the gate names it. We state what the design cannot establish.

## Problem and claim
A byte-agreement oracle between a reference compiler and a rewrite is only evidence in proportion to (a) how much of the reference the corpus reaches and (b) how independent the two implementations are. Neither is usually measured. The project's own roadmap names the failure as the dominant mode of its oracle. Item 429 recurred within one day: its header counts three occurrences, and its body gives a fifth (`docs/v2.0-roadmap.md:1685-1689`). In the two cases its (a) part describes, the self-host was right and the reference wrong, so "the oracle is green" was true and uninformative.

Two further effects make the problem worse in a repository where a model writes code. The corpus directories are globbed, so a generated document enters every projection with no list to edit and is indistinguishable from a hand-written one (`docs/design/544-corpus-provenance.md:20-30`). And the tree that is the oracle is also the next generation's training and scoring set, so independence is "a stock the loop spends" (`docs/design/544-corpus-provenance.md:20-24`).

The claim is limited and checkable. A differential oracle needs a measured reach, a shrink-only list of what it does not reach, a fail-closed measure of independence, and a published disagreement census in which the dangerous direction cannot be re-baselined away. We show these exist in one repository, what each caught, and where each stops.

## Contributions
1. A measurement of the blind spot in statements, not constructs: 8633 of 16037 reference statements unexecuted (53.8%), 2797 of 11217 port statements unrun, 59 of 569 self-host functions never entered, with the unreached mass split into 3899 statements in 263 uncalled functions, 2994 in 318 called-but-unrun functions, and 1740 on declared exclusions (`tools/selfhost_line_coverage.py:18-45`). The construct table understated the gap nearly threefold (the tool's own header says "optimistic by nearly three times").
2. A shrink-only construct-reach ledger with a written disposition per entry, derived from a sibling ledger or stated directly, and failing in both directions (new unreached construct, or stale entry), built after a report that "could not fail" let agreement go vacuous (`tools/oracle_construct_reach.py:1-60`).
3. A first live catch: the coverage ratchet failed on `Secret[T]` marking constructs in the ts and go reference emitters that a written sibling survey had called clean (`docs/v2.0-roadmap.md` item 429 (5); `tests/test_selfhost_coverage.py:127`).
4. Corpus provenance as a fail-closed sidecar: an undeclared document counts as model-authored at every threshold and is its own error; per-corpus independence floors; an empty corpus crosses every floor; and a separate axis (human-typed) because "generation 0" was measured not to mean "hand-written" (`tools/corpus_provenance.py:100-130`).
5. A published gate-versus-reference census with `n`, checker version and a named standing allowance, in which the false-admission bucket is in `NEVER_BASELINED` (`tools/gate_reference_census.py:1071`), dropped when the baseline is written and failed by `--check` even on a hand-edited baseline, plus a driven probe that reports failure instead of hiding it (`docs/design/560-census-artifact.md`).
6. A progress term in the self-evolution reward so that the empty diff does not score perfectly (`docs/design/546-evolution-progress-term.md:1-20`, `tools/evolution_progress.py`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| McKeeman, differential testing (citation to be checked) | Compares implementations on generated inputs; disagreement is the signal | Measures what the comparison never reaches, in statements, and ratchets the unreached set |
| Csmith (Yang et al. 2011) (citation to be checked), YARPGen (citation to be checked) | Generate programs, find compiler bugs by comparison | Not about input generation: about the reach of a fixed, curated corpus behind a CI gate, and the cost of closing it (see the triage argument in the outline) |
| N-version programming (Knight and Leveson 1986) (citation to be checked) | Shows independently written versions fail in correlated ways | Treats independence as a decaying, measured quantity per corpus document, with floors, when a model writes both sides |
| Mutation testing | Estimates whether tests would notice a change | Used here as an ablation: remove the secret fixtures and the gate must name the two markings (`tests/test_selfhost_coverage.py:127-160`) |
| Coverage-guided fuzzing | Maximises reach on one implementation | Coverage of the reference by the oracle corpus, with a ledger the diff must edit |
| Benchmark contamination literature (citation to be checked) | Detects test items leaking into training data | Provenance of scoring documents inside the repository, fail-closed on unknown |
| CompCert translation validation (see the [related-work table](../related-work.md)) | Validates each compilation against a spec | Not attempted; this paper is about the empirical oracle that stands where no proof exists |
| Reproducible-artifact practice (citation to be checked) | Publishes data and scripts | The census files are regenerated, never typed, and `--verify --strict` fails on hand edits (`docs/design/560-census-artifact.md`) |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Oracle blind spot recurred within one day; header says three times, body gives five; the two (a) cases have the self-host right | `docs/v2.0-roadmap.md:1685-1689` (item 429 a-d) | shipped (finding) | Roadmap text is the author's own account; the header and body counts disagree, so both are quoted |
| 8633/16037, 2797/11217, 59/569, 3899+2994+1740 | `tools/selfhost_line_coverage.py:18-45`; `docs/v2.0-roadmap.md:1696` | shipped (measurement, dated 2026-09-02) | Not re-run for this draft; see "Numbers" for the ledger as it stands today |
| Line ledger and ratchet | `tests/fixtures/selfhost_uncovered_lines/` (`reference/`, `selfhost/`, `README.md`); `tests/test_selfhost_line_coverage.py` | shipped | The self-host half is per function, not per line: the IR drops `.line` (`docs/v2.0-roadmap.md` item 429 (2)) |
| Construct ratchet, ablation proof | `tools/selfhost_coverage.py`; `tests/test_selfhost_coverage.py:127`; CI `.github/workflows/ci.yml:698` | shipped | |
| Shrink-only ledger with dispositions | `tools/oracle_construct_reach.py`; `tests/fixtures/oracle_construct_reach_ledger.json`; `tests/test_oracle_construct_reach.py:64,85,301,341,363,378`; CI `.github/workflows/ci.yml:703` | shipped | The ledger holds names only (the names-only test at `tests/test_oracle_construct_reach.py:124`) |
| Provenance fail-closed | `tools/corpus_provenance.py`; `tests/test_corpus_provenance.py:59,70,163,183,193`; CI `.github/workflows/ci.yml:712` | shipped | Gen-0 is a declaration, not a measurement (`docs/design/544-corpus-provenance.md`, "Generation zero is a declaration") |
| Generation is not authorship | `tools/corpus_provenance.py:100-130`; `tests/test_corpus_provenance.py:317,372` | shipped | 153 of 850 gen-0 entries came from `agent/*` branches, 132 more carry a co-author trailer, 415 have no PR; the docs call these lower bounds |
| False-admission cannot be baselined | `tools/gate_reference_census.py:1071,1209`; `tests/test_gate_reference_census.py:756`; `tests/test_census_artifact.py:158,181,190,202` | shipped | |
| Census files, drift verified in CI | `docs/census-artifact/`; `.github/workflows/ci.yml:1363` (`census_artifact.py --verify --strict`); `.github/workflows/pages.yml:106` | shipped | The CI step runs `--verify --strict`, not `--check` |
| Progress term | `tools/evolution_progress.py`; `docs/design/546-evolution-progress-term.md`; `tools/evolution_reward.py:93-107,1310` | shipped | |

## Numbers that exist
- Statement coverage, reference emitters, six tiers: 8633 of 16037 unexecuted (53.8%); per tier covered: py 56.4%, ts 65.9%, go 25.2%, java 55.7%, rust 49.8%, wasm 42.8%. Source: `tools/selfhost_line_coverage.py:18-35`, n = 114 corpus documents, measured 2026-09-02.
- Ports: 2797 of 11217 emitted statements unrun (24.9%); 59 of 569 self-host functions never entered (`docs/v2.0-roadmap.md:1696`).
- Free-adoption experiment: adopting all 1136 byte-agreeing (tier, document) pairs, a tenfold corpus, moves reference coverage from 46.3% to 51.0% (the same docstring's table totals the baseline at 46.2%; the prose figure is used here and the mismatch is unresolved in the source); whole tree reaches 80.9%. That survey was taken at 867 documents (619 compiling) and is explicitly not re-taken (`tools/selfhost_line_coverage.py:36-52`).
- Today's line ledger, summed from `tests/fixtures/selfhost_uncovered_lines/`: reference side 6135 unexecuted statements across 713 function entries; self-host side 794 across 343. These are ledger sums; the totals of statements (the denominators) were not recomputed, so no percentage is stated.
- Construct-reach ledger today: 190 named unreached constructs (emit_py 6, ts 6, go 42, java 38, rust 36, wasm 54, gate_census 6, compile 1, lower_ir 1) against 249 across nine oracles when issue #1203 opened (`tools/oracle_construct_reach.py:15-18`; `tests/fixtures/oracle_construct_reach_ledger.json`).
- Provenance manifest: 1281 generation-0 names (the module docstring counted 850 at an earlier commit: 565 documents plus 285 inline oracle programs) and 53 generation-1 names; floors 75% for each emit corpus, 80% census, 90% selfhost_oracle (`tests/fixtures/corpus_provenance.json`). Per-bucket model-authored fractions are computed by `python3 tools/corpus_provenance.py` at read time and were not run for this draft.
- Census: 1334 case files under `docs/census-artifact/cases/`; 675 `agree-admit`, 242 `agree-refuse/G4`, 116 `agree-refuse/T1`; no case in a `false-*` bucket; `tools/gate_reference_census_baseline.json` has an empty `buckets` object (standing allowance zero as committed).

## Paper outline
1. Introduction: a green oracle and three same-day misses.
2. Background: the two implementations and the byte-agreement gate (cite P46).
3. What an oracle cannot see: wrong agreement, unreached inputs, mirrored data sets.
4. Measuring reach: constructs versus statements, the 3x gap, the triage argument against "just add corpus".
5. Ratchets that fail in both directions; dispositions; the secret-marking catch and its ablation.
6. Independence as a stock: provenance, fail-closed undeclared, generation is not authorship.
7. A census where the dangerous direction cannot be baselined; the driven probe.
8. The empty-diff reward and the progress term.
9. Yield study and limits.

## Threats to validity and limits
- The 53.8% figure is dated 2026-09-02 and the ledger has shrunk since; the headline must be re-taken before it is quoted as current.
- The yield question (does closing the blind spot find bugs) is argued from instances in the roadmap, not measured.
- The tool says "What this cannot check is whether a written reason is a GOOD reason" (`tools/oracle_construct_reach.py`); dispositions are judgements.
- Generation 0 "is a declaration, not a measurement" (`docs/design/544-corpus-provenance.md`). The branch-name and trailer signals are lower bounds, and the trailer stops appearing after 2026-09-13. The 153, 132 and 415 split is stated in a docstring and is not reproduced by a committed script.
- The sidecar "can go stale"; `--check` is what holds it, not the filesystem.
- The setting is a single project with one author identity by policy, a corpus that grew from zero in 39 days, and a workflow in which models write much of the code; generalisation is not shown.
- The self-host measurement is per function, not per line (item 429 (2), "NOT DONE, deliberately").
- No mutation study yet validates the 53.8% figure as a predictor of missed bugs.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: about 40 checked, 5 corrected.
