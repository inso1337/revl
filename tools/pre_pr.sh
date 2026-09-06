#!/usr/bin/env bash
# The cheap half of CI, run before you open a PR.
#
# These checks take seconds and are the ones that most often redden the `lint`
# and `frontend` jobs. Running the full suite locally is NOT the point and is
# explicitly not done here: that is CI's job now (docs/process.md). This exists so a
# fifteen-second mistake does not cost a CI round-trip.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0

echo "== ruff =="
uvx ruff@0.16.4 check || fail=1

echo "== python 3.11 syntax sweep =="
# CI runs 3.11; the dev venv is newer, so 3.12+ syntax passes locally and reds CI.
python3 - <<'PY' || fail=1
import ast, pathlib, sys
bad = []
for p in pathlib.Path(".").rglob("*.py"):
    if any(x in p.parts for x in (".git", "node_modules", ".venv", "target", "build")):
        continue
    try:
        ast.parse(p.read_text(encoding="utf-8"), feature_version=(3, 11))
    except SyntaxError as e:
        bad.append(f"{p}:{e.lineno}: {e.msg}")
    except Exception:
        pass
if bad:
    print("\n".join(bad))
    sys.exit(1)
print("py3.11 clean")
PY

# issue #191: every job's EFFECTIVE GITHUB_TOKEN permissions must support the
# actions it invokes. A job-level `permissions` block REPLACES the workflow-level
# one, which is how the publish job ended up unable to check out the tag it
# publishes. The release path never runs on a PR, so this is the only pre-merge
# signal it gets. Needs PyYAML, which the repo does not otherwise depend on, so
# it runs through uv the way ruff above does.
echo "== workflow permissions =="
uv run --no-project --with pyyaml==6.0.2 python3 tools/check_workflow_permissions.py --self-test || fail=1
uv run --no-project --with pyyaml==6.0.2 python3 tools/check_workflow_permissions.py --strict || fail=1

# issue #292: each fail-silent `revl_*` runtime seam is defined exactly once
# with the arity its getattr caller uses. The merge queue closes the whole
# merge-interaction class; this is the static half that catches the one of the
# three defects a linter could -- a cross-module duplicate/incompatible-arity
# definition F811 and the type checker walk past because the read crosses a
# getattr string literal. Stdlib-only, so no uv wrapper needed.
echo "== runtime seams =="
python3 tools/check_runtime_seams.py --self-test || fail=1
python3 tools/check_runtime_seams.py || fail=1

echo "== roadmap markers =="
# --head-branch is the cheap half of the PR-context check: a marker saying work
# is IN FLIGHT on the branch you are about to open the PR from goes stale the
# moment that PR merges, because the merge deletes the branch. Catching it here
# costs nothing, catching it in CI costs a round-trip, and not catching it at
# all reddens main plus every open PR whose merge-ref carries the new text.
# A detached HEAD reports "HEAD", which names no branch and leaves it off.
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ "$branch" = "HEAD" ] && branch=""
python3 tools/check_roadmap_markers.py --check-contradiction --check-delegation --check-duplicate-headers --check-orphan --head-branch "$branch" || fail=1

# The frontend suite runs tests/test_gate_crate_drift.py on every PR, so a
# stale crate is a hard red. crates/revl-gate embeds emitted rust, which means
# ANY emitter change rewrites it. Regenerate with:
#     python3 tools/build_gate_crate.py
echo "== gate crate drift =="
python3 tools/build_gate_crate.py --check || fail=1

# The wasm gate crate is generated from the same sources and drifts for the
# same reasons, but was not checked here, so its drift only ever surfaced in
# CI as tests/test_gate_wasm_drift.py. Three PRs burned a full 13-job run on
# that in one afternoon; both generated crates are checked together now.
echo "== gate wasm crate drift =="
python3 tools/build_gate_wasm.py --check || fail=1

# issue #255: five docs carried source-derived content with no drift check, and
# the one marker-based mechanism rotted back within a day of being introduced.
# Those blocks are generated now and byte-compared here and in the frontend job.
# A stale block: `make docs-gen`. A coverage failure: write the missing section.
echo "== docs drift =="
python3 tools/docgen.py --check || fail=1

# The site wheel is deliberately NOT checked here. It vendors the whole of
# src/revl, so a per-push gate reddened CI on every source change to a bundled
# module (an outage class, not a defect). It is a deploy artifact checked at
# merge and release time, and docs/process.md makes regenerating it the
# orchestrator's job at merge.

if [ "$fail" -ne 0 ]; then
  echo
  echo "pre-PR checks FAILED. Fix these before opening the PR."
  exit 1
fi
echo
echo "pre-PR checks passed. Open the PR."
