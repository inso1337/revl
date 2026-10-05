"""Cardinality attribution follows REACHABILITY (issue #1905).

`revl audit` reported a `pure` host extern as UNBOUNDED and cited a loop that
does not reach it: the classification (`_classify`) picked ONE culprit out of
the CALLER's reachable closure and stamped that same reason on every extern in
`reach[caller]`, so a loop on a sibling branch of the call graph was blamed for
an extern it never touches.

docs/design/260-emission-cardinality-bounds.md §2.3 defines the verdict by
reachability: a capability is `unbounded` when the component's reachable call
graph contains a recursion SCC that REACHES an emission of that capability.
The issue's Expected section says the same for the reason: "a loop that does
not reach the extern neither makes it unbounded nor is cited as the reason."

These tests assert by VERDICT and by the CITED CULPRIT, not by prose.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cardinality import cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

REPRODUCER = """
extern pure fn host_len(s: Str) -> Int
  = @py { return len(s) }
  = @ts { return BigInt(s.length) }

service S { fn total(xs: List[Str]) -> Int }

fn count_loop(xs: List[Int]) -> Int { var n = 0; for (x of xs) { n += x } return n }

fn measure(s: Str) -> Int { return host_len(s) + count_loop([1, 2]) }

component C provides s: S { provide s { fn total(xs) = measure(xs[0]) } }
"""


def _card(src: str) -> dict:
    return cardinality(compile_source(src, "t1905.rvl"))


def _entry(card: dict, comp: str, token: str) -> dict | None:
    return ((card.get(comp) or {}).get("per_capability") or {}).get(token)


# --------------------------------------------------------------------------
# the reported bug: the verdict and the reason both follow reachability
# --------------------------------------------------------------------------

def test_reproducer_loop_on_a_sibling_branch_does_not_make_the_extern_unbounded():
    """The issue's reproducer. `count_loop` has a `for`, `host_len` is reached
    through `measure` - but `count_loop` never calls `host_len`. The extern is
    crossed once per `measure` call, so the component is not `unbounded`."""
    card = _card(REPRODUCER)
    assert (card.get("C") or {}).get("verdict") != "unbounded"
    host_len = _entry(card, "C", "host_len")
    assert not (host_len and host_len.get("kind") == "unbounded")


def test_reproducer_does_not_cite_the_loop_that_cannot_reach_the_extern():
    """The reason half of the issue: if any entry is emitted at all, it must
    not blame `count_loop`."""
    card = _card(REPRODUCER)
    reason = (_entry(card, "C", "host_len") or {}).get("reason") or ""
    assert "count_loop" not in reason


def test_a_loop_in_the_closure_that_does_not_reach_the_extern_is_not_blamed():
    """The sharpest form: the looping fn and the extern-reaching fn are both in
    the caller's closure, and only the non-looping one reaches the extern."""
    card = _card("""
extern pure fn host_len(s: Str) -> Int = @py { return len(s) }

service S { fn total(s: Str, xs: List[Int]) -> Int }

fn spin(xs: List[Int]) -> Int { var n = 0; for (x of xs) { n += x } return n }
fn onpath(s: Str, xs: List[Int]) -> Int { return spin(xs) + host_len(s) }

component C provides s: S { provide s { fn total(s, xs) = onpath(s, xs) } }
""")
    assert (card.get("C") or {}).get("verdict") != "unbounded"
    reason = (_entry(card, "C", "host_len") or {}).get("reason") or ""
    assert "spin" not in reason


def test_a_pure_extern_called_directly_from_the_body_is_not_a_crossing():
    """`host_len` is `pure`: it crosses nothing, so it has no multiplicity for
    a count table to call `unbounded` (§1: a crossing-free component
    contributes no `cardinality:` clause). The boundary surface still lists it
    as reached host code - that is a different surface (item 343)."""
    card = _card("""
extern pure fn host_len(s: Str) -> Int = @py { return len(s) }

service S { fn total(s: Str) -> Int }

component C provides s: S { provide s { fn total(s) = host_len(s) } }
""")
    assert (card.get("C") or {}).get("verdict") != "unbounded"
    assert _entry(card, "C", "host_len") is None


# --------------------------------------------------------------------------
# over-correction guards: the reachability fix must not blind the analysis
# --------------------------------------------------------------------------

def test_a_loop_that_does_reach_the_extern_still_reports_unbounded():
    """The deliberate counterpart to the reproducer: here the `for` body DOES
    call `host_len`, so the loop multiplies the reach and must still be named
    as the cause."""
    card = _card("""
extern pure fn host_len(s: Str) -> Int = @py { return len(s) }

service S { fn total(s: Str) -> Int }

fn m(s: Str) -> Int { var n = 0; for (i of [1]) { n += host_len(s) } return n }

component C provides s: S { provide s { fn total(s) = m(s) } }
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = _entry(card, "C", "host_len")
    assert entry is not None and entry["kind"] == "unbounded"
    assert entry["bound"] is None
    assert "m" in entry["reason"]


def test_reachability_is_transitive_through_a_helper():
    """`loopfn` has the loop and `helper` makes the external call: the culprit
    is the loop, found THROUGH the helper, not the helper itself."""
    card = _card("""
extern emission fn boom(n: Int) -> Int = @py { return n }

service S { emission fn total(n: Int) -> Int }

fn helper(n: Int) -> Int { return boom(n) }
fn loopfn(n: Int) -> Int { var t = 0; for (i of [1]) { t += helper(i) } return t }

component C provides s: S { provide s { fn total(n) = emit loopfn(n) } }
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = _entry(card, "C", "boom")
    assert entry is not None and entry["kind"] == "unbounded"
    assert "loopfn" in entry["reason"]
    assert "helper" not in entry["reason"]


def test_an_emission_extern_behind_plain_host_code_stays_unbounded():
    """§5.1's load-bearing rule, unchanged: a CROSSING whose only reach is
    behind unchecked host code is never counted as zero. Only the `pure`
    (non-crossing) class is out of scope for the count table."""
    card = _card("""
extern emission fn notify(msg: Str) -> Int = @py { return 0 }

service S { emission fn total(msg: Str) -> Int }

fn straight(msg: Str) -> Int { return notify(msg) }

component C provides s: S { provide s { fn total(msg) = emit straight(msg) } }
""")
    assert card["C"]["verdict"] == "unbounded"
    entry = _entry(card, "C", "notify")
    assert entry is not None and entry["kind"] == "unbounded"
    assert entry["bound"] is None


def test_a_reached_but_certifiable_component_is_untouched():
    """A no-loop, no-extern body keeps its exact count - the fix is confined to
    attribution, not to the count fold."""
    card = _card("""
service DB { emission[db] fn exec(s: Str) -> Int }
service Svc { emission[db] fn run() -> Int }
component C requires db: DB provides svc: Svc {
  provide svc { fn run() -> Int { emit db.exec("a"); emit db.exec("b"); return 0 } }
}
""")
    assert card["C"]["verdict"] == "bounded"
    assert card["C"]["per_capability"]["db"] == {"bound": 2, "kind": "bounded"}
