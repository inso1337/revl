"""v2.0: type/fn lowering to IR v3 and cordis-py emission (syntax-2.0 §2–§3)."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# backends/python stays on sys.path for the EXEC'D modules (`from runtime
# import ...`) — emit itself is loaded by path below, NOT via this entry
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

# unique-name load: bare `import emit` binds the canonical name and collides
# with any other backend suite in the same process (tests/_backend_import.py)
emit = backend_emitter("python")


def _compile_emit(source):
    ir = compile_source(source)
    ns = {}
    exec(compile(emit.emit(ir), "emitted.py", "exec"), ns)
    return ir, ns


def test_types_and_fns_lower_to_ir_v3():
    ir = compile_source(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)
        fn add(a: Int, b: Int) -> Int { return a + b }
        """
    )
    assert ir["ir_version"] == 3
    assert ir["types"]["Row"] == {"params": [], "kind": "record", "fields": {"id": "Int", "name": "Str"}}
    assert ir["types"]["Outcome"]["kind"] == "variant"
    assert [c["name"] for c in ir["types"]["Outcome"]["cases"]] == ["Ok", "NotFound", "Invalid"]
    assert ir["functions"][0]["name"] == "add"


def test_v1_documents_stay_at_version_1():
    ir = compile_source("service A { fn ping() } component C { let x = effect Map.new() undo x.drop() }")
    assert ir["ir_version"] == 1
    assert "types" not in ir and "functions" not in ir


def test_services_20_async_and_commutative_lower_and_emit():
    ir, ns = _compile_emit(
        """
        commutative service Database {
          commutative fn query(sql: Str) -> List[Row]
          async fn stats() -> Stats
          emission fn execute(sql: Str) -> Int
        }

        component Db provides db: Database {
          provide db {
            fn query(sql) { return [] }
            async fn stats() {
              await Job.run("stats")
              return { hits: 0 }
            }
            fn execute(sql) { return 1 }
          }
        }
        """
    )
    assert ir["ir_version"] == 3
    service = ir["services"]["Database"]
    assert service["commutative"] is True
    assert service["methods"]["query"]["commutative"] is True
    assert "async" not in service["methods"]["query"]
    assert service["methods"]["stats"]["async"] is True
    assert "commutative" not in service["methods"]["stats"]
    assert service["methods"]["execute"]["emission"] is True

    emitted_service = ns["SERVICES"]["Database"]
    assert emitted_service["commutative"] is True
    assert emitted_service["query"]["commutative"] is True
    assert emitted_service["stats"]["async"] is True

    source = emit.emit(ir)
    assert "async def stats(self):" in source
    assert "await Job.run('stats')" in source
    assert "'async': True" in source
    assert "'commutative': True" in source


def test_emit_executes_types_and_functions():
    _, ns = _compile_emit(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn add(a: Int, b: Int) -> Int { return a + b }
        fn classify(n: Int) -> Str {
          if (n < 0) return "neg"
          return n === 0 ? "zero" : "pos"
        }
        fn first(xs: List[Int]) -> Int { return xs[0] }

        component Pinger { let p = effect Map.new() undo p.drop() }
        """
    )
    assert ns["add"](1, 2) == 3
    assert ns["classify"](-1) == "neg"
    assert ns["classify"](0) == "zero"
    assert ns["classify"](5) == "pos"
    assert ns["first"]([7, 8]) == 7
    row = ns["Row"](id=1, name="x")
    assert row.id == 1 and row.name == "x"
    assert issubclass(ns["Ok"], ns["Outcome"])
    assert isinstance(ns["NotFound"](), ns["Outcome"])
    assert ns["Invalid"]("bad").value == "bad"


def test_match_expression_emits_and_executes():
    _, ns = _compile_emit(
        """
        type Row = { id: Int, name: Str }
        type Outcome = Ok(Row) | NotFound | Invalid(Str)

        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "not found",
            Invalid(why) => why,
          }
        }
        fn label(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            _ => "other",
          }
        }
        """
    )
    row = ns["Row"](id=1, name="ada")
    assert ns["describe"](ns["Ok"](row)) == "ada"
    assert ns["describe"](ns["NotFound"]()) == "not found"
    assert ns["describe"](ns["Invalid"]("bad")) == "bad"
    assert ns["label"](ns["NotFound"]()) == "other"
    assert ns["label"](ns["Ok"](ns["Row"](id=2, name="bob"))) == "bob"


def test_match_nonexhaustive_over_known_variant_rejected():
    with pytest.raises(RevlError, match=r"non-exhaustive match: missing case `Invalid`"):
        compile_source(
            """
            type Row = { id: Int, name: Str }
            type Outcome = Ok(Row) | NotFound | Invalid(Str)
            fn describe(outcome: Outcome) -> Str {
              return match outcome {
                Ok(row) => row.name,
                NotFound => "-",
              }
            }
            """
        )


def test_let_reassignment_rejected():
    with pytest.raises(RevlError, match="cannot reassign `n`"):
        compile_source("fn f() -> Int { let n = 1 n = 2 return n }")


def test_undeclared_var_rejected():
    with pytest.raises(RevlError, match="`missing` is not declared"):
        compile_source("fn f() -> Int { return missing }")


def test_duplicate_field_rejected():
    with pytest.raises(RevlError, match="duplicate field `id`"):
        compile_source("type Row = { id: Int, id: Str }")


def test_duplicate_case_rejected():
    with pytest.raises(RevlError, match="duplicate case `NotFound`"):
        compile_source("type T = A | NotFound | NotFound")


def test_pure_extern_lowers_to_ir_v3():
    ir = compile_source(
        """
        extern pure fn sha256(data: Bytes) -> Str
          = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
          = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
        """
    )
    assert ir["ir_version"] == 3
    (ext,) = ir["externs"]
    assert ext["name"] == "sha256"
    assert ext["class"] == "pure"
    assert ext["bodies"]["py"] == ' import hashlib; return hashlib.sha256(data).hexdigest() '
    assert ext["bodies"]["ts"] == ' return crypto.createHash("sha256").update(data).digest("hex") '


def test_pure_extern_emits_runnable_python():
    ir = compile_source(
        """
        extern pure fn sha256(data: Bytes) -> Str
          = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
          = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
        """
    )
    ns = {}
    exec(compile(emit.emit(ir), "emitted_extern.py", "exec"), ns)
    assert ns["sha256"](b"abc") == __import__("hashlib").sha256(b"abc").hexdigest()


def test_ts_only_extern_is_not_python_portable():
    ir = compile_source(
        """
        extern pure fn only_ts(data: Bytes) -> Str
          = @ts { return String(data) }
        """
    )
    with pytest.raises(emit.EmitError, match="has no @py body"):
        emit.emit(ir)


def test_acquire_extern_without_undo_rejected():
    with pytest.raises(RevlError, match="must declare `undo`"):
        compile_source("extern acquire fn listen(port: Int) -> Socket = @py { return port }")
def test_test_block_lowers_and_emits_runnable_python():
    ir, ns = _compile_emit(
        """
        verified fn add(a: Int, b: Int) -> Int { return a + b }

        test "add works" {
          assert add(1, 2) == 3
        }
        """
    )
    assert ir["ir_version"] == 3
    assert ir["functions"][0]["verified"] is True
    assert ir["tests"] == [
        {
            "name": "add works",
            "body": [
                {
                    "step": "assert",
                    "expr": {
                        "kind": "bin",
                        "op": "==",
                        "left": {
                            "kind": "call",
                            "callee": {"kind": "var", "name": "add"},
                            "args": [{"kind": "lit", "value": 1}, {"kind": "lit", "value": 2}],
                        },
                        "right": {"kind": "lit", "value": 3},
                    },
                }
            ],
        }
    ]
    assert ns["REVL_TESTS"][0][0] == "add works"
    ns["REVL_TESTS"][0][1]()  # passes


def test_failing_test_assert_is_reported():
    _, ns = _compile_emit('test "boom" { assert 1 == 2 }')
    name, test_fn = ns["REVL_TESTS"][0]
    assert name == "boom"
    with pytest.raises(AssertionError):
        test_fn()


def test_test_block_can_use_host_builtins():
    _, ns = _compile_emit(
        """
        test "map roundtrip" {
          let m = Map.new()
          m.insert("k", "v")
          assert m.get("k") == "v"
        }
        """
    )
    ns["REVL_TESTS"][0][1]()


def test_revl_test_cli_exit_codes(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    passing = tmp_path / "passing.rvl"
    passing.write_text('test "passes" { assert true }', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "revl", "test", str(passing)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS passes" in result.stdout

    failing = tmp_path / "failing.rvl"
    failing.write_text('test "fails" { assert 1 == 2 }', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "revl", "test", str(failing)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "FAIL fails" in result.stdout


def test_loops_mutation_and_destructuring_emit_and_execute():
    _, ns = _compile_emit(
        """
        type Row = { id: Int, name: Str }

        fn count_idents(kinds: List[Str]) -> Int {
          var n = 0
          for (kind of kinds) {
            if (kind == "Ident") n += 1
          }
          return n
        }

        fn skip_spaces(source: Str, start: Int) -> Int {
          var i = start
          while (i < source.length && source.charAt(i) == " ") i += 1
          return i
        }

        fn destructure(row: Row) -> Int {
          let {id, name} = row
          return id + name.length
        }

        fn list_destructure(xs: List[Int]) -> Int {
          let [head, ...rest] = xs
          return head + rest.length
        }
        """
    )
    assert ns["count_idents"](["Ident", "Str", "Ident"]) == 2
    assert ns["skip_spaces"]("   x", 0) == 3
    assert ns["destructure"](ns["Row"](id=4, name="ab")) == 6
    assert ns["list_destructure"]([3, 4, 5]) == 5


def test_arrow_captures_var_by_value_at_creation_time():
    _, ns = _compile_emit(
        """
        fn captured() -> Int {
          var n = 1
          let f = x => x + n
          n += 10
          return f(5)
        }
        """
    )
    assert ns["captured"]() == 6  # f saw n == 1, not the final 11


def test_closure_that_reads_var_then_reassigned_still_by_value():
    # Item 129 positive: read-capture of a `var` snapshots by value even when the
    # var is reassigned AFTER the closure is built — the supported, sound form.
    _, ns = _compile_emit(
        """
        fn snapshot_capture() -> Int {
          var n = 1
          let f = (x: Int) => x + n
          n = 100
          return f(5)
        }
        """
    )
    assert ns["snapshot_capture"]() == 6  # snapshot of n == 1, not the later 100


def test_closure_mutating_captured_var_rejected():
    # Item 129 rejection: a closure that ASSIGNS to a captured binding is
    # reference capture (a shared mutable cell), which breaks the value-semantic
    # equality G7/A8 rest on (docs/closures.md). Refused with an explicit
    # diagnostic that names the binding and points to §3.5 — not the old
    # incidental record-literal parse error.
    with pytest.raises(
        RevlError,
        match=r"a closure cannot assign to `n`: captures are by value",
    ):
        compile_source(
            "fn c(s: Int) -> Int { var n = 0  let g = (x: Int) => { n = x  n }  return g(s) }"
        )


def test_closure_compound_assign_to_captured_var_rejected():
    # The compound form (`n += by`) is reference capture too, and refused.
    with pytest.raises(
        RevlError,
        match=r"a closure cannot assign to `n`: captures are by value",
    ):
        compile_source(
            "fn c() -> Int { var n = 0  let g = () => { n += 1  n }  return g() }"
        )


def test_var_by_value_in_record_literal_accepted():
    # Item 154: a record is a value type, so a bare `var` read in a record-literal
    # field copies the value in — the mutable cell never escapes. This now
    # compiles and emits the copied value correctly.
    _ir, ns = _compile_emit(
        """
        fn leak() -> Int {
          var n = 1
          let row = { value: n }
          n = 99
          return row.value
        }
        """
    )
    # the record captured n's value at construction (1), not the later 99 —
    # confirming it is a by-value copy, not a live reference to the cell
    assert ns["leak"]() == 1


def test_compound_assignment_on_let_rejected():
    with pytest.raises(RevlError, match="cannot reassign `n`"):
        compile_source("fn bump() -> Int { let n = 1 n += 1 return n }")
def test_components_2_block_effect_and_fail_ir_shape():
    ir = compile_source(
        """
        component Failing {
          config { replicas: Int = 0 }
          let scratch = effect Map.new() undo scratch.drop()
          if (config.replicas < 1) fail "at least one replica required"
          let pool = effect {
            let url = "postgres://db"
            Pool.open(url, config.replicas)
          } undo pool.close()
        }
        """
    )
    assert ir["ir_version"] == 3
    (body,) = [c["body"] for c in ir["components"]]
    assert body[0]["step"] == "let-effect"
    assert body[1] == {
        "step": "if",
        "cond": {
            "kind": "bin",
            "op": "<",
            "left": {"kind": "config", "field": "replicas"},
            "right": {"kind": "lit", "value": 1},
        },
        "then": [{"step": "fail", "message": {"kind": "lit", "value": "at least one replica required"}}],
    }
    assert body[2]["step"] == "let-effect"
    assert body[2]["setup"] == [
        {"step": "let", "name": "url", "value": {"kind": "lit", "value": "postgres://db"}}
    ]
    assert body[2]["acquire"] == {
        "kind": "host",
        "fn": "Pool.open",
        "args": [{"kind": "name", "id": "url"}, {"kind": "config", "field": "replicas"}],
    }
    assert body[2]["undo"]["method"] == "close"


def test_components_2_emits_fail_as_runtime_l_raise():
    ir = compile_source(
        """
        component Failing {
          config { replicas: Int = 0 }
          let scratch = effect Map.new() undo scratch.drop()
          if (config.replicas < 1) fail "at least one replica required"
        }
        """
    )
    generated = emit.emit(ir)
    assert "raise RuntimeError('at least one replica required')" in generated
    assert "scratch = Map.new()" in generated
    assert "yield lambda: scratch.drop()" in generated


def test_components_2_fail_exec_reverts_accumulated_effects():
    pytest.importorskip("cordis")
    import runtime  # noqa: E402
    from cordis import Context  # noqa: E402
    from cordis.fiber import FiberState  # noqa: E402

    ir = compile_source(
        """
        component Failing {
          config { replicas: Int = 0 }
          let scratch = effect Map.new() undo scratch.drop()
          if (config.replicas < 1) fail "at least one replica required"
        }
        """
    )
    ns = {}
    exec(compile(emit.emit(ir), "emitted_components2_fail.py", "exec"), ns)
    events = []
    runtime.set_trace(events.append)

    async def run():
        root = Context()
        fiber = root.plugin(ns["Failing"])
        for _ in range(20):
            await asyncio.sleep(0)
        return root, fiber

    try:
        root, fiber = asyncio.run(run())
        assert fiber.state is FiberState.FAILED
        normalized = [re.sub(r"#\d+", "", event) for event in events]
        assert "map.drop" in normalized, "effects before deliberate L-Raise must revert"
    finally:
        runtime.set_trace(None)
