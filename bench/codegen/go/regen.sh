#!/usr/bin/env bash
# Recompile the bench programs and re-emit their Go. Run after any change to
# backends/go/emit.py, then re-run the A/B suite:
#
#   sh bench/codegen/go/regen.sh && (cd bench/codegen/go && go test ./ab/ -bench . -benchmem -run XXX)
#
# PYTHON may be pointed at whichever interpreter has the frontend importable;
# the default assumes the repo's own .venv.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../../.." && pwd)"
PYTHON="${PYTHON:-$root/.venv/bin/python}"

export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"

for probe in probe probe2; do
  "$PYTHON" -m revl compile "$here/$probe.rvl" -o "$here/$probe.ir.json"
done

"$PYTHON" "$root/backends/go/emit.py" "$here/probe.ir.json"  loops  > "$here/emitted/loops/gen.go"
"$PYTHON" "$root/backends/go/emit.py" "$here/probe2.ir.json" values > "$here/emitted/values/gen.go"

if command -v gofmt >/dev/null 2>&1; then
  gofmt -w "$here/emitted/loops/gen.go" "$here/emitted/values/gen.go"
fi
echo "regenerated $here/emitted/{loops,values}/gen.go"
