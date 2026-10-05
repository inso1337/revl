"""The approval ticket names the arguments whose DECLARED parameter is untrusted
(issue #1981, the runtime half of inso1337/revl-harness#26).

`taintOrigins` is `static_taint(component)` - a property of the COMPONENT, not of
the call. So an operator answering the class-(c) prompt reads
`taintOrigins: ["input"]` both for a call handing the capability a page-derived
value and for one handing it nothing but author-typed literals, and cannot tell
which argument in front of them is untrusted. The declared signature already
separates them - `shout(sink: Trusted[Str], msg: Untrusted[Str])` versus
`quiet(sink: Trusted[Str], msg: Trusted[Str])` - and issue #1937's IR keys carry
that qualifier across a boundary beside the stripped type, so the ticket reads
that statement instead of re-deriving it (`ClassMap.declared_untrusted_args`).

What the field names is the DECLARATION and never a flow-derived origin: the
origin label the issue also asks for (`web`) is a fact about flow that no IR key
records per argument, and a program the checker ADMITS - this issue's own repro is
one - never has the checker compute it at all. The last test pins that silence so
a later change cannot quietly imply a verified origin.

Analysis and disclosure only: no syntax, IR, emitter or self-host change, and the
ticket hash is unchanged (the field lands after it, as every other disclosure
field does).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp import approval  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402

#: the harness issue's repro, verbatim. Both operations cross the SAME class-(c)
#: capability (`announce`) and differ only in the declared trust of `msg`.
REPRO = """
extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return 'planted' }
extern emission fn announce(sink: Str, msg: Str) = @py { return }
service Ops {
  emission fn shout(sink: Trusted[Str], msg: Untrusted[Str])
  emission fn quiet(sink: Trusted[Str], msg: Trusted[Str])
}
component Agent provides ops: Ops {
  provide ops {
    fn shout(sink, msg) { emit announce(sink, msg) }
    fn quiet(sink, msg) { emit announce(sink, msg) }
  }
}
"""

#: the same wiring with every qualifier removed: a composition that declares
#: nothing untrusted must come out of this change untouched.
NO_QUALIFIER = """
extern emission fn announce(sink: Str, msg: Str) = @py { return }
service Ops { emission fn shout(sink: Str, msg: Str) }
component Agent provides ops: Ops {
  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }
}
"""

ARGS = ["/tmp/s.log", "planted-in-a-web-page"]


def _ticket(method: str, source: str = REPRO) -> dict:
    cm = ClassMap(compile_source(source, "arg_origin.rvl"))
    return cm.build_ticket(cm.classify_call("ops", method), list(ARGS))


def test_the_untrusted_argument_is_named_by_position_and_by_its_own_name():
    """`index` is the IR's own 0-based position in the declared parameter list
    (the issue calls `msg` "argument 1"); `name` is carried so no reader has to
    count - the two must agree, or the field is worse than useless."""
    args = _ticket("shout")["untrustedArguments"]
    assert args == [{"index": 1, "name": "msg"}], args


def test_a_trusted_only_operation_reports_no_untrusted_argument():
    """`quiet` declares `msg: Trusted[Str]`, so the honest answer is silence -
    the key is absent rather than an empty list, matching the IR's own "absent
    unless declared" rule and `taintOrigins`' omit-when-empty rule."""
    assert "untrustedArguments" not in _ticket("quiet")


def test_the_disclosure_is_the_only_field_that_separates_the_two_calls():
    """The issue's actual complaint, asserted directly: the two tickets carry the
    SAME component-level `taintOrigins`, and the new argument-level field is what
    now tells them apart. A change that made both tickets equal again - or that
    dropped `taintOrigins` instead of adding the position - reds here."""
    shout, quiet = _ticket("shout"), _ticket("quiet")
    assert shout["taintOrigins"] == quiet["taintOrigins"] == ["input"]
    assert shout.get("untrustedArguments") != quiet.get("untrustedArguments")
    assert shout["method"] != quiet["method"]  # the calls really are distinct


def test_the_disclosure_does_not_move_the_ticket_hash():
    """The field lands AFTER `build_ticket` computed `hash`, so a ticket's
    identity - which standing grants and recorded approvals are bound to - is
    unchanged by adding it. Proven by building the same ticket with the field
    suppressed and comparing: present-and-removed must hash the same."""
    ir = compile_source(REPRO, "arg_origin.rvl")
    cm = ClassMap(ir)
    reach = cm.classify_call("ops", "shout")
    with_args = cm.build_ticket(reach, list(ARGS))
    assert with_args["untrustedArguments"] == [{"index": 1, "name": "msg"}]

    class NoUntrustedArgs(approval.ClassMap):
        def declared_untrusted_args(self, component, key, method):
            return []

    without = NoUntrustedArgs(ir).build_ticket(reach, list(ARGS))
    assert "untrustedArguments" not in without
    assert with_args["hash"] == without["hash"]


def test_a_composition_declaring_no_qualifier_gains_no_field():
    """Non-vacuity in the other direction: the field is driven by the IR's
    per-parameter statement, not by the operation's name. An unqualified
    signature gains nothing - no `untrusted` flag on any parameter, no field."""
    ir = compile_source(NO_QUALIFIER, "no_qualifier.rvl")
    params = ir["services"]["Ops"]["methods"]["shout"]["params"]
    assert not any(p.get("untrusted") for p in params), params
    cm = ClassMap(ir)
    ticket = cm.build_ticket(cm.classify_call("ops", "shout"), list(ARGS))
    assert "untrustedArguments" not in ticket


def test_the_field_never_claims_a_flow_origin_it_did_not_verify():
    """The issue asks for "argument 1 (`msg`) is untrusted from `web`". The
    declaration can say the first half and not the second: the origin is computed
    by the checker's flow walk and raised in its own G9 refusal, and this program
    is ADMITTED, so no walk ran and no IR key records per-argument origin. Naming
    `web` here would be a verified-sounding claim about a value that is, in this
    very repro, an author-typed literal. Pin the honest silence."""
    args = _ticket("shout")["untrustedArguments"]
    assert "web" not in json.dumps(args)
    assert set(args[0]) == {"index", "name"}, args[0]
