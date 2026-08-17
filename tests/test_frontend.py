"""Frontend tests: the reference roundtrip and the rejection suite.

The rejection files in examples/rejections/ are the checker's executable
spec (DESIGN.md §9): each must fail to compile with the message its header
comment promises.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

EXAMPLES = ROOT / "examples"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- roundtrip

def test_user_cache_compiles_to_reference_ir():
    """The frontend must produce exactly the hand-lowered IR that both
    backends were built and tested against."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    reference = json.loads((EXAMPLES / "user_cache.ir.json").read_text())
    assert ir == reference


def test_end_to_end_python_golden():
    """source -> frontend IR -> cordis-py emitter == checked-in golden."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_py_emit", ROOT / "backends" / "python" / "emit.py")
    golden = (ROOT / "backends" / "python" / "golden" / "user_cache.py").read_text()
    assert emitter.emit(ir) == golden


def test_end_to_end_typescript_golden():
    """source -> frontend IR -> cordis-ts emitter == checked-in golden."""
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    emitter = _load_module("revl_ts_emit", ROOT / "backends" / "typescript" / "emit.py")
    golden = (ROOT / "backends" / "typescript" / "golden" / "user_cache.ts").read_text()
    assert emitter.emit(ir) == golden


# ---------------------------------------------------------------- rejections

REJECTIONS = {
    "a1_await_in_method.rvl": "`await` is only allowed in a component body",
    "v2_async_signature_mismatch.rvl": "method `stats` of provision `db` is not async but service Database declares it async",
    "v2_same_realm_conflict.rvl": "provision conflict: key `kv` in realm `tenant_a` is provided by both StoreOne and StoreTwo (G2)",
    "v2_dynamic_realm.rvl": "dynamic realm labels are not supported",
    "v2_intercept_on_provision.rvl": "`intercept` applies to required keys only — `kv` is a provision",
    "v2_intercept_undeclared.rvl": "`db` is not a declared requirement of Watcher",
    "v2_compound_assign_on_let.rvl": "cannot reassign `n` — it is `let` (single-assignment)",
    "v2_isolate_after_effect.rvl": "`isolate` must precede every effect, emit, await, and provide statement",
    "v2_let_reassignment.rvl": "cannot reassign `n` — it is `let` (single-assignment)",
    "v2_match_nonexhaustive.rvl": "non-exhaustive match: missing case `Invalid`",
    "v2_var_in_record.rvl": "`var` `n` cannot be used in a record literal",
    "v2_undeclared_fn_var.rvl": "`missing` is not declared in this function",
    "v2_use_cycle.rvl": "import cycle:",
    "v2_use_private.rvl": "`helper` is module-private",
    "v2_extern_unclassified.rvl": "unclassified extern",
    "v2_extern_acquire_no_undo.rvl": "acquire extern `listen` must declare `undo` (G4)",
    "v2_fail_in_pure_fn.rvl": "`fail` is only allowed in a component activation body (A8)",
    "v2_verified_direct_recursion.rvl": "verified fn `recurse` is not total",
    "g1_undeclared_access.rvl": "`db` is not a declared requirement of Logger",
    "g2_provision_conflict.rvl": "provision conflict: key `db` is provided by both PgDatabase and SqliteDatabase (G2)",
    "g3_dependency_cycle.rvl": "dependency cycle: Alpha -> Beta -> Alpha (G3)",
    "g4_missing_undo.rvl": "effect has no `undo` and `Pool.open` is not pure",
    "g4_unmarked_emission.rvl": "call to emission `db.execute` must be marked `emit` (G4)",
    "a2_acquire_after_provide.rvl": "acquisition after `provide`",
}


def test_every_rejection_file_is_covered():
    on_disk = {p.name for p in (EXAMPLES / "rejections").glob("*.rvl")}
    assert on_disk == set(REJECTIONS)


@pytest.mark.parametrize("filename,expected", sorted(REJECTIONS.items()))
def test_rejection(filename, expected):
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(EXAMPLES / "rejections" / filename)])
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------- v1 features

def test_migrator_compiles_to_reference_ir():
    """await + compensate lowering matches the frozen v1 reference."""
    ir = compile_files([str(EXAMPLES / "migrator.rvl")])
    reference = json.loads((EXAMPLES / "migrator.ir.json").read_text())
    assert ir == reference


def test_a3_host_colliding_names_are_renamed():
    from revl import compile_source

    ir = compile_source(
        """
        component Renamer {
          let frame = effect Map.new() undo frame.drop()
          let class = effect Map.new() undo class.drop()
        }
        """
    )
    body = ir["components"][0]["body"]
    binds = [step["bind"] for step in body]
    assert binds == ["frame_", "class_"], "host-reserved names must be renamed (A3)"
    assert body[0]["undo"]["target"]["id"] == "frame_"


def test_a4_literal_dollars_are_escaped():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Pricer requires bus: Bus {
          let item = effect Map.new() undo item.drop()
          emit bus.send("cost: $$9.99 for $item")
        }
        """
    )
    fmt = ir["components"][0]["body"][1]["expr"]["args"][0]
    assert fmt["template"] == "cost: $$9.99 for $0"
    assert fmt["args"] == [{"kind": "name", "id": "item"}]


def test_a5_compensate_lowering():
    from revl import compile_source

    ir = compile_source(
        """
        service Bus { emission fn send(msg: Str) }
        component Notifier requires bus: Bus {
          emit bus.send("hello") compensate bus.send("goodbye")
        }
        """
    )
    step = ir["components"][0]["body"][0]
    assert step["step"] == "emit"
    assert step["compensate"]["method"] == "send"


# ---------------------------------------------------------------- diagnostics

def test_rejections_carry_file_and_line():
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(EXAMPLES / "rejections" / "g1_undeclared_access.rvl")])
    rendered = str(excinfo.value)
    assert "g1_undeclared_access.rvl:" in rendered
    assert excinfo.value.line == 12  # the `let rows = effect db.query(...)` line
