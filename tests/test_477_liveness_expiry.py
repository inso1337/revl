"""Roadmap item 477 — silence-driven liveness expiry for hung providers.

The gap item 477 names: revl withdrawal is FAULT-driven. A provider that
faults settles into ``FAILED`` and its withdrawal carries a
:func:`why_runtime.cause_trigger` with a classifiable diagnostic ``code``;
dependents tear down through the ``provider-withdrawn`` cascade. There is no
vocabulary for the QUIET case — a provider that neither fails nor answers
(deadlock, stuck host-call, partition with no failure signal).

This file pins the smallest landed slice of the runtime complement: a
withdrawal-WITH-CAUSE that is distinct IN KIND from a fault, plus the pure gate
that decides expiry from a declared ceiling and an observed silence. The
source-level ``liveness`` declaration grammar and ``reconcileLivenessFromWorld``
on restart are the stated follow-ups in docs/design/477-liveness-expiry.md; the
cause taxonomy and the gate are what a producer of either will consult, so they
are the part that is testable with no runtime (the same honesty rule the pure
layer of test_why_runtime.py follows).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import why_runtime as wr  # noqa: E402


# ---- the pure gate ------------------------------------------------------

def test_gate_fires_only_on_a_silence_past_the_ceiling():
    assert wr.liveness_expired(1000, 1500) is True      # silent past ceiling
    assert wr.liveness_expired(1000, 1000) is False     # exactly at: still live
    assert wr.liveness_expired(1000, 250) is False      # well inside


def test_gate_is_defensive_about_a_partial_world():
    # no ceiling declared -> can never expire (never fabricate an expiry)
    assert wr.liveness_expired(None, 10_000) is False
    assert wr.liveness_expired(0, 10_000) is False
    assert wr.liveness_expired(-5, 10_000) is False
    # no liveness observation yet -> not-yet-expired, not an infinite silence
    assert wr.liveness_expired(1000, None) is False


# ---- the cause is distinct in KIND from a fault -------------------------

def test_liveness_expiry_is_a_distinct_cause_kind():
    expiry = wr.cause_liveness_expired(ceiling_ms=1000, silent_ms=1500)
    fault = wr.cause_trigger("host raised", code="R2")
    withdrawn = wr.cause_provider_withdrawn("PgDatabase", "db", code="R2")

    assert expiry["kind"] == wr.LIVENESS_EXPIRED
    # the three withdrawal roots/edges never collide
    assert expiry["kind"] != fault["kind"]
    assert expiry["kind"] != withdrawn["kind"]
    # a fault carries a classifiable diagnostic code; an expiry must NOT — it
    # classifies no error, and a fabricated code would let the QUIET case
    # masquerade as a fault.
    assert "code" in fault
    assert "code" not in expiry
    # the operator-visible accounting rides on the cause
    assert expiry["ceilingMs"] == 1000
    assert expiry["silentMs"] == 1500


# ---- a hung provider is withdrawn-with-cause, roots there, cascades -----

def _hung_provider_trace() -> list[dict]:
    """PgDatabase hangs (silent 1500ms past a 1000ms ceiling) and is withdrawn
    with a liveness-expiry cause; UserCache goes down because it injects `db`,
    exactly as it would under any other withdrawal of its provider."""
    return [
        wr.make_event(0, 1, wr.LOAD, "PgDatabase", "PENDING -> ACTIVE",
                      wr.cause_boot()),
        wr.make_event(1, 1, wr.LOAD, "UserCache", "PENDING -> ACTIVE",
                      wr.cause_requirements([{"component": "PgDatabase",
                                              "key": "db"}])),
        wr.make_event(2, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("PgDatabase", "db")),
        wr.make_event(3, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_liveness_expired(ceiling_ms=1000,
                                                silent_ms=1500)),
    ]


def test_hung_provider_withdrawal_roots_at_the_liveness_expiry():
    trace = wr.Trace(_hung_provider_trace())

    # the provider's own withdrawal roots at the expiry — NOT mistaken for a
    # provider cascade, NOT a fault trigger.
    root = trace.cause_chain("PgDatabase")
    assert len(root) == 1
    assert root[0].cause["kind"] == wr.LIVENESS_EXPIRED

    # the dependent still tears down through the ordinary provider-withdrawn
    # edge, and its chain walks up to the hung provider's expiry root.
    dep = trace.cause_chain("UserCache")
    assert [f.component for f in dep] == ["UserCache", "PgDatabase"]
    assert dep[0].cause["kind"] == wr.PROVIDER_WITHDRAWN
    assert dep[-1].cause["kind"] == wr.LIVENESS_EXPIRED


def test_render_names_the_expiry_as_root_cause_and_shows_the_accounting():
    trace = wr.Trace(_hung_provider_trace())
    rendered = wr.render_chain("PgDatabase", trace.cause_chain("PgDatabase"))
    assert "(root cause)" in rendered           # a root, like boot/trigger
    assert "1500ms" in rendered and "1000ms" in rendered  # operator-visible
    assert "hung" in rendered
