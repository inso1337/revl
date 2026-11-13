"""The update consents a caller may not grant itself (docs/updates.md).

One definition: the operator surface honours a consent only when the server
was started with ``--update-allow-consent NAME``, the CLI help lists the same
names, and the refusal message names the flag. ``force`` is not here because
it only re-plans the release the composition already runs and opts into
nothing else.
"""

from __future__ import annotations

UPDATE_OPERATOR_CONSENTS = ("allowDowngrade", "acceptNotLive",
                            "acceptIncompatible", "acceptUnknownVersion",
                            "acknowledgeWidening")
