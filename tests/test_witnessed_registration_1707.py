"""A witnessed extern is class (a) only where its inverse is REGISTERED
(issue #1707, the soundness half of the class-preserving relay).

`docs/design/243-witnessed-externs.md` rule 1 says a witnessed extern is
"refused outside effect position". The checker enforced that in a `fn` or `test`
body (G4, `lower.py`) but not in a PROVIDE-METHOD body, so

    provide ops { fn stash(p) { let r = stash_path(p) } }

compiled. The emitter registers an inverse off the STEP KIND — only an
`effect`/`let-effect` acquisition whose callee is the witnessed extern
(`_method_witnessed_step` in `backends/python/emit.py`) — so the `let` spelling
fired the host mutation and registered NOTHING. `ClassMap` nevertheless gave
the extern class (a) from its declaration alone, and the relay fold then
relaxed a relay over it to (a) as well: the relay auto-approved with zero
prompts and abort did not restore the file. That is the "D1" defect the guide
warns about, in the one spelling the checker did not refuse.

Issue #2044 closed that hole by extending rule 1 to a provide-method body: the
`let`/`return`/`let r = …; return r` spellings are now REFUSED at the call site
(their G4 diagnostic is pinned in
`tests/test_witnessed_value_position_2044.py`), so they can no longer be class
(a) or class (c) — they do not compile.

What this file guards is the registration fact that survives that refusal: a
witnessed extern reached where nothing registers. The spelling that still
compiles is a marked `emit` of the extern, which builds an emission step and
not an effect step, so the inverse is still never registered and the crossing
is still class (c):

    provide ops { fn stash(p) { emit stash_path(p) } }

Registration is a property of the call site, so the class is now a property of
the call site too: `Composition.witnessed_registered` reads exactly the test
the emitter registers on, `ClassMap` gives a witnessed extern class (a) only
where its name is in that set, and the erase report's `_crossings` folds the
unregistered case into the irreversible bucket. Both folds read the one
predicate, so they cannot disagree (the item-414 discipline), and the predicate
is the emitter's own, so it can only ever UNDER-claim registration: a relay over
a crossing without a registered inverse can never become class (a) or (b).

Nothing here is a relaxation. The `effect`-position spelling keeps class (a),
and the run-level tests in `tests/test_class_preserving_relay_1707.py` still
prove its derived inverse undoes the effect.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import effect_classes  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402

_HEAD = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
)

_TAIL = (
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "service Relay {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn stash_and_shout(p: Str, sink: Str)\n"
    "}\n"
)


def _source(body: str) -> str:
    return _HEAD + _TAIL + (
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn stash(p) { " + body + " }\n"
        "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
        "  }\n"
        "}\n"
        "component Front requires ops: Ops provides relay: Relay {\n"
        "  provide relay {\n"
        "    fn stash(p) { emit ops.stash(p) }\n"
        "    fn stash_and_shout(p, sink) {\n"
        "      emit ops.stash(p)\n"
        "      emit ops.shout(sink, p)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


#: the value-position spellings issue #2044 now REFUSES: a witnessed mutation
#: is only valid in effect position, so these do not compile (G4).
REFUSED = {
    "let": "let r = stash_path(p)",
    "return": "return stash_path(p)",
    "let_return": "let r = stash_path(p)\n return r",
}

#: the one spelling that registers, and so keeps class (a).
REGISTERED = "effect stash_path(p)"

#: a witnessed extern reached where nothing registers, which still compiles: a
#: marked `emit` builds an emission step, not an effect step. This is the
#: crossing the class map, the erase report and the relay fold must keep (c).
UNREGISTERED = "emit stash_path(p)"


def _ir(body: str) -> dict:
    return compile_source(_source(body), "witnessed_registration.rvl")


def _class(key: str, method: str, ir: dict) -> str | None:
    return ClassMap(ir).classify_call(key, method)["class"]


def _reach(key: str, method: str, ir: dict) -> dict:
    return ClassMap(ir).classify_call(key, method)


# ---------------------------------------------------------------------------
# the registration fact itself
# ---------------------------------------------------------------------------

def test_the_effect_position_call_registers_and_the_others_do_not():
    """The predicate is read off the lowered steps, and it is exactly the
    emitter's own registration test."""
    from revl.query import Composition
    registered = Composition(_ir(REGISTERED))
    scopes = [s for s in registered.scopes.values()
              if s["kind"] == "provide-method" and s["key"] == "ops"
              and s["method"] == "stash"]
    assert len(scopes) == 1
    assert registered.witnessed_registered(scopes[0]["nodes"]) == {"stash_path"}

    # the `emit` spelling still compiles and still registers nothing
    index = Composition(_ir(UNREGISTERED))
    scopes = [s for s in index.scopes.values()
              if s["kind"] == "provide-method" and s["key"] == "ops"
              and s["method"] == "stash"]
    assert len(scopes) == 1
    assert index.witnessed_registered(scopes[0]["nodes"]) == set()

    # and the value spellings are refused outright (issue #2044), so they can
    # no longer register OR silently fail to
    for spelling, body in REFUSED.items():
        with pytest.raises(RevlError) as ei:
            _ir(body)
        assert "cannot be called in a value position" in str(ei.value), spelling


def test_the_registered_form_still_lowers_to_an_effect_step():
    """The registration the predicate reads is the `effect` step the emitter
    keys its transactional frame off — not a new spelling of it."""
    ir = _ir(REGISTERED)
    (comp,) = [c for c in ir["components"] if c["name"] == "Agent"]
    (scope,) = [s for s in comp["body"] if s["step"] == "provide"]
    (method,) = [m for m in scope["methods"] if m["name"] == "stash"]
    assert method["body"] == [
        {"step": "effect",
         "acquire": {"kind": "fn", "name": "stash_path", "args": [
             {"kind": "name", "id": "p"}]}}]


# ---------------------------------------------------------------------------
# the class: (a) only where the inverse is registered
# ---------------------------------------------------------------------------

def test_the_effect_position_call_keeps_class_a():
    ir = _ir(REGISTERED)
    assert _class("ops", "stash", ir) == "a"
    assert _class("relay", "stash", ir) == "a"


@pytest.mark.parametrize("spelling", sorted(REFUSED))
def test_a_value_position_witnessed_call_is_refused(spelling):
    """Issue #2044: a value position builds no effect step, so it is refused
    rather than compiled into a call whose inverse never registers."""
    with pytest.raises(RevlError) as ei:
        _ir(REFUSED[spelling])
    assert "cannot be called in a value position" in str(ei.value), spelling
    assert "effect position" in str(ei.value), spelling


def test_an_unregistered_witnessed_call_is_class_c():
    """A witnessed extern reached where nothing registers is as irreversible
    as an emission, whatever its declaration says."""
    assert _class("ops", "stash", _ir(UNREGISTERED)) == "c"


def test_a_relay_over_an_unregistered_witnessed_call_is_class_c():
    """THE NEGATIVE CASE the issue demands: a relay that reaches a crossing
    without a registered inverse must never become class (a) or (b)."""
    reach = _reach("relay", "stash", _ir(UNREGISTERED))
    assert reach["class"] == "c"
    assert reach["class"] not in ("a", "b")
    assert "fs" in reach["classC"]


def test_the_crossing_says_the_inverse_was_not_registered():
    reach = _reach("ops", "stash", _ir(UNREGISTERED))
    externs = [c for c in reach["crossings"] if c["kind"] == "extern"]
    assert len(externs) == 1
    assert externs[0]["name"] == "stash_path"
    assert externs[0]["class"] == "witnessed"
    assert externs[0]["actionClass"] == "c"
    assert externs[0]["registered"] is False


def test_the_registered_crossing_is_not_marked_unregistered():
    reach = _reach("ops", "stash", _ir(REGISTERED))
    externs = [c for c in reach["crossings"] if c["kind"] == "extern"]
    assert externs[0]["actionClass"] == "a"
    assert "registered" not in externs[0]
    assert reach["classC"] == set()


def test_a_relay_that_also_reaches_a_non_witnessed_crossing_is_still_class_c():
    """The issue's third exit test, on the unregistered fixture: the worst
    rule is unchanged, so the (c) of the `announce` emission still wins."""
    for body in (REGISTERED, UNREGISTERED):
        reach = _reach("relay", "stash_and_shout", _ir(body))
        assert reach["class"] == "c", body
        assert "announce" in reach["classC"], body


def test_the_helper_fn_spelling_is_refused_outright():
    """The other way to factor a witnessed call — behind a plain `fn` helper —
    never compiles: rule 1's effect-position refusal covers a `fn` body. Issue
    #2044 makes the provide-method body agree, so the two spellings now refuse
    with the same code and the same advice."""
    with pytest.raises(RevlError) as ei:
        compile_source(
            _HEAD
            + "fn helper(p: Str) -> Result[Stash, FsError] {"
              " return stash_path(p) }\n",
            "helper.rvl")
    assert "cannot be called in the body of fn `helper`" in str(ei.value)
    assert "effect position" in str(ei.value)


# ---------------------------------------------------------------------------
# the report: the rise is named, with the operation and the crossing
# ---------------------------------------------------------------------------

def test_dropping_the_effect_for_a_marked_emit_reports_the_class_rise():
    """Exit test (a) of the issue, as a diff against the running composition:
    the edit that drops the `effect` is reported, not silent."""
    before, after = _ir(REGISTERED), _ir(UNREGISTERED)
    out = effect_classes.report(after, before, against=True)
    assert {"key": "ops", "method": "stash", "component": "Agent",
            "before": "a", "after": "c"} in out["effectClassChanges"]
    assert {"key": "relay", "method": "stash", "component": "Front",
            "before": "a", "after": "c"} in out["effectClassChanges"]


def test_the_warning_names_the_operation_and_the_crossing():
    before, after = _ir(REGISTERED), _ir(UNREGISTERED)
    warnings = effect_classes.report(after, before, against=True)[
        "effectClassWarnings"]
    (warn,) = [w for w in warnings if w["key"] == "ops"]
    assert warn["code"] == "EFFECT_CLASS_ROSE"
    assert warn["before"] == "a" and warn["after"] == "c"
    assert "`ops.stash`" in warn["message"]
    assert "rose from class (a) to class (c)" in warn["message"]
    assert "`stash_path` (witnessed extern) in Agent" in warn["message"]
    assert "no inverse registered at this call site" in warn["message"]
    (crossing,) = warn["crossings"]
    assert crossing["kind"] == "extern" and crossing["name"] == "stash_path"
    assert crossing["actionClass"] == "c" and crossing["registered"] is False


def test_the_provided_classes_carry_the_registered_flag_and_the_reason():
    classes = {(o["key"], o["method"]): o
               for o in effect_classes.provided_classes(_ir(UNREGISTERED))}
    assert classes[("ops", "stash")]["class"] == "c"
    assert classes[("relay", "stash")]["class"] == "c"
    (raised,) = [c for c in classes[("ops", "stash")]["raisedBy"]
                 if c["kind"] == "extern"]
    assert raised["registered"] is False
    assert raised["text"].endswith("— no inverse registered at this call site")


def test_the_registered_form_reports_no_rise():
    """The control: the `effect` spelling is (a) on both sides, so an edit that
    leaves it alone reports nothing."""
    out = effect_classes.report(_ir(REGISTERED), _ir(REGISTERED), against=True)
    assert out["effectClassChanges"] == []
    assert out["effectClassWarnings"] == []


def test_factoring_the_witnessed_op_behind_a_relay_emission_reports_no_rise():
    """The class-preserving relay: an edit that adds `relay.stash` forwarding
    to the class-(a) op reports no change at all, which is what "preserving"
    means here."""
    before = compile_source(
        _HEAD
        + "service Ops { emission fn stash(p: Str) }\n"
        + "service Relay { emission fn stash(p: Str) }\n"
        + "component Agent provides ops: Ops {\n"
          "  provide ops { fn stash(p) { effect stash_path(p) } }\n"
          "}\n", "before.rvl")
    after = compile_source(
        _HEAD
        + "service Ops { emission fn stash(p: Str) }\n"
        + "service Relay { emission fn stash(p: Str) }\n"
        + "component Agent provides ops: Ops {\n"
          "  provide ops { fn stash(p) { effect stash_path(p) } }\n"
          "}\n"
          "component Front requires ops: Ops provides relay: Relay {\n"
          "  provide relay { fn stash(p) { emit ops.stash(p) } }\n"
          "}\n", "after.rvl")
    assert _class("relay", "stash", after) == "a"
    out = effect_classes.report(after, before, against=True)
    # the relay is NEW, and it arrives AT class (a) — the class of the op it
    # forwards to, not a (c) it would have had on its own
    assert [c for c in out["effectClassChanges"] if c["key"] == "relay"] == [
        {"key": "relay", "method": "stash", "component": "Front",
         "before": None, "after": "a"}]
    # and nothing ROSE: the refactor is class-preserving, so there is no warning
    assert [w for w in out["effectClassWarnings"] if w["key"] == "relay"] == []


# ---------------------------------------------------------------------------
# the erase report agrees with the class map
# ---------------------------------------------------------------------------

_ERASE_SOURCE = """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { return }
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py { return Ok({}) }
service Ops {
  emission fn stash(p: Str)
}
service Relay {
  emission fn stash(p: Str)
}
component Agent provides ops: Ops {
  isolate ops in realm("alpha")
  provide ops {
    fn stash(p) { %s }
  }
}
component Front requires ops: Ops provides relay: Relay {
  isolate ops in realm("alpha")
  isolate relay in realm("alpha")
  provide relay {
    fn stash(p) { emit ops.stash(p) }
  }
}
"""


def _erase_crossings(body: str) -> dict:
    from revl import erase_report
    ir = compile_source(_ERASE_SOURCE % body, "erase_registration.rvl")
    return erase_report.build_report(ir, "alpha", prove_residue=False)[
        "boundaryCrossings"]


def test_the_erase_report_counts_the_registered_witness_as_revertible():
    cross = _erase_crossings(REGISTERED)
    assert [(w["name"], w["actionClass"], w["revertible"])
            for w in cross["witnessed"]] == [("stash_path", "a", True)]
    assert cross["externs"] == []
    # the relay took the (a) of its target, so it is not a bare seam either
    assert cross["total"] == 0 and cross["bareTokens"] == []


def test_the_erase_report_counts_the_unregistered_witness_as_irreversible():
    """One predicate, two folds: the report cannot call revertible what the
    class map calls (c)."""
    cross = _erase_crossings(UNREGISTERED)
    assert cross["witnessed"] == []
    (host,) = cross["externs"]
    assert host["name"] == "stash_path" and host["class"] == "witnessed"
    assert host["actionClass"] == "c" and host["registered"] is False
    assert "not registered at this call site" in host["note"]
    assert sorted(cross["bareTokens"]) == ["emit:Front:ops.stash",
                                           "host:Agent:stash_path"]
    assert cross["total"] == 2


# ---------------------------------------------------------------------------
# the guide's printed claim (part 3 of #1707)
# ---------------------------------------------------------------------------


def test_the_guide_s_witnessed_spelling_classifies_as_the_guide_says():
    """`docs/harness-gate-guide.md` prints the witnessed spelling and its class.
    tests/test_doc_examples.py compiles that block but checks only its syntax, so
    the class it prints is checked here: a guide that told a reader a value
    spelling keeps the auto-approve would be the same silent downgrade in prose.
    Issue #2044 supersedes the three-spelling table, so the guide prints only the
    registering `effect` spelling and names the refused value spellings in prose.
    """
    doc = (ROOT / "docs" / "harness-gate-guide.md").read_text(encoding="utf-8")
    (block,) = [b for b in re.findall(r"^```revl\n(.*?)^```", doc, re.M | re.S)
                if "stash_path" in b and "enqueue" not in b]
    ir = compile_source(block, "harness-gate-guide.md")
    assert _class("ops", "stash", ir) == "a"
    # the guide no longer prints a value spelling as a compiling class (c) ...
    assert "let r = stash_path(p) }" not in block
    assert "return stash_path(p) }" not in block
    # ... but it still names the refused spellings and the rule that refuses them
    assert "let r = stash_path(p)" in doc
    assert "return stash_path(p)" in doc
    assert "only valid in effect position" in " ".join(doc.split())


# ---------------------------------------------------------------------------
# the run: an unregistered witness cannot auto-approve
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the run-level proof needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


def _session(body: str):
    from revl.mcp.session import Session
    session = Session()
    session.approval_policy = "auto"
    session.load(_ir(body), record=True)
    return session


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


@needs_cordis
def test_an_unregistered_witnessed_relay_prompts_instead_of_auto_approving(
        path):
    """THE SECURITY TEST. Before this fix the relay auto-approved with zero
    prompts and abort reported no residue while the file stayed renamed."""
    from revl.mcp.approval import ApprovalRequired
    session = _session(UNREGISTERED)
    with pytest.raises(ApprovalRequired):
        session.call("relay", "stash", [path])
    # nothing fired: the mutation never crossed the boundary
    assert os.path.exists(path) and not os.path.exists(path + ".bak")
    assert Path(path).read_text(encoding="utf-8") == "deliverable"
    owner = session._owner
    assert owner.prompts["perCall"] == 1
    assert owner.approvals["silent"] == 0
    session.abort()


@needs_cordis
def test_the_registered_relay_still_auto_approves_and_abort_undoes_it(path):
    """The control the fix must not disturb: class (a) still means no prompt,
    and the registered inverse still lands the file back."""
    session = _session(REGISTERED)
    session.call("relay", "stash", [path])
    assert not os.path.exists(path) and os.path.exists(path + ".bak")
    owner = session._owner
    assert owner.prompts["perCall"] == 0
    assert owner.approvals["silent"] == 1
    result = session.abort()
    assert result["aborted"] and result["noResidue"]
    assert Path(path).read_text(encoding="utf-8") == "deliverable"
    assert not os.path.exists(path + ".bak")
