"""The `--recompute` evidence producer of `revl policy evaluate` (item 290 §4).

It runs the operator's own local producers (the fault sweep, the inverse round
trip) against a component, which is running the program, not compiling it. It
lived in `revl.policy`, whose import chain the compiler shares, so its lazy
`import revl.fault` put the fault sweep, the tier runners, placement and
`revl.mcp` on the compiler's import graph (issue #1780). The policy evaluator
now takes it as an argument (`recompute_producer`), and the one caller that
recomputes, the `revl policy evaluate` command, passes it.
"""

from __future__ import annotations

from dataclasses import replace


def recompute_component(ir: dict, name: str, published,
                         *, gauntlet_dossier) -> tuple:
    """Item 290 §4, slice 3 (`--recompute`): run the operator's OWN local
    producers against the component in hand and return
    ``(bundle, recomputed_facets)``.

    The producers are the shipped entry points, reused verbatim (never
    re-derived here): `fault.sweep_dossier` (the per-component fault sweep),
    `fault.roundtrip_dossier` (the composition's inverse round-trip), and the
    caller-supplied cold `mcp.gauntlet.run` dossier. Operator-run evidence needs
    no attestation root — the operator produced it — so the facets it yields are
    marked `recomputed` (§6.3: rooted by construction). A producer whose runtime
    is absent is honestly skipped (never faked): that facet is left as the
    PUBLISHED bundle carried it and stays out of `recomputed_facets`, so the
    report keeps marking it `published`.
    """
    from . import fault  # noqa: PLC0415 — lazy: the runtime-tested producers
    from . import registry as reg  # noqa: PLC0415
    fields: dict = {}
    recomputed: set = set()
    try:
        fields["fault_sweep"] = fault.sweep_dossier(ir, only=name)
        recomputed.add("fault-sweep")
    except (ModuleNotFoundError, ImportError):
        pass  # cordis-py absent: honestly unavailable, never faked
    try:
        fields["inverse_roundtrip"] = fault.roundtrip_dossier(ir)
        recomputed.add("inverse-roundtrip")
    except (ModuleNotFoundError, ImportError):
        pass
    if gauntlet_dossier is not None:
        fields["gauntlet"] = gauntlet_dossier
        recomputed.add("gauntlet")
    base = published if published is not None else reg.EvidenceBundle()
    return replace(base, **fields), frozenset(recomputed)
