"""truc — the revl component manager, built in revl (roadmap item 136).

truc is a revl composition that manages revl components: `add` fetches a petit
bout from the registry, `assemble` admits every fetched bout through revl's own
gate before composing them. The differentiator is not code truc implements — it
is the host gate truc *calls* (`revl.compiler.compile_files`, in-process). See
docs/truc.md (identity) and docs/design/truc-architecture.md (architecture).

The console-script entry point is `main`; `python -m revl.truc` works too.
"""

from ._launcher import main

__all__ = ["main"]
