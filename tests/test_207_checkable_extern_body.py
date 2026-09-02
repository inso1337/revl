"""Item 207: the falsification harness for docs/design/207-checkable-extern-body.md.

The design pass recommends DELETING `shape-proven` from item 309's idempotence
lattice rather than building a checkable extern-body form to reach it. That
recommendation rests on five structural claims about `origin/main`, and this
file asserts every one of them mechanically, so the recommendation can be
re-checked by running the tests instead of by rereading the argument.

The claims, in the order the design note makes them:

* C1  There is no revl-expressed extern body to run a shape rule over: the
      parser refuses an extern with no `@backend` body, and the `undo` slot
      holds an expression, not a body.
* C2  The register field is PROVENANCE-DISJOINT by classification. `keyed` can
      only come from an `emission` and `read`/`undo idempotent` can only come
      from a non-emission, because `idempotent` is emission-only and an emission
      cannot declare `undo`. `shape-proven`, whose designed provenance is an
      INVERSE body, is therefore an inverse-only register.
* C3  `REDISPATCH_FREE` is read at the OWED-DEFERRED-EMISSION seam only, and
      `deferred` is emission-only. So an inverse-only register can never be seen
      by the set that contains it. The membership is inert.
* C4  In the one path that does read an inverse's register, `_replay_tier`,
      `shape-proven` produces exactly the verdict `undo idempotent` already
      produces today. The behavioural delta of the whole feature, in recovery,
      is zero.
* C5  The one surface where the tier would change a verdict is the POLICY floor,
      and the gap there is real: no mutating local inverse can reach a strong
      floor today, because `keyed` is emission-only and `read` requires the
      inverse to mutate nothing.

And the falsifier for the recommendation itself:

* C6  Deleting `shape-proven` from `_REGISTER_RANK` and `REDISPATCH_FREE`
      changes no verdict any shipped surface can produce, because no input that
      reaches those surfaces carries it.

If a future change makes C3 or C4 false, the tier has become load-bearing and
the design note's recommendation must be re-argued. That is what this file is
for.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from revl.lower import (  # noqa: E402
    REDISPATCH_FREE,
    _REGISTER_RANK,
    RevlError,
    _register_satisfies,
    check_and_lower,
)
from revl.parser import Parser  # noqa: E402
from revl.recovery import _replay_tier, _reissue_permitted  # noqa: E402
from revl.audit_diff import _recovery_surface  # noqa: E402


def _lower(src: str) -> dict:
    return check_and_lower(Parser(src, "t.rvl").parse())


_PRE = (
    "type W = { path: Str }\n"
    "type E = { msg: Str }\n"
    "extern pure fn restore(w: W) -> Unit = @py { pass }\n"
)


# --------------------------------------------------------------------- C1
# There is no body to read.

def test_c1_an_extern_with_no_backend_body_is_refused():
    """The premise of the whole item, asserted rather than quoted.

    A shape rule over an inverse body needs a body written in revl. The parser
    refuses a declaration that does not carry at least one host body, so the
    only body an extern can have is host code, opaque by construction."""
    with pytest.raises(RevlError) as exc:
        _lower("extern pure fn restore(w: Str) -> Unit\n")
    assert "must declare at least one" in str(exc.value)
    assert "@backend" in str(exc.value)


def test_c1_the_undo_slot_is_an_expression_over_extern_calls():
    """The second half of C1: the `undo` slot is not a body either.

    It holds a call expression whose callee is another extern, so walking it
    reaches a host body in one step and stops. `_recovery_surface` records the
    inverse by NAME; there is nothing structural under that name to inspect."""
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result)\n"
        "    = @py { pass }\n"))
    by_name = {e["name"]: e for e in ir["externs"]}
    # the slot is a one-node call expression naming another extern ...
    assert by_name["rm"]["undo"] == {
        "kind": "call",
        "callee": {"kind": "var", "name": "restore"},
        "args": [{"kind": "var", "name": "result"}],
    }
    # ... and following that name reaches host text and stops. `bodies` is all
    # there is under an inverse; there is no revl structure to run a rule over.
    assert by_name["restore"]["bodies"], "an inverse resolves to host bodies"
    assert by_name["restore"].get("undo") is None
    assert set(by_name["restore"]) == {"bodies", "class", "name", "params",
                                       "returns"}


# --------------------------------------------------------------------- C2
# The register is provenance-disjoint by classification.

def test_c2_idempotent_is_emission_only():
    """`keyed` cannot be an inverse's register."""
    with pytest.raises(RevlError) as exc:
        _lower(_PRE + (
            "extern witnessed[fs] idempotent fn rm(path: Str) -> Result[W, E]\n"
            "    undo restore(result) = @py { pass }\n"))
    assert "idempotent" in str(exc.value)


def test_c2_an_emission_cannot_declare_undo():
    """...and symmetrically, an inverse register cannot be an emission's."""
    with pytest.raises(RevlError) as exc:
        _lower(_PRE + (
            "extern emission[gw] fn send(w: W) -> Unit\n"
            "    undo restore(w) = @py { pass }\n"))
    assert "cannot declare `undo`" in str(exc.value)


def test_c2_the_two_provenances_never_meet_on_one_declaration():
    """The consequence: a register is either a FORWARD claim about an emission
    or an INVERSE claim about a witnessed/acquire extern, never both.

    `shape-proven` is designed as a check over an inverse BODY, so it belongs to
    the second family for good."""
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result) = @py { pass }\n"
        "extern emission[inv] idempotent(key: k) fn release(k: Str) -> Unit "
        "= @py { pass }\n"))
    by_name = {e["name"]: e for e in ir["externs"]}
    assert by_name["rm"]["register"] == "declared"     # inverse family
    assert by_name["release"]["register"] == "keyed"   # emission family
    assert by_name["rm"]["class"] == "witnessed"
    assert by_name["release"]["class"] == "emission"


# --------------------------------------------------------------------- C3
# `REDISPATCH_FREE`'s membership is inert.

def test_c3_deferred_is_emission_only():
    """`REDISPATCH_FREE` is read only for an OWED DEFERRED EMISSION
    (`recovery.py::_reissue_permitted` and its one call site), and only an
    `emission` can be `deferred`. So the set's inputs are emission registers."""
    with pytest.raises(RevlError) as exc:
        _lower(_PRE + (
            "extern witnessed[fs] deferred fn rm(path: Str) -> Result[W, E]\n"
            "    undo restore(result) = @py { pass }\n"))
    assert "only valid on an `emission`" in str(exc.value)


def test_c3_no_declaration_can_put_an_inverse_register_on_an_owed_emission():
    """The recovery surface, walked: every `owed-emission` row's register comes
    from an emission declaration, and every `inverse` row's from a non-emission.

    `shape-proven` is an inverse-family register, so it can never appear on the
    row family `REDISPATCH_FREE` grades."""
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result) = @py { pass }\n"
        "extern emission[inv] deferred idempotent(key: k) fn release(k: Str) "
        "-> Unit = @py { pass }\n"
        "extern emission[http] deferred idempotent fn ping(k: Str) -> Unit "
        "= @py { pass }\n"))
    surface = _recovery_surface(ir)
    kinds = {(row["kind"], row["register"]) for row in surface}
    owed = {reg for kind, reg in kinds if kind == "owed-emission"}
    inverses = {reg for kind, reg in kinds if kind == "inverse"}
    assert owed == {"keyed", "declared"}
    assert inverses == {"declared"}
    # and the whole point: no row of either family carries the tier.
    assert "shape-proven" not in {reg for _, reg in kinds}


def test_c3_the_reissue_gate_is_only_ever_asked_about_emission_registers():
    """`_reissue_permitted` is total over the register vocabulary, and its
    `shape-proven` arm is unreachable given C2 + C3: the argument is read off a
    deferred-emission descriptor."""
    # the arm exists ...
    assert _reissue_permitted("shape-proven", "declared") is True
    assert _reissue_permitted("shape-proven", "keyed") is True
    # ... and is behaviourally identical to the `keyed` arm it sits beside, for
    # every knob setting. Deleting it therefore cannot change a verdict that a
    # keyed descriptor does not already produce.
    for strength in (None, "declared", "keyed", "shape-proven", "strong"):
        assert _reissue_permitted("shape-proven", strength) \
            == _reissue_permitted("keyed", strength)


# --------------------------------------------------------------------- C4
# In the inverse path, the tier buys nothing.

def test_c4_shape_proven_and_declared_idempotent_replay_identically():
    """`_replay_tier` is where an INVERSE's register is read. A shape-proven
    inverse would be spelled `undo idempotent <inv>(result)` with a body that
    passes the check, so `declared_idempotent` is True either way — and the
    verdict is `free` either way.

    The entire recovery-side value of item 309 slice 4 is this equality being
    false, and it is true."""
    assert _replay_tier("shape-proven", True) == "free"
    assert _replay_tier("declared", True) == "free"
    assert _replay_tier(None, True) == "free"
    # the one register that DOES change the inverse verdict is 440's `read`.
    assert _replay_tier("read", True) == "read"
    assert _replay_tier("read", False) == "read"
    # and the fail-closed floor is untouched by any of it.
    assert _replay_tier("shape-proven", False) == "fenced"


# --------------------------------------------------------------------- C5
# The policy floor is the one real gap, and it is real.

def test_c5_no_mutating_local_inverse_can_reach_a_strong_floor_today():
    """The honest half of the case FOR the tier.

    An operator who writes `requires idempotent-teardown(strength: strong)`
    means "auto-replay only what was verified". Today an inverse has exactly two
    routes above `declared`, and neither is open to a local mutating inverse:
    `keyed` is emission-only (C2), and `read` requires the inverse to change
    nothing. `stdlib/fs.rvl`'s `restore`/`unrm`/`unmove`/`rmdir_if_empty` all
    mutate, so all four are permanently `declared`."""
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
        "    undo idempotent restore(result) = @py { pass }\n"))
    [row] = [r for r in _recovery_surface(ir) if r["kind"] == "inverse"]
    assert row["register"] == "declared"
    assert not _register_satisfies(row["register"], "strong")
    assert not _register_satisfies(row["register"], "keyed")
    assert not _register_satisfies(row["register"], "shape-proven")
    # the floor is satisfiable in principle, just not by this declaration.
    assert _register_satisfies("shape-proven", "strong")


def test_c5_the_read_tier_route_requires_a_non_mutating_inverse():
    """`undo pure` is the only strong route an inverse has, and lower anchors it
    to a `pure`-classified callee. That is a shape check, not a proof, which is
    exactly why the design note argues a second shape-checked tier adds a second
    unverified claim rather than a verification."""
    ir = _lower(_PRE + (
        "extern witnessed[fs] fn probe(path: Str) -> Result[W, E]\n"
        "    undo pure restore(result) = @py { pass }\n"))
    [row] = [r for r in _recovery_surface(ir) if r["kind"] == "inverse"]
    assert row["register"] == "read"
    assert _register_satisfies("read", "strong")
    # and `restore` mutates: `pure` is checked for shape only. The read tier is
    # an AUTHOR CLAIM held to a classification, never a verification of the body.
    with pytest.raises(RevlError):
        _lower(_PRE + (
            "extern emission[gw] fn send(k: Str) -> Unit = @py { pass }\n"
            "extern witnessed[fs] fn probe(path: Str) -> Result[W, E]\n"
            "    undo pure send(path) = @py { pass }\n"))


# --------------------------------------------------------------------- C6
# The recommendation's own falsifier.

def test_c6_deleting_the_tier_changes_no_producible_verdict():
    """The removal is a no-op over every input a shipped surface can produce.

    Enumerated the same way item 309's tripwire enumerates: every declaration
    surface that produces a register at all. If this set ever grows a
    `shape-proven`, the deletion is no longer free and the design note is
    overturned."""
    surfaces = [
        _PRE + ("extern witnessed[fs] fn rm(path: Str) -> Result[W, E]\n"
                "    undo idempotent restore(result) = @py { pass }\n"),
        _PRE + ("extern witnessed[fs] fn probe(path: Str) -> Result[W, E]\n"
                "    undo pure restore(result) = @py { pass }\n"),
        ("type H = { fd: Int }\n"
         "extern pure fn shut(h: H) -> Unit = @py { pass }\n"
         "extern acquire fn open(p: Str) -> H\n"
         "    undo idempotent shut(result) = @py { pass }\n"),
        "extern emission[inv] idempotent(key: k) fn release(k: Str) -> Unit "
        "= @py { pass }\n",
        "extern emission[http] idempotent fn put(k: Str) -> Unit = @py { pass }\n",
        "extern emission[inv] deferred idempotent(key: k) fn queue(k: Str) "
        "-> Unit = @py { pass }\n",
    ]
    produced = set()
    for src in surfaces:
        for ext in _lower(src)["externs"]:
            if ext.get("register"):
                produced.add(ext["register"])
    assert produced == {"declared", "keyed", "read"}

    # every consumer, over exactly that producible vocabulary, is indifferent to
    # whether `shape-proven` is a member of the rank map and the free set.
    without = frozenset(REDISPATCH_FREE - {"shape-proven"})
    for register in produced | {None}:
        assert (register in REDISPATCH_FREE) == (register in without)
    ranks_without = {k: v for k, v in _REGISTER_RANK.items()
                     if k != "shape-proven"}
    for register in produced:
        for floor in ("declared", "keyed", "strong"):
            expected = ranks_without.get(register, -1) >= ranks_without.get(floor, 0)
            assert _register_satisfies(register, floor) == expected


def test_c6_the_only_floor_that_loses_a_spelling_is_the_named_peer():
    """The one visible consequence of deletion, stated so it is not a surprise.

    `requires register shape-proven` / `idempotent-teardown(strength:
    shape-proven)` stops being a spellable floor. It is today an alias for
    `strong` in every producible case (nothing produces the tier, so the floor
    is satisfied only by `keyed` or `read`), which is what makes the removal a
    rename of a synonym rather than a loss of expressiveness."""
    for register in ("declared", "keyed", "read"):
        assert _register_satisfies(register, "shape-proven") \
            == _register_satisfies(register, "strong")


# ------------------------------------------------------ the defect this pass found
# Not about `shape-proven`, but found by walking every reader of the register.

def test_f1_a_register_floor_must_be_worst_wins_over_a_token():
    """The one defect this design pass found, now fixed and pinned.

    `_capability_registers` folded a token's declarations STRONGEST-wins; 290
    §3.2 specifies WORST-wins ("the effective register of a capability is the
    WEAKEST among the declarations behind it. One bare `declared` inverse beside
    three keyed ones fails a `keyed` floor"). Under the old fold one keyed
    declaration carried every bare-`declared` declaration on the same token past
    a `keyed`/`strong` floor — fail-open, in a rule whose only job is to refuse.

    `db` is declared by two emissions: one `idempotent(key: k)` (`keyed`) and one
    bare `idempotent` (`declared`). The token's effective register is the weakest
    of them."""
    from revl import compile_source  # noqa: PLC0415
    from revl.audit_diff import audit_report  # noqa: PLC0415

    src = (
        "extern emission[db] idempotent(key: k) fn push(k: Str) -> Unit "
        "= @py { pass }\n"
        "extern emission[db] idempotent fn ping(k: Str) -> Unit = @py { pass }\n"
        "component Main {\n"
        "  emit push(\"a\")\n"
        "  emit ping(\"b\")\n"
        "}\n")
    audit = audit_report(compile_source(src))
    # both declarations are behind the one token ...
    registers = {e["name"]: e.get("register")
                 for e in audit["externs"] if e.get("register")}
    assert registers == {"push": "keyed", "ping": "declared"}
    # ... so the token's effective register is the weakest of them.
    assert audit["capability_registers"] == {"db": "declared"}


def test_f1_the_weakest_declaration_reaches_the_refusal():
    """The same fold, driven end to end through `capability ... requires
    register`, which is the rule the fold direction actually decides.

    The shape differs from the map-level reproducer above in one way that is
    itself worth pinning: a register rule selects a token only when that token is
    in the component's REACH, and a directly emitted extern contributes its own
    NAME as a host reach, not its capability token. A service-method emission is
    what puts `db` itself in the reach set. So `db` here carries two
    declarations, a `keyed` extern and a bare-`idempotent` service method, and is
    reached.

    Before the fix the token folded to `keyed` and both the `keyed` and `strong`
    floors ADMITTED. Worst-wins folds it to `declared`, and the floor refuses."""
    from revl.compiler import compile_source  # noqa: PLC0415
    from revl.audit_diff import audit_report  # noqa: PLC0415
    from revl.policy import evaluate, parse_policy  # noqa: PLC0415

    src = (
        "extern emission[db] idempotent(key: k) fn push(k: Str) -> Unit "
        "= @py { pass }\n"
        "service Store { emission[db] idempotent fn ping(key: Str) }\n"
        "component CsvReader requires store: Store {\n"
        "  emit store.ping(\"c\")\n"
        "  emit push(\"a\")\n"
        "}\n")
    audit = audit_report(compile_source(src, "c.rvl"))
    assert audit["capability_registers"] == {"db": "declared"}
    for floor in ("keyed", "strong"):
        violations = evaluate(
            parse_policy(f"capability db requires register {floor}"), audit)
        assert any(v.kind == "register" and v.token == "db"
                   for v in violations), floor
    # the floor the weakest declaration does meet still admits, so the fix is a
    # direction change and not a blanket refusal.
    assert evaluate(parse_policy("capability db requires register declared"),
                    audit) == []
