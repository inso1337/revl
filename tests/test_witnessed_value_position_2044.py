"""A `witnessed` extern called in a provide-method body's VALUE position is
refused — issue #2044.

Design: docs/design/243-witnessed-externs.md, rule 1 ("refused outside effect
position"). The rule was already enforced in a `fn`/`test` body
(`_refuse_effect_position_bound_externs_in_fn_body`), but item 318 — which made
a witnessed call legal in a provide-method body, the per-tool-call agent fs
mutation shape — never carried the rule into that stratum. So all three value
spellings compiled, fired the host mutation and registered NO inverse: no
`effect`/`let-effect` step is built, `witnessed_registered` never sees the
callee, and the auto-approve guarantee is silently downgraded to class (c)
prompt-per-call.

This is a diagnostics gap, not a soundness escape: `provided_classes` already
reported class (c) for these programs, it just never said so at the call site.
The fix is `_refuse_unpositioned_witnessed_call` (src/revl/lower.py), called
from the `ExprCall`/`ExprVar` arm of `_lower_component_pure_expr` — the one
funnel every value expression of a provide-method (and activation) body passes
through. The exemption is by IDENTITY, exactly as `_refuse_unbracketed_host_
acquire` reads `_host_acquire_root`: `_acquire_position` marks the bracket's own
acquisition expression, and only that object is admitted.

The toy witnessed extern is the rename-with-a-data-witness stand-in the rest of
the witnessed suites use.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.mcp.effect_classes import provided_classes

# ---------------------------------------------------------------------------
# the toy witnessed effect (a per-call file mutation with a declared inverse)
# ---------------------------------------------------------------------------

_PRELUDE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py { return Ok(Stash{path: p, bak: p}) }\n"
    "service Ops { emission fn stash(p: Str) }\n"
)

# `Ops.stash` is an `emission fn`, so the witnessed crossing stays visible to a
# consumer of `Ops`, and the method body declares no result — the totality rule
# (a body that never returns a value) is a separate slice and must not be what
# these programs are refused for.
_PROVIDE = (
    "component Agent provides ops: Ops {{\n"
    "  provide ops {{\n"
    "    fn stash(p) {{\n"
    "      {body}\n"
    "    }}\n"
    "  }}\n"
    "}}\n"
)

_VALUE_SPELLINGS = {
    # the three spellings from the issue: bind-then-discard, direct return, and
    # bind-then-return. All three fire the mutation and register nothing.
    "let": "let r = stash_path(p)",
    "return": "return stash_path(p)",
    "let-return": "let r = stash_path(p)\n      return r",
}


def _program(body: str) -> str:
    return _PRELUDE + _PROVIDE.format(body=body)


def _method_body(comp: dict, key: str, method: str) -> list:
    """The lowered step list of one provide-method, found in the `provide`
    body step's `methods` list."""
    for step in comp["body"]:
        if step.get("step") == "provide" and step.get("name") == key:
            for m in step["methods"]:
                if m["name"] == method:
                    return m["body"]
    raise AssertionError(f"no method {key}.{method}")


def _activation(comp: dict) -> list:
    """A component's activation steps — every body step that is not a
    `provide`."""
    return [s for s in comp["body"] if s.get("step") != "provide"]


# ---------------------------------------------------------------------------
# the gap: a value position in a provide-method body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", sorted(_VALUE_SPELLINGS))
def test_witnessed_in_a_provide_method_value_position_is_refused(spelling):
    with pytest.raises(RevlError) as ei:
        compile_source(_program(_VALUE_SPELLINGS[spelling]), "t.rvl")
    err = ei.value
    assert err.code == "G4"
    assert "witnessed extern `stash_path` cannot be called in a value position" in str(err)
    assert "a witnessed mutation is only valid in effect position" in str(err)
    # the hint must point at the SPELLING that works here. Unlike a plain `fn`
    # body, `effect` IS available in a provide method, so the author has a move.
    assert "spell it `effect stash_path(…)` here" in str(err)


def test_plain_fn_body_and_provide_method_now_agree_on_the_rule():
    # Before the fix the two strata disagreed: the `fn` body was refused and the
    # provide-method body was not, so the same call read as legal or illegal
    # depending on where the author put it. Both are now refused, under the same
    # code and the same rule, differing only in the position they name.
    with pytest.raises(RevlError) as fn_err:
        compile_source(
            _PRELUDE + "fn helper(p: Str) -> Result[Stash, FsError]"
                      " { return stash_path(p) }\n",
            "t.rvl")
    with pytest.raises(RevlError) as method_err:
        compile_source(_program("return stash_path(p)"), "t.rvl")
    assert fn_err.value.code == method_err.value.code == "G4"
    assert "cannot be called in the body of fn `helper`" in str(fn_err.value)
    assert "cannot be called in a value position" in str(method_err.value)
    for err in (fn_err.value, method_err.value):
        assert "a witnessed mutation is only valid in effect position" in str(err)


def test_nested_witnessed_call_below_the_root_is_also_refused():
    # The exemption is the bracket's OWN root acquisition, not its whole
    # expression tree: `wrap` is the root here, so the witnessed argument is
    # still a value position. The trailing `effect` keeps the body from being
    # refused by the unrelated totality rule (a body that returns no value), so
    # on the unfixed tree this program compiles — the mutation fires and no
    # inverse is registered for the nested call.
    with pytest.raises(RevlError) as ei:
        compile_source(
            _PRELUDE + "extern pure fn wrap(w: Result[Stash, FsError])"
                      " -> Result[Stash, FsError] = @py { return w }\n"
                      + _PROVIDE.format(body="let r = wrap(stash_path(p))\n"
                                             "      effect stash_path(p)"),
            "t.rvl")
    assert ei.value.code == "G4"
    assert "cannot be called in a value position" in str(ei.value)


# ---------------------------------------------------------------------------
# the admitted forms keep their registered inverse
# ---------------------------------------------------------------------------

def test_effect_position_form_still_lowers_and_registers_its_inverse():
    # (b) of the required evidence: `effect stash_path(p)` is the position the
    # rule admits, and the step it builds is the one the accumulator registers
    # its inverse from (`acquire.kind == "fn"`, no site `undo`).
    ir = compile_source(_program("effect stash_path(p)"), "t.rvl")
    (comp,) = ir["components"]
    assert _method_body(comp, "ops", "stash") == [
        {"step": "effect",
         "acquire": {"kind": "fn", "name": "stash_path",
                     "args": [{"kind": "name", "id": "p"}]}},
    ]


def test_effect_position_form_reports_class_a_not_c():
    # The point of the fix: the class report and the call-site verdict now say
    # the same thing. The admitted spelling is class (a) with the witnessed
    # extern named as the raiser; the refused spellings never reach a class
    # report at all, where before they silently reached class (c).
    ir = compile_source(_program("effect stash_path(p)"), "t.rvl")
    (cls,) = [c for c in provided_classes(ir) if c["method"] == "stash"]
    assert cls["class"] == "a"
    (raised,) = cls["raisedBy"]
    assert raised["kind"] == "extern"
    assert raised["name"] == "stash_path"
    assert raised.get("registered", True) is not False


def test_effect_block_tail_acquisition_is_effect_position():
    # The block form's acquisition is its LAST statement's expression
    # (parser.py `effect_form`), so the tail is the bracketed root and lowers to
    # the `let-effect` step the accumulator registers from. The block's setup is
    # stratum-1 pure code and rides the step.
    ir = compile_source(
        _PRELUDE + (
            "component Agent provides ops: Ops {\n"
            "  let saved = effect {\n"
            '    var q = "initial"\n'
            "    stash_path(q)\n"
            "  }\n"
            "  provide ops {\n"
            "    fn stash(p) { effect stash_path(p) }\n"
            "  }\n"
            "}\n"
        ), "t.rvl")
    (comp,) = ir["components"]
    assert _activation(comp) == [
        {"step": "let-effect",
         "acquire": {"kind": "fn", "name": "stash_path",
                     "args": [{"kind": "name", "id": "q"}]},
         "bind": "saved",
         "setup": [{"step": "let", "name": "q",
                    "value": {"kind": "lit", "value": "initial"}}]},
    ]


def test_marked_emit_of_a_witnessed_call_is_out_of_scope():
    # `emit stash_path(p)` is a different position (an emission step, not an
    # acquisition) and stays admitted here — the emission path registers no
    # inverse either, and `provided_classes` reports it class (c) with
    # `registered: false`. Pinned so widening or narrowing that boundary is a
    # deliberate change rather than a side effect of this rule.
    ir = compile_source(_program("emit stash_path(p)"), "t.rvl")
    (comp,) = ir["components"]
    assert _method_body(comp, "ops", "stash") == [
        {"step": "emit",
         "expr": {"kind": "fn", "name": "stash_path",
                  "args": [{"kind": "name", "id": "p"}]}},
    ]
    (cls,) = [c for c in provided_classes(ir) if c["method"] == "stash"]
    assert cls["class"] == "c"
    assert cls["raisedBy"][0]["registered"] is False
