"""IR as a compiler from before issue #2842 emitted it.

Since issue #2842 the compiler refuses every program that uses an emission
callable as a function value (`lower._emission_value_externs`). The layers that
read an IR rather than a source (federation admission, the query facts, the
approval class map, the erase report, the audit diff, the parallel plan, the
cardinality and capability folds) can still be handed an IR built before that
rule, in a plan or a running manifest, so each keeps its own reading of the
value channel. Their tests compile the same source under `value_rule_off()`,
which switches off that one rule and changes nothing else, and pair it with a
check that the current compiler refuses the source.
"""

from __future__ import annotations

import contextlib


@contextlib.contextmanager
def value_rule_off():
    """Compile with the first-class emission value rule switched off."""
    import revl.lower as lower

    saved = lower._emission_value_externs
    lower._emission_value_externs = lambda *args, **kwargs: {}
    try:
        yield
    finally:
        lower._emission_value_externs = saved
