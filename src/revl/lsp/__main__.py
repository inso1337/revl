"""`python -m revl.lsp` — run the language server over stdio.

An editor launches this and speaks LSP on the process's stdin/stdout. A
separate entry point rather than a `revl` subcommand, so it does not touch the
CLI dispatch a parallel roadmap item owns.
"""

from __future__ import annotations

import sys

from .server import serve

if __name__ == "__main__":
    sys.exit(serve())
