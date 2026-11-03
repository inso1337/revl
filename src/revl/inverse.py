"""The single inverse-provenance vocabulary of an audit crossing (issue #2692).

Before this module `revl` recorded pieces of "can this crossing be undone, and
how far can the undo be trusted" in several unrelated places: the crash-recovery
re-idempotency register (`docs/crash-recovery.md` §4b), the approval classes
(`mcp/effect_classes.py`), an emission's `compensate` offset, the rule that an
emission is irreversible (`docs/backend-ir-v3.md`), and the `undo_idempotent`
flag on the audit's extern entries. A reviewer had to read a different document
per crossing to answer one question, and the "nothing is known" case existed
only as a recovery OUTCOME after the fact, never as a property of the crossing
before it runs.

`inverse_class` derives one value from the facts already in the IR, and
`revl audit --json` carries it as the `inverse` field of every extern entry (the
per-component crossing in `boundary[name].externs[]` and the declared extern in
the top-level `externs` list). There is no new syntax and no behaviour change:
nothing in the language or the runtime reads this field.

The vocabulary is CLOSED. A consumer may switch on it, and an unrecognised value
is to be treated as `unknown`:

    declared      an exact inverse the author declared (`undo ...`)
    generated     an exact inverse derived from trusted metadata (no current
                  declaration produces one; reserved for a synthesized inverse,
                  which is out of scope for this slice)
    compensation  a best-effort offset (`compensate ...`), not a restoration
    forward       irreversible, but recoverable forward by re-issuing the
                  crossing, where the declaration carries an idempotency
                  register (a deferred emission with `idempotent` or
                  `idempotent(key: ...)`). The strength is the register's, not
                  this class's: `read`/`keyed` are free to re-issue, while a
                  `declared` claim is fenced and operator-gated.
    irreversible  an emission with no inverse, no offset and no forward claim
    unknown       no provenance fact at all

The class is DERIVED, never inferred from a name or a shape. `open`/`close`-style
pairing is refused: an inverse is accepted only from a declaration (the `undo`
slot) or from trusted metadata, and anything else is `unknown`. Extending
`inverse_class` to read synthesized inverses is a later slice; this one only
makes the field exist, visible, and diffable.

`unknown` is a VALUE, never a missing field: every audit extern entry this
version emits carries `inverse`, so a consumer never has to tell "no provenance"
from "the producer did not know about the field". A document captured before the
field existed has no `inverse` on its externs at all, and that absence is not
`unknown`; `audit_diff.diff_inverses` treats it as not comparable.
"""

from __future__ import annotations

#: The closed vocabulary, ordered from the strongest recovery story to the
#: weakest. This order is the trust order `audit_diff.diff_inverses` uses: a move
#: toward the front is a strengthening of the recovery story, a move toward the
#: back is a weakening. `generated` sits behind `declared` because this slice
#: does not yet accept a synthesized inverse as stronger evidence than an
#: author's exact declaration; it is a reserved value, not an absent one.
INVERSE_CLASSES = (
    "declared",
    "generated",
    "compensation",
    "forward",
    "irreversible",
    "unknown",
)

#: rank by position: `declared` is the highest, `unknown` the lowest. A class
#: outside the vocabulary ranks below `unknown`, so a payload naming something
#: this revl does not know is never read as an improvement.
_INVERSE_RANK = {name: len(INVERSE_CLASSES) - index
                 for index, name in enumerate(INVERSE_CLASSES)}


def inverse_class(extern: dict | None) -> str:
    """The inverse provenance of one declared extern, as one closed-vocabulary
    class.

    *extern* is a lowered extern IR entry (or an audit extern entry, which
    carries the same `class`/`undo`/`compensate`/`deferred`/`register` fields),
    or `None` for a crossing with no declaration behind it (the `"*"`
    first-class dispatch in a boundary table). Every branch reads a DECLARED
    fact; none looks at a name or a shape.

    Ordering of the cases is the vocabulary's trust order: an exact inverse, then
    an offset, then a forward-only recovery claim, then the emission rule, then
    `unknown`. An emission is irreversible by `docs/backend-ir-v3.md`, so it
    reads `irreversible` unless it carries a compensation or a forward claim; a
    non-emission class with no `undo` (a `pure` read or a `local` write, both of
    which are forbidden a slot) has no provenance fact at all and reads
    `unknown`.
    """
    if not extern:
        return "unknown"
    cls = extern.get("class")
    if cls == "emission":
        if extern.get("compensate") is not None:
            return "compensation"
        # A deferred emission is the one crossing with an owed record to
        # re-issue. It is forward-recoverable only when the declaration carries
        # an idempotency register (`idempotent`, `idempotent(key: p)`); without
        # one the recovery is the absent tier of `docs/crash-recovery.md` §4b,
        # which is not a forward claim at all. The register itself, carried
        # beside this class, says how strong the claim is: `read`/`keyed` are
        # free to re-issue, `declared` is fenced and operator-gated.
        if extern.get("deferred") and extern.get("register"):
            return "forward"
        return "irreversible"
    if cls in ("acquire", "witnessed") and extern.get("undo") is not None:
        return "declared"
    return "unknown"


def inverse_rank(cls: str | None) -> int:
    """The trust rank of an inverse class, for the audit diff's direction.

    A value outside the vocabulary ranks below `unknown`: an unrecognised payload
    is never read as a strengthening.
    """
    return _INVERSE_RANK.get(cls, 0)
