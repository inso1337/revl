"""Roadmap item 470 / issue #822: the exit criterion, as one executable claim.

The item's exit sentence is a single property over four dimensions:

    an action that exceeds its declared intent (a wider verb, a higher amount,
    a different tenant, or an extra capability) is refused with the intent it
    violated.

`tests/test_470_intent_refinement.py` covers the kernel and
`tests/test_470_intent_surface.py` covers the surface one behaviour at a time,
each with the needle its own slice introduced. What neither states is the
sentence itself: that ALL FOUR excess kinds refuse, in BOTH stating slots, and
that every one of those refusals carries the same three things an operator needs
to act on it. That is what regresses silently if a later change drops one
dimension or stops naming the declaration, so it is pinned here once, over a
parametrised matrix, rather than inferred from eight separate needles.

Three assertions make up "refused with the intent it violated":

  * the refusal names the OPERATION whose declaration was exceeded, so the
    reader knows which intent is being talked about;
  * it names the DECLARED value on the dimension that was exceeded, beside the
    requested one, so the reader can see the gap rather than be told there is
    one;
  * it points at the `within` clause itself, so the declaration is locatable in
    the source rather than only nameable.

Two controls keep the matrix honest, and both are the reason the refusals above
are evidence at all. Every refused program is compiled a SECOND time with the
declaration removed and must compile: the boundary it crosses is legitimate, and
the only thing that refuses it is the declaration it cannot be shown to refine.
And a crossing that genuinely refines the declaration compiles unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

# One provider, one declared operation, one crossing. `cap` is the capability
# the crossed operation is scoped to, which is where the action's OBJECT and
# AMOUNT are read from; `within` is the declaration; `acting` is what the
# crossing states about itself.
STEP = """
service Store {{ emission[{cap}] fn ingest(row: Str) -> Int }}
service Worker {{ emission fn run() -> Str{within} }}
component W requires fs: Store provides worker: Worker {{
  provide worker {{ fn run() {{ emit fs.ingest("row"){acting} return "k" }} }}
}}
"""

# The same program with the crossing in VALUE position, which is the slot a
# value-returning emission is written in. Both slots run the same per-crossing
# check, so the exit criterion has to hold in both or it holds in neither.
BINDING = """
service Store {{ emission[{cap}] fn ingest(row: Str) -> Int }}
service Worker {{ emission fn run() -> Str{within} }}
component W requires fs: Store provides worker: Worker {{
  provide worker {{ fn run() {{ let n = emit fs.ingest("row"){acting} return "k" }} }}
}}
"""

SHAPES = {"emit step": STEP, "let binding": BINDING}

DEFAULT_CAP = 'fs.write(path="/tmp/out")'
DEFAULT_WITHIN = ' within { object: fs.write(path="/tmp"), verbs: [ingest] }'
DEFAULT_ACTING = ' acting { verb: ingest }'

# The four excess kinds the exit sentence names. Each entry is the program that
# exceeds the declaration on exactly one dimension, plus the DECLARED value and
# the REQUESTED value the refusal has to put in front of the reader.
EXCESS = {
    "a wider verb": dict(
        within=' within { object: fs.write(path="/tmp"), verbs: [read] }',
        declared="does not permit",
        requested="performs `ingest`",
    ),
    "a higher amount": dict(
        cap='fs.write(path="/tmp/out", calls=5)',
        within=' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' ceilings: { calls: 1 } }',
        declared="above the declared ceiling `calls=1`",
        requested="spends `calls=5`",
    ),
    "a different tenant": dict(
        within=' within { object: fs.write(path="/tmp"), verbs: [ingest],'
               ' tenant: "eu" }',
        acting=' acting { verb: ingest, tenant: "us" }',
        declared="confined to tenant `eu`",
        requested="runs in tenant `us`",
    ),
    "an extra capability": dict(
        cap="db",
        declared='it names `fs.write(path="/tmp")`',
        requested="reaches `db`",
    ),
}

MATRIX = [
    pytest.param(shape, kind, id=f"{kind} ({slot})")
    for slot, shape in SHAPES.items()
    for kind in EXCESS
]


def _source(shape, case, *, declared: bool = True) -> str:
    return shape.format(
        cap=case.get("cap", DEFAULT_CAP),
        within=case.get("within", DEFAULT_WITHIN) if declared else "",
        acting=case.get("acting", DEFAULT_ACTING) if declared else "",
    )


@pytest.mark.parametrize("shape,kind", MATRIX)
def test_an_action_that_exceeds_its_declared_intent_is_refused(shape, kind):
    """The first half of the sentence: each of the four excess kinds refuses."""
    with pytest.raises(RevlError):
        compile_source(_source(shape, EXCESS[kind]), "<test>")


@pytest.mark.parametrize("shape,kind", MATRIX)
def test_the_refusal_names_the_intent_it_violated(shape, kind):
    """The second half, which is the half a predicate cannot carry: the refusal
    names the declaring operation, both values on the dimension exceeded, and
    the line the declaration is written on."""
    case = EXCESS[kind]
    with pytest.raises(RevlError) as exc:
        compile_source(_source(shape, case), "<test>")
    error = exc.value
    message = str(error)

    # the operation whose declaration was exceeded
    assert "exceeds the intent `Worker.run` declares" in message
    # the declared value, beside the requested one
    assert case["declared"] in message
    assert case["requested"] in message
    # the declaration itself, locatable in the source
    assert "The declaration is the `within` clause at line" in message
    # and the structured projection agents read, not only the rendered text
    assert error.category == "intent-refinement"


@pytest.mark.parametrize("shape,kind", MATRIX)
def test_the_declaration_is_what_refuses_it(shape, kind):
    """The control that makes the matrix above evidence rather than a tautology.

    Every refused program compiles once the declaration is removed, so none of
    the eight refusals comes from the boundary being crossed, the capability
    being reached, or the shape being written. Each comes from a declaration the
    crossing cannot be shown to refine, which is the whole claim.
    """
    assert compile_source(
        _source(shape, EXCESS[kind], declared=False), "<test>")


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES)
def test_a_crossing_that_refines_the_declaration_still_compiles(shape):
    """The other control: the rule admits the legitimate program. `/tmp/out` is
    inside the declared `/tmp` cone, `ingest` is a declared verb, no ceiling is
    stated and none is spent, and no tenant is declared."""
    assert compile_source(_source(shape, {}), "<test>")
