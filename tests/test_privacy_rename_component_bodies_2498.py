"""A module-private fn renamed apart is renamed at its call sites in every
body a component or a lifecycle test holds (issues #2498 and #2499).

`_apply_module_privacy` renames two same-named private fns apart as
`fold_records__m0` and `fold_records__m1` when two modules are compiled as one
unit. Its reference rewrite must follow the rename into every body the module
owns. Where it does not, the bare `fold_records` is left behind and resolved as
a component requirement, so the unit is refused with a G1 error naming a
requirement nobody wrote:

    a.rvl:11: `fold_records` is not a declared requirement of MemImpl

The issues' own reproduction (a call inside a `provide` method body) was fixed
by #2462, which walks component bodies. A lifecycle test's `load X with {..}`
config and `call key.op(..)` arguments were still skipped, and refused the same
way once the clashing module joined the unit. These tests compile the issues'
two files as one unit, and run them on the py tier.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl import compile_files

ROOT = Path(__file__).resolve().parents[1]

# the issues' two files, verbatim
A = '''service Mem {
  fn count() -> Int
}

fn fold_records(records: List[Int]) -> List[Int] {
  return records
}

component MemImpl provides mem: Mem {
  provide mem {
    fn count() = fold_records([1, 2]).length()
  }
}
'''

B = '''fn fold_records(fr: Int, text: Str) -> Int {
  return fr + text.length()
}

pub fn use_b() -> Int {
  return fold_records(1, "ab")
}
'''

# A lifecycle test drives the provider, so the run proves count() == 2.
A_RUN = A + '''
lifecycle test "count through the provider" {
  load MemImpl
  let n = call mem.count()
  assert n == 2
  unload MemImpl
  assert no_residue
}
'''

B_RUN = B + '''
test "use_b" {
  assert use_b() == 3
}
'''

# One body kind per case. A component body admits no plain `let` (G6), so
# its other expression slots (effects, emits, approvals) are the #2451 tests'.
ACC = '''service Acc {
  fn add(n: Int) -> Int
}

fn fold_records(records: List[Int]) -> List[Int] {
  return records
}

component AccImpl provides acc: Acc {
  config { base: Int = 0 }
  provide acc {
    fn add(n: Int) = PROVIDE
  }
}

lifecycle test "t" {
  load AccImpl with { base: LOAD }
  let n = call acc.add(CALL)
  unload AccImpl
}
'''

HELPER = "fold_records([7]).length()"
PLAIN = {"PROVIDE": "n", "LOAD": "1", "CALL": "1"}
BODY_KINDS = {
    "provide method body": {"PROVIDE": "n + " + HELPER},
    "lifecycle load config": {"LOAD": HELPER},
    "lifecycle call argument": {"CALL": HELPER},
}


def _compile(tmp_path: Path, a: str, b: str = B) -> dict:
    (tmp_path / "a.rvl").write_text(a, encoding="utf-8")
    (tmp_path / "b.rvl").write_text(b, encoding="utf-8")
    return compile_files([str(tmp_path / "a.rvl"), str(tmp_path / "b.rvl")])


def _fill(slots: dict) -> str:
    source = ACC
    for key, value in {**PLAIN, **slots}.items():
        source = source.replace(key, value)
    return source


def _fn_names(ir: dict) -> set:
    return {fn["name"] for fn in ir["functions"]}


def test_the_issue_s_two_files_compile_as_one_unit(tmp_path):
    ir = _compile(tmp_path, A)
    assert {"fold_records__m0", "fold_records__m1"} <= _fn_names(ir)
    components = json.dumps(ir["components"])
    assert '"fold_records__m0"' in components
    assert '"fold_records"' not in components


@pytest.mark.parametrize("kind", sorted(BODY_KINDS))
def test_every_body_kind_calls_its_own_module_s_helper(tmp_path, kind):
    ir = _compile(tmp_path, _fill(BODY_KINDS[kind]))
    wire = json.dumps({"components": ir["components"], "tests": ir["tests"]})
    assert '"fold_records__m0"' in wire
    assert '"fold_records"' not in wire


def test_control_no_clash_keeps_the_bare_name(tmp_path):
    ir = _compile(tmp_path, _fill(BODY_KINDS["lifecycle call argument"]),
                  B.replace("fold_records", "other_records"))
    assert "fold_records" in _fn_names(ir)
    assert '"fold_records__m0"' not in json.dumps(ir)


def _cordis_python() -> str | None:
    if importlib.util.find_spec("cordis") is not None:
        return sys.executable
    venv = ROOT / "backends" / "python" / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else None


@pytest.mark.skipif(_cordis_python() is None,
                    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
@pytest.mark.parametrize("a", ["issue", "lifecycle call argument"])
def test_the_unit_runs_on_the_py_tier(tmp_path, a):
    source = A_RUN if a == "issue" else _fill(BODY_KINDS[a]).replace(
        "  unload AccImpl", "  assert n == 1\n  unload AccImpl")
    (tmp_path / "a.rvl").write_text(source, encoding="utf-8")
    (tmp_path / "b.rvl").write_text(B_RUN, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [_cordis_python(), "-m", "revl", "test", "--backend", "py",
         str(tmp_path / "a.rvl"), str(tmp_path / "b.rvl")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS use_b" in result.stdout
    assert "[py] pass: 2 test(s) passed" in result.stdout
