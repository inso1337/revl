#!/usr/bin/env bash
# Regenerate the checked-in emitted Go from the source IR fixtures, then gofmt.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# --- ir_version 1/2 component scenarios (stc-go runtime) ------------------
python3 "$here/emit.py" "$root/examples/user_cache.ir.json" usercache \
  > "$here/scenarios/emitted/usercache/gen.go"
python3 "$here/emit.py" "$root/backends/typescript/tests/fixtures/tenants.ir.json" tenants \
  > "$here/scenarios/emitted/tenants/gen.go"

# --- ir_version 3 pure/typed-core fixtures (ordinary Go, no stc runtime) ---
# The v3_tests fixture carries `test` blocks that become real Go tests, so it
# is emitted straight into a *_test.go file. The other two are libraries the
# hand-written *_exec_test.go harnesses call with computed assertions.
fx="$root/backends/typescript/tests/fixtures"
python3 "$here/emit.py" "$fx/v3_tests.ir.json"            tests \
  > "$here/v3/tests/gen_test.go"
python3 "$here/emit.py" "$fx/v3_types_functions.ir.json"  types_functions \
  > "$here/v3/types_functions/gen.go"
python3 "$here/emit.py" "$fx/v3_stdlib.ir.json"           stdlib \
  > "$here/v3/stdlib/gen.go"

if command -v gofmt >/dev/null 2>&1; then
  gofmt -w "$here/scenarios/emitted/usercache/gen.go" \
           "$here/scenarios/emitted/tenants/gen.go" \
           "$here/v3/tests/gen_test.go" \
           "$here/v3/types_functions/gen.go" \
           "$here/v3/stdlib/gen.go"
fi
echo "regenerated emitted Go under $here/scenarios/emitted/ and $here/v3/"
