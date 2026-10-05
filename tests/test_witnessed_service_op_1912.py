"""A service operation may declare the witnessed class — issue #1912.

A `witnessed` extern is the reversible boundary class (design 243): the write
persists on commit and reverts on abort, so an abort is residue-free. The G4
fold has always seeded those externs as crossings (`emission_analysis
._emitting_capabilities` reads class `emission` and `witnessed` alike,
deliberately), so a provider body that calls one reaches a crossing — and the
only service spelling that admitted the body was `emission`.

That spelling is the wrong size. `emission[caps] fn set(...)` says the provider
MAY cross irreversibly, and every surface that reads the declaration believes
it: `revl audit` renders the service row `set: emission`, `boundary._boundary`
counts a consumer's call to it among that component's `emissions`, and a gate
that pends by the declaration pends every write. The measured harm in the issue
is a record store whose writes commit-settle and abort-revert: it cannot offer
the plain-looking `fn create(...)` its generated interface declares, because
`emission` is both the only admitted spelling and an over-promise.

So the operation now says the smaller true thing. `witnessed[store] fn set(...)`
bounds the provider to the reversible class under the same scope grammar
`emission[store]` uses, exactly as `emission[caps]` is (docs/capabilities.md):

* a provider may be PURER than declared, never less pure — a body that reaches
  a true (irreversible) `emission` is still refused, and refused for THAT
  reason (`emission-propagation`), whatever its scope;
* a scoped `witnessed[caps]` is a subset bound like `emission[caps]`: a body
  that reaches a witnessed crossing outside `caps` is refused
  (`emission-capability`) with a repair that stays in the class the author
  declared;
* the plain `fn set(...)` refusal is untouched — the diagnostic that asks for
  `emission fn set(...)` is still exactly what a plain declaration gets, and
  the fix does not admit one.

The verdicts below are asserted as verdicts: which programs compile, which
error category comes back, and what the audit's service view reports — not only
as message text. That includes the MCP bridge, which derives its hints from the
same declaration: a tool that may write cannot advertise the checker's
read-only proof, so the class travels in `x-revl.classification` instead.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.distribute import distributability  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp.schema import tools_from_ir  # noqa: E402

# The issue's own shape: a store extern in the witnessed class with its inverse,
# and a second witnessed extern under a different capability plus a true
# emission, so the body's reach can be varied without touching the declaration.
PRELUDE = """type Snap = { before: Str }
type Failure = { code: Str }

extern pure fn settled() -> Unit = @py { return } = @ts { return }
extern acquire fn put_back(s: Snap) -> Unit undo settled() = @py { return } = @ts { return }
extern witnessed[store] fn write_val(v: Str) -> Result[Snap, Failure] undo put_back(result)
  = @py { return Ok({"before": ""}) }
  = @ts { return { kind: "Ok", value: { before: "" } } }
extern witnessed[tmp] fn scratch(f: Str) -> Result[Snap, Failure] undo put_back(result)
  = @py { return Ok({"before": ""}) }
  = @ts { return { kind: "Ok", value: { before: "" } } }
extern emission[log] fn log_line(m: Str) -> Unit = @py { return } = @ts { return }
"""

# the body of the provider. `effect` on a witnessed extern is the ordinary
# spelling of a call that has an inverse; the store never needs an `emit`.
WITNESSED_ONLY = "effect write_val(v)"
OUT_OF_SCOPE = "effect write_val(v)\n      effect scratch(v)"
TRUE_EMISSION = "effect write_val(v)\n      emit log_line(v)"


def program(decl: str, body: str, service: str = "Box") -> str:
    return (PRELUDE
            + f"\nservice {service} {{ {decl} }}\n\n"
            + f"component B provides box: {service} {{\n"
              f"  provide box {{\n"
              f"    fn set(v) {{\n"
              f"      {body}\n"
              f"      return v\n"
              f"    }}\n"
              f"  }}\n"
              f"}}\n")


SET = "fn set(v: Str) -> Str"
WITNESSED_SCOPED = "witnessed[store] fn set(v: Str) -> Str"
WITNESSED_BARE = "witnessed fn set(v: Str) -> Str"
EMISSION_SCOPED = "emission[store] fn set(v: Str) -> Str"
EMISSION_BARE = "emission fn set(v: Str) -> Str"

FAILED = "witnessed_op.rvl"


def refused(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, FAILED)
    return excinfo.value


# ------------------------------------------------------------- the admission

@pytest.mark.parametrize("decl", [WITNESSED_SCOPED, WITNESSED_BARE],
                         ids=["scoped", "bare"])
def test_a_witnessed_declaration_admits_a_witnessed_only_provider(decl):
    """The issue itself: the store's `set` may write, revertibly.

    Before the fix this program had no spelling at all — `fn set` was refused
    and `witnessed[store] fn set` was a parse error ("expected fn, found
    'witnessed'") — so the only admitted form was the over-wide `emission`.
    """
    assert compile_source(program(decl, WITNESSED_ONLY), FAILED)


def test_the_admitted_provider_is_not_reported_as_an_irreversible_crossing():
    """What the audit and every gate that reads the declaration now see.

    A `witnessed` operation is a reversible write, so the surfaces keyed on
    `emission` must read it as NOT one: the IR flag stays false (which is what
    the approval class map, `distribute`, `cardinality`, `boundary` and the
    OpenAPI/WIT exporters read), the scope is carried under its own key, and
    the service no longer renders as an emission in distributability. This is
    the half of the issue that says a gate pends every write today.
    """
    ir = compile_source(program(WITNESSED_SCOPED, WITNESSED_ONLY), FAILED)
    spec = ir["services"]["Box"]["methods"]["set"]
    assert spec["emission"] is False
    assert spec["witnessed"] == ["store"]
    assert distributability(ir)["Box"]["reasons"] != ["set: emission (sync)"]
    boundary = audit_report(ir)["boundary"]["B"]
    assert boundary["emissions"] == []
    # the crossing is still enumerated — as the reversible class it is, on the
    # host-code surface where the extern it really reaches is named.
    assert [ext for ext in boundary["externs"] if ext["name"] == "write_val"] == [
        {"name": "write_val", "class": "witnessed", "backends": ["py", "ts"],
         "capabilities": ["store"]}]


# ------------------------------------------------------ the purity direction

@pytest.mark.parametrize("decl", [WITNESSED_SCOPED, WITNESSED_BARE],
                         ids=["scoped", "bare"])
def test_a_witnessed_declaration_still_refuses_a_true_emission(decl):
    """A provider may be purer than declared, never less pure.

    `witnessed[...]` bounds a provider to the effects a commit settles and an
    abort reverts. `log_line` is an irreversible `emission`, so a body that
    reaches it is refused — the bound is a ceiling, not a synonym for "any".
    """
    error = refused(program(decl, TRUE_EMISSION))
    assert error.category == "emission-propagation"
    assert error.code == "G4"
    assert "`Box.set` is declared" in error.message
    assert "log_line()" in error.message
    # and the repair offered is the wider class, because no capability list
    # widens a witnessed promise into an irreversible one.
    assert "`emission fn set(...)`" in (error.hint or "")


def test_the_true_emission_refusal_is_not_a_blanket_refusal():
    """The control for the test above: the same body is refused for the reason
    the fix names, not because every witnessed program is now refused.

    `emission[store]` is a capability bound, so the same body fails on its
    SCOPE instead — a different category, which is what keeps the
    `emission-propagation` assertion above meaningful.
    """
    error = refused(program(EMISSION_SCOPED, TRUE_EMISSION))
    assert error.category == "emission-capability"
    assert "`Box.set` is declared `emission[store]`" in error.message
    # the same body under a declaration wide enough for it is admitted, so the
    # refusal above really is about the scope.
    assert compile_source(program("emission[store, log] fn set(v: Str) -> Str",
                                  TRUE_EMISSION), FAILED)


# --------------------------------------------------------- the scope bound

def test_a_witnessed_scope_is_a_subset_bound_like_an_emission_scope():
    """`witnessed[store]` holds the provider to `store`, exactly as
    `emission[store]` holds it to `store` — a witnessed crossing the
    declaration did not name is out of bounds."""
    error = refused(program(WITNESSED_SCOPED, OUT_OF_SCOPE))
    assert error.category == "emission-capability"
    assert error.code == "G4"
    assert "`tmp`" in error.message
    assert "`scratch()`" in error.message


def test_widening_the_witnessed_scope_admits_the_same_body():
    """The scope refusal is a real subset check: naming the capability is the
    fix, and the fix is in the class the author declared."""
    assert compile_source(program("witnessed[store, tmp] fn set(v: Str) -> Str",
                                  OUT_OF_SCOPE), FAILED)


def test_the_scope_repair_stays_in_the_witnessed_class():
    """Widening is the one direction a capability diagnostic must not push by
    default, and the issue measures the cost of being pushed there: a repair
    that says `emission[store, tmp]` hands the author back an irreversible
    promise for a reversible write, which is the bug."""
    hint = refused(program(WITNESSED_SCOPED, OUT_OF_SCOPE)).hint or ""
    assert "`witnessed[store, tmp] fn set(...)`" in hint
    assert "`emission[" not in hint


# ------------------------------------------------------------- the controls

def test_the_plain_declaration_is_still_refused_and_still_asks_for_emission():
    """The diagnostic the issue reproduced, unchanged.

    A plain `fn set` whose provider reaches a crossing is still refused with
    the same category and still offered the same repair — the new class is
    admitted *before* that refusal, it does not replace it. 48 sites across
    tests, examples and fixtures pin this wording.
    """
    error = refused(program(SET, WITNESSED_ONLY))
    assert error.category == "emission-propagation"
    assert "`Box.set` is declared plain, but this implementation reaches " \
           "`write_val()`" in error.message
    assert "mark it `emission fn set(...)`" in (error.hint or "")


@pytest.mark.parametrize("decl", [EMISSION_SCOPED, EMISSION_BARE],
                         ids=["scoped", "bare"])
def test_emission_declarations_keep_working_unchanged(decl):
    """No regression: the wider declaration still admits the same body, and
    still reports itself AS an emission on the surface a gate reads."""
    source = program(decl, WITNESSED_ONLY)
    ir = compile_source(source, FAILED)
    assert ir["services"]["Box"]["methods"]["set"]["emission"] is True
    if decl == EMISSION_SCOPED:
        assert distributability(ir)["Box"]["reasons"] == ["set: emission (sync)"]


def test_a_plain_declaration_still_refuses_an_emission_scope_excess():
    """The pre-existing capability check on a plain `emission[caps]` method is
    untouched: the witnessed arm is an `if` ahead of it, not a rewrite."""
    error = refused(program(EMISSION_SCOPED, OUT_OF_SCOPE))
    assert error.category == "emission-capability"
    assert "emits through `tmp`" in error.message
    assert "`emission[store, tmp] fn set(...)`" in (error.hint or "")


def test_an_operation_cannot_be_declared_both_emission_and_witnessed():
    """Two different bounds, not two halves of one.

    `emission[caps]` already admits a provider that only performs witnessed
    effects under `caps` (it is the wider promise), so a declaration naming
    both is refused at parse rather than silently resolved to one.
    """
    error = refused(program("emission[store] witnessed[store] " + SET,
                            WITNESSED_ONLY))
    assert "cannot be declared both `emission` and `witnessed`" in error.message


# ------------------------------------------------- the consumer's own bound

CONSUMER = """type Stash = { path: Str }
type FsError = { code: Str }

extern pure fn unstash(w: Stash) -> Unit = @py { return } = @ts { return }
extern witnessed[store] fn write_val(v: Str) -> Result[Stash, FsError] undo unstash(result)
  = @py { return Ok({"path": v}) }
  = @ts { return { kind: "Ok", value: { path: v } } }

service Box { witnessed[store] fn set(v: Str) -> Str }
service App { DECL }

component B provides box: Box {
  provide box {
    fn set(v) {
      effect write_val(v)
      return v
    }
  }
}

component A requires box: Box provides app: App {
  provide app {
    fn go2(v) {
      BODY
      return r
    }
  }
}
"""

CALL = "let r = box.set(v)"


@pytest.mark.parametrize("decl", ["witnessed[box] fn go2(v: Str) -> Str",
                                  "emission[box] fn go2(v: Str) -> Str"],
                         ids=["witnessed", "emission"])
def test_a_caller_bounds_itself_by_the_capability_it_reaches(decl):
    """The bound is not provider-only: a component that injects `box` and calls
    `box.set` declares the crossing it makes, under either class."""
    assert compile_source(CONSUMER.replace("DECL", decl).replace("BODY", CALL),
                          FAILED)


def test_a_caller_that_declares_the_wrong_capability_is_refused():
    """The control: `witnessed[store]` does not cover a call that reaches
    `box`, so the caller's scope is a real check too."""
    error = refused(CONSUMER.replace("DECL", "witnessed[store] fn go2(v: Str) -> Str")
                            .replace("BODY", CALL))
    assert error.category == "emission-capability"
    assert "`box`" in error.message


def test_a_witnessed_crossing_is_not_spelled_emit():
    """The marker rule reads the declaration, and it decides in the sound
    direction: `emit` is how an IRREVERSIBLE crossing is made visible, so
    spelling it on a reversible one is refused — the mirror image of issue
    #1912's complaint, where a reversible write had to be declared and called
    as an irreversible one. The call itself needs no marker (the test above
    compiles it), so this is the only spelling the new class does NOT have.
    """
    error = refused(CONSUMER.replace("DECL", "witnessed[box] fn go2(v: Str) -> Str")
                            .replace("BODY", "emit box.set(v)\n      let r = v"))
    assert "`emit` on `box.set`, which is not declared `emission`" in error.message


# ------------------------------------------------- the MCP hint stays honest

def tool(decl: str, body: str = WITNESSED_ONLY) -> dict:
    return {t["name"]: t for t in
            tools_from_ir(compile_source(program(decl, body), FAILED))}["revl.box.set"]


def test_a_witnessed_operation_does_not_advertise_the_read_only_proof():
    """The tool description may not claim what the declaration no longer says.

    `readOnlyHint: true` is the compiler's *proof* that a body mutates nothing
    (tests/test_mcp_hint_adversarial.py states the guarantee). A witnessed
    operation writes, so it must not claim it — while `destructiveHint` stays
    false, because an abort reverts every write the checker admitted. The
    class is carried in the provenance instead, in the vocabulary
    `revl mcp import` already uses for it (`EFFECT_WITNESSED`).
    """
    spec = tool(WITNESSED_SCOPED)
    assert spec["annotations"]["readOnlyHint"] is False
    assert spec["annotations"]["destructiveHint"] is False
    assert spec["x-revl"]["classification"] == "witnessed"
    assert spec["x-revl"]["witnessed"] == ["store"]
    assert spec["x-revl"]["capabilities"] == []
    assert spec["x-revl"]["guarantee"] == (
        "G4 — a witnessed bound to [store]: every mutation carries a tracked "
        "inverse, and no provider can reach an emission")
    assert spec["description"].startswith(
        "Box.set — provided at key `box` by component `B`. Reversible:")


def test_the_plain_and_emission_hints_are_unchanged():
    """The control, and the regression guard for the two classes that predate
    this change: only the new class moves the annotations."""
    plain = tool(SET, "let s = v")
    assert plain["annotations"]["readOnlyHint"] is True
    assert plain["x-revl"]["classification"] == "checked"
    assert plain["x-revl"]["witnessed"] == []
    emission = tool(EMISSION_SCOPED)
    assert emission["annotations"]["readOnlyHint"] is False
    assert emission["annotations"]["destructiveHint"] is True
    assert emission["x-revl"]["classification"] == "emission"
    assert emission["x-revl"]["witnessed"] == []
