#!/usr/bin/env bash
# The cheap half of CI, run before you open a PR.
#
# These three checks take seconds and are the ones that most often redden the
# `lint` job. Running the full suite locally is NOT the point and is explicitly
# not done here: that is CI's job now (docs/process.md). This exists so a
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

echo "== roadmap markers =="
python3 tools/check_roadmap_markers.py --check-contradiction || fail=1

if [ "$fail" -ne 0 ]; then
  echo
  echo "pre-PR checks FAILED. Fix these before opening the PR."
  exit 1
fi
echo
echo "pre-PR checks passed. Open the PR."
