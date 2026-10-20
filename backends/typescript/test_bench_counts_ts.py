"""bench/codegen/typescript/results/counts.txt is what the bench prints now.

The file is the checked-in output of `node --expose-gc bench/codegen/typescript/run.mjs`.
Every number in it is a count, not a duration, so it is deterministic and a
test can recompute it: this runs the bench's own driver and compares the
output with the file, except the first line, which names the node version.

It caught a summary and a string-scan table that stayed on the pre-#2313
numbers after the case and the emitter had moved on (issue #2456).

Skips (never passes) when this machine's node cannot run .ts modules or
`cordis` (two bench cases import it) is not installed under
backends/typescript/node_modules.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.test import _node_can_run_emitted, _node_version  # noqa: E402

BENCH = ROOT / "bench" / "codegen" / "typescript"
MODULES = BACKEND / "node_modules"

pytestmark = [
    pytest.mark.skipif(
        not _node_can_run_emitted(_node_version()),
        reason="the bench needs node >= 22.18 or >= 23.6 to run .ts modules",
    ),
    pytest.mark.skipif(
        not (MODULES / "cordis").is_dir(),
        reason="cordis is not installed: run `npm ci` in backends/typescript",
    ),
]


def test_counts_file_matches_the_bench(tmp_path):
    # Run a copy so the symlink that lets `cordis` resolve never lands in the
    # tree; the bench imports `cordis` and re-exports backends/typescript/runtime.ts.
    shutil.copytree(
        BENCH, tmp_path / "bench" / "codegen" / "typescript",
        ignore=shutil.ignore_patterns("results"),
    )
    (tmp_path / "node_modules").symlink_to(MODULES, target_is_directory=True)
    (tmp_path / "backends").symlink_to(ROOT / "backends", target_is_directory=True)
    run = subprocess.run(
        ["node", "--expose-gc",
         str(tmp_path / "bench" / "codegen" / "typescript" / "run.mjs")],
        capture_output=True, text=True, timeout=300, cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    printed = run.stdout.splitlines()
    committed = (BENCH / "results" / "counts.txt").read_text(encoding="utf-8").splitlines()
    assert printed, "the bench printed nothing"
    assert printed[1:] == committed[1:], (
        "bench/codegen/typescript/results/counts.txt is stale: regenerate it "
        "with `node --expose-gc bench/codegen/typescript/run.mjs > "
        "bench/codegen/typescript/results/counts.txt`"
    )
