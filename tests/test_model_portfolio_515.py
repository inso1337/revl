"""The model portfolio as a device-profiled placement (roadmap item 515,
issue #1189).

The executable spec for slice 1 of `docs/design/539-model-portfolio.md`: the
`device` clause on a `model role`, the ordered candidate set on a `route
model` arm, and the published definition of `placement_digest`.

These programs are inline for the reason `tests/test_model_placement_512.py`
states: `examples/` and `tests/fixtures/` are census corpus roots and
`selfhost/parser.rvl` does not parse `route model`, so an admitting fixture in
either would be a `false-reject` census entry on the day it landed.

The line this file cannot cross is stated once, here, and tested nowhere,
because no test can cross it either: a `device` clause is a DECLARATION about
hardware. Every assertion below is about a declaration, its arms and the
ceiling above it. Whether a GPU with 6144 MiB free exists on the machine that
runs the program is a fact no compiler reads.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

# Spelled here rather than imported, so this module still COLLECTS against a
# tree without the change: the non-vacuity run then reads as "the control
# passes and the portfolio tests fail", not as a collection error.
CODE = "G-MODEL-PLACE"
DEVICE_CLASSES = ("cpu", "gpu", "npu")

# The control: an item-512 program, unchanged by this item. It must compile
# both before and after, which is what makes the failure counts below mean
# something.
CONTROL = """
model role local on_device
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""

# The flagship admitted program: one member at two quantisation points, which
# is item 515's own argument - three placements and three load costs, not
# three models - written as two roles and one ordered candidate set.
PORTFOLIO = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | small,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""


def _refuses(src: str, name: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, name)
    return excinfo.value


# --------------------------------------------------------------------------
# The device profile: the demand a placement declares
# --------------------------------------------------------------------------

def test_the_control_still_compiles():
    """An item-512 program with no device clause and no candidate set."""
    ir = compile_source(CONTROL, "control.rvl")
    assert ir["components"][0]["name"] == "Classifier"


def test_a_device_profile_is_admitted_on_a_role():
    ir = compile_source(PORTFOLIO, "portfolio.rvl")
    assert ir["components"][0]["name"] == "Classifier"


def test_the_profile_reaches_the_role_table():
    from revl.model_route import roles
    from revl.parser import Parser

    table = roles(Parser(PORTFOLIO, "portfolio.rvl").parse())
    assert table["fast"].profile.device == "gpu"
    assert table["fast"].profile.memory_mib == 6144
    assert table["fast"].profile.quant == "q4_k_m"
    # A role with no clause carries no profile rather than a defaulted one: a
    # default would be a requirement nobody wrote.
    assert table["cloud"].profile is None


def test_an_unknown_device_class_is_refused():
    src = PORTFOLIO.replace("device gpu memory 6144", "device gpu0 memory 6144")
    err = _refuses(src, "device.rvl")
    assert "gpu0" in str(err)
    assert err.code == CODE
    for known in DEVICE_CLASSES:
        assert known in str(err)


def test_a_zero_memory_floor_is_refused():
    src = PORTFOLIO.replace("memory 6144", "memory 0")
    err = _refuses(src, "mem0.rvl")
    assert "memory 0" in str(err)
    assert err.code == CODE


def test_the_quantisation_tag_is_not_checked_against_a_vocabulary():
    """Open world, on purpose (item 538): a tag the compiler has never seen
    compiles, because nothing under the compiler learns what a quantisation
    is."""
    src = PORTFOLIO.replace("quant q4_k_m", "quant awq_w4a16_g128")
    assert compile_source(src, "tag.rvl")["components"][0]["name"] == "Classifier"


def test_a_device_clause_needs_all_three_parts():
    src = PORTFOLIO.replace("device gpu memory 6144 quant q4_k_m", "device gpu")
    _refuses(src, "partial.rvl")


def test_a_profile_on_an_off_device_role_is_admitted():
    """`off_device` names where the call goes, not that there is no device
    there. Refusing a profile on one would be a rule this item cannot
    justify."""
    src = PORTFOLIO.replace(
        "model role cloud off_device",
        "model role cloud off_device device gpu memory 81920 quant fp8")
    assert compile_source(src, "offdev.rvl")["components"][0]["name"] == "Classifier"


# --------------------------------------------------------------------------
# The candidate set: the scheduling surface, and what it may not do
# --------------------------------------------------------------------------

def test_the_candidate_set_reaches_the_route_table():
    from revl.model_route import check
    from revl.parser import Parser

    placed = check(Parser(PORTFOLIO, "portfolio.rvl").parse())
    arm = placed["Classifier"]["classify"]["confidential"]
    assert arm["candidates"] == ("fast", "small")
    # The head is unchanged, which is what item 514's ceiling reads.
    assert arm["role"] == "fast"
    assert arm["residence"] == "on_device"


def test_a_single_role_arm_still_reports_a_one_tuple():
    from revl.model_route import check
    from revl.parser import Parser

    placed = check(Parser(CONTROL, "control.rvl").parse())
    assert placed["Classifier"]["classify"]["confidential"]["candidates"] == \
        ("local",)


def test_any_available_role_is_refused_by_name():
    """The refusal `docs/design/531-model-placement.md` section 9 names: a
    placement must not be able to pick a role no arm names."""
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "confidential -> *")
    err = _refuses(src, "anyrole.rvl")
    assert err.code == CODE
    assert "any available role" in str(err)


def test_any_available_role_is_refused_in_the_fallback_position_too():
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "confidential -> fast | *")
    err = _refuses(src, "anyfallback.rvl")
    assert err.code == CODE
    assert "any available role" in str(err)


def test_a_repeated_candidate_is_refused():
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "confidential -> fast | small | fast")
    err = _refuses(src, "dup.rvl")
    assert err.code == CODE
    assert "twice" in str(err)


def test_an_undeclared_candidate_in_the_tail_is_refused():
    """Item 512 checked the head. The tail is where a scheduler actually goes,
    so the rule had to reach it."""
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "confidential -> fast | absent")
    err = _refuses(src, "undeclared.rvl")
    assert err.code == CODE
    assert "absent" in str(err)


def test_a_fallback_that_leaves_the_device_is_refused():
    """The fail-open shape this item exists to remove: the head stays on the
    device, the fallback does not, and the origin ceiling read only the
    head."""
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "confidential -> fast | cloud")
    err = _refuses(src, "escape.rvl")
    assert err.code == CODE
    assert "cloud" in str(err)
    assert "off_device" in str(err)


def test_a_candidate_set_spanning_two_residences_is_refused():
    """Same rule, on a non-confidential origin where the ceiling has nothing
    to say: a scheduler still may not choose the residence."""
    src = """
model role near on_device device cpu memory 512 quant int8
model role far off_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    public -> near | far
  }
  provide out { fn classify(text) = text }
}
"""
    err = _refuses(src, "residence.rvl")
    assert err.code == CODE
    assert "residence" in str(err)


def test_an_unprofiled_candidate_in_a_set_is_refused():
    src = PORTFOLIO.replace(
        "model role small on_device device cpu memory 512 quant int8",
        "model role small on_device")
    err = _refuses(src, "unranked.rvl")
    assert err.code == CODE
    assert "small" in str(err)
    assert "device profile" in str(err)


def test_a_candidate_set_may_mix_device_classes():
    """A GPU member and a CPU member for one origin is the portfolio's whole
    point; what may not differ is the residence."""
    ir = compile_source(PORTFOLIO, "mixed.rvl")
    assert ir["components"][0]["name"] == "Classifier"


def test_the_secret_origin_reaches_no_candidate():
    src = PORTFOLIO.replace("confidential -> fast | small",
                            "secret -> fast | small")
    err = _refuses(src, "secret.rvl")
    assert err.code == "G-SECRET-FLOW"


# --------------------------------------------------------------------------
# No IR: item 512's property, held by this item
# --------------------------------------------------------------------------

def test_a_profiled_placement_writes_no_ir():
    """A device profile and a candidate set are checked at admission and
    contribute nothing to the IR, exactly as `route model` did. Deleting the
    declarations leaves a byte-identical program."""
    stripped = """
service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  provide out { fn classify(text) = text }
}
"""
    assert compile_source(PORTFOLIO, "x.rvl") == \
        compile_source(stripped, "x.rvl")


# --------------------------------------------------------------------------
# What `placement_digest` is computed over (item 517's open ask)
# --------------------------------------------------------------------------

SUPPLY = {
    "role": "fast",
    "residence": "on_device",
    "device": "gpu",
    "memory_mib": 6144,
    "quantisation": "q4_k_m",
    "runtime_build": "llama.cpp-b4021",
    "weights_digest":
        "9f" * 32,
}


def test_the_field_list_is_published_and_ordered():
    from revl.model_profile import PLACEMENT_DIGEST_FIELDS

    assert PLACEMENT_DIGEST_FIELDS == (
        "role", "residence", "device", "memory_mib", "quantisation",
        "runtime_build", "weights_digest")


def test_the_preimage_is_exactly_these_bytes():
    from revl.model_profile import placement_preimage

    assert placement_preimage(SUPPLY) == (
        b"revl-placement-v1\n"
        b"role=fast\n"
        b"residence=on_device\n"
        b"device=gpu\n"
        b"memory_mib=6144\n"
        b"quantisation=q4_k_m\n"
        b"runtime_build=llama.cpp-b4021\n"
        b"weights_digest=" + b"9f" * 32 + b"\n")


def test_the_digest_is_sixty_four_lowercase_hex():
    from revl.model_profile import placement_digest

    digest = placement_digest(SUPPLY)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_two_quantisation_points_of_one_model_digest_differently():
    """The reason item 517 carries the field at all: same weights, same seed,
    same temperature, different distribution."""
    from revl.model_profile import placement_digest

    other = dict(SUPPLY, quantisation="q8_0", memory_mib=11264)
    assert placement_digest(SUPPLY) != placement_digest(other)


def test_the_runtime_build_moves_the_digest():
    from revl.model_profile import placement_digest

    other = dict(SUPPLY, runtime_build="llama.cpp-b4022")
    assert placement_digest(SUPPLY) != placement_digest(other)


def test_a_missing_field_is_refused_rather_than_skipped():
    from revl.model_profile import placement_preimage

    for name in list(SUPPLY):
        partial = {k: v for k, v in SUPPLY.items() if k != name}
        with pytest.raises(ValueError) as excinfo:
            placement_preimage(partial)
        assert name in str(excinfo.value)


def test_an_unknown_field_is_refused():
    from revl.model_profile import placement_preimage

    with pytest.raises(ValueError) as excinfo:
        placement_preimage(dict(SUPPLY, temperature="0.0"))
    assert "temperature" in str(excinfo.value)


def test_a_line_break_in_a_value_is_refused():
    """There is no escaping in this format, so a value carrying an LF could
    forge a field line."""
    from revl.model_profile import placement_preimage

    with pytest.raises(ValueError) as excinfo:
        placement_preimage(dict(SUPPLY, runtime_build="a\nrole=cloud"))
    assert "line break" in str(excinfo.value)


def test_the_declared_floor_is_the_demand_and_not_the_supply():
    """`declared_floor` publishes the three fields a provider compares its own
    published profile against. revl performs no such comparison: the other
    four members of the digest are fields it never learns."""
    from revl.model_profile import PLACEMENT_DIGEST_FIELDS, declared_floor
    from revl.model_route import roles
    from revl.parser import Parser

    table = roles(Parser(PORTFOLIO, "portfolio.rvl").parse())
    floor = declared_floor(table["fast"].profile)
    assert floor == {"device": "gpu", "memory_mib": 6144,
                     "quantisation": "q4_k_m"}
    assert set(floor) < set(PLACEMENT_DIGEST_FIELDS)
    assert declared_floor(table["cloud"].profile) is None


def test_nothing_in_the_compiler_computes_a_placement_digest():
    """The seam item 538 draws: revl binds the digest and never produces one.
    A compile that computed one would mean the compiler had learned what a
    quantisation is."""
    import revl.model_profile as mp

    calls = []
    original = mp.placement_digest
    mp.placement_digest = lambda fields: calls.append(fields) or original(fields)
    try:
        compile_source(PORTFOLIO, "nodigest.rvl")
    finally:
        mp.placement_digest = original
    assert calls == []
