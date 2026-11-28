"""The self-host gate refuses a typed `event` the reference refuses - issue #3396.

The reference's `_lower_event_decls` (lower.py) runs after
`_validate_declared_types` and raises (code G4) on the first bad `event`
declaration: the key must name a declared field, the key's type must be a
serializable scalar (`Str`/`Int`), and the event's record must be
`fully_expressible` (a handler validates every delivered item at the boundary,
so an inexact schema is refused rather than shipped vacuous - item 257 §3.3).
`selfhost/lower.rvl`'s gate admitted all three shapes; `event_decls_walk` now
ports them as a fail-fast phase in `collect_nonlink`, right after the
config-data walk and ahead of the shadowed-callable one, matching the
reference's phase order.

Every refusal case below asserts the gate's wire is BYTE-IDENTICAL to the
reference's `code|message`, and every admission case asserts both trees admit:
the gate must never admit what the reference refuses, and must not refuse
what the reference admits.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from _compiled_ir import compiled_ir  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _emitted() -> dict:
    """`selfhost/lower.rvl` compiled and run under the python backend - the
    same harness `tests/test_selfhost_lower.py` uses."""
    ir = compiled_ir(ROOT / "selfhost" / "lower.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_lower_3396", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def gate():
    ns = _emitted()
    return ns["admit_src"], ns["admit_all"]


def _ref(src: str) -> str:
    """The reference refusal rendered in the gate's wire format, "" if admitted."""
    try:
        compile_source(src, "case.rvl")
        return ""
    except RevlError as e:
        return f"{e.code}|{e.message}"


# ---------------------------------------------------------------- refusals

AGG_FIELD = "event E(key: id) { id: Str, a: Aggregate[Str] }\n"

RESULT_FIELD = "event E(key: id) { id: Str, r: Result[Int, Str] }\n"

OPT_OPT = "event E(key: id) { id: Str, o: Opt[Opt[Str]] }\n"

MAP_INT_KEY = "event E(key: id) { id: Str, m: Map[Int, Str] }\n"

LIST_AGG = "event E(key: id) { id: Str, xs: List[Aggregate[Str]] }\n"

# the reason recurses into a named record's fields
NESTED_UNKNOWN = "type R = { a: Aggregate[Str] }\nevent E(key: id) { id: Str, r: R }\n"

# a transparent alias is erased before the check, so this is AGG_FIELD again
ALIAS_FIELD = "type Agg = Aggregate[Str]\nevent E(key: id) { id: Str, a: Agg }\n"

KEY_MISSING = "event E(key: nope) { id: Str }\n"

KEY_COMPOUND = "event E(key: k) { k: List[Int] }\n"

# two events: the FIRST bad one in declaration order is the one reported
SECOND_BAD = (
    "event Ok(key: id) { id: Str, n: Int }\n"
    "event E(key: id) { id: Str, a: Aggregate[Str] }\n"
)

FIRST_BAD = (
    "event E(key: id) { id: Str, a: Aggregate[Str] }\n"
    "event Ok(key: id) { id: Str, n: Int }\n"
)

REFUSALS = [
    AGG_FIELD, RESULT_FIELD, OPT_OPT, MAP_INT_KEY, LIST_AGG,
    NESTED_UNKNOWN, ALIAS_FIELD, KEY_MISSING, KEY_COMPOUND,
    SECOND_BAD, FIRST_BAD,
]

# ---------------------------------------------------------------- admissions

ADMISSIONS = [
    "event E(key: id) { id: Str, n: Int }\n",
    "event Shipped(key: shipment_id, window: 8) { shipment_id: Str, carrier: Str }\n",
    "event E(key: id) { id: Str, xs: List[Str], m: Map[Str, Int], o: Opt[Str] }\n",
    "type Receipt = { order_id: Str, total: Int }\n"
    "event E(key: id) { id: Str, r: Receipt }\n",
    # a variant payload that is expressible
    "type V = Ok(Int) | Err(Str)\nevent E(key: id) { id: Str, v: V }\n",
]


@pytest.mark.parametrize("src", REFUSALS, ids=lambda s: " ".join(s.split())[:60])
def test_gate_refusal_is_byte_identical_to_the_reference(gate, src):
    admit_src, _ = gate
    want = _ref(src)
    assert want != "", "reference must refuse every case in REFUSALS"
    assert want.startswith("G4|"), want
    assert admit_src(src) == want


@pytest.mark.parametrize("src", ADMISSIONS, ids=lambda s: " ".join(s.split())[:60])
def test_gate_admits_what_the_reference_admits(gate, src):
    admit_src, _ = gate
    assert _ref(src) == ""
    assert admit_src(src) == ""


def test_the_reported_event_is_the_first_bad_one_in_declaration_order(gate):
    admit_src, _ = gate
    # both SECOND_BAD and FIRST_BAD carry exactly one bad event, E; the wire
    # must name `E`, not `Ok`
    for src in (SECOND_BAD, FIRST_BAD):
        assert "`event E`" in admit_src(src)
        assert admit_src(src) == _ref(src)


def test_the_refusal_carries_the_event_declaration_line(gate):
    """`admit_all` is the test-only whole-sink wire "<line>|<tag>|<msg>"; the
    reference anchors the diagnostic at the `event` keyword's line."""
    _, admit_all = gate
    src = "\n\n" + AGG_FIELD
    want = _ref(src)
    got = admit_all(src)
    assert got.startswith("3|"), got
    assert got == f"3|{want}", (got, want)


def test_a_bad_event_wins_over_a_later_fault(gate):
    """Phase order: `_lower_event_decls` raises before `_lower_fns`, so on a
    program carrying both, the reference reports the event and so must the
    gate (the event phase sits ahead of the fn binding discipline)."""
    admit_src, _ = gate
    src = AGG_FIELD + "\nfn f() { let x = 1 }\nfn f() { let y = 2 }\n"
    want = _ref(src)
    assert want.startswith("G4|"), want
    assert "`event E`" in want
    assert admit_src(src) == want
