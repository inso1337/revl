"""What a model council ANSWERS with, at the type level (item 516 slice 3).

`docs/design/543-model-council.md` section 3 is the design; this module is the
half of it that a caller can hold in its hand. Slice 1 checked the
DECLARATION, slice 2 bound a council to an action, and both stop at the point
where the council would produce something. Until this module there was no
answer type at all: `Aggregate[` occurred once in `src/revl/`, in a comment in
`revl.model_council` saying it was described and not written, so the roadmap's
first exit clause - "a two-member council that disagrees does not admit" - had
no call site to be true at.

THE TYPES
---------
A member answers with `Answer[T]`, not with `T`::

    Says(T) | Abstains | Unreachable

`Abstains` and `Unreachable` are separate constructors although the
aggregation treats them alike, because the evidence has to tell a member that
said "I will not say" from a member whose host was down: the first is a
statement and the second is not, and a record that conflates them cannot say
whether the council is structurally broken or merely unlucky.

The council answers with `Aggregate[T]`::

    Agreed(T) | Split(Dissent) | Inquorate(Dissent)

**`Split` carries no `T`.** That sentence is the whole item at the type level.
There is no total projection `Aggregate[T] -> T`, because the only constructor
holding an answer is the one that means the members agreed. A caller that
wants a `T` has to match, and the arm it writes for `Split` cannot be
satisfied by reading a value out of the payload, because the payload is not of
that type. That is a stronger guarantee than a rule forbidding the shortcut,
because it needs no rule.

`Dissent` is the per-member record in DECLARED member order, and the answers
in it are carried as DIGESTS rather than as values of `T`. A `T` inside
`Split` is a `T` in hand, and the next author writes `split[0].answer`, which
is `aggregate first` spelled at the call site where the checker cannot see it.
A digest does not typecheck as the value, so the shortcut is not available.
It is also item 517's decision verbatim: a hash binds the answer without
moving the content, and a `Split` full of raw completions would carry the
cloud proposer's text to wherever the caller logged the dissent.

THE RULE THIS MODULE ENFORCES
-----------------------------
`G-COUNCIL-SPLIT`, the same guarantee slice 1 registered
(`docs/design/557-council-disagreement.md`, issue #1190): **disagreement can
never be silently resolved toward allow.**

Slice 1 spends that guarantee on the declaration, refusing `on_tie allow`,
`quorum answered`, a missing `aggregate` and `aggregate first` BY NAME. This
module spends it at the call site, and it has to, because an answer type whose
consumer may ignore the dissent arm would undo all four of those refusals one
level up: a council declared with every safe word still tells its caller
nothing if the caller is allowed to write a match that only mentions `Agreed`.

So a `match` on an `Aggregate[T]` must NAME `Split` and `Inquorate`. Two
things make that rule bite, and both of them are gaps in the general
exhaustiveness check rather than new opinions about matching:

1. **A `_` catch-all does not discharge them.** For an ordinary ADT a
   wildcard is a fine way to say "everything else", and `revl.lower`'s
   `_check_match_exhaustiveness` accepts one. Here "everything else" is
   exactly the case the construct exists to make un-ignorable, and a
   `_ => proceed` arm is `on_tie allow` written at the call site. The author
   who wants one outcome for both dissent arms writes both arms; that costs a
   line and says which line it is.
2. **A parameterised scrutinee is checked.** The general check reads
   `types[<scrutinee type>]`, which misses `Aggregate[Str]` because the table
   is keyed by the bare head. `Aggregate[T]` is never written without an
   argument, so without this the rule would be vacuous on every real program.
   The fix here is deliberately scoped to the provided types (see
   `ANSWER_TYPES`) rather than applied to every generic ADT: widening the
   general check is a change to what the compiler admits for programs that
   have nothing to do with councils, and it belongs to whoever owns that
   check.

NOT ITEM 471, AND NOT BY CONVENTION
-----------------------------------
A council member is a model. It has a function and a placement and NO
identity, it consents to nothing, and its agreement is not approval (design
note 543 section 10). Item 471's multi-party human approval is a different
construct whose votes come from operators and are statements about consent.

This module is kept out of that vocabulary the same way `revl.model_council`
is, and it is checked the same way: it imports nothing but the error type, so
it cannot reach the approval machinery, and no refusal it raises uses one of
471's words. `Split` means the models disagreed. It does not mean anyone
declined.
"""

from __future__ import annotations

from .errors import RevlError

# The guarantee, unchanged from slice 1 (`revl.model_council.CODE`). It is
# spelled here rather than imported because `revl.model_council`'s import list
# is pinned by a test that keeps item 471's machinery out of the council
# checker, and an import EDGE in the other direction would make this module a
# second thing that has to be read before that pin can be trusted. The two
# constants are kept equal by `test_the_answer_type_carries_the_same_code`.
CODE = "G-COUNCIL-SPLIT"
CATEGORY = "model-council"

_DESIGN = "docs/design/543-model-council.md"

#: The council's answer type. Named here because the exhaustiveness rule below
#: is about THIS type and not about generic ADTs in general.
AGGREGATE = "Aggregate"

#: A single member's answer type.
ANSWER = "Answer"

#: One row of the dissent record: which member, where it ran, and what it said
#: as a DIGEST. `Dissent` of design section 3 is `List[DissentEntry]`; the
#: alias itself is not provided because `revl.lower` expands an alias at the
#: point of use, so a provided one would not survive into the case payloads
#: where the "no `T` on the `Split` path" property has to be readable.
DISSENT_ENTRY = "DissentEntry"

#: What `Split` and `Inquorate` carry.
DISSENT = f"List[{DISSENT_ENTRY}]"

#: The arms of `Aggregate[T]` that a consumer may not leave unnamed. Both of
#: them, and for different reasons that design section 5 keeps apart: `Split`
#: means the council worked and the members disagree, which is information
#: about the QUESTION; `Inquorate` means the council did not work, which is
#: information about the DEPLOYMENT. A caller may escalate the first and
#: alarm on the second, and conflating them makes both untreatable - so
#: neither may be reached by a wildcard that also swallows the other.
DISSENT_ARMS = ("Split", "Inquorate")

#: The constructor that carries a value, and the only one.
AGREED = "Agreed"

#: The provided type table, in `revl.lower`'s own `types` shape so it can be
#: merged into the program's table with no translation. Seeded ONLY into a
#: program that names one of them (`mentions`), which is what keeps every
#: program on the tree byte-identical through here: design section 9's "an
#: admitted program is byte-identical to the same program with the declaration
#: deleted" is a property of the COUNCIL declaration, and a type nobody names
#: must not cost an IR entry either.
ANSWER_TYPES: dict[str, dict] = {
    ANSWER: {
        "params": ["T"],
        "kind": "variant",
        "cases": [
            {"name": "Says", "payload": "T"},
            {"name": "Abstains", "payload": None},
            {"name": "Unreachable", "payload": None},
        ],
    },
    DISSENT_ENTRY: {
        "params": [],
        "kind": "record",
        "fields": {
            # what the member was FOR, and where it ran: design section 3's
            # record, in declared member order.
            "function": "Str",
            "role": "Str",
            "residence": "Str",
            # the answer as a DIGEST, never as a `T`. See the module docstring.
            "answer": f"{ANSWER}[Str]",
        },
    },
    AGGREGATE: {
        "params": ["T"],
        "kind": "variant",
        "cases": [
            {"name": AGREED, "payload": "T"},
            {"name": "Split", "payload": DISSENT},
            {"name": "Inquorate", "payload": DISSENT},
        ],
    },
}

#: Every name this module provides. A `type` declaration may not take one, for
#: the reason `Principal` may not be declared (`revl.lower._lower_type_decls`):
#: a program that can write its own `Aggregate[T]` can write one whose only
#: case is `Agreed`, and the rule below would then be enforcing a property of
#: a type the program had already opted out of.
PROVIDED = tuple(ANSWER_TYPES)


def _applied(type_name: str | None) -> tuple:
    """`"Aggregate[Str]"` -> `("Aggregate", ["Str"])`; `"Str"` -> `("Str", [])`.

    A deliberately small reader, and not `revl.typecheck.parse_type`, which
    answers the same question for the whole type algebra. This module imports
    the error type and nothing else, so that the argument keeping it out of
    item 471's vocabulary - it cannot reach the approval machinery - is one an
    `ast` walk over the import list can check, exactly as
    `revl.model_council`'s is. Function types are not read apart here because
    a provided type inside one still shows up in the split below.
    """
    if not type_name:
        return None, []
    name = type_name.strip()
    if "[" not in name or not name.endswith("]"):
        return name, []
    head, _, rest = name.partition("[")
    inner, depth, args = rest[:-1], 0, []
    current = ""
    for ch in inner:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        args.append(current.strip())
    return head.strip(), args


def mentions(type_name: str | None) -> bool:
    """Does `type_name` name one of the provided types, at any depth?

    APPLIED, not bare: `Aggregate[Str]` counts and `Aggregate` does not. The
    provided types are both generic, so a bare head is not a use of one, and
    requiring the argument is what keeps a program that has its own
    non-generic `Answer` - `examples/model_council_binding.rvl` declares
    `service Answer`, which is a different namespace and a real name in the
    corpus today - from dragging a type table entry in behind it.
    """
    head, args = _applied(type_name)
    if head in (AGGREGATE, ANSWER) and args:
        return True
    return any(mentions(arg) for arg in args or ())


def seeded(types: dict, declared) -> dict:
    """`types` with the provided entries added, when the program names one.

    `declared` is every type the program WROTE DOWN - parameters, results,
    record fields, case payloads - and a provided type is seeded only if one
    of them names it applied. That condition is what keeps design section 9's
    byte-identity honest one level further than section 9 itself claims: not
    only does a council declaration cost the IR nothing, a provided type
    nobody names costs it nothing either, so every program in the tree that
    predates this slice emits exactly what it emitted.

    `DISSENT_ENTRY` rides in with the others rather than on its own mention:
    it is reachable only THROUGH a `Split` or an `Inquorate` payload, so a
    program naming `Aggregate[T]` needs it and a program that does not cannot
    see it.
    """
    if not any(mentions(t) for t in declared):
        return types
    # The program's own declarations win a name collision rather than being
    # shadowed silently; `_lower_type_decls` refuses the collision outright
    # (see `reserved_name_error`), so this order is what a reader would guess
    # and never what decides a program.
    return {**ANSWER_TYPES, **types}


def applied_spec(type_name: str | None) -> dict | None:
    """The variant spec for an APPLIED provided type, `T` substituted.

    `Aggregate[Str]` -> the `Aggregate` spec with `Agreed`'s payload read
    `Str` and the two dissent arms' read `List[DissentEntry]`. `None` for
    everything else, including a bare `Aggregate`, which is not a use of a
    generic type.

    Both the checker's match inference and the lowering's exhaustiveness pass
    look a scrutinee's spec up by its WHOLE spelling, which finds nothing for
    any applied generic. That is a general gap and this is not a general fix:
    it resolves the two provided types and leaves every user ADT exactly as it
    was, because widening the lookup for user ADTs changes what the compiler
    admits for programs that have nothing to do with councils.
    """
    head, args = _applied(type_name)
    spec = ANSWER_TYPES.get(head or "")
    if spec is None or not args or spec.get("kind") != "variant":
        return None
    bound = dict(zip(spec.get("params") or [], args))
    return {
        "params": [],
        "kind": "variant",
        "cases": [
            {"name": case["name"],
             "payload": bound.get(case["payload"], case["payload"])}
            for case in spec.get("cases", [])
        ],
    }


def arm_payload(scrutinee_type: str | None, pattern: str) -> str | None:
    """What a match arm over a PROVIDED type binds, with `T` substituted.

    This is the other half of "`Split` carries no `T`", and without it that
    sentence is prose rather than a property. `revl.lower._arm_payload_type`
    looks a variant's payload up by the scrutinee's whole spelling, which
    finds nothing for an applied generic, so every arm over an `Aggregate[Str]`
    would bind a value of UNKNOWN type - and an unknown type unifies with
    whatever the arm is asked to produce. `Split(d) => d` in a function
    returning `Str` would then compile, which is exactly the total projection
    `Aggregate[T] -> T` that design section 3 says does not exist.

    Substituting gives `Agreed` the `Str` and gives `Split` and `Inquorate`
    the dissent record, so the shortcut stops typechecking rather than being
    forbidden by a rule.
    """
    head, args = _applied(scrutinee_type)
    spec = ANSWER_TYPES.get(head or "")
    if spec is None or not args:
        return None
    params = spec.get("params") or []
    for case in spec.get("cases", []):
        if case["name"] != pattern:
            continue
        payload = case.get("payload")
        if payload is None:
            return None
        bound = dict(zip(params, args))
        return bound.get(payload, payload)
    return None


def reserved_name_error(filename: str, line: int, name: str) -> RevlError:
    """`type Aggregate[T] = ...` and its siblings, refused.

    The name is reserved rather than merely occupied, and the reason is the
    one that makes the whole slice worth having: the exhaustiveness rule below
    is a statement about a CLOSED set of constructors. A program allowed to
    declare its own `Aggregate` could declare `Agreed(T)` alone, match it
    exhaustively with one arm, and satisfy every rule here while having
    removed the case the construct exists for.
    """
    return RevlError(
        filename, line,
        f"`type {name}` declares a name the model council's answer type "
        f"already has",
        hint=(
            f"`{ANSWER}[T]` and `{AGGREGATE}[T]` are provided, not declared, "
            f"for the reason `Principal` is: a program that declares its own "
            f"`{AGGREGATE}[T]` can leave out the constructor that carries the "
            f"members' disagreement, and every rule about handling that "
            f"constructor would then be checking a type the program had "
            f"already opted out of. Rename this type ({_DESIGN} section 3)"),
        code=CODE, category=CATEGORY)


def unhandled_dissent_error(filename: str, line: int, scrutinee: str,
                            missing: tuple, wildcard: bool) -> RevlError:
    """A `match` on an `Aggregate[T]` that does not name every dissent arm.

    The message deliberately does NOT read "non-exhaustive match". That
    sentence belongs to `revl.lower`'s general check over declared ADTs, and
    the two are different claims: the general one says the author forgot a
    case, this one says the author wrote a consumer that can proceed without
    ever learning that the models disagreed. Sharing a sentence would also
    file this refusal under the type layer's census tag rather than leaving it
    where it belongs, with the flow-position rules the self-host gate does not
    decide (`docs/design/556-model-council-selfhost.md` section 1.3).
    """
    named = ", ".join(f"`{arm}`" for arm in missing)
    plural = "arm" if len(missing) == 1 else "arms"
    if wildcard:
        why = (
            f"a `_` arm does not answer for {named}: it is the one place the "
            f"members' disagreement could be read, and a catch-all that "
            f"swallows it is `on_tie allow` written at the call site instead "
            f"of in the declaration, where the checker can see it")
    else:
        names_it = "names none of them" if len(missing) > 1 else "omits it"
        why = (
            f"the council's answer carries its disagreement in {named}, and a "
            f"consumer that {names_it} can proceed without ever learning that "
            f"the members did not agree")
    return RevlError(
        filename, line,
        f"`match` on `{scrutinee}` leaves the model council's {plural} "
        f"{named} unhandled",
        hint=(
            f"{why}. `Split` means the council worked and the members "
            f"disagree, which is about the question; `Inquorate` means the "
            f"council did not work, which is about the deployment, so the two "
            f"are named separately and neither carries a `T` to fall back on. "
            f"Write an arm for each ({_DESIGN} section 3)"),
        code=CODE, category=CATEGORY)


def check_match(scrutinee_type: str | None, patterns, filename: str,
                line: int) -> None:
    """Refuse a `match` over an `Aggregate[T]` that ignores the dissent.

    `patterns` is the arm patterns in source order, `_` included. Silent for
    every other scrutinee, which is every match in the tree today: this is an
    ADDITIONAL rule over one provided type, not a change to how matching
    works.
    """
    head, args = _applied(scrutinee_type)
    if head != AGGREGATE or not args:
        return
    named = set(patterns)
    missing = tuple(arm for arm in DISSENT_ARMS if arm not in named)
    if not missing:
        return
    raise unhandled_dissent_error(filename, line, scrutinee_type or AGGREGATE,
                                  missing, wildcard="_" in named)
