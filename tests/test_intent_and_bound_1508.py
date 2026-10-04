"""A spawn-handle crossing is named by its op's declared scope. Issue #1508.

Item 470's intent check read `_emit_crossed_caps`, which resolves no
spawn-handle crossing, so `emit w.task.run(p) acting { verb: run }` under
`within { object: net, verbs: [run] }` was refused as "an unnameable boundary"
although `Task.run` is declared `emission[net]`, while the same crossing
through a `requires` key was admitted. Design note 470 section 3.3 listed the
handle among the shapes "the resolution returns nothing" for; that was true of
the code then and not of the crossing. The op's declared scope is a fact the
spawned provider is held to by its own G4 provider bound, the same fact the
`requires` twin relies on, and #1519 and #1680 to #1684 already resolve these
crossings to it for the marker and the approval floor.

Decided: the intent check resolves through the one shared resolver
(`lower._resolved_crossed_caps`). And, for consistency with issue #1682's
parameter, the provider upper bound reads every resolved crossing (a direct
handle, an alias, a service-typed local, a receiver written in place) at the
op's declared scope, `*` for a bare op, in every position it is written.

The intent rows are inline: the self-host gate does not parse `within` or
`acting` (it answers `BAD|bad method signature`, out of slice for item 470),
so a corpus document would read as a false reject. The provider-bound rows are
the corpus in tests/fixtures/handle_provider_bound/, which the gate decides.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
BOUND = ROOT / "tests" / "fixtures" / "handle_provider_bound"

_WORKER = """service Task {
  %s fn run(p: Str) -> Int
}
component Worker provides task: Task {
  provide task { fn run(p) = 1 }
}
"""


def _sup(body: str, *, op: str = "emission[net]", bound: str = "emission") -> str:
    return (_WORKER % op) + f"""service Sup {{
  {bound} fn go(p: Str) -> Int within {{ object: net, verbs: [run] }}
}}
component Supervisor provides sup: Sup {{
  provide sup {{
    fn go(p: Str) {{
      let w = effect spawn Worker with {{ }} undo w.dispose()
{body}      return 0
    }}
  }}
}}
"""


STEP = "      emit w.task.run(p) acting { verb: run }\n"
ALIAS = "      let t = w.task\n      emit t.run(p) acting { verb: run }\n"
_EXCEEDS = "this `emit` exceeds the intent `Sup.go` declares: "

INTENT_ROWS = {
    # the reproducer: refused as unnameable before, admitted now
    "reproducer": (_sup(STEP), None),
    "alias": (_sup(ALIAS), None),
    # the check still judges the verb and the object
    "wrong_verb": (_sup(STEP.replace("verb: run", "verb: delete")),
                   _EXCEEDS + "the action performs `delete` on `net`, which "
                   "the declared intent does not permit there"),
    "wider_object": (_sup(STEP, op="emission[shell.exec]"),
                     _EXCEEDS + "the action reaches `shell.exec`, a capability "
                     "the declared intent does not name (it names `net`)"),
    "no_acting": (_sup("      emit w.task.run(p)\n"),
                  "`Sup.go` declares an intent (`within` at line 8), so this "
                  "`emit` must state what it does with `acting { … }`"),
    # a bare op still names nothing the declaration can be shown to cover
    "bare_op": (_sup(STEP, op="emission"),
                "this `emit` crosses an unnameable boundary, and the intent "
                "`Sup.go` declares authorizes `net`"),
    # the parent's own bound covers the op's scope: admitted
    "bound_covers": (_sup(STEP, bound="emission[net]"), None),
    # the parent's own bound does NOT cover the op's scope: still refused
    "bound_misses": (_sup(STEP, bound="emission[db]"),
                     "`Sup.go` is declared `emission[db]`, but this "
                     "implementation emits through `net` (reaching `Task.run`)"),
}


@pytest.mark.parametrize("row", sorted(INTENT_ROWS))
def test_a_handle_crossing_under_a_declared_intent(row):
    src, message = INTENT_ROWS[row]
    if message is None:
        assert compile_source(src, f"{row}.rvl")
        return
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, f"{row}.rvl")
    assert excinfo.value.code == "G4"
    assert str(excinfo.value.message).startswith(message), excinfo.value.message


def test_the_requires_twin_is_admitted_as_before():
    src = """service Task { emission[net] fn run(p: Str) -> Int }
service Sup {
  emission fn go(p: Str) -> Int within { object: net, verbs: [run] }
}
component Supervisor requires task: Task provides sup: Sup {
  provide sup {
    fn go(p: Str) {
      emit task.run(p) acting { verb: run }
      return 0
    }
  }
}
"""
    assert compile_source(src, "t.rvl")


BOUND_REFUSED = {
    "g4_step_not_covered": ("`Sup.run` is declared `emission[db]`, but this "
                            "implementation emits through `net` (reaching "
                            "`Task.go`)"),
    "g4_value_not_covered": ("`Sup.run` is declared `emission[db]`, but this "
                             "implementation emits through `net` (reaching "
                             "`Task.go`)"),
    "g4_alias_not_covered": ("`Sup.run` is declared `emission[db]`, but this "
                             "implementation emits through `net` (reaching "
                             "`Task.go`)"),
    "g4_bare_op": ("`Sup.run` is declared `emission[net]`, but this "
                   "implementation emits through an unnameable host boundary "
                   "(reaching `Task.go`)"),
    "g4_plain_method": ("`Sup.run` is declared plain, but this implementation "
                        "reaches `Task.go`"),
}
BOUND_ADMITTED = ("ok_step_covered", "ok_value_covered", "ok_alias_covered")


def test_the_bound_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in BOUND.glob("*.rvl")) == sorted(
        list(BOUND_REFUSED) + list(BOUND_ADMITTED))


@pytest.mark.parametrize("stem", sorted(BOUND_REFUSED))
def test_a_handle_crossing_outside_the_parent_bound_is_refused(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source((BOUND / f"{stem}.rvl").read_text(), f"{stem}.rvl")
    assert excinfo.value.code == "G4"
    assert excinfo.value.message == BOUND_REFUSED[stem]


@pytest.mark.parametrize("stem", BOUND_ADMITTED)
def test_a_handle_crossing_inside_the_parent_bound_is_admitted(stem):
    assert compile_source((BOUND / f"{stem}.rvl").read_text(), f"{stem}.rvl")
