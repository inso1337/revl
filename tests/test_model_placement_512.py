"""Model placement as a checked route condition (roadmap item 512, issue #1186).

The executable spec for slice 1 of `docs/design/531-model-placement.md`: the
`model role` declaration, the `route model on <action>` clause, and every
refusal the pair carries.

These programs are written INLINE rather than dropped in `examples/` or
`tests/fixtures/`, on purpose. Both directories are census corpus roots
(`tools/gate_reference_census.py` CORPUS_DIRS), and `selfhost/parser.rvl` does
not parse `route model` yet — so an admitting fixture in either place would be
a `false-reject` entry in the census the moment it landed. The self-host port
is slice 3; see the design doc's section 7.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import GUARANTEES, classify  # noqa: E402

# Spelled here rather than imported from `revl.model_route`, so this module
# still COLLECTS against a tree that has no such module. That is what makes the
# non-vacuity run readable: on the tree without this change the control below
# passes and every placement test fails, instead of the whole file erroring at
# import time. `test_the_module_constants_match` is the tie-back.
CODE = "G-MODEL-PLACE"
RESIDENCES = ("on_device", "off_device")

# The flagship: the roadmap's own exit test, in its own shape. An action that
# is routed for a confidential origin, and two roles that differ only in where
# they run.
PROGRAM = """
model role local on_device
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> %s,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""

# The same component with no placement at all. The CONTROL: it compiles on this
# tree and on the tree without this change, so a red here is the harness and
# not the feature.
NO_ROUTE = """
service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""


def _refusal(src, name="m.rvl"):
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, name)
    return classify(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------- the control


def test_a_component_with_no_placement_compiles():
    """Passes on this tree AND on the tree without `route model`."""
    ir = compile_source(NO_ROUTE, "control.rvl")
    assert [c["name"] for c in ir["components"]] == ["Classifier"]


# ------------------------------------------------- the roadmap's own exit test


def test_a_confidential_action_routed_to_a_cloud_role_does_not_compile():
    """Item 512's exit test, verbatim: the diagnostic names the action, the
    origin and the role."""
    record, text = _refusal(PROGRAM % "cloud")
    assert record["code"] == CODE == "G-MODEL-PLACE"
    assert "classify" in text          # the action
    assert "confidential" in text      # the origin
    assert "cloud" in text             # the role
    # and it says WHICH residence made it a refusal, so the fix is readable
    assert "off_device" in text


def test_the_same_action_routed_to_an_on_device_role_compiles():
    """The other half of the exit test: the refusal is about the PLACEMENT, not
    about routing a confidential origin at all."""
    ir = compile_source(PROGRAM % "local", "ok.rvl")
    assert [c["name"] for c in ir["components"]] == ["Classifier"]


# --------------------------------------------------------- the failure DIRECTION


def test_a_star_arm_to_an_off_device_role_is_admitted():
    """`* -> cloud` is legal, because `*` does not cover a confidentiality
    origin. This pins the READING of the catch-all: admitting the arm is only
    sound because the arm does not mean what it looks like it means."""
    src = """
    model role cloud off_device
    service Answer { fn classify(text: Str) -> Str }
    component Classifier provides out: Answer {
      route model on classify { * -> cloud }
      provide out { fn classify(text) = text }
    }
    """
    assert compile_source(src, "star.rvl")["components"][0]["name"] == "Classifier"


def test_an_undeclared_role_is_refused_not_assumed():
    """The fail-closed core: an unknown role has no residence, so the placement
    cannot be checked. It is refused, never defaulted to a permissive one."""
    src = PROGRAM % "local"
    record, text = _refusal(src.replace("* -> cloud", "* -> whatever"))
    assert record["code"] == CODE
    assert "whatever" in text
    assert "no declared model role" in text


def test_an_unknown_residence_is_refused():
    record, text = _refusal(
        (PROGRAM % "local").replace("model role cloud off_device",
                                    "model role cloud offdevice"))
    assert record["code"] == CODE
    assert "offdevice" in text
    for residence in RESIDENCES:
        assert residence in text


def test_a_role_declared_twice_is_refused():
    record, text = _refusal(
        (PROGRAM % "local").replace("model role local on_device",
                                    "model role local on_device\n"
                                    "model role local off_device"))
    assert record["code"] == CODE
    assert "declared twice" in text


def test_an_unknown_origin_class_is_refused_with_the_vocabulary():
    record, text = _refusal(
        (PROGRAM % "local").replace("confidential -> local", "confidentail -> local"))
    assert record["code"] == CODE
    assert "confidentail" in text
    # the vocabulary comes from the item-249 lattice, so it is named in full
    for origin in ("confidential", "input", "model", "net", "web", "fs"):
        assert origin in text


def test_the_same_origin_routed_twice_is_refused():
    record, text = _refusal(
        (PROGRAM % "local").replace("* -> cloud", "confidential -> local"))
    assert record["code"] == CODE
    assert "routed twice" in text


def test_an_empty_route_block_is_refused():
    src = """
    service Answer { fn classify(text: Str) -> Str }
    component Classifier provides out: Answer {
      route model on classify { }
      provide out { fn classify(text) = text }
    }
    """
    record, text = _refusal(src, "empty.rvl")
    assert record["code"] == CODE
    assert "names no role" in text


def test_two_route_blocks_for_one_action_are_refused():
    src = """
    model role local on_device
    service Answer { fn classify(text: Str) -> Str }
    component Classifier provides out: Answer {
      route model on classify { confidential -> local }
      route model on classify { * -> local }
      provide out { fn classify(text) = text }
    }
    """
    record, text = _refusal(src, "twice.rvl")
    assert record["code"] == CODE
    assert "routed twice" in text


def test_a_route_naming_no_action_of_the_component_is_refused():
    """The keyed-to-a-thing-that-outlived-it shape: a renamed method leaves a
    block that protects nothing while still reading as a placement."""
    record, text = _refusal(
        (PROGRAM % "local").replace("route model on classify",
                                    "route model on classifier"))
    assert record["code"] == CODE
    assert "classifier" in text
    assert "classify" in text  # the actions the component does declare


def test_a_route_after_an_action_is_refused_prelude_rule():
    src = """
    model role local on_device
    service Answer { fn classify(text: Str) -> Str }
    component Classifier provides out: Answer {
      provide out { fn classify(text) = text }
      route model on classify { confidential -> local }
    }
    """
    record, text = _refusal(src, "prelude.rvl")
    assert record["code"] == CODE
    assert "must precede every effect" in text


def test_route_model_is_refused_inside_a_method_body():
    src = """
    model role local on_device
    service Answer { fn classify(text: Str) -> Str }
    component Classifier provides out: Answer {
      provide out {
        fn classify(text) {
          route model on classify { confidential -> local }
          return text
        }
      }
    }
    """
    with pytest.raises(RevlError, match="not allowed inside a method body"):
        compile_source(src, "inmethod.rvl")


# ------------------------------------------- the secret origin has no placement


def test_the_secret_origin_has_no_role_at_any_residence():
    """A capability-bound secret never reaches a model prompt. G-SECRET-FLOW
    already names an LLM prompt as a disclosure sink for it, so an arm placing
    one contradicts a shipped guarantee rather than proposing a new policy."""
    for role, residence in (("local", "on_device"), ("cloud", "off_device")):
        src = f"""
        model role {role} {residence}
        service Answer {{ fn classify(text: Str) -> Str }}
        component Classifier provides out: Answer {{
          route model on classify {{ secret -> {role} }}
          provide out {{ fn classify(text) = text }}
        }}
        """
        record, text = _refusal(src, "secret.rvl")
        assert record["code"] == "G-SECRET-FLOW", (role, residence)
        assert "secret" in text and role in text


# ------------------------------------------------------------ what it does NOT do


def test_an_admitted_placement_writes_no_ir():
    """512 is a permission checked at admission, not a runtime selection (that
    is item 515). The clause therefore contributes NOTHING to the IR, and an
    admitted program is byte-identical to the same program without it — which
    is why no emitter changed in this slice."""
    with_route = compile_source(PROGRAM % "local", "same.rvl")
    without = compile_source(
        """
        model role local on_device
        model role cloud off_device
        service Answer { fn classify(text: Str) -> Str }
        component Classifier provides out: Answer {
          provide out { fn classify(text) = text }
        }
        """.replace("        ", ""), "same.rvl")
    assert with_route == without


def test_a_declared_role_alone_writes_no_ir_and_needs_no_route():
    """A role is a declaration, not a requirement: declaring one and routing
    nothing is legal (items 515/516 bind roles a component does not itself
    route)."""
    before = compile_source(NO_ROUTE, "roles.rvl")
    after = compile_source("model role local on_device\n" + NO_ROUTE, "roles.rvl")
    assert before == after


# ------------------------------------------------------------- the code registry


def test_the_module_constants_match():
    """This file spells the code and the vocabulary; `revl.model_route` owns
    them. Pin the two together so the local copies cannot drift."""
    from revl import model_route
    assert model_route.CODE == CODE
    assert model_route.RESIDENCES == RESIDENCES
    assert model_route.CATEGORY == "model-placement"


def test_the_guarantee_code_is_registered_and_explains_itself():
    from revl.diagnostics import FIXES, explain
    assert CODE in GUARANTEES
    assert CODE in FIXES
    assert explain(CODE)["guarantee"] == GUARANTEES[CODE]
