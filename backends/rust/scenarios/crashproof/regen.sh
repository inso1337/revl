#!/usr/bin/env bash
# Regenerate the emitted crash-recovery producer lib from its IR fixture (item
# 322 Slice 2). Emitted in RECORD mode (--record), so the witnessed
# transactional step writes a durable discharge-descriptor to the rust WAL sink;
# a non-record emission would omit both and is the byte-identical default every
# other scenario uses. Run from anywhere; paths resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
emit="$here/../../emit.py"
root="$here/../../../.."

PYTHONPATH="$root/src" python3 "$emit" "$here/crashproof.ir.json" --record \
  > "$here/src/lib.rs"

echo "regenerated $here/src/lib.rs"
