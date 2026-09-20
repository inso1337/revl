"""The G4 subset check reads a scoped extern by its declared token — issue #1234.

`tests/test_247_capability_reach_spellings.py` is the pin for which spelling a
POLICY rule selects: a scoped extern's reach is its DECLARED token, never its
name. That file lists the surfaces already keyed that way — the witnessed seed,
`lower._extern_emission_caps`, `audit_diff._capability_registers`, the approval
`ClassMap` — and item 343's policy-reach slice added `policy.component_reach`.

The G4 subset check was not on that list and was not keyed that way. Its fold,
`emission_analysis._emitting_capabilities`, seeded an `emission` extern with the
extern's own NAME whatever scope it declared, while the `witnessed` seed three
lines below it read `capabilities or [name]`. So a provider that was exactly in
bounds was refused:

    extern emission[db] fn pg_write(t: Str) = @py { return }
    service Worker { emission[db] fn go(t: Str) }

    `Worker.go` is declared `emission[db]`, but this implementation emits
    through `pg_write` ...
      ... widen the declaration to `emission[db, pg_write] fn go(...)` ...

The refusal is fail-closed, but the repair it hands the author is not: it tells
them to widen a declaration that was already correct, toward a token no
`capability <glob>` rule can select (that is the half `test_247` pins). Widening
is the one direction a capability diagnostic must never push by default, and
G8's subject is reach that is enumerated and bounded.

The tests below are the two halves of that, plus the host-code enumeration the
same fold feeds: reach is the declared token for AUTHORITY, and the extern name
for the `externs` table and the cardinality ceilings, which item 343 recorded as
deliberately staying name-keyed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.policy import component_reach  # noqa: E402

# the issue's reproducer: a `db`-scoped extern, a `db`-declared method, and a
# provider that emits straight through it. Exactly in bounds.
DIRECT = """
extern emission[db] fn pg_write(t: Str) = @py { return }
service Worker { emission[db] fn go(t: Str) }
component W provides worker: Worker {
  provide worker {
    fn go(t) { emit pg_write(t) }
  }
}
"""

# the same crossing one `fn` hop away. The scope is what propagates, so
# refactoring a body into a helper cannot change the verdict.
TRANSITIVE = """
extern emission[db] fn pg_write(t: Str) = @py { return }
fn wrapped(t: Str) { pg_write(t) }
service Worker { emission[db] fn go(t: Str) }
component W provides worker: Worker {
  provide worker {
    fn go(t) { emit wrapped(t) }
  }
}
"""


@pytest.mark.parametrize("source", [DIRECT, TRANSITIVE],
                         ids=["direct", "through-a-helper-fn"])
def test_a_provider_inside_a_scoped_externs_declared_bound_is_admitted(source):
    """`emission[db]` implemented by a `db`-scoped extern is a subset, not an
    excess. This is the refusal issue #1234 reported."""
    assert compile_source(source, "in_bounds.rvl")


@pytest.mark.parametrize("source", [DIRECT, TRANSITIVE],
                         ids=["direct", "through-a-helper-fn"])
def test_the_g4_fold_and_the_g8_reach_agree_on_the_token(source):
    """The cross-surface check, the same one `test_247` makes against the
    approval fold. G4 decides at compile time what `component_reach` reports to
    a policy rule; if they name the crossing differently, an author reads one
    namespace and an operator writes another."""
    audit = audit_report(compile_source(source, "in_bounds.rvl"))
    assert {r.token for r in component_reach(audit, "W")} == {"db"}


# --------------------------------------------------------------- the controls

# genuinely out of bounds: the method promises `bus`, the body reaches `db`.
# Refused before the fix and after it — a broken fixture that stopped refusing
# altogether would pass the admission tests above and fail this one.
OUT_OF_BOUNDS = """
extern emission[db] fn pg_write(t: Str) = @py { return }
service Worker { emission[bus] fn go(t: Str) }
component W provides worker: Worker {
  provide worker {
    fn go(t) { emit pg_write(t) }
  }
}
"""

# an UNSCOPED extern still names itself (docs/capabilities.md §2), so this one
# is out of bounds too, and its token is `send_mail`. The rule the fix must not
# disturb.
UNSCOPED_OUT_OF_BOUNDS = OUT_OF_BOUNDS.replace(
    "emission[db] fn pg_write", "emission fn send_mail").replace(
    "pg_write(t)", "send_mail(t)")


def test_a_provider_outside_the_declared_scope_is_still_refused():
    """The control, on the tree with the fix and on the tree without it: the
    subset check still refuses an excess. Which token it names is the next
    test; that it refuses at all is what keeps a fixture that stopped checking
    from reading as a fix."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(OUT_OF_BOUNDS, "out_of_bounds.rvl")
    assert excinfo.value.category == "emission-capability"
    assert "`Worker.go` is declared `emission[bus]`" in str(excinfo.value)


def test_the_refusal_names_the_declared_token_as_the_excess():
    with pytest.raises(RevlError) as excinfo:
        compile_source(OUT_OF_BOUNDS, "out_of_bounds.rvl")
    assert "emits through `db`" in str(excinfo.value)


def test_the_repair_names_the_declared_token_and_not_the_extern_name():
    """The half that makes this more than a spelling preference.

    A compiler-generated repair carries the compiler's authority. Widening to
    `emission[bus, pg_write]` would hand the author a token `test_247` pins as
    NOT a policy token, so the declaration they end up with is both broader
    than the program needs and unselectable by the rule an operator writes.
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(OUT_OF_BOUNDS, "out_of_bounds.rvl")
    hint = excinfo.value.hint or ""
    assert "`emission[bus, db] fn go(...)`" in hint
    assert "pg_write" not in hint


def test_an_unscoped_extern_still_names_itself():
    """The name-as-capability rule for a bare `emission`, unchanged: absent a
    scope the extern IS the boundary, so the token is its own name."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(UNSCOPED_OUT_OF_BOUNDS, "unscoped.rvl")
    assert "emits through `send_mail`" in str(excinfo.value)
    assert "`emission[bus, send_mail] fn go(...)`" in (excinfo.value.hint or "")


# ------------------------------------------------- the host-code enumeration

@pytest.mark.parametrize("source", [DIRECT, TRANSITIVE],
                         ids=["direct", "through-a-helper-fn"])
def test_the_audit_externs_table_stays_keyed_by_the_extern_name(source):
    """Item 343 recorded that the per-component `externs` table and the
    cardinality ceilings enumerate HOST CODE, not authority, and stay keyed by
    name. `_boundary` folds the emission fixed point in to catch reach no call
    name names, so a token-keyed fold leaks into a name table: before the
    name-keyed twin, TRANSITIVE enumerated a host extern called `db` with no
    class and no bodies, beside the real `pg_write` entry.
    """
    audit = audit_report(compile_source(source, "in_bounds.rvl"))
    entries = {e["name"]: e for e in audit["boundary"]["W"]["externs"]}
    assert set(entries) == {"pg_write"}
    assert entries["pg_write"]["class"] == "emission"
    assert entries["pg_write"]["capabilities"] == ["db"]
