# Compile sites and the export-area trust rule (issue #2563)

`revl_export` writes source only under `<root>/.revl/export/`, so a file there
is the agent's, by its path. The rule that decides that
(`export_area.in_export_area`, applied by `CliRule` and by the MCP server)
takes a path. A compile site that hands the compiler a label instead of a path
leaves the rule nothing to look at, and the text compiles with the operator's
trust.

Issue #2563 is the audit of those sites. This file is the census of the tree at
the slice's base (`origin/main` c397b526a) plus the sites the slice fixed;
`tools/label_compile_sites.py` produces the inventory.

## The census

    python tools/label_compile_sites.py            # readable table
    python tools/label_compile_sites.py --json     # machine rows

The pass parses every module under `src/revl` (no imports, no execution), finds
each call to `compiler.compile_source`, `compiler.compile_files` and
`registry._compile_source`, and prints the argument that names the text. A
literal that cannot be a real on-disk path (`<candidate>.rvl`, `component.rvl`,
`/__revl_registry__/...`, or an absent filename, which defaults to `<string>`)
is a **label**; an expression built from a path is **real**. That is a first
pass: `real` still has to be a file at run time, and a `label` can be correct
when its text is a module constant or an editor buffer. The tables below are
the reviewed classification.

**88 compile call sites: 74 real, 14 label.** The four compile sites this slice
fixed are among the real ones now; the fifth is a payload, not a compile call.

## The class this slice fixed

Registry and ship text compiled under a synthetic label while the file it came
from was in hand. All are read-back paths: the entry was loaded from a real
`components/<name>/component.rvl` (or the project entry file), then its text was
handed to the compiler under `"component.rvl"` / `<candidate>.rvl`, or the
gauntlet got `{"source": ...}`. The label answers no file, so the export rule
saw nothing and the text compiled as the operator's own.

| site | function | what it feeds |
|---|---|---|
| src/revl/__main__.py:244 | `_run_policy` | `revl policy evaluate --registry`: the audit IR |
| src/revl/__main__.py:250 | `_run_policy` | `revl policy evaluate --registry`: the attestation IR |
| src/revl/registry.py:2000 | `_compiled_entry_ir` | the adapter probe (`_compiled_entry_ir`) |
| src/revl/truc/_host.py:919 | `_knowledge_signed` | the vendored knowledge signature (`_knowledge_signed`) |
| src/revl/truc/components/shipper.rvl:278 + src/revl/truc/externs.rvl:123 + src/revl/truc/_host.py:1508 | `Shipper.run` | the cold gauntlet now gets `{"files": [sourcePath]}` through `host_gauntlet(source, sourcePath)` |

`revl policy evaluate --registry` was the observable fail-open: for an entry
whose registry had been placed under `.revl/export/`, `Registry.from_dir` had
already refused to verify it under the authoring profile (and recorded the
problem), and the dry run then printed `clean` and exited 0 by compiling the
same text under the label. It now exits 2 with the profile's refusal.

`truc ship` had the same shape one step earlier: `ship_context` resolved the
entry's real path (used by the admission and publish compiles since #2615 and
#2674), but the fresh gauntlet was handed only the text, so an exported entry
was graded admissible and BOOTED in the scratch session with the operator's
trust. The shipper now passes the entry's empty-or-real path through the
`host_gauntlet` extern and the gauntlet compiles the file. The empty case (text
with no file behind it) keeps the old behaviour and the publish refuses it
later, as `ship_ordered` already documents.

Tests: `tests/test_registry_entry_path_2563.py`, one per site class plus a
control for each (the same bytes outside the export area still evaluate, probe,
grade and verify), and a regression test for the stored path being absolute
(a relative `--registry` plus a later chdir must not hide the export area).
Red before: 5 failed, 4 passed. Green after: 9 passed.

The in-memory case is unchanged and deliberate: `registry._compile_source`
with no path still compiles under the virtual label
(`/__revl_registry__/<name>/component.rvl`), as slice 1 (#2615) left it. The
inversion for text from no file is the central rule's slice; every production
caller on this path holds a file.

## The 14 label-only sites that remain

Every one is classified. The embedder API makes the caller the operator; the
MCP sites either pass a profile already or are documented derivation/preview
compiles that admit and run nothing; the generated text sites have no file of
origin. The one label site whose text can be author-reachable today is
`plan.py:67`: the `revl_plan` preview hands it the agent's inline source, and
nothing there is admitted or booted. Profiling that label by default is the
central rule's slice, which is why it is not changed here.

| site | function | names | verdict | why |
|---|---|---|---|---|
| src/revl/gate.py:278 | `admit` | `(default <string>)` | out of scope | `Gate.admit`: embedder API; the caller is the operator's own code (issue list a) |
| src/revl/gate.py:297 | `admit_into` | `(default <string>)` | out of scope | `Gate.admit_into`: embedder API; the operator hands the manifest and the text |
| src/revl/gate.py:324 | `compile_to` | `(default <string>)` | out of scope | `Gate.compile_to`: embedder API |
| src/revl/gate.py:871 | `_compile_base` | `(default <string>)` | out of scope | `Gate._compile_base`: the embedder's own in-memory base sources |
| src/revl/gate.py:1032 | `propose` | `'<candidate>.rvl'` | already correct | `Gate.propose` passes the authoring profile for the agent's source |
| src/revl/gate.py:1188 | `_compile_candidate_composition` | `'candidate.rvl'` | already correct | composition compile after `propose` already decided the author |
| src/revl/mcp/edit.py:772 | `compile_virtual` | `'<candidate>.rvl'` | already correct | passes `profile=AUTHORING.profile()` |
| src/revl/mcp/operator.py:1060 | `_compile_candidate` | `'<candidate>.rvl'` | out of scope | target/class derivation only, documented UNPROFILED; the handler's compile enforces the trust |
| src/revl/mcp/proxy.py:403 | `_compile` | `'<revl mcp proxy>.rvl'` | out of scope | generated proxy source (`profile=None` already); upstream text only in comments |
| src/revl/mcp/query_tools.py:45 | `_compile` | `'<query>.rvl'` | out of scope | shape query, never admitted |
| src/revl/mcp/server.py:851 | `_compile_once` | `'<candidate>.rvl'` | already correct | the one compiler door; `compile_under_authoring` decides the profile |
| src/revl/plan.py:67 | `_compile` | `'<candidate>.rvl'` | out of scope | library dispatch; the MCP path that reaches it is the central rule's marker slice |
| src/revl/pool_dispatch.py:398 | `_compile_artifact` | `['artifact.rvl']` | out of scope | digest-pinned artifact bytes, compiled only to classify; export origin unverified (issue list a) |
| src/revl/registry.py:1819 | `_needs` | `'<need>.rvl'` | out of scope | consumer `need` declaration, shape query only |

## Non-AST label sites

One site loses a path without calling the compiler itself, so the AST pass
cannot see it:

* `src/revl/cli/change.py:927` joins `repair --candidate` files into
  `{"source": "\n".join(...)}`. The join loses every path, and the repair door
  compiles the result as `<candidate>.rvl`. It needs a decision the join cannot
  express (profile the whole candidate if any named file is under the export
  area, or give the door a files form) and it boots a runtime to test, so it is
  left for the central-rule slice. It is the only known remaining site of this
  class.

The other payload sites are covered: `policy evaluate --registry --recompute`
and `truc ship` now send `files`; `registry._compiled_entry_ir` and
`_knowledge_signed` were compile calls in the census above.

## Appendix: the complete census (88 sites)

`already correct` means the export rule can see a real file, or a
profile/derivation decision is already made at that site; `out of scope` names
why a label cannot be export text there.

| site | function | argument | class | verdict | why |
|---|---|---|---|---|---|
| src/revl/__main__.py:244 | `_run_policy` | `entry.source_path` | real | fixed here | `revl policy evaluate --registry`: the audit IR |
| src/revl/__main__.py:250 | `_run_policy` | `entry.source_path` | real | fixed here | `revl policy evaluate --registry`: the attestation IR |
| src/revl/__main__.py:275 | `_run_policy` | `args.files` | real | already correct | real path argument |
| src/revl/__main__.py:400 | `_run_simulate` | `args.composition` | real | already correct | real path argument |
| src/revl/__main__.py:937 | `_run_emit` | `args.files` | real | already correct | real path argument |
| src/revl/__main__.py:1109 | `compile_source_holes` | `filename` | real | out of scope | scaffold skeleton built from CLI flags (generated) |
| src/revl/__main__.py:1331 | `_run_analyze` | `args.files` | real | already correct | real path argument |
| src/revl/__main__.py:1551 | `_dispatch` | `args.files` | real | already correct | real path argument |
| src/revl/_process_runner.py:901 | `run` | `spec['files']` | real | already correct | real path argument |
| src/revl/attest.py:740 | `run_gate` | `list(paths)` | real | out of scope | no in-tree caller passes `source=` to `run_gate` |
| src/revl/attest.py:742 | `run_gate` | `filename` | real | out of scope | same `run_gate`: the `source=` case has no in-tree caller |
| src/revl/bundle.py:1140 | `_verify_bundle_dir` | `source_paths` | real | already correct | real path argument |
| src/revl/cli/adapt.py:40 | `_load` | `path` | real | already correct | real path argument |
| src/revl/cli/change.py:82 | `_run_canary` | `list(args.files)` | real | already correct | real path argument |
| src/revl/cli/change.py:890 | `_run_repair` | `args.files` | real | already correct | real path argument |
| src/revl/cli/interop.py:415 | `_run_mcp` | `args.files` | real | already correct | real path argument |
| src/revl/cli/interop.py:535 | `_run_serve` | `args.files` | real | already correct | real path argument |
| src/revl/cli/interop.py:781 | `_run_export` | `args.files` | real | already correct | real path argument |
| src/revl/cli/interop.py:860 | `_run_contract` | `args.files` | real | already correct | real path argument |
| src/revl/cli/interop.py:889 | `_run_contract` | `provider_paths` | real | already correct | real path argument |
| src/revl/cli/interop.py:1020 | `_act_session` | `args.files` | real | already correct | real path argument |
| src/revl/cli/observe.py:58 | `_run_why` | `args.check` | real | already correct | real path argument |
| src/revl/cli/observe.py:117 | `_run_dash` | `args.files` | real | already correct | real path argument |
| src/revl/cli/promote.py:176 | `_run_promote` | `list(args.files)` | real | already correct | real path argument |
| src/revl/cli/promote.py:177 | `_run_promote` | `list(args.candidate)` | real | already correct | real path argument |
| src/revl/compiler.py:1116 | `compile_source` | `[filename]` | real | already correct | internal pass-through |
| src/revl/compiler.py:1245 | `compile_files` | `once` | real | already correct | internal pass-through |
| src/revl/composition.py:2511 | `_admit_full` | `[os.path.join(root, p) for p in table.paths()]` | real | already correct | real path argument |
| src/revl/composition.py:2643 | `admit_composition` | `[os.path.join(root, p) for p in paths]` | real | already correct | real path argument |
| src/revl/composition_diff.py:44 | `load_composition` | `[path]` | real | already correct | real path argument |
| src/revl/composition_diff.py:49 | `load_composition` | `[path]` | real | already correct | real path argument |
| src/revl/composition_diff.py:54 | `load_composition` | `[path]` | real | already correct | real path argument |
| src/revl/counterfactual.py:219 | `_candidate_audit` | `list(files)` | real | already correct | real path argument |
| src/revl/dev.py:260 | `_preflight` | `files` | real | already correct | real path argument |
| src/revl/formatter.py:717 | `_compile_ir` | `[filename]` | real | out of scope | editor/stdin text, diagnostics only |
| src/revl/formatter.py:719 | `_compile_ir` | `filename` | real | out of scope | editor/stdin text, diagnostics only |
| src/revl/gate.py:278 | `admit` | `(default <string>)` | label | out of scope | `Gate.admit`: embedder API; the caller is the operator's own code (issue list a) |
| src/revl/gate.py:297 | `admit_into` | `(default <string>)` | label | out of scope | `Gate.admit_into`: embedder API; the operator hands the manifest and the text |
| src/revl/gate.py:324 | `compile_to` | `(default <string>)` | label | out of scope | `Gate.compile_to`: embedder API |
| src/revl/gate.py:871 | `_compile_base` | `(default <string>)` | label | out of scope | `Gate._compile_base`: the embedder's own in-memory base sources |
| src/revl/gate.py:875 | `_compile_base` | `paths` | real | already correct | real path argument |
| src/revl/gate.py:1032 | `propose` | `'<candidate>.rvl'` | label | already correct | `Gate.propose` passes the authoring profile for the agent's source |
| src/revl/gate.py:1188 | `_compile_candidate_composition` | `'candidate.rvl'` | label | already correct | composition compile after `propose` already decided the author |
| src/revl/gate.py:1196 | `_compile_candidate_composition` | `paths` | real | already correct | real path argument |
| src/revl/gate_service.py:69 | `admit` | `paths` | real | already correct | real paths, untrusted author already |
| src/revl/gate_service.py:122 | `admit_structured` | `paths` | real | already correct | real paths, untrusted author already |
| src/revl/gate_service.py:189 | `admit_case` | `list(_RUNNING_SOURCES)` | real | out of scope | module-constant fixture sources |
| src/revl/lsp/analysis.py:59 | `compute_diagnostics` | `filename` | real | out of scope | editor buffer, diagnostics only |
| src/revl/mcp/edit.py:772 | `compile_virtual` | `'<candidate>.rvl'` | label | already correct | passes `profile=AUTHORING.profile()` |
| src/revl/mcp/operator.py:1060 | `_compile_candidate` | `'<candidate>.rvl'` | label | out of scope | target/class derivation only, documented UNPROFILED; the handler's compile enforces the trust |
| src/revl/mcp/operator.py:1064 | `_compile_candidate` | `list(arguments['files'])` | real | out of scope | target/class derivation only, as at 1060 |
| src/revl/mcp/persist.py:253 | `_recompile` | `name` | real | already correct | snapshot recompile, explicit profile |
| src/revl/mcp/persist.py:267 | `_recompile` | `list(files)` | real | already correct | snapshot recompile, explicit profile |
| src/revl/mcp/proxy.py:403 | `_compile` | `'<revl mcp proxy>.rvl'` | label | out of scope | generated proxy source (`profile=None` already); upstream text only in comments |
| src/revl/mcp/query_tools.py:45 | `_compile` | `'<query>.rvl'` | label | out of scope | shape query, never admitted |
| src/revl/mcp/query_tools.py:53 | `_compile` | `list(files)` | real | already correct | real path argument |
| src/revl/mcp/server.py:848 | `_compile_once` | `paths` | real | already correct | real path argument |
| src/revl/mcp/server.py:851 | `_compile_once` | `'<candidate>.rvl'` | label | already correct | the one compiler door; `compile_under_authoring` decides the profile |
| src/revl/mcp/server.py:855 | `_compile_once` | `list(files)` | real | already correct | real path argument |
| src/revl/mcp/session.py:5041 | `admit` | `filename` | real | already correct | admission hot path, real filename |
| src/revl/mcp/session.py:9628 | `_forward_surface_for_turn` | `filename` | real | already correct | admission hot path, real filename |
| src/revl/placement.py:2009 | `seam_readmission` | `list(files)` | real | already correct | real path argument |
| src/revl/placement.py:2067 | `swap_admission` | `list(files)` | real | already correct | real path argument |
| src/revl/placement.py:2336 | `_plan_time_residency` | `files` | real | already correct | real path argument |
| src/revl/placement.py:4194 | `run_placement` | `files` | real | already correct | real path argument |
| src/revl/plan.py:67 | `_compile` | `'<candidate>.rvl'` | label | out of scope | library dispatch; the MCP path that reaches it is the central rule's marker slice |
| src/revl/plan.py:71 | `_compile` | `list(files)` | real | already correct | real path argument |
| src/revl/pool_dispatch.py:398 | `_compile_artifact` | `['artifact.rvl']` | label | out of scope | digest-pinned artifact bytes, compiled only to classify; export origin unverified (issue list a) |
| src/revl/pool_dispatch.py:405 | `_compile_artifact` | `[str(root.joinpath(*name.split('/'))) for name in artifact.roots()]` | real | out of scope | artifact roots, compiled only to classify |
| src/revl/recover_binding.py:361 | `compile_composition` | `list(files)` | real | already correct | real path argument |
| src/revl/registry.py:836 | `build_index` | `[str(source_path)]` | real | already correct | real path argument |
| src/revl/registry.py:962 | `verify` | `[str(source_path)]` | real | already correct | real path argument |
| src/revl/registry.py:1069 | `_compile_source` | `[path]` | real | already correct | the helper resolves the real path; its no-path branch is the deferred label fallback |
| src/revl/registry.py:1085 | `_compile_stored` | `[path]` | real | already correct | publish dry run of text already compiled from its origin; the name is the destination path |
| src/revl/registry.py:1199 | `release_facts` | `source_path` | real | already correct | `_compile_source` is handed the real `source_path` |
| src/revl/registry.py:1269 | `release_facts` | `entry_dir / 'component.rvl'` | real | already correct | the entry's real `component.rvl` inside the registry |
| src/revl/registry.py:1513 | `publish_release` | `entry_dir / 'component.rvl'` | real | already correct | the entry's real `component.rvl` inside the registry |
| src/revl/registry.py:1514 | `publish_release` | `source_path` | real | already correct | `_compile_source` is handed the real `source_path` |
| src/revl/registry.py:1609 | `_verified_facts` | `[str(source_path)]` | real | already correct | real path argument |
| src/revl/registry.py:1819 | `_needs` | `'<need>.rvl'` | label | out of scope | consumer `need` declaration, shape query only |
| src/revl/registry.py:2000 | `_compiled_entry_ir` | `entry.source_path` | real | fixed here | the adapter probe (`_compiled_entry_ir`) |
| src/revl/run.py:2556 | `_reload` | `files` | real | already correct | real path argument |
| src/revl/run.py:2735 | `run_command` | `args.files` | real | already correct | real path argument |
| src/revl/scaffold.py:368 | `scaffold_document` | `filename` | real | out of scope | generated scaffold skeleton |
| src/revl/truc/_host.py:464 | `_compile_row` | `[path]` | real | already correct | the row path is the vendored real file |
| src/revl/truc/_host.py:919 | `_knowledge_signed` | `str(source_path)` | real | fixed here | the vendored knowledge signature (`_knowledge_signed`) |
| src/revl/truc/_launcher.py:255 | `main` | `sources` | real | already correct | real path argument |
| src/revl/truc/reproduce.py:814 | `reproduce` | `[str(source_path)]` | real | already correct | real path argument |
