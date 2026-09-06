"""Follow-up to #460: `stdlib/shell.rvl`'s `plan_op_name(plan, idx)` accessor.

#460 landed `classify` + `plan_verdict` / `plan_is_witnessed` / `plan_reason` /
`plan_op_count`, but no reader for the op NAME each witnessed op lowered from
(`ops[idx]["op"]`). The harness's terminal toolbox renders `witnessed[mkdir]`
from that string; without an accessor the best a consumer could say was
`witnessed[1 op]`. This suite pins the new accessor on BOTH tiers the harness
uses (py, and — with node — ts), matching the sibling accessors' shape: a total,
pure `Str` reader that yields "" for an out-of-range index or an emission plan.

The harness mirror is the same one #460 pins: a user-origin consumer that reaches
`classify` through the sanctioned `use "stdlib/shell.rvl"` door.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from revl.compiler import compile_files

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _backend_import import backend_emitter  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PY = _ROOT / "backends" / "python"
_BACKEND_TS = _ROOT / "backends" / "typescript"
# The emitted py `classify` body imports `revl_shell_classify` from
# `backends/python` (the runtime's sys.path analog of the stdlib-ref root).
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(
    not _HAS_NODE, reason="node is required to run the emitted ts consumer")

# A user-origin consumer that reads the op name at an index off a classify plan,
# exactly as the harness's terminal toolbox does to render `witnessed[<op>]`.
_CONSUMER = """use "stdlib/shell.rvl" { classify, plan_op_name }

pub fn op_name_at(cmd: Str, idx: Int) -> Str {
  return plan_op_name(classify(cmd), idx)
}
"""

#: (cmd, idx, expected op name). Witnessed plans expose their op names; an
#: out-of-range index and an emission plan both yield "".
_EXPECTED = [
    ["mkdir d", 0, "mkdir"],       # single witnessed op -> its name
    ["mv a b", 0, "mv"],
    ["rm a b c", 0, "rm"],         # rm a b c -> 3 ops
    ["rm a b c", 2, "rm"],         # the last op, by index
    ["rm a b c", 3, ""],           # past the end -> ""
    ["mkdir d", -1, ""],           # negative index -> ""
    ["echo hi | cat", 0, ""],      # emission plan (no ops) -> ""
]


def _write_consumer(tmp_path: Path) -> Path:
    proj = tmp_path / "consumer"
    proj.mkdir()
    app = proj / "app.rvl"
    app.write_text(_CONSUMER, encoding="utf-8")
    return app


def _py_answers(app: Path) -> list[list]:
    src = backend_emitter("python").emit(compile_files([str(app)]))
    mod = types.ModuleType("revl_plan_op_name_gen")
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(src, "<plan_op_name artifact>", "exec"), mod.__dict__)
        return [[cmd, idx, mod.op_name_at(cmd, idx)] for cmd, idx, _ in _EXPECTED]
    finally:
        sys.modules.pop("revl_plan_op_name_gen", None)


def _ts_answers(app: Path) -> list[list]:
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "plan_op_name_consumer.ts"
    module.write_text(
        backend_emitter("typescript").emit(compile_files([str(app)]),
                                           runtime_import="../../runtime.ts"),
        encoding="utf-8")
    harness = generated / "_plan_op_name_harness.ts"
    harness.write_text(
        ";(globalThis as any).__REVL_STDLIB_REF_ROOT__ = process.argv[2]\n"
        "const calls = JSON.parse(process.argv[3]) as [string, number][]\n"
        "const mod = await import('./plan_op_name_consumer.ts')\n"
        "process.stdout.write(JSON.stringify(\n"
        "  calls.map(([cmd, idx]) => [cmd, idx, mod.op_name_at(cmd, BigInt(idx))])))\n",
        encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(harness), str(_ROOT),
             json.dumps([[cmd, idx] for cmd, idx, _ in _EXPECTED])],
            capture_output=True, text=True, cwd=str(_BACKEND_TS),
            env=dict(os.environ))
    finally:
        module.unlink(missing_ok=True)
        harness.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"ts consumer harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_plan_op_name_on_py(tmp_path):
    app = _write_consumer(tmp_path)
    assert _py_answers(app) == _EXPECTED


@_needs_node
def test_plan_op_name_on_ts(tmp_path):
    app = _write_consumer(tmp_path)
    assert _ts_answers(app) == _EXPECTED
