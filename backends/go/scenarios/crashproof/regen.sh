#!/usr/bin/env bash
# Regenerate the emitted crash-recovery producer from its IR fixture (item 322
# Slice 1). Emitted in RECORD mode (--record), so the witnessed transactional
# step writes a durable discharge-descriptor to the go WAL sink; a non-record
# emission would omit both and is the byte-identical default every other
# scenario uses. Run from anywhere; paths resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
emit="$here/../../emit.py"

python3 "$emit" "$here/crashproof.ir.json" crashrecovery --record \
  | gofmt > "$here/gen_crash_recovery_test.go"

echo "regenerated $here/gen_crash_recovery_test.go"
