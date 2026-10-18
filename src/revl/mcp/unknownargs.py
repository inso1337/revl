"""Undeclared top-level arguments refused by name (issue #2532).

Every verb declares its arguments in `inputSchema`, and every schema says
`additionalProperties: false`. Nothing enforced it: a key the verb does not
declare was dropped on the floor, and the refusal the caller then got blamed
something else. `revl_plan {withdraw: {...}}` answered "provide `source` or
`files`" and never said that `withdraw` is not an argument of `revl_plan` (it
is one of `revl_change`; the plan verb calls the same thing `replacing`), so
a caller that mixed up two verbs was pointed at the wrong problem.

This runs once, at the transport boundary (`server.handle`, and the inner
call of the `revl_verbs {name, args}` hatch), before any handler and before
the boolean check (`boolargs.py`): a call with an undeclared top-level key is
a usage error that names every such key. For each key it also says where the
key does belong:

- a declared key it is a misspelling of, or the declared key this verb uses
  for the same thing (`_SAME_THING`), as "did you mean `x`";
- the other verbs that declare the key by name, so a caller that reached for
  the wrong verb is told which one it meant.

When exactly one mechanical remedy exists the refusal carries it as `next`
(issue #1691): the same call with the key renamed when it maps to one
declared key and no other verb claims it, or the same call with the unknown
keys dropped when nothing claims them at all. Two candidate remedies (rename
on this verb, or the other verb) are named in the message and left to the
caller.

Handler-to-handler calls never pass through here: `_call_tool` is below the
boundary, so an undo run by `revl_step_back`, or `revl_edit` loading through
`_tool_load`, is checked by nothing it did not already satisfy.
"""

from __future__ import annotations

import difflib

#: One thing, two verbs, two names. The plan family says `replacing` (the
#: components the candidate replaces) where `revl_change` says `withdraw`;
#: `assert` is the bisect predicate; `loses` is the drift verb's spelling.
#: Each maps a spelling to the one a verb may declare instead; the lookup
#: only fires when the target IS declared by the refusing verb.
_SAME_THING = {
    "withdraw": "replacing",
    "replacing": "withdraw",
    "predicate": "assert",
    "losses": "loses",
}


def declared(schema: dict | None) -> set:
    """The top-level keys a schema declares (empty for a schema that declares
    none, or no schema)."""
    if not isinstance(schema, dict):
        return set()
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


def unknown(schema: dict | None, arguments: dict) -> list:
    """The top-level keys of `arguments` the schema does not declare, in the
    order they were sent."""
    if not isinstance(arguments, dict):
        return []
    known = declared(schema)
    return [key for key in arguments if key not in known]


def meant(key: str, known: set) -> str | None:
    """The declared key `key` most plausibly stands for: the verb's own name
    for the same thing, else a close spelling. `None` when neither."""
    alias = _SAME_THING.get(key)
    if alias in known:
        return alias
    close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.75)
    return close[0] if close else None


def elsewhere(key: str, name: str, schemas: dict) -> list:
    """The other verbs that declare `key`, in advertised order."""
    return [verb for verb, schema in schemas.items()
            if verb != name and key in declared(schema)]


#: How many other verbs a hint names before "and N more": `source` alone is
#: declared by some forty verbs, and a list that long is noise, not a hint.
_NAMED = 3


def _named(verbs: list) -> str:
    shown = ", ".join(f"`{verb}`" for verb in verbs[:_NAMED])
    rest = len(verbs) - _NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


def check(name: str, arguments: dict, schemas: dict) -> tuple:
    """`(message, next)` for a call of `name` with undeclared top-level keys,
    or `(None, None)` when every key is declared. `schemas` maps every verb
    to its `inputSchema`, the same objects the server advertises, so the
    rule and the schemas cannot drift."""
    schema = schemas.get(name)
    extra = unknown(schema, arguments)
    if not extra:
        return None, None
    known = declared(schema)
    parts = []
    renames: dict = {}
    claimed = False
    for key in extra:
        guess = meant(key, known)
        others = elsewhere(key, name, schemas)
        verbs = _named(others)
        if guess is not None:
            renames[key] = guess
        if others:
            claimed = True
        if guess is not None and others:
            hint = f"did you mean `{guess}`, or {verbs}?"
        elif guess is not None:
            hint = f"did you mean `{guess}`?"
        elif others:
            hint = f"an argument of {verbs}, not of `{name}`"
        else:
            hint = ""
        parts.append(f"`{key}` ({hint})" if hint else f"`{key}`")
    plural = "s" if len(extra) > 1 else ""
    message = (f"unknown argument{plural} for `{name}`: {', '.join(parts)}. "
               f"`{name}` declares "
               + (", ".join(f"`{key}`" for key in sorted(known)) or "no arguments")
               + "; an argument it does not declare is refused, not ignored")
    return message, _next(name, arguments, extra, renames, claimed)


def _next(name: str, arguments: dict, extra: list, renames: dict,
          claimed: bool) -> dict | None:
    """The one mechanical remedy, when exactly one exists: the call with every
    unknown key renamed to the declared key it stands for (only when no other
    verb claims any of them), or with the unknown keys dropped (only when
    nothing suggests they belong anywhere)."""
    if claimed:
        return None
    if len(renames) == len(extra):
        if len(set(renames.values())) != len(renames) \
                or any(target in arguments for target in renames.values()):
            return None
        fixed = {renames.get(key, key): value for key, value in arguments.items()}
        return {"tool": name, "arguments": fixed, "ready": True}
    if not renames:
        kept = {key: value for key, value in arguments.items()
                if key not in extra}
        return {"tool": name, "arguments": kept, "ready": True}
    return None
