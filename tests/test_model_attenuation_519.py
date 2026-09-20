"""The model role in the capability attenuation product (item 519, issue #1193).

The executable spec for slice 1 of `docs/design/539-model-in-attenuation.md`:
the `reaches [...]` clause on `model role`, the effective-ceiling fold, and the
one refusal it carries.

These programs are written INLINE rather than dropped in `examples/` or
`tests/fixtures/`, for the reason section 6.1 of
`docs/design/531-model-placement.md` gives: both directories are census corpus
roots (`tools/gate_reference_census.py` CORPUS_DIRS) and `selfhost/parser.rvl`
does not parse `model role` at all, so an admitting fixture in either place
would be a `false-reject` census entry the moment it landed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import GUARANTEES, classify  # noqa: E402

# Spelled here rather than imported from `revl.model_route`, so this module
# still COLLECTS against a tree that has no reach clause. That is what makes
# the non-vacuity run readable: the controls pass on the old tree and every
# reach test fails, instead of the file erroring at import.
CODE = "G-MODEL-PLACE"

# A component that really consults a model: it wires a service whose emission
# method declares a `model.*` token, and its provider body crosses that
# boundary. `%s` is the role's reach clause, the one thing under test.
CONSULTS = """
model role local on_device %s

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  route model on classify { * -> local }
  provide out { fn classify(text) = emit llm.complete(text) }
}
"""

# CONTROL A: the same component with no `route model` block and no role. It
# compiles on this tree and on the tree without this change.
NO_ROLE = """
service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  provide out { fn classify(text) = emit llm.complete(text) }
}
"""

# CONTROL B: item 512's own shape - a routed component that crosses NOTHING.
# It compiles on both trees too, which is what keeps this slice free of a
# migration (design note section 6.1).
ROUTES_BUT_CROSSES_NOTHING = """
model role local on_device
service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify { * -> local }
  provide out { fn classify(text) = text }
}
"""


def _refusal(src, name="m.rvl"):
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, name)
    return classify(excinfo.value), str(excinfo.value)


def _reach_record(src, name="m.rvl"):
    ir = compile_source(src, name)
    return (ir.get("manifest") or {}).get("model_reach")


# ------------------------------------------------------------------ controls


def test_a_model_consuming_component_with_no_role_compiles():
    """CONTROL A. Passes on this tree AND on the tree without this change."""
    ir = compile_source(NO_ROLE, "control_a.rvl")
    assert [c["name"] for c in ir["components"]] == ["Classifier"]
    assert "model_reach" not in (ir.get("manifest") or {})


def test_a_routed_component_that_crosses_nothing_compiles():
    """CONTROL B. A role can only steer an action that reaches a boundary, so
    a component holding none has no ceiling for a role to widen. This is the
    shape every item-512 fixture has, which is why none of them migrate."""
    ir = compile_source(ROUTES_BUT_CROSSES_NOTHING, "control_b.rvl")
    assert [c["name"] for c in ir["components"]] == ["Classifier"]
    assert "model_reach" not in (ir.get("manifest") or {})


# ------------------------------------------------- the roadmap's own exit test


def test_a_role_reaching_past_its_component_does_not_compile():
    """Item 519's exit test: a role reaching a capability the component does
    not hold is refused, and the diagnostic names BOTH sets."""
    record, text = _refusal(CONSULTS % "reaches [shell.exec]")
    assert record["code"] == CODE == "G-MODEL-PLACE"
    assert "Classifier" in text           # the component
    assert "classify" in text             # the action
    assert "local" in text                # the role
    assert "shell.exec" in text           # what the role reaches
    assert "model.complete" in text       # what the component holds


def test_a_role_reaching_no_further_than_its_component_compiles():
    """The other half: the refusal is about the WIDENING, not about declaring
    a reach at all."""
    rows = _reach_record(CONSULTS % "reaches [model.complete]")
    assert rows == [{
        "component": "Classifier", "action": "classify", "origin": "*",
        "role": "local", "residence": "on_device",
        "holds": ["model.complete"], "reaches": ["model.complete"],
        "effective": ["model.complete"], "attenuated": [],
        "reach_declared": True,
    }]


def test_a_narrower_role_attenuates_and_the_record_names_the_drop():
    """The roadmap's `attenuates to the intersection` clause, as the product
    RECORD rather than as a refusal: a role that reaches less than its
    component holds is admitted, and what it does not reach is named."""
    rows = _reach_record("""
model role local on_device reaches []

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  route model on classify { * -> local }
  provide out { fn classify(text) = emit llm.complete(text) }
}
""")
    assert rows[0]["reaches"] == []
    assert rows[0]["attenuated"] == ["model.complete"]
    assert rows[0]["reach_declared"] is True


# ------------------------------------------------------- the failure DIRECTION


def test_an_undeclared_reach_is_refused_not_read_as_empty():
    """THE fail-closed core of the item. A role that declares no reach is not
    an inert role: reading silence as `reaches nothing` would make an unknown
    model contribute nothing to the product, which is the fail-OPEN shape the
    item exists to remove."""
    record, text = _refusal(CONSULTS % "")
    assert record["code"] == CODE
    assert "declares no reach" in text
    # and it says what to write, rather than claiming the author wrote `[*]`
    assert "reaches [...]" in text


def test_a_declared_unbounded_reach_is_refused_and_says_what_was_written():
    """`reaches [*]` is the honest spelling of the unbounded role. It is
    refused the same way, but the diagnostic must not accuse the author of
    silence: they wrote the clause."""
    record, text = _refusal(CONSULTS % "reaches [*]")
    assert record["code"] == CODE
    assert "reaches [*]" in text
    assert "declares no reach" not in text


# A component that consults a model AND holds a parameterised file boundary,
# so the reach fold has a valuation to compare. The model wiring is what makes
# the product ask the question at all (`_consults_a_model`); the `fs.write`
# valuation is what it compares.
PARAMETERISED = """
model role local on_device reaches [%s]

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Store { emission[fs.write(path="/tmp")] fn put(row: Str) -> Str }
service Answer { emission[llm, st] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions requires st: Store
                     provides out: Answer {
  route model on classify { * -> local }
  provide out {
    fn classify(text) {
      let stored = emit st.put(text)
      return emit llm.complete(stored)
    }
  }
}
"""


def test_a_dropped_capability_parameter_widens():
    """The fold is `cap_order.covers`, not set membership: a component holding
    `fs.write(path="/tmp")` does not cover a role reaching bare `fs.write`."""
    record, text = _refusal(PARAMETERISED % "fs.write")
    assert record["code"] == CODE
    assert "fs.write" in text


def test_a_narrower_parameter_on_the_role_is_admitted():
    """The same pair the other way round: a role reaching a path the component
    holds is narrowing, and narrowing is sound."""
    rows = _reach_record(PARAMETERISED % 'fs.write(path="/tmp/jobs")')
    assert rows[0]["role"] == "local"
    assert rows[0]["reaches"] == ['fs.write(path="/tmp/jobs")']


def test_every_named_role_is_folded_in_not_only_the_first():
    """Design note decision 4: slice 1 folds every role the block NAMES, so a
    block whose SECOND arm reaches too far is refused too. Which role a given
    crossing actually reaches is slice 2."""
    src = """
model role local on_device reaches [model.complete]
model role wide  on_device reaches [shell.exec]

service Completions { emission[model.complete] fn complete(p: Str) -> Str }
service Answer { emission[llm] fn classify(text: Str) -> Str }

component Classifier requires llm: Completions provides out: Answer {
  route model on classify { confidential -> local, * -> wide }
  provide out { fn classify(text) = emit llm.complete(text) }
}
"""
    record, text = _refusal(src)
    assert record["code"] == CODE
    assert "wide" in text and "shell.exec" in text


# ----------------------------------------------------------------- the surface


def test_the_reach_clause_is_optional_and_contextual():
    """`reaches` is read only in this slot, so a program that uses it as an
    ordinary name keeps parsing and the self-hosted lexer needs no sync."""
    src = """
service Answer { fn classify(reaches: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(reaches) = reaches }
}
"""
    assert compile_source(src, "ctx.rvl")["components"][0]["name"] == "Classifier"


def test_a_duplicate_capability_in_the_reach_set_is_refused():
    """A reach is a set, so naming one capability twice is a typo, not an
    idempotent write."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(CONSULTS % "reaches [model.complete, model.complete]",
                       "dup.rvl")
    assert "duplicate capability" in str(excinfo.value)


def test_an_unknown_capability_parameter_refuses_at_parse():
    """The reach tokens funnel through `_capability_params`, so the closed
    parameter registry refuses HERE rather than going silently inert."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(CONSULTS % 'reaches [fs.write(pth="/tmp")]', "par.rvl")
    assert "pth" in str(excinfo.value)


# --------------------------------------------------------------- the registry


def test_no_new_guarantee_code_is_invented():
    """Section 3.2: the refusal reuses item 512's registered code, so item
    523's generated tier matrix needs no new ACKNOWLEDGED entry."""
    assert CODE in GUARANTEES
    assert ("a role reaches no capability the component routing through it "
            "holds") in GUARANTEES[CODE]


def test_the_module_reads_silence_as_the_unnameable_boundary():
    """The tie-back for the constant this file spells by hand."""
    from revl import model_route

    assert model_route.UNDECLARED_REACH == ("*",)
    role = model_route.Role("r", "on_device", 1, None)
    assert role.reach_declared is False
    assert role.reach_tokens == ("*",)
    declared = model_route.Role("r", "on_device", 1, ())
    assert declared.reach_declared is True
    assert declared.reach_tokens == ()
