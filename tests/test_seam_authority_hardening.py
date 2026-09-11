"""Third-pass adversarial-review hardening for the cross-process distribution
seam (item 363 follow-up: Findings A, B, C).

The value-copy boundary between components in different processes had three
holes the review found on top of the item-363 nested-resource-taint fix:

A (CRITICAL) a resource crossing two SAME-TIER processes (py<->py) was NOT
    refused: the refusal ran only inside `cross_tier_boundary_check`, which
    short-circuited same-tier seams, so a `Socket` crossed two py processes
    ungated and the encoder shipped it as a dead `{"fd": 7}`. The refusal is now
    tier-AGNOSTIC (`resource_crossing_refusal`), running on every cross-process
    seam. Only a CROSS-PROCESS crossing is refused: a resource shared WITHIN one
    process (a key a process both requires and provides) is fine.

B (HIGH) the wire encoder was FAIL-OPEN: `_encode_value` degraded any unknown
    object to a dead `{"$kind": <typename>}` tag, so every miss by the plan-time
    name check marshaled silently. It now RAISES `SeamMarshalError` on any value
    that is not a scalar/list/dict/declared value record/emitted ADT-Result
    case, on BOTH the return path and the argument path.

C (verify 363) a resource nested in a record/variant a wrapper renames, and a
    closed generic argument, must be caught. Item 363's transitive taint already
    walks record fields AND variant case payloads, and the signature-level scan
    resolves a closed generic argument (`ConnG[Socket]`), so all four shapes are
    already refused; these are the regression pins, at BOTH a cross-tier and a
    same-tier seam.

D (HIGH) the wire DECODER was FAIL-OPEN in the same way B was, on the other
    side: `_decode_value` resolved an untrusted `$kind` with a bare
    `getattr(module, kind)` and CALLED the result. Every program fn is emitted
    as a module-level `def`, so `{"$kind": "<fn name>", "$value": <arg>}` invoked
    that fn with an attacker-chosen argument — and `_invoke` decoded its args
    BEFORE consulting the export allow-list, so a forged tag reached functions
    the seam never exported. A tag must now name a declared ADT/`Result` case
    class and nothing else, refused as `SeamTagError` (a `SeamMarshalError`) and
    answered as an ordinary error reply.
"""

import asyncio
import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.distribute import _resource_taint  # noqa: E402
from revl.placement import (  # noqa: E402
    cross_tier_boundary_check,
    resource_crossing_refusal,
)


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _ir(tmp: Path, app: str) -> dict:
    return compile_files([_write(tmp, "app.rvl", app)])


def _bridge():
    spec = importlib.util.spec_from_file_location(
        "revl_seam_bridge", ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The full corpus of resource-crossing shapes plus a genuinely value-typed one.
#   Socket   the bare handle (extern acquire return)
#   Conn     a resource NESTED in a record   (record fields walk)
#   Outer    a two-level nest Outer -> Conn -> Socket
#   Outcome  a resource in a VARIANT case payload  (variant cases walk)
#   ConnG[T] a generic instantiated over the resource (signature arg resolves)
#   ValRec   a genuinely value-typed nested record   (must still cross)
_SEAM_APP = """
type Socket = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Socket undo close_sock(0) = @py { return {"fd": 1} }
type Conn = { sock: Socket }
type Outer = { c: Conn }
type Outcome = Live(Socket) | Dead
type ConnG[T] = { sock: T }
type Inner = { a: Int }
type ValRec = { inner: Inner, label: Str }

service TopSvc { async fn top() -> Socket }
service RecSvc { async fn getc() -> Conn }
service OuterSvc { async fn geto() -> Outer }
service VarSvc { async fn getv() -> Outcome }
service GenSvc { async fn getg() -> ConnG[Socket] }
service ValSvc { async fn getp() -> ValRec }
service Ctl { async fn go() -> Str }
component C provides t: TopSvc {
  provide t { async fn top() = open_sock() }
}
"""


# --------------------------------------------------------------------------
# Finding A — the resource refusal is tier-AGNOSTIC (same-tier too)
# --------------------------------------------------------------------------

def _seams_for(service: str, host_backend: str, consumer_backend: str):
    """A one-key cross-process seam: `w` provides `k: <service>`, `c` requires
    it and provides an unrelated `ctl: Ctl`."""
    requires = {"c": {"k": service}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"k": service}}
    owner = {"k": "w", "ctl": "c"}
    backends = {"c": consumer_backend, "w": host_backend}
    return requires, provides, owner, backends


def test_a_top_level_resource_same_tier_seam_is_refused(tmp_path):
    ir = _ir(tmp_path, _SEAM_APP)
    requires, provides, owner, backends = _seams_for("TopSvc", "py", "py")
    problem = resource_crossing_refusal(ir, requires, provides, owner, backends)
    assert problem is not None
    assert "TopSvc" in problem and "Socket" in problem and "top" in problem
    # and the public entry point (cross_tier_boundary_check) surfaces it too
    boundary, _ = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert boundary is not None and "Socket" in boundary


def test_a_top_level_resource_cross_tier_seam_is_still_refused(tmp_path):
    ir = _ir(tmp_path, _SEAM_APP)
    requires, provides, owner, backends = _seams_for("TopSvc", "go", "py")
    problem = resource_crossing_refusal(ir, requires, provides, owner, backends)
    assert problem is not None and "Socket" in problem


def test_a_value_typed_same_tier_seam_still_admits(tmp_path):
    # no over-refusal: a value-typed service crossing two py processes is fine
    ir = _ir(tmp_path, _SEAM_APP)
    requires, provides, owner, backends = _seams_for("ValSvc", "py", "py")
    assert resource_crossing_refusal(ir, requires, provides, owner, backends) is None
    boundary, report = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, ir.get("services") or {})
    assert boundary is None and report == []   # admits, no report on same tier


def test_a_resource_shared_within_one_process_is_not_refused(tmp_path):
    # only a CROSS-PROCESS crossing is the target: a key a single process both
    # provides and requires is served in-memory and must NOT be refused.
    ir = _ir(tmp_path, _SEAM_APP)
    requires = {"p": {"k": "TopSvc"}}
    provides = {"p": {"k": "TopSvc"}}       # same process owns and consumes it
    owner = {"k": "p"}
    backends = {"p": "py"}
    assert resource_crossing_refusal(ir, requires, provides, owner, backends) is None


# --------------------------------------------------------------------------
# Finding C — nested / variant / two-level / generic all refused, both tiers
# --------------------------------------------------------------------------

def test_c_363_already_taints_records_variants_and_two_level(tmp_path):
    ir = _ir(tmp_path, _SEAM_APP)
    taint = _resource_taint(ir)
    # the transitive closure over the type table (item 363): the bare handle,
    # the record nest, the two-level nest, and the VARIANT case payload
    assert {"Socket", "Conn", "Outer", "Outcome"} <= taint
    assert "ValRec" not in taint and "Inner" not in taint   # value types untainted


@pytest.mark.parametrize("service,method,carrier", [
    ("RecSvc", "getc", "Conn"),        # resource nested in a record
    ("OuterSvc", "geto", "Outer"),     # two-level record nest
    ("VarSvc", "getv", "Outcome"),     # resource in a variant case payload
    ("GenSvc", "getg", "Socket"),      # closed generic arg ConnG[Socket]
])
@pytest.mark.parametrize("host_backend", ["py", "go"])   # same-tier and cross-tier
def test_c_nested_resource_crossing_is_refused(tmp_path, service, method, carrier,
                                               host_backend):
    ir = _ir(tmp_path, _SEAM_APP)
    requires, provides, owner, backends = _seams_for(service, host_backend, "py")
    problem = resource_crossing_refusal(ir, requires, provides, owner, backends)
    assert problem is not None, (service, host_backend)
    assert service in problem and carrier in problem


@pytest.mark.parametrize("host_backend", ["py", "go"])
def test_c_value_typed_nested_record_still_crosses(tmp_path, host_backend):
    ir = _ir(tmp_path, _SEAM_APP)
    requires, provides, owner, backends = _seams_for("ValSvc", host_backend, "py")
    assert resource_crossing_refusal(ir, requires, provides, owner, backends) is None


# --------------------------------------------------------------------------
# Finding B — the wire encoder is fail-CLOSED
# --------------------------------------------------------------------------

# emitted case-class shapes, exactly as backends/python/emit.py produces them:
# a plain slots-only base, slots-only nullary/payload cases, and Ok/Err.
class _Base:
    __slots__ = ()


class _Live(_Base):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Dead(_Base):
    __slots__ = ()


class _Ok:
    __slots__ = ("value",)

    def __init__(self, value=None):
        self.value = value


@dataclasses.dataclass
class _Rec:
    a: int
    b: str


class _OpaqueDict:            # a plain host object: carries a per-instance __dict__
    def __init__(self):
        self.fd = 7


class _OpaqueSlots:          # slots-based, but a foreign slot name (not "value")
    __slots__ = ("fd",)

    def __init__(self):
        self.fd = 7


def test_b_encoder_encodes_legit_values():
    b = _bridge()
    assert b._encode_value(5) == 5
    assert b._encode_value([1, "a", {"k": 2}]) == [1, "a", {"k": 2}]
    assert b._encode_value({"k": [1, 2]}) == {"k": [1, 2]}
    assert b._encode_value(_Rec(1, "x")) == {"a": 1, "b": "x"}
    assert b._encode_value(_Live(3)) == {"$kind": "_Live", "$value": 3}
    assert b._encode_value(_Dead()) == {"$kind": "_Dead"}
    assert b._encode_value(_Ok(9)) == {"$kind": "_Ok", "$value": 9}
    # nested ADT-in-list still recurses
    assert b._encode_value([_Ok(1), _Live(2)]) == [
        {"$kind": "_Ok", "$value": 1}, {"$kind": "_Live", "$value": 2}]


@pytest.mark.parametrize("opaque", [_OpaqueDict(), _OpaqueSlots(), object()])
def test_b_encoder_refuses_opaque_objects(opaque):
    b = _bridge()
    with pytest.raises(b.SeamMarshalError):
        b._encode_value(opaque)


def test_b_arg_path_fails_closed_on_opaque_and_encodes_adt():
    # the argument path routes through the SAME marshaller as the return path,
    # so an opaque arg raises SeamMarshalError (was a bare TypeError from raw
    # json.dumps) while a value/ADT arg encodes.
    b = _bridge()
    assert b._encode_value([1, "x", {"k": 2}]) == [1, "x", {"k": 2}]     # values ok
    assert b._encode_value([_Ok(1)]) == [{"$kind": "_Ok", "$value": 1}]  # ADT ok
    with pytest.raises(b.SeamMarshalError):
        b._encode_value([_OpaqueDict()])                                 # opaque refused


def test_b_encode_decode_roundtrip_through_module():
    b = _bridge()
    mod = sys.modules[__name__]
    enc = b._encode_value(_Live(_Ok(4)))
    assert enc == {"$kind": "_Live", "$value": {"$kind": "_Ok", "$value": 4}}
    dec = b._decode_value(enc, mod)
    assert isinstance(dec, _Live) and isinstance(dec.value, _Ok) and dec.value.value == 4


def test_b_is_emitted_case_discriminates():
    b = _bridge()
    assert b._is_emitted_case(_Live(1))
    assert b._is_emitted_case(_Dead())
    assert b._is_emitted_case(_Ok(1))
    assert not b._is_emitted_case(_OpaqueDict())
    assert not b._is_emitted_case(_OpaqueSlots())
    assert not b._is_emitted_case(_Rec(1, "x"))


# --------------------------------------------------------------------------
# Finding D — a wire `$kind` may only name a DECLARED case
# --------------------------------------------------------------------------

_INVOKED: list = []


def _record(*args):
    """Stands in for an emitted program fn: a module-level `def` that a forged
    `$kind` could name. Records the call so a test can prove it never ran."""
    _INVOKED.append(args)
    return "ran"


def _emitted_module():
    """A module-shaped object holding what an emitted program module holds: its
    own functions as module-level names, the declared case classes, and the
    non-case classes (a record) that share the namespace."""
    mod = types.ModuleType("revl_emitted_probe")
    mod._record = _record
    mod.privileged = _record          # a fn the seam never exported
    mod._Live = _Live
    mod._Dead = _Dead
    mod._Ok = _Ok
    mod._Rec = _Rec
    return mod


def test_d_the_probe_fn_is_reachable_by_name():
    # non-vacuity: the gadget set is real — these names ARE module attributes and
    # calling one DOES record, so the refusals below are not passing by accident.
    mod = _emitted_module()
    _INVOKED.clear()
    assert mod._record("x") == "ran"
    assert _INVOKED == [("x",)]
    assert getattr(mod, "privileged") is _record


@pytest.mark.parametrize("kind", ["_record", "privileged", "_Rec", "__builtins__",
                                  "__spec__", "no_such_name", 7, None, ["_Ok"]])
def test_d_forged_kind_is_refused_and_never_invoked(kind):
    b = _bridge()
    mod = _emitted_module()
    _INVOKED.clear()
    with pytest.raises(b.SeamTagError) as err:
        b._decode_value({"$kind": kind, "$value": "victim"}, mod)
    assert _INVOKED == []                     # the fn was never CALLED
    assert repr(kind) in str(err.value)       # and the tag is named in the text


def test_d_refusal_is_a_marshal_error_so_existing_catchers_hold():
    # `SeamTagError` subclasses `SeamMarshalError`, so every existing catcher
    # (and the provider's marshalling of a failed call) still sees a refusal.
    b = _bridge()
    assert issubclass(b.SeamTagError, b.SeamMarshalError)
    with pytest.raises(b.SeamMarshalError):
        b._decode_value({"$kind": "_record"}, _emitted_module())


def test_d_forged_kind_never_reaches_a_function_outside_exports():
    # THE pin for the authorisation bypass: `_invoke` decodes args BEFORE it
    # consults `exports`, so pre-fix this frame invoked `privileged` — a fn the
    # seam does not export — with an attacker-chosen argument. It must now be
    # refused, with no invocation, and answered rather than raised.
    b = _bridge()
    mod = _emitted_module()
    _INVOKED.clear()
    req = {"key": "svc", "method": "ping",
           "args": [{"$kind": "privileged", "$value": "victim"}]}
    reply = asyncio.run(b._invoke({"svc": object()}, {"svc": ["ping"]}, req, mod))
    assert _INVOKED == []
    assert reply["ok"] is False
    # answered, not raised, and the tag is scrubbed out of the text by the same
    # funnel every other failure crosses (item 421 F5) rather than echoed back.
    assert "SeamTagError" in reply["error"]
    assert "<redacted:arg>" in reply["error"]


def test_d_a_legitimate_tagged_arg_still_reaches_the_exported_method():
    # non-vacuity for the pin above: the same call path with a DECLARED case
    # decodes, passes the allow-list, and is delivered.
    b = _bridge()
    seen = []

    class _Svc:
        def ping(self, arg):
            seen.append(arg)
            return "pong"

    req = {"key": "svc", "method": "ping",
           "args": [{"$kind": "_Live", "$value": 3}]}
    reply = asyncio.run(b._invoke({"svc": _Svc()}, {"svc": ["ping"]}, req,
                                  _emitted_module()))
    assert reply == {"ok": True, "value": "pong"}
    assert len(seen) == 1 and isinstance(seen[0], _Live) and seen[0].value == 3


def test_d_legit_cases_still_decode():
    b = _bridge()
    mod = _emitted_module()
    assert b._decode_value({"$kind": "_Ok", "$value": 4}, mod).value == 4
    assert isinstance(b._decode_value({"$kind": "_Dead"}, mod), _Dead)
    nested = b._decode_value({"$kind": "_Live", "$value": {"$kind": "_Ok",
                                                          "$value": 1}}, mod)
    assert isinstance(nested, _Live) and nested.value.value == 1


def test_d_without_a_module_a_tagged_dict_still_passes_through():
    # the legacy serve form has no module to resolve tags against, so the
    # pre-existing pass-through is unchanged.
    b = _bridge()
    assert b._decode_value({"$kind": "anything"}, None) == {"$kind": "anything"}


def test_d_is_case_class_discriminates():
    # the predicate's discriminator is `__slots__`: emitted cases declare it, a
    # record does not — which is what keeps `$kind: "<record name>"` refused.
    b = _bridge()
    assert b._is_case_class(_Live) and b._is_case_class(_Dead)
    assert b._is_case_class(_Ok) and b._is_case_class(_Base)
    assert not b._is_case_class(_Rec)
    assert not b._is_case_class(object)
    assert not b._is_case_class(_Live(1))      # an instance, not the class
    assert not b._is_case_class(_record)       # a function
    assert not b._is_case_class(None)
