"""`revl erase-report` — item 254 (compensate-grade witnessed network), the
item-248 measurement extended to the network boundary.

Item 254's payoff, stated in the roadmap, is that "item 248's measurement
extends to the network boundary — the fraction of an agent's API traffic that
became witnessed or compensable, per session, with proof." The compensate-grade
network effect an `import openapi` PUT lowers to (Slice 1) is an emission EXTERN
that OWNS its reversal through an extern-declared `compensate` slot, wired to
fire at teardown by the py runtime (commit 4413fc8). Two gaps this slice closes
on the erase-report boundary surface:

  1. that compensate-bearing emission EXTERN was misreported BARE — `_crossings`
     read the `compensate` clause only off per-call service-emission facts, and
     hard-coded every host extern `compensated: False`. The extern-declared
     `compensate` lives on the extern IR entry, so a network compensate-grade
     crossing counted as a bare, un-offset emission — the exact opposite of the
     item.
  2. there was no network-boundary fraction at all: every crossing folded into
     one bare/compensated total with no way to read the API-traffic subset.

The `erase_net.rvl` fixture's realm `edge` makes three crossings: a
`net.edge_api` PUT that owns its restore (compensable), a bare `net.edge_api`
emission, and one NON-network service emission — so the network fraction is a
real subset (1 of 2), never the whole surface.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files  # noqa: E402
from revl import erase_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET = os.path.join(ROOT, "tests", "fixtures", "erase_net.rvl")
REALMS = os.path.join(ROOT, "tests", "fixtures", "erase_realms.rvl")
TENANTS = os.path.join(ROOT, "examples", "tenants.rvl")


@pytest.fixture(scope="module")
def net_report():
    ir = compile_files([NET])
    return erase_report.build_report(ir, "edge", prove_residue=False)


# ------------------------------------------ gap 1: the emission extern is not bare

def test_compensate_bearing_net_emission_extern_is_compensated(net_report):
    """The `net.edge_api` PUT owns a `compensate` slot on its extern IR entry;
    the erase report must tag it compensated, not bare (it was hard-coded bare)."""
    externs = {e["name"]: e for e in net_report["boundaryCrossings"]["externs"]}
    assert externs["http_put"]["compensated"] is True
    assert externs["http_put"]["capabilities"] == ["net.edge_api"]
    # the bare net emission stays bare — nothing was done about it.
    assert externs["http_bare"]["compensated"] is False


def test_compensated_count_now_includes_the_extern(net_report):
    """One compensated (http_put), two bare (http_bare + the non-net
    store.keep) — the extern-owned compensate is no longer lost."""
    cross = net_report["boundaryCrossings"]
    assert cross["total"] == 3
    assert cross["compensatedCount"] == 1
    assert cross["bareCount"] == 2
    assert "host:EdgeApp:http_put" not in cross["bareTokens"]


# ------------------------------------------ gap 2: the network-boundary fraction

def test_network_boundary_isolates_the_api_traffic(net_report):
    """Only the two `net.*` crossings are the API boundary; the in-process
    `store.keep` service emission is excluded."""
    net = net_report["boundaryCrossings"]["networkBoundary"]
    assert net["total"] == 2
    assert net["compensatedCount"] == 1
    assert net["bareCount"] == 1
    assert net["witnessedCount"] == 0


def test_network_compensable_fraction_is_computed(net_report):
    """The item-254 headline: half this realm's API traffic is compensable."""
    net = net_report["boundaryCrossings"]["networkBoundary"]
    assert net["compensableCount"] == 1
    assert net["compensableFraction"] == 0.5
    assert net["compensableTokens"] == ["host:EdgeApp:http_put"]
    assert net["bareTokens"] == ["host:EdgeApp:http_bare"]


def test_summary_carries_the_network_headline(net_report):
    summary = net_report["summary"]
    assert summary["networkCrossings"] == 2
    assert summary["networkCompensableFraction"] == 0.5


def test_render_shows_the_network_boundary_line(net_report):
    text = erase_report.render(net_report)
    assert "network boundary: 1 of 2 API crossing(s) witnessed/compensable" in text
    assert "(50.0%)" in text
    # the compensate-bearing extern renders compensated, not bare.
    assert "[compensated]  EdgeApp  host http_put()" in text


# ------------------------------------------ no network boundary → additive only

def test_non_network_realm_reports_an_empty_network_boundary():
    """A realm that touches no `net.*` cap gets a well-formed but empty network
    boundary (total 0, fraction None) — additive, and the pre-254 bare/compensated
    totals are unchanged."""
    ir = compile_files([REALMS])
    report = erase_report.build_report(ir, "alpha", prove_residue=False)
    net = report["boundaryCrossings"]["networkBoundary"]
    assert net["total"] == 0
    assert net["compensableFraction"] is None
    assert report["summary"]["networkCrossings"] == 0
    assert report["summary"]["networkCompensableFraction"] is None
    # the network-boundary line is suppressed when there is no API traffic.
    assert "network boundary:" not in erase_report.render(report)


def test_fully_revertible_realm_has_no_network_boundary():
    """tenants.rvl realm `tenant_a` makes no irreversible crossing at all, so the
    network boundary is empty and the totals are still zero (regression: the new
    fold must not invent a crossing)."""
    ir = compile_files([TENANTS])
    report = erase_report.build_report(ir, "tenant_a", prove_residue=False)
    cross = report["boundaryCrossings"]
    assert cross["total"] == 0
    assert cross["networkBoundary"]["total"] == 0
    assert cross["networkBoundary"]["compensableFraction"] is None
