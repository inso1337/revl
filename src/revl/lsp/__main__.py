"""`revl lsp` (the documented happy path) — `python -P -m revl.lsp` (the
`-P` is the PYTHONSAFEPATH safety bit, issue #317) is the
absolute-interpreter fallback, kept in lockstep with the subcommand
form because both spellings are real entry points for an editor
launching the language server. A separate entry point rather than a
`revl` subcommand, so it does not touch the CLI dispatch a parallel
roadmap item owns.
"""

from __future__ import annotations

import sys

# issue #317: see revl._safepath — `-m` puts the working directory at
# sys.path[0]. An editor launches this with the project root as its cwd.
from .._safepath import drop_cwd_entry

drop_cwd_entry()

from .server import serve

if __name__ == "__main__":
    sys.exit(serve())
