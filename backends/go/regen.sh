#!/usr/bin/env bash
# Regenerate the checked-in emitted Go from the source IR fixtures, then gofmt.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

python3 "$here/emit.py" "$root/examples/user_cache.ir.json" usercache \
  > "$here/scenarios/emitted/usercache/gen.go"
python3 "$here/emit.py" "$root/backends/typescript/tests/fixtures/tenants.ir.json" tenants \
  > "$here/scenarios/emitted/tenants/gen.go"

if command -v gofmt >/dev/null 2>&1; then
  gofmt -w "$here/scenarios/emitted/usercache/gen.go" "$here/scenarios/emitted/tenants/gen.go"
fi
echo "regenerated emitted Go under $here/scenarios/emitted/"
