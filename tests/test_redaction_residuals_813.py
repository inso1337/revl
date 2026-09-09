"""Canaries for the documented redaction residuals (issue #813).

`test_secret_externalization.py` covers leak-PREVENTION canaries (registered
secrets are scrubbed from every sink). This file pins the DOCUMENTED
LIMITATIONS those canaries cannot catch — the residual edges where the
exact-match funnel deliberately does NOT scrub, so a regression that widened
coverage (or a new sink that forgot `redact_call_text`) fails a test instead of
opening a quiet leak. Each residual has a POSITIVE control (scrubbed where the
contract says it is) and a NEGATIVE canary (not scrubbed where the contract
says it isn't), plus a boundary check at the two length-floors.

The three residuals documented in `backends/python/confidential.py`:
  1. exact-match misses host-side reformatting (truncation, case-fold, %.2f);
  2. `_MIN_MARKABLE = 4` floor: secrets shorter than the floor are not registered
     and print verbatim through every registry funnel;
  3. consumer call args are never registered by design — only
     `redact_call_text` covers them, so a sink that forgets it reopens F5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backends" / "python"))

import confidential  # noqa: E402  (load path mirrors test_secret_externalization.py)


@pytest.fixture(autouse=True)
def _isolate_registry():
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


CANARY = "SEKRIT-CANARY-813-LONG"   # >= _MIN_MARKABLE
PLACEHOLDER = confidential.REDACTED
PLACEHOLDER_ARG = confidential.REDACTED_ARG


# --- residual 1: exact-match misses host-side reformatting -------------------

def test_residual1_a_registered_secret_is_scrubbed_in_full():
    confidential.register_secret_value("abcdef")
    out = confidential.redact_text("vault refused key abcdef at host 1.2.3.4")
    assert "abcdef" not in out
    assert PLACEHOLDER in out


def test_residual1_b_truncated_form_leaks():
    """A host that truncates before printing emits `abcde`, none of whose
    registered needle is an exact match for the remembered `abcdef` — so it
    crosses. FAILS if anyone tightens the matcher to prefix/substring, forcing
    a review of the documented tradeoff (confidential.py:346-348)."""
    confidential.register_secret_value("abcdef")
    out = confidential.redact_text("vault refused key abcde at host 1.2.3.4")
    assert "abcde" in out, out       # truncated form is NOT a registered needle
    assert PLACEHOLDER not in out    # and therefore not redacted


def test_residual1_c_case_fold_leaks():
    """The match is exact: an upstream that lower-cases trace text emits
    `ABCDEF`, no needle matches the registered lowercase `abcdef`."""
    confidential.register_secret_value("abcdef")
    out = confidential.redact_text("VAULT REFUSED KEY ABCDEF AT HOST")
    assert "ABCDEF" in out, out
    assert PLACEHOLDER not in out


def test_residual1_false_positive_prefix_is_not_over_redacted():
    """Exact-match also means: a prefix of a registered value is NOT erased, so
    ordinary diagnostics that share a prefix stay readable."""
    confidential.register_secret_value("abcdef")
    out = confidential.redact_text("abc is just a prefix of something")
    assert "abc" in out
    assert PLACEHOLDER not in out
# --- residual 2: _MIN_MARKABLE = 4 floor -------------------------------------

def test_residual2_the_floor_is_four():
    assert confidential._MIN_MARKABLE == 4


def test_residual2_a_registered_secret_at_the_floor_is_scrubbed():
    confidential.register_secret_value("abcd")              # len == 4 (== floor)
    assert confidential.is_secret_value("abcd")
    out = confidential.redact_text("token=abcd")
    assert "abcd" not in out
    assert PLACEHOLDER in out


def test_residual2_b_a_secret_below_the_floor_leaks():
    """`register_secret_value` refuses strings shorter than `_MIN_MARKABLE`, so a
    1- or 3-char secret is never added to the registry and crosses verbatim. Pins
    the documented coin-flip floor (confidential.py:52-57)."""
    confidential.register_secret_value("k")                 # len 1 < floor
    assert not confidential.is_secret_value("k")
    out = confidential.redact_text("api_key=k")
    assert "k" in out, out
    assert PLACEHOLDER not in out

    confidential.register_secret_value("abc")               # len 3, just below
    assert not confidential.is_secret_value("abc")

# --- residual 3: consumer call args are not registered by design -------------

def test_residual3_a_redact_text_alone_does_not_cover_consumer_args():
    """A CALLER'S OWN argument is transient — never registered, by design, so the
    text funnel has no needle for it. A new sink that prints free-form text
    containing a caller arg MUST go through `redact_call_text`; this canary
    fails if a sink is wired to `redact_text` instead and the arg is secret."""
    caller_arg = "caller-arg-secret-813"        # >= floor, but NOT registered
    out = confidential.redact_text("echo " + caller_arg)
    assert caller_arg in out, out                # text funnel has no needle
    assert PLACEHOLDER_ARG not in out


def test_residual3_b_redact_call_text_covers_the_caller_arg():
    caller_arg = "caller-arg-secret-813"
    out = confidential.redact_call_text("echo " + caller_arg, [caller_arg])
    assert caller_arg not in out, out
    assert PLACEHOLDER_ARG in out


def test_residual3_c_redact_call_text_still_scrubs_registered_secrets():
    """Integration: the call-arg funnel runs first (arg placeholder), then the
    registered-secret funnel — both layers cooperate in one call."""
    confidential.register_secret_value(CANARY)
    caller_arg = "caller-transient-9"
    out = confidential.redact_call_text(
        "err upstream echoed " + CANARY + " with " + caller_arg, [caller_arg])
    assert CANARY not in out
    assert caller_arg not in out
    assert PLACEHOLDER in out
    assert PLACEHOLDER_ARG in out


def test_residual3_d_the_arg_floor_is_three():
    """The arg sub-floor (`_MIN_MATCHABLE_ARG = 3`) is deliberately lower than
    the registration floor: a short caller arg that is ordinary English ("id",
    "on", "a") is left alone. Pins that boundary."""
    assert confidential._MIN_MATCHABLE_ARG == 3
    out_hit = confidential.redact_call_text("x abc y", ["abc"])   # len == 3 -> hit
    assert "abc" not in out_hit
    assert PLACEHOLDER_ARG in out_hit
    out_miss = confidential.redact_call_text("x ab y", ["ab"])     # len 2 -> miss
    assert "ab" in out_miss
    assert PLACEHOLDER_ARG not in out_miss


# --- baseline happy path (guards against the file going vacuous) -------------

def test_baseline_a_registered_secret_is_scrubbed():
    confidential.register_secret_value(CANARY)
    out = confidential.redact_text("vault refused key " + CANARY + " at host")
    assert CANARY not in out
    assert PLACEHOLDER in out


def test_baseline_a_public_value_next_to_a_secret_is_not_over_redacted():
    """Control for the false-positive half: an ordinary value beside a secret
    survives, so the registry redacts what was declared and nothing else."""
    confidential.register_secret_value(CANARY)
    out = confidential.redact_text(
        "vault refused key " + CANARY + " -- note PUBLIC-NOTE-642")
    assert CANARY not in out
    assert "PUBLIC-NOTE-642" in out

