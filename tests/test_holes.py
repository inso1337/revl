"""Typed holes (docs/holes.md).

A hole is a placeholder that has a type, satisfies the checker, and is
recorded as an unmet obligation. The four things that must stay true:

  1. it checks as its expected type, and the code *around* it still produces
     real diagnostics — that is the whole point;
  2. every hole is reported (file, line, expected type, message), and
     compilation still succeeds, because a draft is not a failure;
  3. admission refuses it — a hole may never enter a running composition;
  4. every backend refuses to emit it, in its own words, before producing a
     single character.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.diagnostics import classify  # noqa: E402

TIERS = ("python", "typescript", "rust", "java", "wasm")

CACHE = "service Cache { fn get(key: Str) -> Str }\n"

# Line numbers are asserted throughout, so these are spelled out a line at a
# time rather than composed from triple-quoted fragments: the hole is on line 3.
DRAFT = (
    "service Cache { fn get(key: Str) -> Str }\n"                  # 1
    "component C provides c: Cache {\n"                            # 2
    '  provide c { fn get(key) = hole "look up in the store" }\n'   # 3
    "}\n"                                                          # 4
)

FINISHED = (
    "service Cache { fn get(key: Str) -> Str }\n"
    "component C provides c: Cache {\n"
    "  provide c { fn get(key) = key }\n"
    "}\n"
)

# The backend matrix uses an Int-shaped service so that the only thing any
# tier can refuse is the hole, and a refusal test fails for the reason it
# names. This is NOT a wasm-tier limit: that tier lowers `Str`, `Bytes`,
# lists, records, variants, `Opt` and `Result` across the service boundary
# as canonical-ABI pointers, and refuses only `Float`, `Map` and function
# types (`backends/wasm/emit.py`, `_V3Emitter._check_type`).
INT_DRAFT = (
    "service Counter { fn bump(n: Int) -> Int }\n"                 # 1
    "component K provides k: Counter {\n"                          # 2
    '  provide k { fn bump(n) = hole "the increment policy" }\n'    # 3
    "}\n"                                                          # 4
)

INT_FINISHED = (
    "service Counter { fn bump(n: Int) -> Int }\n"
    "component K provides k: Counter {\n"
    "  provide k { fn bump(n) = n }\n"
    "}\n"
)


def _err(source: str, **kwargs) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, **kwargs)
    return str(excinfo.value)


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"holes_{tier}_emit", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- 1. checking -----------------------------------------------------------

def test_hole_takes_the_declared_return_type():
    ir = compile_source('fn f() -> Int { return hole "the score" }')
    node = ir["functions"][0]["body"][0]["expr"]
    assert node == {"kind": "hole", "type": "Int", "file": "<string>",
                    "line": 1, "message": "the score"}


def test_hole_takes_the_service_return_type_in_a_provide_method():
    # A6: the service declaration is the source of truth for the signature,
    # so it is also the source of the obligation
    ir = compile_source(DRAFT)
    assert ir["holes"] == [{"file": "<string>", "line": 3, "type": "Str",
                            "message": "look up in the store"}]


def test_hole_takes_a_declared_parameter_type_in_argument_position():
    ir = compile_source("fn g(n: Int) -> Int { return n }\n"
                        'fn f() -> Int { return g(hole "how many") }')
    assert ir["holes"] == [{"file": "<string>", "line": 2, "type": "Int",
                            "message": "how many"}]


def test_hole_takes_bool_in_a_condition():
    ir = compile_source('fn f() -> Int { if (hole "is it ready") { return 1 }\n'
                        "  return 2 }")
    assert [h["type"] for h in ir["holes"]] == ["Bool"]


def test_the_message_is_optional():
    ir = compile_source("fn f() -> Int { return hole }")
    assert ir["holes"] == [{"file": "<string>", "line": 1, "type": "Int",
                            "message": None}]


def test_annotated_hole_needs_no_context():
    ir = compile_source('fn f() -> Int { let x = hole[Str] "a name"\n  return 1 }')
    assert [h["type"] for h in ir["holes"]] == ["Str"]


def test_hole_without_a_type_or_a_context_is_refused():
    # inventing a type would hand the author an obligation the compiler made
    # up — the exact noise holes exist to remove
    err = _err('fn f() -> Int { let x = hole "todo"\n  return 1 }')
    assert "has no expected type" in err
    assert "hole[Str]" in err  # the hint shows the annotated form


def test_annotated_hole_is_still_checked_against_its_context():
    err = _err('fn f() -> Int { return hole[Str] "todo" }')
    assert "expects `Int`, got `Str`" in err


def test_builtin_generic_arity_is_checked_in_a_hole_annotation():
    err = _err('fn f() -> Int { let x = hole[List[Int, Str]] "todo"\n  return 1 }')
    assert "`List` takes 1 type argument(s), got 2" in err


def test_surrounding_code_still_produces_real_diagnostics():
    # the point of a hole: the rest of the body is checked normally
    err = _err("type R = { id: Int }\n"
               'fn f(r: R) -> Int { let n = hole[Int] "count"\n  return r.nope }')
    assert "`R` has no field `nope`" in err


def test_a_hole_does_not_suppress_the_null_rule():
    err = _err('fn f() -> Int { let n = hole[Int] "count"\n  return null }')
    assert "`null` has no type" in err


def test_a_holes_type_flows_into_the_rest_of_the_body():
    err = _err("fn g(n: Int) -> Int { return n }\n"
               'fn f() -> Int { let s = hole[Str] "a name"\n  return g(s) }')
    assert "argument 1 of `g(...)` expects `Int`, got `Str`" in err


def test_hole_in_a_ternary_is_not_ambiguous_with_the_else_branch():
    # the reason the annotation is `hole[T]` and not `hole : T` (docs/holes.md §1)
    ir = compile_source('fn f(c: Bool) -> Int { return c ? hole "yes" : 2 }')
    assert [h["type"] for h in ir["holes"]] == ["Int"]


def test_hole_in_a_component_effect_and_undo_position():
    ir = compile_source(
        "service Cache { fn get(key: Str) -> Str }\n"                          # 1
        "component C provides c: Cache {\n"                                    # 2
        '  let m = effect hole[Int] "the pool" undo hole[Int] "release it"\n'   # 3
        "  provide c { fn get(key) = key }\n"                                  # 4
        "}\n")
    assert [(h["line"], h["message"]) for h in ir["holes"]] == [
        (3, "the pool"), (3, "release it")]


def test_emit_on_a_hole_is_refused():
    # `emit` marks a call to a declared `emission` operation; a hole is not one
    err = _err("service S { emission fn f(x: Int) -> Int }\n"
               "component C provides s: S {\n"
               '  provide s { fn f(x) = emit hole[Int] "x" }\n}')
    assert "not declared `emission`" in err


def test_hole_is_a_reserved_word():
    err = _err("type R = { hole: Int }")
    assert "expected ident, found 'hole'" in err


# ---- 2. obligation reporting -----------------------------------------------

def test_a_draft_with_holes_compiles():
    ir = compile_source(DRAFT)
    assert ir["manifest"]["loadOrder"] == ["C"]  # a real, linked composition


def test_finished_code_carries_no_holes_key():
    # an IR document for finished code is byte-identical to what it was
    # before holes existed
    assert "holes" not in compile_source(FINISHED)


def test_obligations_are_sorted_and_complete():
    ir = compile_source(
        "fn a() -> Int { return hole \"second\" }\n"
        "fn b() -> Str { return hole \"third\" }\n")
    assert [h["line"] for h in ir["holes"]] == [1, 2]
    assert {h["type"] for h in ir["holes"]} == {"Int", "Str"}


def test_holes_name_the_file_they_were_written_in(tmp_path):
    lib = tmp_path / "lib.rvl"
    lib.write_text('pub fn helper() -> Int { return hole "the helper" }\n')
    root = tmp_path / "main.rvl"
    root.write_text('use "./lib.rvl" { helper }\n'
                    "fn main() -> Int { return helper() }\n")
    ir = compile_files([str(root)])
    assert len(ir["holes"]) == 1
    assert ir["holes"][0]["file"].endswith("lib.rvl")


def test_cli_reports_holes_on_stderr_and_exits_zero(tmp_path):
    draft = tmp_path / "draft.rvl"
    draft.write_text(DRAFT)
    result = subprocess.run(
        [sys.executable, "-m", "revl", "compile", str(draft)],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert result.returncode == 0
    assert "1 open hole" in result.stderr
    assert "expects `Str`" in result.stderr
    assert 'look up in the store' in result.stderr
    # stdout stays exactly the IR document
    assert json.loads(result.stdout)["holes"][0]["type"] == "Str"


def test_cli_json_diagnostics_includes_holes(tmp_path):
    draft = tmp_path / "draft.rvl"
    draft.write_text(DRAFT)
    result = subprocess.run(
        [sys.executable, "-m", "revl", "compile", str(draft), "--json-diagnostics"],
        capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert result.returncode == 0
    payload = json.loads(result.stderr)
    assert payload["ok"] is True
    (hole,) = payload["holes"]
    assert hole["severity"] == "obligation"
    assert hole["code"] == "T3"
    assert hole["expected"] == "Str"
    assert hole["message"] == "look up in the store"
    assert "obligation" in hole["guarantee"]


# ---- 3. admission ----------------------------------------------------------

def test_admission_refuses_a_candidate_with_a_hole():
    running = compile_source(FINISHED)
    err = _err(DRAFT, filename="cand.rvl", manifest=running)
    assert "admission refused" in err
    assert "1 typed hole" in err
    assert "may never enter a running composition" in err


def test_admission_refusal_is_a_structured_diagnostic():
    running = compile_source(FINISHED)
    with pytest.raises(RevlError) as excinfo:
        compile_source(DRAFT, "cand.rvl", manifest=running)
    record = classify(excinfo.value)
    assert record["code"] == "T3"
    assert record["category"] == "admission"
    assert record["line"] == 3


def test_admission_accepts_the_same_candidate_once_the_hole_is_filled():
    running = compile_source(FINISHED)
    ir = compile_source(FINISHED, "cand.rvl", manifest=running)
    assert ir["manifest"]["loadOrder"] == ["C"]


def test_a_hole_elsewhere_in_the_composition_also_blocks_admission():
    # not only holes in components: an unfinished pure fn is just as unrunnable
    running = compile_source(FINISHED)
    err = _err(FINISHED + '\nfn helper() -> Int { return hole "later" }',
               filename="cand.rvl", manifest=running)
    assert "admission refused" in err


def test_admission_gate_over_files(tmp_path):
    draft = tmp_path / "draft.rvl"
    draft.write_text(DRAFT)
    running = compile_source(FINISHED)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(draft)], manifest=running)
    assert "admission refused" in str(excinfo.value)


# ---- 4. backends -----------------------------------------------------------

@pytest.mark.parametrize("tier", TIERS)
def test_every_backend_refuses_to_emit_a_hole(tier):
    module = _emitter(tier)
    kwargs = {"package_name": "holes_case"} if tier == "java" else {}
    with pytest.raises(module.EmitError) as excinfo:
        module.emit(compile_source(INT_DRAFT), **kwargs)
    message = str(excinfo.value)
    assert "refusing to emit" in message
    assert "typed hole" in message
    # the refusal locates the obligation rather than merely naming the node
    assert "<string>:3 (expects `Int`)" in message
    assert "docs/holes.md" in message


@pytest.mark.parametrize("tier", TIERS)
def test_every_backend_still_emits_the_finished_component(tier):
    # the same source with the hole filled: the refusal above is about the
    # hole and nothing else
    module = _emitter(tier)
    kwargs = {"package_name": "holes_case"} if tier == "java" else {}
    assert module.emit(compile_source(INT_FINISHED), **kwargs)


def test_a_hole_in_a_pure_fn_also_blocks_emission():
    module = _emitter("python")
    ir = compile_source('pub fn f() -> Int { return hole "later" }')
    with pytest.raises(module.EmitError) as excinfo:
        module.emit(ir)
    assert "refusing to emit Python" in str(excinfo.value)


# ---- 5. MCP ----------------------------------------------------------------

def test_mcp_check_exposes_open_obligations():
    from revl.mcp.server import _tool_check

    result = _tool_check({"source": DRAFT})
    assert result["ok"] is True
    (hole,) = result["holes"]
    assert (hole["code"], hole["expected"], hole["line"]) == ("T3", "Str", 3)
    assert hole["message"] == "look up in the store"


def test_mcp_check_reports_no_obligations_for_finished_code():
    from revl.mcp.server import _tool_check

    assert _tool_check({"source": FINISHED})["holes"] == []


def test_mcp_admit_refuses_a_draft():
    from revl.mcp.server import _tool_admit

    running = compile_source(FINISHED)
    result = _tool_admit({"source": DRAFT, "manifest": running})
    assert result["ok"] is False
    assert result["admitted"] is False
    assert result["diagnostics"][0]["code"] == "T3"


def test_mcp_session_load_refuses_a_draft():
    from revl.mcp.session import Session, SessionError

    session = Session()
    with pytest.raises(SessionError) as excinfo:
        session.load(compile_source(DRAFT))
    assert "open typed hole" in str(excinfo.value)
    assert not session.loaded
