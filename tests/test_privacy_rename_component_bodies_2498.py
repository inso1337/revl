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


# --- every expression slot the rewrite walks (review of #2498) --------------
#
# Each snippet puts the module's private helper in one slot. After
# `_rewrite_module` renames it, a generic walk over EVERY dataclass field of
# the parsed program must find no bare `fold_records` left. The walk follows
# fields by reflection, not by node kind, so a slot listed in SLOTS fails here
# even if the rewrite skips a node kind inside it. A new node kind is covered
# only once a snippet for it is added to SLOTS. A `merge(..)`
# operand is a stream name (a call there does not parse), so it has no slot.

from revl import compiler as _compiler
from revl import parser as _parser

SLOT_HEAD = '''service Mem { fn go(x: Int) -> Int }
service Vault { endorse[confidential] fn lookup(name: Str) -> Result[Str, Str] }
fn fold_records(records: List[Int]) -> List[Int] { return records }
'''

SLOTS = {
    "emit expression": '''component W provides mem: Mem {
  provide mem { fn go(x) { let a = emit send_mail(fold_records([x]).length()) return a } }
}''',
    "endorse expression": '''component K provides vault: Vault {
  provide vault { fn lookup(name) = Err(endorse[confidential](fold_records([1]), reason = "r")) }
}''',
    "spawn config": '''component S {
  let w = effect spawn Child with { n: fold_records([1]).length() } undo w.dispose()
}''',
    "subscribe stage": '''component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map(x => fold_records([x]).length()) undo sub.close()
}''',
    "subscribe replay cursor": '''component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src replay(from: fold_records([1])) undo sub.close()
}''',
    "effect replay cursor": '''component C {
  let src = effect Stream.source() replay(from: fold_records([1])) undo src.close()
}''',
    "requires replay cursor": '''component C requires s: Stream[Int] replay(from: fold_records([1])) {
  let sub = subscribe s undo sub.close()
}''',
    "fn parameter default": '''fn g(n: Int = fold_records([1]).length()) -> Int { return n }''',
}


def _bare_refs(node, seen=None) -> int:
    seen = set() if seen is None else seen
    if id(node) in seen:
        return 0
    seen.add(id(node))
    if isinstance(node, _parser.ExprVar):
        return int(node.name == "fold_records")
    if isinstance(node, (list, tuple, set)):
        return sum(_bare_refs(n, seen) for n in node)
    if isinstance(node, dict):
        return sum(_bare_refs(n, seen) for n in node.values())
    if hasattr(node, "__dataclass_fields__"):
        return sum(_bare_refs(getattr(node, f), seen)
                   for f in node.__dataclass_fields__)
    return 0


def _parse(source: str):
    return _parser.Parser(source, "a.rvl").parse()


@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_every_expression_slot_is_rewritten(slot):
    program = _parse(SLOT_HEAD + SLOTS[slot] + "\n")
    assert _bare_refs(program) == 1   # the slot's call, before the rewrite
    _compiler._rewrite_module(program, {"fold_records": "fold_records__m0"}, {})
    assert _bare_refs(program) == 0


def test_the_review_s_emit_case_compiles_as_one_unit(tmp_path):
    a = '''service Mail { emission[mail] fn go(x: Int) -> Int }

extern emission[mail] fn send_mail(n: Int) -> Int = @py { return n }

fn fold_records(records: List[Int]) -> List[Int] {
  return records
}

component Writer provides mail: Mail {
  provide mail { fn go(x) { let a = emit send_mail(fold_records([x]).length()) return a } }
}
'''
    ir = _compile(tmp_path, a)
    assert '"fold_records__m0"' in json.dumps(ir["components"])


def test_a_parameter_default_calls_its_own_module_s_helper(tmp_path):
    a = '''fn fold_records(records: List[Int]) -> List[Int] {
  return records
}

pub fn g(n: Int = fold_records([1, 2]).length()) -> Int {
  return n
}

test "default" {
  assert g() == 2
}
'''
    ir = _compile(tmp_path, a)
    assert '"fold_records__m0"' in json.dumps(ir["tests"])
