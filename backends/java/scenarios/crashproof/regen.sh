#!/usr/bin/env bash
# Regenerate the emitted crash-recovery producer module from its IR fixture (item
# 322 Slice 2). Emitted in RECORD mode (--record), so the witnessed transactional
# step writes a durable discharge-descriptor to the java WAL sink (fsync via
# FileChannel.force); a non-record emission would omit both and is the byte-
# identical default every other scenario uses. The package is `revl` so the
# hand-written CrashProducer can reference `revl.Components`. Run from anywhere;
# paths resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
emit="$here/../../emit.py"

python3 "$emit" "$here/crashproof.ir.json" revl --record > "$here/revl/Components.java"

echo "regenerated $here/revl/Components.java"
