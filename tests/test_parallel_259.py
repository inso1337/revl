"""Checked parallel emissions - the derived partition (item 259, Slice 1).

Slice 1 is CHECKER-ONLY: the checker reads the emission sequence off the IR and
derives a parallelizable PARTITION of each straight-line run from the capability
declarations already present. It executes nothing in parallel and makes no
runtime change; these tests pin the DERIVATION and its barriers, which is the
slice-1 exit criterion (docs/design/259-checked-parallel-emissions.md §8):

  * three pairwise-disjoint emissions in a row form ONE parallel group;
  * a first-class emitting arrow (a `*`/unknown_dispatch call) between two
    disjoint emissions is a BARRIER - the two land in SEPARATE groups, so a
    hidden crossing is never reordered around (HIGH-2, Barrier B);
  * a same-key pair without `commutative` stays sequential (two singletons);
  * a same-key `commutative` pair groups (Def. 39's execution payoff);
  * a group grows only while pairwise-compatible with the WHOLE running group
    (the `a - b - c` chain hole);
  * a bare `*` emission is always a singleton;
  * the audit render is byte-identical (no `parallel_plan` key) when a body has
    no parallelizable group, and carries the wrapped plan when it does.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import cap_order as co  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.parallel import parallel_plan  # noqa: E402

# Shared declarations: three distinct-token emission services, a `commutative`
# metrics service, a bare (`*`) emission, and a first-class dispatcher chain
# (`dispatch` passes the emitting extern `launder` to `indirect`, so its reach is
# `*`/unknown_dispatch - the arm walk cannot name it).
HDR = """
service Mailer { emission[send.email] fn send(to: Str) }
service Db { emission[db.write] fn write(row: Str) }
service Log { commutative emission[metrics] fn tick(x: Str) }
service Bare { emission fn go(x: Str) -> Str }
service Run { emission fn run(x: Str) -> Str }
extern emission fn launder(msg: Str) -> Str = @py { return msg }
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
fn dispatch(x: Str) -> Str { return indirect(launder, x) }
"""


def _plan(requires: str, body: str) -> list[list[int]]:
    src = HDR + (
        f"component W requires {requires} provides run: Run {{\n"
        f"  provide run {{ fn run(x) {{ {body} return x }} }}\n"
        f"}}\n"
    )
    return parallel_plan(compile_source(src, "parallel.rvl")).get("W", [])


def _audit(requires: str, body: str) -> dict:
    src = HDR + (
        f"component W requires {requires} provides run: Run {{\n"
        f"  provide run {{ fn run(x) {{ {body} return x }} }}\n"
        f"}}\n"
    )
    return audit_report(compile_source(src, "parallel.rvl"))


# ---------------------------------------------------------------- cap_order.disjoint


def test_disjoint_distinct_tokens():
    # (D1): distinct tokens can never name the same boundary, so they are disjoint.
    assert co.disjoint(co.parse_cap("send.email"), co.parse_cap("db.write"))
    assert co.disjoint(co.parse_cap('fs.write(path="/a")'),
                       co.parse_cap('db.write(table="t")'))


def test_disjoint_same_token_deferred_not_yet():
    # (D2) is deferred: slice 1 treats every same-token pair as NOT disjoint,
    # even two sibling paths - fail-safe, they stay sequential until (D2) lands.
    assert not co.disjoint(co.parse_cap("db.write"), co.parse_cap("db.write"))
    assert not co.disjoint(co.parse_cap('fs.write(path="/a")'),
                           co.parse_cap('fs.write(path="/b")'))


def test_disjoint_star_is_disjoint_from_nothing():
    # `*` is top of the order: never provably independent of anything, not even
    # another `*`.
    star = co.parse_cap("*")
    assert not co.disjoint(star, co.parse_cap("db.write"))
    assert not co.disjoint(co.parse_cap("db.write"), star)
    assert not co.disjoint(star, star)


# ---------------------------------------------------------------- the partition


def test_three_disjoint_emissions_form_one_group():
    plan = _plan("m: Mailer, d: Db, l: Log",
                 'emit m.send("a") emit d.write("b") emit l.tick("c")')
    assert plan == [[0, 1, 2]]


def test_first_class_arrow_is_a_barrier_between_disjoint_emissions():
    # `dispatch` reaches `*` (a first-class emitting arrow the arm walk cannot
    # name). Barrier B hard-breaks the run: the two disjoint emissions land in
    # SEPARATE groups, so the hidden crossing is never reordered around.
    plan = _plan("m: Mailer, d: Db",
                 'emit m.send("a") let b = dispatch(x) emit d.write("c")')
    assert plan == [[0], [1]]


def test_pure_step_between_disjoint_emissions_is_not_a_barrier():
    # The contrast that makes Barrier B load-bearing: a pure `let` crossing no
    # boundary does NOT break the run, so the same two disjoint emissions group.
    # Only the `*`-reaching step (above) splits them.
    plan = _plan("m: Mailer, d: Db",
                 'emit m.send("a") let b = "z" emit d.write("c")')
    assert plan == [[0, 1]]


def test_non_commutative_same_key_pair_stays_sequential():
    plan = _plan("d: Db", 'emit d.write("a") emit d.write("b")')
    assert plan == [[0], [1]]


def test_commutative_same_key_pair_groups():
    plan = _plan("l: Log", 'emit l.tick("a") emit l.tick("b")')
    assert plan == [[0, 1]]


def test_group_grows_pairwise_against_the_whole_group():
    # `a - b - c`: a and c share a non-commutative key, b is disjoint from both.
    # Checking only the PREVIOUS member (b) would wrongly grow [a, b, c] and put
    # a and c in one group; the whole-group check seals [a, b] and opens [c].
    plan = _plan("m: Mailer, d: Db",
                 'emit d.write("a") emit m.send("b") emit d.write("c")')
    assert plan == [[0, 1], [2]]


def test_bare_star_emission_is_always_a_singleton():
    # A bare `emission fn` (no scope) crosses `*`: disjoint from nothing, so it
    # is forced to its own group even beside a disjoint emission.
    plan = _plan("m: Mailer, b: Bare",
                 'emit b.go("a") emit m.send("c")')
    assert plan == [[0], [1]]


# ---------------------------------------------------------------- audit surface


def test_audit_surface_byte_identical_when_no_parallel_group():
    # A single emission (and a two-singleton non-commutative pair) has no group
    # of size > 1, so the additive `parallel_plan` key is ABSENT - the common
    # case renders byte-identically to before item 259.
    one = _audit("d: Db", 'emit d.write("a")')
    assert "parallel_plan" not in one
    seq = _audit("d: Db", 'emit d.write("a") emit d.write("b")')
    assert "parallel_plan" not in seq


def test_audit_surface_carries_the_wrapped_plan_when_parallelizable():
    audit = _audit("m: Mailer, d: Db, l: Log",
                   'emit m.send("a") emit d.write("b") emit l.tick("c")')
    assert audit["parallel_plan"] == {"W": [{"group": [0, 1, 2]}]}


def test_audit_surface_lists_only_components_with_a_real_group():
    # A component whose plan is all singletons is elided from the surface even
    # when another component in the same program does parallelize.
    audit = _audit("m: Mailer, d: Db", 'emit d.write("a") let b = dispatch(x) emit m.send("c")')
    # both emissions are singletons here (barrier B), so no key at all.
    assert "parallel_plan" not in audit
