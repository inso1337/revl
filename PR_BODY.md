## What and why

`revl test <files>` ran every `test` block in a compilation and reported one aggregate count plus failures. There was no way to see **which** tests a compilation collects without running them, and no way to run a single one without compiling a reduced file set, which is not viable in a composition whose components do not stand alone. This is the highest-value subset of #843.

Two flags, plus a defined exit-code contract:

- **`--list`** prints every collected test unit name and executes nothing. Compiling the sources *is* the collection, so no tier, emitter, or runner is reached. It answers "which tests does this compilation collect" for free, and it is the liveness proof an aggregate pass count cannot give.
- **`--filter PATTERN`** runs only the collected units whose name contains `PATTERN`. This is a plain **substring** match, deliberately not a regex: a regex would be a second language for the same job, and an accidentally special character (`.`, `(`, `|`) would silently select a different set than the author read.
- Selection prunes the **IR's test sections**, not a runner's output, so every tier runner and every mode that reads a test section honours the same selection. `--mock-requires` runs the `tests` section's lifecycle blocks and therefore selects them too. The input IR is not mutated.
- Both flags cover the whole named surface: `test`, `fault test`, and `prop test` blocks, in the order the py reference tier runs them.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | listed everything / every selected test passed |
| 1 | a selected test failed (unchanged) |
| 2 | a usage error: `--filter` matched nothing, `--list` or `--filter` over a compilation that collects **zero** test units, or `--filter` combined with `--sweep` / `--schedule-*` |

An empty selection is never a silent green, because that is precisely the ambiguity the issue is about: a green run over zero tests is indistinguishable from a green run over tests that all passed.

`--filter` with `--sweep` or `--schedule-seeds` is refused rather than quietly ignored: those modes build their units from components (the sweep synthesizes `sweep <component> @ step N` names and the schedule sweep does not read `tests` at all), so they do not run named units and filtering them would select nothing while looking like it selected something. The message says to run them separately.

A command line carrying neither flag is unchanged, including the flagless zero-test path (`no tests to run`, exit 0).

## Issue

Advances #843 (`revl test` cannot select, list, or observe an individual test). Selection and reporting only: nothing about the checker or any guarantee is weakened, and the same tests keep the same verdicts.

## Verification

Interpreter: `PYTHONPATH=src /Users/inso/Projects/revl/.venv/bin/python -m pytest` (local Python 3.14; CI runs 3.11, no 3.12+ syntax used).

Narrowest module for the verb, and the red proof that the new tests are real:

```
$ PYTHONPATH=src python -m pytest tests/test_test_command.py -q
18 passed, 2 skipped, 2 warnings in 6.20s

$ git stash push -- src/revl/test.py src/revl/cli/parser.py src/revl/__main__.py
$ PYTHONPATH=src python -m pytest tests/test_test_command.py -q
9 failed, 9 passed, 2 skipped        # every new flag test red, on the unpatched tree
$ git stash pop
$ PYTHONPATH=src python -m pytest tests/test_test_command.py -q
18 passed, 2 skipped
```

The 10 new tests:

- `test_list_prints_every_collected_name_and_runs_nothing` asserts the exact stdout lines and, in the same assertion, that no verdict line, no tier line, and no failure from the test that fails when it executes appear.
- `test_list_never_reaches_a_tier_runner` monkeypatches *every* `RUNNERS` entry to raise, so reaching a runner is a hard failure rather than an inferred absence.
- `test_list_covers_every_named_test_section`, `test_list_with_a_filter_lists_only_the_selection`.
- `test_filter_runs_only_the_selection`, `test_filter_selects_the_units_every_tier_receives`.
- `test_filter_matching_nothing_is_a_usage_error_not_a_green` (exit 2, empty stdout, message on stderr).
- `test_list_of_a_compilation_with_no_tests_is_a_usage_error`.
- `test_filter_refuses_the_modes_that_do_not_run_named_units` (`--sweep` and `--schedule-seeds`).
- `test_no_new_flags_keeps_the_aggregate_output_and_exit_codes`.

Whole-selection run for a CLI change (`tools/affected_tests.py`, 344 files + gates), in the foreground:

```
$ PYTHONPATH=src python -m pytest <selected files> -q
8971 passed, 806 skipped, 20 xfailed, 17 warnings in 3260.27s (0:54:20)   # exit 0
```

Lint, docs, and the repo's own gates:

```
$ python -m ruff check .                     # All checks passed!
$ python tools/docgen.py --write             # docgen: blocks already current.
$ python tools/docgen.py --check             # 9 generated blocks current, 4 coverage checks pass.
$ python tools/conformance.py --check-readme # up to date
```

Manual transcripts (a 3-test file: `add works`, `add is commutative`, `boom` where `boom` fails):

```
$ revl test demo.rvl --list
add works
add is commutative
boom
3 test(s) collected                                      # exit 0, nothing executed

$ revl test demo.rvl --filter add
PASS add works
PASS add is commutative
[py] pass: 2 test(s) passed                              # exit 0, `boom` never ran

$ revl test demo.rvl --filter zzz                        # exit 2
error: --filter 'zzz' matched none of the 3 collected test unit(s); nothing ran

$ revl test notests.rvl --list                           # exit 2
error: this compilation collects no test units, so there is nothing to list or select; nothing ran

$ revl test notests.rvl                                  # exit 0, unchanged
no tests to run

$ revl test demo.rvl --sweep --filter add                # exit 2
error: --filter selects test units by name, and --sweep / --schedule-* sweep the composition's steps or interleavings rather than running named units; run them separately (issue #843)
```

Docs touch the CLI surface, so the docgen blocks were regenerated with `--write` and re-checked with `--check`; no golden CLI help file exists in the repo, and docgen's generated verb blocks are the only generated CLI surface. No new ` ```revl ` fenced example was added to a doc, so `tests/test_doc_examples.py` is unaffected (it is in the affected selection above and passed).

## Deliberately left out

- **`--report json|tap`** is not implemented. It is a separate shape concern (a machine-readable serialization of the same run) and deserves its own PR; this one keeps the change to selection plus the exit-code contract.
- **Regex filtering** is not implemented; `--filter` is a documented plain substring match.
- **`-v`** was not added: the py tier already prints per-test `PASS <name>` / `FAIL <name>: <msg>` lines, and every tier prints one line per unit, so there is no verbosity to switch on. Adding a flag that changes nothing would be worse than not adding it.
- **Verified-effect round trips** (`roundtrip_units`, docs/verified-effect.md) are keyed by *component*, not by a test name, so they sit outside the selection surface: `--list` does not name them and `--filter` does not select them. They keep running, which is what "selection and reporting only" means for a check with no name to select. This is documented in `collected_tests`' docstring.
- The vendored site/playground wheel is not rebuilt: `tools/check_site_wheel.py` reports it stale because these files are vendored, but CI's wheel rebuild is a main-only workflow and per-PR CI deliberately does not gate it.
