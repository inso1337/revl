"""Roadmap item 441 / issue #120, S1 — the termination language surface
(docs/design/458-termination-language-surface.md §7 "S1").

S1 is the type surface plus the checker rules that make a termination criterion
a CHECKED construct rather than a naming convention, OFF BY DEFAULT and
byte-identical when unused (L6). The surface is two builtin marker type heads on
service operations, `Criterion` and `Guard`, each erasing to `Opt[Bool]` on
every tier; nothing is lexed (no new keyword), so the self-host lexer parity
oracle is untouched. The rules that land in S1:

  * L1 — a criterion/guard body reaches NO emission and NO witnessed extern. The
    load-bearing rule: it is the existing G5 teardown-slot walker
    (`_walk_inverse_emissions`) pointed at the marked provider body, with a third
    refusal that names the criterion. 441's C1 — "a criterion that causes its own
    satisfaction" — is exactly what this refuses.
  * L2 — a goal-service operation is not an `emission` (refused at the signature).
  * L4 — `Criterion`/`Guard` have no constructor: the only position that admits
    one is a service-operation return, where it erases to `Opt[Bool]`. Every
    other position (a `let` annotation, a record field, a parameter, an arrow
    return, an application `Criterion[..]`) is refused.
  * L5 — which operations are criteria/guards is a HEADER fact: the services IR
    gains one `termination` key per marked operation.
  * L6 — off is byte-identical: a program with no marker has the same IR.

L3 (a turn may not provide a goal service) is a session rule and lands in S3;
it is not exercised here.

The last test is the self-host half: `selfhost/lower.rvl` builds the same
services IR (erased `returns` + the `termination` key), byte-identical to the
reference over marker sources — the byte-agreement gate §6 names.

EXIT (§7): the §1.1 sketch — a criterion that writes the file it checks for — is
refused by `revl check` with no session.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# --------------------------------------------------------------- source helpers

def _svc_component(op_body: str, *, extra: str = "", requires: str = "",
                   provides_svc: str = "Done", key: str = "done",
                   marker: str = "Criterion", op: str = "ok") -> str:
    """A component providing a one-operation goal service, with `op_body` the
    body of the marked operation."""
    req = f"requires {requires} " if requires else ""
    return (
        f"{extra}"
        f"service {provides_svc} {{ fn {op}() -> {marker} }}\n"
        f"component C {req}provides {key}: {provides_svc} {{\n"
        f"  provide {key} {{ fn {op}() {op_body} }}\n"
        f"}}\n"
    )


# --------------------------------------------------------------- L1 refusals

def test_l1_criterion_reaching_direct_emission_is_refused():
    """The §1.1 sketch, the EXIT criterion: a criterion that causes its own
    satisfaction is refused by `revl check`, naming the criterion and the
    boundary it reaches."""
    src = _svc_component(
        "{ let n = write_report() return Some(true) }",
        extra='extern emission fn write_report() -> Int = @py { return 1 }\n')
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    err = ei.value
    assert err.code == "L1", err
    msg = str(err)
    assert "Done.ok" in msg and "write_report" in msg
    assert "READ" in msg


def test_l1_through_plain_fn_names_the_chain():
    """The same boundary, one `fn` indirection later — the derivation is rendered
    the way the shared G5 walker renders it (`through a -> b`)."""
    src = _svc_component(
        "{ let n = helper() return Some(true) }",
        extra=('extern emission fn send() -> Int = @py { return 1 }\n'
               'fn helper() -> Int { return send() }\n'))
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    assert ei.value.code == "L1"
    msg = str(ei.value)
    assert "helper" in msg and "send" in msg and "through" in msg


def test_l1_through_witnessed_extern_is_refused():
    """G4 lets a provide body reach a witnessed extern (item 318); L1 does NOT —
    a criterion may not cross even a transactional boundary."""
    src = _svc_component(
        "{ effect stash(\"f\") return Some(true) }",
        extra=('type Stash = { path: Str, bak: Str }\n'
               'type FsErr = { code: Str }\n'
               'extern pure fn unstash(w: Stash) -> Unit = @py { pass }\n'
               'extern witnessed[fs] fn stash(path: Str) -> Result[Stash, FsErr] '
               'undo idempotent unstash(result) = @py { return ("ok", {}) }\n'))
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    assert ei.value.code == "L1", ei.value
    assert "witnessed" in str(ei.value) and "stash" in str(ei.value)


def test_l1_through_emission_service_op_is_refused_as_a_criterion():
    """An `emission` service operation off a required binding. The provide-body
    emit-marking rule would otherwise refuse this with a generic message; L1 runs
    first so the refusal names the criterion."""
    src = _svc_component(
        "{ let n = sink.push(1) return Some(true) }",
        requires="sink: Sink",
        extra="service Sink { emission fn push(x: Int) -> Int }\n")
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    assert ei.value.code == "L1", ei.value
    assert "sink.push" in str(ei.value)


def test_l1_applies_to_guards_too():
    src = _svc_component(
        "{ let n = send() return Some(false) }",
        marker="Guard", provides_svc="Safe", key="safe", op="breach",
        extra='extern emission fn send() -> Int = @py { return 1 }\n')
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    assert ei.value.code == "L1"
    assert "guard" in str(ei.value) and "Safe.breach" in str(ei.value)


@pytest.mark.parametrize("body,extra,requires", [
    # a pure body
    ("= Some(true)", "", ""),
    # a read of a plain (non-emission) required service operation
    ("= Some(repo.exit_code() == 0)",
     "service Repo { fn exit_code() -> Int }\n", "repo: Repo"),
    # a read through a pure extern
    ("{ let n = probe() return Some(n == 0) }",
     "extern pure fn probe() -> Int = @py { return 0 }\n", ""),
])
def test_l1_accepts_read_only_bodies(body, extra, requires):
    """A criterion that only READS — pure, or a plain service op, or a pure/acquire
    extern — compiles unchanged. L1 refuses only emission and witnessed reaches."""
    src = _svc_component(body, extra=extra, requires=requires)
    ir = compile_source(src)  # no raise
    assert ir["services"]["Done"]["methods"]["ok"]["termination"] == "criterion"


# --------------------------------------------------------------- L2

def test_l2_emission_marker_operation_is_a_signature_refusal():
    """`emission fn x() -> Criterion` is refused at the signature, before any
    provider exists."""
    with pytest.raises(RevlError) as ei:
        compile_source("service S { emission fn x() -> Criterion }")
    assert "emission" in str(ei.value)
    assert "Criterion" in str(ei.value)


# --------------------------------------------------------------- L4

@pytest.mark.parametrize("src", [
    "fn f() -> Int { let c: Criterion = Some(true) return 0 }",   # let annotation
    "type R = { g: Guard }",                                      # record field
    "fn f(c: Criterion) -> Int { return 0 }",                     # a parameter
    "service S { fn f() -> List[Criterion] }",                    # nested argument
    "service S { fn f() -> (Int) -> Guard }",                     # an arrow return
    "service S { fn f() -> Criterion[Int] }",                     # an application
])
def test_l4_marker_outside_service_return_is_refused(src):
    with pytest.raises(RevlError) as ei:
        compile_source(src)
    assert ei.value.code == "L4", (src, ei.value)


def test_l4_service_return_position_is_the_one_admitted_spelling():
    """The single position that admits a marker: it erases to `Opt[Bool]` and
    leaves the `termination` key."""
    ir = compile_source("service S { fn f() -> Criterion }")
    method = ir["services"]["S"]["methods"]["f"]
    assert method["returns"] == "Opt[Bool]"
    assert method["termination"] == "criterion"


# --------------------------------------------------------------- L5 / L6

def test_l5_termination_is_a_header_fact():
    ir = compile_source(
        "service S {\n"
        "  fn a() -> Criterion\n"
        "  fn b() -> Guard\n"
        "  fn c() -> Int\n"
        "  fn d() -> Opt[Bool]\n"
        "}\n")
    methods = ir["services"]["S"]["methods"]
    assert methods["a"]["termination"] == "criterion"
    assert methods["b"]["termination"] == "guard"
    # a plain Opt[Bool] operation with no marker carries NO termination key
    assert "termination" not in methods["c"]
    assert "termination" not in methods["d"]
    assert methods["d"]["returns"] == "Opt[Bool]"


def test_l6_marker_erases_to_the_same_ir_as_a_plain_optional():
    """A marked operation's IR equals the same operation written `Opt[Bool]`,
    except for the added `termination` key — the erasure is exact."""
    marked = compile_source(
        "service S { fn f() -> Criterion }")["services"]["S"]["methods"]["f"]
    plain = compile_source(
        "service S { fn f() -> Opt[Bool] }")["services"]["S"]["methods"]["f"]
    assert marked == {**plain, "termination": "criterion"}


def test_l6_unmarked_program_has_no_termination_key_anywhere():
    ir = compile_source(
        "service Store { fn get(k: Int) -> Int fn put(k: Int, v: Int) }\n"
        "component C requires s: Store { effect s.put(1, 2) undo s.put(1, 0) }\n")
    blob = json.dumps(ir)
    assert "termination" not in blob


# --------------------------------------------------------------- self-host port

@pytest.fixture(scope="module")
def lower_to_ir():
    """`selfhost/lower.rvl::lower_to_ir`, compiled by revl, emitted through the
    python backend and executed — the same harness the byte-agreement gate uses
    (tests/test_selfhost_lower_ir.py)."""
    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "pyemit_458", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_lower_ir_458.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["lower_to_ir"]


@pytest.mark.parametrize("src", [
    "service S { fn a() -> Criterion }",
    "service S { fn a() -> Guard }",
    # markers next to plain operations and a scoped emission, to pin the key's
    # position relative to `returns`/`emission`
    "service S {\n"
    "  fn a() -> Criterion\n"
    "  fn b(x: Int) -> Int\n"
    "  fn c() -> Guard\n"
    "  emission[db] fn d(x: Int) -> Int\n"
    "}\n",
])
def test_selfhost_services_ir_agrees_byte_for_byte(lower_to_ir, src):
    """The §6 gate: `selfhost/lower.rvl` erases the marker to `Opt[Bool]` and
    emits the `termination` key exactly where the reference does, so the services
    table is byte-identical over marker sources."""
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert native["services"] == reference["services"]
