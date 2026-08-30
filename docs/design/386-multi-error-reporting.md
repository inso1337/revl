# 386: report ALL refusals per compile, not just the first

Design note for roadmap item 386. This is design-first. It changes no parser,
typecheck, lower, or runtime code. It records how the compiler fails-fast on the
first refusal today, a collect-and-continue recovery model, how to recover past
a refusal without emitting cascading false errors, the per-file vs per-compile
scope decision, the output shape, and a staged plan an implementation agent can
pick up.

## Status

SKELETON. Research and design in progress.
