"""Issue #2216: the marked slow lane.

A test that is slow by NATURE rather than by repeating work a session could
share carries `@pytest.mark.slow`. The fast lanes (the pre-commit hook,
`pre-merge --affected`, and CI's pull-request `root-suite-affected` job) set
REVL_SKIP_SLOW=1 and tests/conftest.py deselects the marked tests; the full
gate (plain `make pre-merge`) and the merge-queue `frontend` matrix do not, so
the slow lane still runs before land.

Kept small and pure so both halves are unit-testable without a pytest run:
`enabled` decides the lane from an environment mapping, `split` partitions a
collection on the marker.
"""

from __future__ import annotations

ENV = "REVL_SKIP_SLOW"
MARKER = "slow"


def enabled(environ) -> bool:
    """True when this run is a fast lane that deselects the slow marker."""
    return environ.get(ENV) == "1"


def split(items):
    """(kept, deselected) partitioned on the slow marker, order preserved."""
    slow, fast = [], []
    for item in items:
        target = slow if item.get_closest_marker(MARKER) is not None else fast
        target.append(item)
    return fast, slow
