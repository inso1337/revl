"""Capability, not presence: can the PATH's node actually run the `.ts` files
these gates emit?

`shutil.which("node")` answers whether a node binary exists. On a host whose
node predates type stripping that binary exists and then dies on the first
`.ts` file with `ERR_UNKNOWN_FILE_EXTENSION`, which reads as a portability
failure of the tier under test rather than a toolchain version. The four gates
in this change ask the capability question instead.

The probe is a real two-line `.ts` file, never `node -e`: the inline form is
CommonJS and never reaches the type-stripping path, so it answers "fine" on
every node that has ever shipped. The gate passes only when the probe exits 0
AND prints the expected value; anything else (a non-zero exit, a different
stdout, a node that cannot be executed at all) is reported as a skip whose
reason names the version found, or the node path when even that could not be
read, so the reader knows what to install.

One helper for the gates that emit ts and then run it: each passes its own
no-node message, so the original skip text is preserved where no node exists.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

#: Two lines: a type annotation the stripper must remove, then the value the
#: gate compares. Kept free of imports so the probe needs no project files.
PROBE_SOURCE = "const n: number = 41\nconsole.log(n + 1)\n"

#: The only stdout that proves stripping happened AND execution followed it.
PROBE_STDOUT = "42"

#: Where the capability came from, named in the skip so the fix is concrete.
TYPE_STRIPPING = "type stripping is on by default from 22.18 and 23.6"


def node_ts_skip_reason(
    absent_reason: str,
    *,
    node: "str | None" = None,
    which=shutil.which,
    runner=subprocess.run,
) -> "str | None":
    """The reason the PATH's node cannot run the guarded ts, or None if it can.

    `absent_reason` is the calling gate's existing message for the no-node case;
    it is returned verbatim when no node is found so those gates keep their
    original skip text. When node exists but cannot run `.ts`, the reason names
    the version it found and the type-stripping requirement. A node that cannot
    be executed at all is named by path or by the version already read, never
    reported as absent. `node`, `which` and `runner` are injectable so the
    decision can be unit-tested without depending on the host's toolchain.
    """
    if node is None:
        node = which("node")
        if node is None:
            return absent_reason
    version = ""
    try:
        version = runner(
            [node, "--version"], capture_output=True, text=True,
            timeout=30).stdout.strip()
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.ts"
            probe.write_text(PROBE_SOURCE, encoding="utf-8")
            run = runner(
                [node, str(probe)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        if version:
            return (f"node {version} found, but could not be probed for .ts "
                    f"support ({TYPE_STRIPPING})")
        return (f"node at {node} could not be probed for .ts support "
                f"({TYPE_STRIPPING})")
    if run.returncode == 0 and run.stdout.strip() == PROBE_STDOUT:
        return None
    return (f"node {version or 'unknown'} found, needs one that runs .ts "
            f"directly ({TYPE_STRIPPING})")
