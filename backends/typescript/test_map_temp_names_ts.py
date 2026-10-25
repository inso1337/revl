"""Issue #2900: the Map `set` and `remove` helpers must not capture user names.

The helpers used to emit `(() => { const c = new Map(<target>); ... })()`, so a
user Map called `c` was read inside its own `const c` initialiser and threw a
ReferenceError at run time. The operands are now arguments of the call, so no
user expression sits inside the closure whose temporaries are `_m`, `_k`, `_v`,
`_c`, `K` and `V`. Each of those names, and `c`, is used here as a user Map
variable that is set and removed, and the result is checked under node.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from _node_capability import node_ts_skip_reason  # noqa: E402

_NODE_REASON = node_ts_skip_reason("node is required to run the emitted ts")
needs_node = pytest.mark.skipif(_NODE_REASON is not None, reason=str(_NODE_REASON))

# `c` is the name from the issue; the rest are every other name the helpers
# introduce (value parameters, the copy, and the two type parameters).
NAMES = ["c", "_c", "_m", "_k", "_v", "K", "V"]


def _emit(src: str, runtime_import: str = "./runtime.ts") -> str:
    spec = importlib.util.spec_from_file_location("revl_ts_emit_mt", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(src), runtime_import=runtime_import)


def _program(name: str) -> str:
    return (
        f"fn probe({name}: Map[Str, Int], k: Str) -> Int {{\n"
        f"  let a = {name}.set(k, 7)\n"
        f"  let b = a.remove(\"x\")\n"
        f"  let d = {name}.set(\"x\", {name}.size())\n"
        f"  let e = {name}.remove(\"y\")\n"
        f"  return b.size() * 1000 + d.size() * 100 + e.size() * 10 + {name}.size()\n"
        f"}}\n"
    )


@pytest.mark.parametrize("name", NAMES)
def test_emitted_helpers_do_not_read_a_user_name_inside_the_closure(name):
    out = _emit(_program(name))
    assert "const c = new Map" not in out
    assert "(() => { const" not in out


@needs_node
@pytest.mark.parametrize("name", NAMES)
def test_map_named_like_a_helper_temporary_runs(name, tmp_path):
    mod = tmp_path / "m.ts"
    mod.write_text(_emit(_program(name), str(BACKEND / "runtime.ts")), encoding="utf-8")
    harness = tmp_path / "h.ts"
    harness.write_text(
        "import { probe } from './m.ts'\n"
        "const base = new Map<string, bigint>([['x', 1n], ['y', 2n]])\n"
        "process.stdout.write(String(probe(base, 'z')) + ' ' + base.size)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(harness)], capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    # a = {x,y,z}; b = {y,z} (2); d = {x,y} (2); e = {x} (1); base untouched (2)
    assert proc.stdout == "2212 2"


@needs_node
def test_remove_alone_on_a_map_named_c_runs(tmp_path):
    """`remove` is exercised with no `set` on `c`, so it is covered by its own run."""
    src = ("fn probe(c: Map[Str, Int]) -> Int {\n"
           "  let e = c.remove(\"y\")\n"
           "  return e.size() * 10 + c.size()\n"
           "}\n")
    mod = tmp_path / "m.ts"
    mod.write_text(_emit(src, str(BACKEND / "runtime.ts")), encoding="utf-8")
    harness = tmp_path / "h.ts"
    harness.write_text(
        "import { probe } from './m.ts'\n"
        "const base = new Map<string, bigint>([['x', 1n], ['y', 2n]])\n"
        "process.stdout.write(String(probe(base)))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(harness)], capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "12"
