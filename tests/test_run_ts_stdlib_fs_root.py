"""`revl test --backend ts` must stamp the item-410 stdlib host-ref root.

Regression coverage for a class of bug in `src/revl/test.py::run_ts`: it ran
`vitest run <generated test>` without ever setting
`globalThis.__REVL_STDLIB_REF_ROOT__` — the install-tree root a stdlib-origin
`@ts ref` thunk resolves against, lazily, at its first call
(backends/typescript/emit.py's `_revl_ref_path_stdlib`, the item-410 two-root
scheme). Every *other* ts runner sets this global: the node placement runner
self-derives it two directories up from its own location
(backends/typescript/placement_runner.ts), and `revl run --backend ts` passes
it explicitly (src/revl/run_ts.py's `stdlibRefRoot: str(stdlib_root().parent)`
in its spec). `revl test --backend ts` alone left it unset, so `write(...)`
threw "revl: no stdlib host-ref root set; the runner must set
globalThis.__REVL_STDLIB_REF_ROOT__ (item 410 two-root scheme)" for EVERY
`revl test --backend ts` composition that consumes `stdlib/fs.rvl` (directly,
or transitively through stdlib/shell.rvl's cp/touch/rm/mkdir/mv lowering).

revl's own suite stayed green through this because its only ts proof of the
fs catalog, backends/typescript/tests/ts_witnessed_fs.test.ts, sets the global
BY HAND before importing the compiled module — masking exactly this bug. This
suite instead drives `revl.test.run_ts` (the function `revl test --backend
ts` calls) directly, over a REAL `use "stdlib/fs.rvl" { write }` consumer
resolved through the item-319 stdlib search path — the same way a consumer
outside the revl checkout resolves it (tests/test_stdlib_search_path.py) — so
its `write` ref is stamped `"root": "stdlib"` and takes exactly the code path
the bug broke. It asserts both the vitest verdict AND the file mutation
actually landing on disk, so a fix that only silences the vitest exit code
without the write really happening would not pass either.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.test import run_ts  # noqa: E402

needs_vitest = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists(),
    reason="vitest not installed in backends/typescript")

#: A minimal consumer over the REAL stdlib/fs.rvl: one witnessed write, loaded
#: and cleanly unloaded (an implicit commit) inside a `lifecycle test` — the
#: same load/unload/no_residue shape tests/test_lifecycle_cross_tier.py
#: already proves runs for real on the ts tier for a synthetic (non-stdlib)
#: coeffect. The only thing new here is the `@ts ref` STDLIB origin, which is
#: exactly what the bug broke.
CONSUMER = """\
use "stdlib/fs.rvl" { write }

component WriteOnce {
  effect write("out.txt", "v1")
}

lifecycle test "fs write persists on clean unload" {
  load WriteOnce
  unload WriteOnce
  assert no_residue
}
"""


@pytest.fixture
def fs_ir(tmp_path):
    # Deliberately OUTSIDE the revl checkout (tmp_path), like
    # test_stdlib_search_path.py's "a consumer outside the stdlib tree" case:
    # `use "stdlib/fs.rvl"` cannot resolve relative to this file, so it falls
    # back to the item-319 search path and lands on the real, installed
    # stdlib/fs.rvl — recorded as install ("stdlib") origin, which is what
    # stamps the `write` ref `"root": "stdlib"` and routes its call through
    # `_revl_ref_path_stdlib` on the ts tier (item 410).
    main = tmp_path / "consumer.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("REVL_FS_WORKSPACE", str(root))
    return root


def test_write_ref_is_stamped_stdlib_origin(fs_ir):
    """Sanity: the fixture exercises the item-410 STDLIB ref path this bug is
    about (`__REVL_STDLIB_REF_ROOT__`), not the user-tree `__REVL_REF_ROOT__`
    path (item 396) that a same-file/user-module ref would take instead."""
    ext = next(e for e in fs_ir["externs"] if e["name"] == "write")
    assert ext["refs"]["ts"]["root"] == "stdlib"


@needs_vitest
def test_ts_tier_runs_a_real_stdlib_fs_write_end_to_end(fs_ir, workspace):
    """The real `revl test --backend ts` path (`revl.test.run_ts`) over a
    `stdlib/fs.rvl` consumer must PASS — and the write must have actually
    landed on disk, not just a green vitest exit code."""
    verdict, detail = run_ts(fs_ir)
    assert verdict == "pass", detail
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "v1"
