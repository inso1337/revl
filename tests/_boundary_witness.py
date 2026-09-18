"""One shared check for the self-host emit oracles' deferral-boundary witnesses.

A boundary witness exists to prove that a port and its reference emitter
disagree at a NAMED place: the reference emits ``reference_token``, the port
answers with ``port_token`` instead, and the two outputs therefore differ. That
proof only holds if ``port_token`` is text ONLY the port emits. When it is text
BOTH sides emit, the case still passes, but for an unrelated reason: it passes
whether the boundary is real, has moved, or has silently closed, while the test
name and the green tick both read correctly.

Three witnesses were in that state (item 1136). The go and wasm ``in-file-tests``
cases pinned the document's own emitted function (``func f()`` / ``$f``) over a
document whose entire test section the port was dropping, and the wasm
``wasm-extern`` case pinned ``;; Generated``, which is part of the banner both
sides write. The audit that closed the issue found two more the issue had not
named: go's ``float-format-helper`` (``revlFtoa(x)`` is the call, which the
reference emits too; what the port omits is the helper's definition) and wasm's
``utf8-string-pool`` (``(data`` opens the segment on both sides; what diverges
is the segment's bytes).

The meta-check below is the durable half of that fix: a witness token that
appears in the reference's own output for that document is refused by name,
where the case runs, so a future case cannot be added with the same defect. Its
non-vacuity is proven by planting the exact historical token back --
``test_a_witness_token_both_sides_emit_is_rejected`` in
tests/test_selfhost_emit_go.py and tests/test_selfhost_emit_wasm.py.

Not every boundary has port-only text. Where the port DROPS something the
reference emits and puts nothing in its place, the witness runs the other way:
pass ``port_token=None``, and the reference token's ABSENCE from the port's
output is the evidence. That direction cannot hold for an unrelated reason
either, because ``reference_token`` is asserted PRESENT in the reference's
output first, so the pair of assertions names both halves of the divergence.
"""

_SHARED_TOKEN_REASON = (
    "boundary witness {token!r} is text the REFERENCE also emits for this "
    "document, so this case passes whether or not the boundary is still there. "
    "Pin text only the port emits (its `<<DEFER-...>>` / `<<UNSUPPORTED-...>>` "
    "marker, or the divergent bytes themselves), or pass port_token=None to "
    "witness the boundary by the reference token's absence from the port's "
    "output."
)


def shared_witness_token_reason(reference_output, port_token):
    """Why ``port_token`` cannot witness a boundary, or ``None`` if it can.

    Returns a sentence when the token is text the reference emits too. Callers
    that cannot raise ``AssertionError`` (an xfail-strict case, say) can feed it
    to ``pytest.fail``; the rest use ``assert_boundary_witness`` below.
    """
    if port_token is None or port_token not in reference_output:
        return None
    return _SHARED_TOKEN_REASON.format(token=port_token)


def assert_boundary_witness(reference_output, port_output, reference_token,
                            port_token):
    """Hold one deferred family to the boundary its case names."""
    assert reference_token in reference_output, (
        f"the reference no longer emits {reference_token!r}: this case no "
        "longer exercises its reference boundary"
    )
    reason = shared_witness_token_reason(reference_output, port_token)
    assert reason is None, reason
    if port_token is None:
        assert reference_token not in port_output, (
            f"the port emits {reference_token!r} now too: the boundary has "
            "closed, move its witness into CORPUS"
        )
    else:
        assert port_token in port_output, (
            f"the port no longer emits {port_token!r}: the boundary has moved"
        )
    assert port_output != reference_output, (
        "boundary is stale: move its witness into CORPUS"
    )
