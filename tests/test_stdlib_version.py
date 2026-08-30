"""The stdlib version stamp — drift detection for vendored stdlib copies
(roadmap item 389).

A consumer (the revl-harness) holds byte-COPIES of ``stdlib/{json,value,str}.rvl``.
When item 104 added ``value_is_object`` upstream the compiler began recommending
a fix (``value_is_object``) that did not exist in the consumer's copied tree, and
nothing noticed. The stamp closes that gap: a single ``pub fn stdlib_version()``
travels with every copy of the stdlib, and the compiler knows the version it
ships. A consumer can read its own copy's stamp and compare it to the compiler's
``EXPECTED_STDLIB_VERSION`` to detect that its vendored stdlib has drifted.

This suite proves:
  * the in-repo ``stdlib/version.rvl`` stamp matches the compiler's expected
    version (they can never silently disagree inside the checkout);
  * ``read_stamp`` reads the literal out of a stdlib tree, and returns None for a
    tree that predates the stamp;
  * a DRIFTED vendored copy (an older stamp, or no stamp at all) is DETECTED by
    ``check_drift``;
  * ``revl doctor`` warns when the resolved stdlib's stamp differs from the
    compiler's expected version;
  * ``stdlib_version()`` runs cross-tier (py + ts) and returns the current stamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.stdlib_version import (  # noqa: E402
    EXPECTED_STDLIB_VERSION,
    check_drift,
    parse_stamp,
    read_stamp,
)


def test_expected_version_is_nonempty():
    assert EXPECTED_STDLIB_VERSION
    assert isinstance(EXPECTED_STDLIB_VERSION, str)
