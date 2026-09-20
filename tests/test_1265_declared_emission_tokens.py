"""A service method that spells `emission` bare declares no reach (issue #1265).

Issue #1223's rule is that an undeclared authority is not an empty one. A
service method spelling `emission` with NO capability list is the `key:` element
of that rule: a declared WIRING with an undeclared REACH. PR #1264 measured what
refusing it costs in the checker, took the narrower arm, and pinned the residual
rather than assuming it away. The repair the measurement pointed at is on the
COMPOSITION side and is what this file covers: the shipped services declare the
boundary they cross, so the element the checker would have to refuse is not
there to refuse.

The ordering matters and it is the decision. Tightening the checker first starts
refusing programs that were admitted yesterday, on a surface people are using,
as a side effect of an unrelated item. Declaring the tokens first is inert for
what is admitted today (`tools/gate_reference_census.py --check` reports no
change from the baseline) and leaves the checker change with nothing to break.

Nothing here registers a guarantee code. The refusal a declared token produces
is `G4`, the capability-scoped emission bound `docs/capabilities.md` has carried
since item 343.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl._paths import stdlib_root  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# --------------------------------------------------------------------------- #
# NON-VACUITY. A declared token has to BOUND something, or it is a sentence that
# reads as a declaration and refuses nothing - which is worse than the bare
# spelling, because a reader believes it.
#
# The differential is one source and two declarations of the same method. The
# body crosses two host boundaries; the declaration names one.
# --------------------------------------------------------------------------- #

_TWO_BOUNDARIES = """
extern emission fn host_read(path: Str) -> Str = @py {{ return "" }}
extern emission fn host_print(text: Str) = @py {{ return }}

service Workspace {{
  {decl} fn read(path: Str) -> Str
}}

component Leaky provides ws: Workspace {{
  provide ws {{
    fn read(path) {{
      emit host_print(path)
      return emit host_read(path)
    }}
  }}
}}
"""


def test_the_declared_token_refuses_a_provider_that_crosses_past_it():
    """WITH the token: refused, naming the boundary the body reached past it."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_TWO_BOUNDARIES.format(decl="emission[host_read]"),
                       "workspace.rvl")
    assert excinfo.value.code == "G4"
    assert "emission[host_read]" in excinfo.value.message
    assert "host_print" in excinfo.value.message


def test_the_same_provider_is_admitted_under_the_bare_spelling():
    """WITHOUT it: admitted. This is the half that makes the test above a
    measurement rather than an assertion - the bare spelling promises nothing,
    so the second boundary is not a refusal, and the token is what turns it
    into one."""
    doc = compile_source(_TWO_BOUNDARIES.format(decl="emission"),
                         "workspace.rvl")
    assert {c["name"] for c in doc["components"]} == {"Leaky"}


# --------------------------------------------------------------------------- #
# THE LOAD-BEARING ONE. `stdlib/admit.rvl` is the in-language admit crossing
# every `mcp.session.admit` turn rides, so its spelling is an operator-facing
# surface rather than an internal one.
# --------------------------------------------------------------------------- #

_TURN_THROUGH_THE_DECIDER = (
    "service Turn { emission[admission] fn run(s: Str) -> Str }\n"
    "component TurnComp requires admission: Admission provides turn: Turn {\n"
    '  provide turn { fn run(s) = emit admission.admit(s, ["Ops"]) }\n'
    "}\n"
)


def _admit_composition(tmp_path):
    base = os.path.join(str(tmp_path), "base.rvl")
    return compile_files([base, str(stdlib_root() / "admit.rvl")],
                         sources={base: _TURN_THROUGH_THE_DECIDER})


def test_the_admit_crossing_names_its_boundary_on_the_audit_surface(tmp_path):
    """What changes for an operator, stated rather than inherited.

    Before: a component crossing `admission.admit` reported its capability as
    `*` - the unnameable host boundary, which no held set covers and which is
    disjoint from nothing - because the service method said `emission` and
    stopped there. After: it reports `host_admit`, the boundary the shipped
    `AdmitGate` actually reaches, and the compiler has CHECKED that claim
    (G4) rather than believed it."""
    report = audit_report(_admit_composition(tmp_path))
    caps = report["boundary"]["TurnComp"]["capabilities"]["admission.admit"]
    assert caps == ["host_admit"], caps
    assert "*" not in caps


def test_a_second_provider_of_admission_may_not_widen_the_decider(tmp_path):
    """The token is a bound on every provider of `Admission`, not a label on
    the shipped one: a replacement decider that reaches a second host boundary
    is refused, which is the property the bare spelling could not state."""
    src = (
        "extern emission fn side_channel(s: Str) = @py { return }\n"
        "component OtherGate provides admission2: Admission {\n"
        "  provide admission2 {\n"
        "    fn admit(source, granted) {\n"
        "      emit side_channel(source)\n"
        '      return ""\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    base = os.path.join(str(tmp_path), "other.rvl")
    with pytest.raises(RevlError) as excinfo:
        compile_files([base, str(stdlib_root() / "admit.rvl")],
                      sources={base: src})
    assert excinfo.value.code == "G4"
    assert "side_channel" in excinfo.value.message


# --------------------------------------------------------------------------- #
# THE RATCHET. The point of the item is that these particular services stopped
# spelling `emission` bare; a later edit that puts one back should say so.
# Named one by one rather than scanned, because the set is a decision (several
# services on the tree are DELIBERATELY left undeclared, and the PR says which
# and why) and a scan would quietly adopt whatever the tree happens to hold.
# --------------------------------------------------------------------------- #

_DECLARED = {
    "stdlib/admit.rvl": [("Admission", "admit", ("host_admit",))],
    "src/revl/truc/components/workspace.rvl": [
        ("Workspace", "read", ("host_read",)),
        ("Workspace", "print", ("host_print",)),
        ("Workspace", "apply", ("host_apply",)),
    ],
    "src/revl/truc/components/gatekeeper.rvl": [
        ("Gate", "admit_all", ("host_admit_all",)),
    ],
    "src/revl/truc/components/cli.rvl": [("Cli", "run", ("asm", "ship"))],
    "examples/user_cache.rvl": [("Cache", "put", ("db",))],
}


@pytest.mark.parametrize("path", sorted(_DECLARED))
def test_the_shipped_service_declares_its_capability_tokens(path):
    from revl.parser import Parser
    text = (ROOT / path).read_text(encoding="utf-8")
    program = Parser(text, path).parse()
    services = {s.name: s for s in program.services}
    for service_name, method_name, expected in _DECLARED[path]:
        method = services[service_name].methods[method_name]
        assert method.emission, f"{service_name}.{method_name} is not an emission"
        assert method.capabilities is not None, (
            f"{service_name}.{method_name} spells `emission` bare; an "
            f"undeclared reach is not an empty one (issue #1265)")
        assert tuple(method.capabilities) == expected
