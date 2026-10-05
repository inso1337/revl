"""Nested symbols (issue #1733): the module lives in `revl.nested`, outside
`revl.mcp` (issue #1780); this is that same module object."""

import sys

from .. import nested as _nested

sys.modules[__name__] = _nested
