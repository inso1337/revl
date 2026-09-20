"""Item 513: compile the typed model boundary into a decoding grammar.

Item 257 gave a `validated` emission a JSON Schema derived from its return
type and checks the completion against it revl-side. That check is *post hoc*:
the model is asked in prose, answers freely, and the boundary then decides
whether the answer was usable. A schema-valid answer is therefore probabilistic
where it could be mechanical, and every invalid sample is a wasted crossing and
a retry multiplier against item 260's ceiling.

This module derives, from the same declared type, a **decoding grammar**: a
portable description of the language of the responses the type accepts. A
provider that honours it cannot sample a token that leaves that language, so
the shape stops being something the model has to get right.

The compiler/provider split (see `docs/design/542-grammar-constrained-decoding.md`,
sections 2 and 3):

  * revl **states**. It derives grammar text from the declared type, pins a
    digest over it, and binds both into the crossing's IR. The grammar is data,
    identical in all six tiers, and no tier links a decoder.
  * the provider **performs**. Constraining a decode is a host concern, and
    revl does not model it, call it, or verify it.

The two derivations are kept from drifting apart by construction: the grammar
is rendered from the very schema object the validator reads, not from a second
walk over the surface type. The surface type is consulted only for the
*admission* predicate below, which is where the grammar's own refusal lives.

## What has no grammar

Two gates, in order, both at compile time and both fail-closed.

1. `fully_expressible` (item 257, `revl.mcp.schema`). A type with no exact
   schema has no grammar either. This gate is inherited unchanged and is never
   widened here. A context-free grammar *can* express a recursive type, which
   the inline schema cannot, so the grammar domain is naturally wider than the
   schema domain. Admitting a type on the grammar side that the validator then
   refuses would be a false reject at run time, which is worse than the refusal
   it replaces, so the wider domain is deliberately not used. Lifting recursion
   is a change to both derivations at once (§7 of the design note).

2. `grammar_refusal_reason` (below), a grammar-specific gate. A type can have
   an exact schema and still have no *usable* grammar, because a grammar is
   judged on the strings it accepts rather than on the values it validates. The
   case that reaches the surface today is a **null-ambiguous `Opt`**: an
   `Opt[T]` whose `T` already accepts `null` (`Opt[Unit]`, `Opt[Opt[U]]`, and
   those nested in a list, map, record or variant payload). Its grammar has two
   derivations of the string `null`, so a constrained decode that emits `null`
   does not name which revl value was meant. Item 257's schema collapses those
   types -- `Opt[Opt[Str]]` and `Opt[Str]` derive the same schema -- so the
   validator cannot see the ambiguity and neither gate before this one catches
   it.

There is no third outcome. A type that passes both gates gets a grammar; a type
that fails either is refused at compile time with the offending position named.
A response type is never quietly demoted to an unconstrained decode, because a
caller who believes a decode is constrained and is wrong is worse off than one
who was refused.

## Fail-closed in the renderer too

`_render` dispatches on schema node shape and raises on a shape it does not
recognise. It never falls back to a permissive rule. A catch-all alternative in
a decoding grammar is the same failure as a silent downgrade: it reads as a
constraint and accepts everything.
"""

from __future__ import annotations

import hashlib
import json

from .mcp.schema import _parse_type

#: The grammar dialect this module emits. GBNF (llama.cpp's grammar format) is
#: the one constrained-decoding dialect with implementations across local
#: runtimes, which is the motivating case: constraining the decode converts a
#: competence problem into a mechanical one, and that is worth much more to a
#: 7B-class local model than to a frontier one. The key is carried in the IR so
#: a later dialect is an added value, not a reinterpretation of these bytes.
GRAMMAR_FORMAT = "gbnf"

#: The start symbol of every derived grammar.
GRAMMAR_ROOT = "root"

#: Lexical rules, emitted only when the derivation actually reaches them, so a
#: grammar stays as small as the type it came from. Order is fixed for a stable
#: digest.
_LEXICAL: dict[str, str] = {
    "ws": r'ws ::= [ \t\n\r]*',
    "hex": r'hex ::= [0-9a-fA-F]',
    # A raw control character is not legal inside a JSON string, and a grammar
    # that admitted one would let a constrained decode produce a string the
    # boundary's own validator then refuses -- a false reject, which is a worse
    # failure than the one this item removes. The class excludes them.
    "schar": r'schar ::= [^"\\\x00-\x1F] | "\\" ["\\/bfnrt] | "\\u" hex hex hex hex',
    "string": r'string ::= "\"" schar* "\""',
    "integer": r'integer ::= "-"? ("0" | [1-9] [0-9]*)',
    "number": r'number ::= integer ("." [0-9]+)? ([eE] [-+]? [0-9]+)?',
    "boolean": r'boolean ::= "true" | "false"',
    "nullv": r'nullv ::= "null"',
    "b64c": r'b64c ::= [A-Za-z0-9+/]',
    "b64q": r'b64q ::= b64c b64c b64c b64c',
    "b64tail": r'b64tail ::= b64c b64c "==" | b64c b64c b64c "="',
    "b64string": r'b64string ::= "\"" b64q* b64tail? "\""',
}

#: Which lexical rules each lexical rule itself depends on.
_LEXICAL_DEPS: dict[str, tuple[str, ...]] = {
    "string": ("schar",),
    "schar": ("hex",),
    "number": ("integer",),
    "b64string": ("b64q", "b64tail"),
    "b64q": ("b64c",),
    "b64tail": ("b64c",),
}


# --------------------------------------------------------------- admission

def _admits_null(type_name: str | None) -> bool:
    """Does this surface type's grammar accept the bare string `null`?

    `Unit` renders as `null`, and an `Opt[_]` adds `null` as an alternative.
    Nothing else does: item 257 renders every variant of a `validated` boundary
    as a tagged object, so a nullary case is `{"tag": "Nil"}` and not `null`.
    """
    if not type_name:
        return False
    if type_name == "Unit":
        return True
    head, args = _parse_type(type_name)
    return head == "Opt" and bool(args)


def grammar_refusal_reason(type_name: str | None, types: dict | None = None,
                           seen: frozenset = frozenset()) -> str | None:
    """Why this surface type has no unambiguous grammar, or `None` when it has
    one (item 513, §4).

    Runs AFTER `fully_expressible` has accepted the type, so it may assume the
    schema derivation terminates and leaves no unconstrained stub. It walks the
    surface type rather than the schema because the ambiguity it looks for is
    exactly the thing the schema derivation has already collapsed.

    Total for the same reason `fully_expressible` is: `seen` grows on every
    nominal descent and the set of nominal names is finite.
    """
    types = types or {}
    if not type_name:
        return None
    head, args = _parse_type(type_name)
    if head == "Opt" and args:
        inner = args[0]
        if _admits_null(inner):
            return (f"reaches `{type_name}`, whose grammar derives the string "
                    f"`null` from both `{type_name}` and `{inner}`, so a "
                    "constrained decode of `null` does not name one value "
                    "(flatten it, or wrap the inner type in a named tagged "
                    "variant)")
        return grammar_refusal_reason(inner, types, seen)
    if head == "List" and args:
        return grammar_refusal_reason(args[0], types, seen)
    if head == "Map" and len(args) == 2:
        return grammar_refusal_reason(args[1], types, seen)
    if type_name in seen:
        return None  # unreachable on a fully_expressible type; harmless if not
    spec = types.get(type_name)
    if not spec:
        return None  # a scalar, or a nominal `fully_expressible` already judged
    inner_seen = seen | {type_name}
    if spec.get("kind") == "record":
        for field_type in (spec.get("fields") or {}).values():
            reason = grammar_refusal_reason(field_type, types, inner_seen)
            if reason is not None:
                return reason
        return None
    if spec.get("kind") == "variant":
        for case in spec.get("cases") or []:
            if case.get("payload"):
                reason = grammar_refusal_reason(case["payload"], types, inner_seen)
                if reason is not None:
                    return reason
        return None
    return None


# --------------------------------------------------------------- rendering

class GrammarDerivationError(Exception):
    """A schema node the renderer does not recognise. Raised rather than
    rendering a permissive rule: a decoding grammar that accepts everything is
    the fail-open shape this item exists to remove."""


def _lit(text: str) -> str:
    """A GBNF string literal for an exact JSON token, e.g. `"\\"tag\\""`."""
    body = json.dumps(text)          # JSON-escape the payload
    inner = body.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{inner}"'


class _Deriver:
    """Renders a derived JSON Schema (item 257, `validated=True`) to GBNF.

    Sub-schemas are memoised on their canonical JSON, so a type reached twice
    produces one rule and the grammar stays proportional to the *distinct*
    shapes in the response type rather than to its size.
    """

    def __init__(self) -> None:
        self.rules: list[tuple[str, str]] = []
        self.memo: dict[str, str] = {}
        self.lexical: set[str] = set()
        self._n = 0

    def _lex(self, name: str) -> str:
        if name not in self.lexical:
            self.lexical.add(name)
            for dep in _LEXICAL_DEPS.get(name, ()):
                self._lex(dep)
        return name

    def _fresh(self) -> str:
        self._n += 1
        return f"t{self._n}"

    def _define(self, key: str, body: str) -> str:
        name = self._fresh()
        self.rules.append((name, body))
        self.memo[key] = name
        return name

    def ref(self, schema: dict) -> str:
        """The rule name (or lexical name) that accepts `schema`."""
        key = json.dumps(schema, sort_keys=True)
        if key in self.memo:
            return self.memo[key]
        name = self._render(schema, key)
        self.memo[key] = name
        return name

    def _render(self, schema: dict, key: str) -> str:
        if not isinstance(schema, dict) or not schema:
            raise GrammarDerivationError(f"empty or non-object schema node: {schema!r}")

        if "const" in schema:
            return self._define(key, _lit(str(schema["const"])))

        if schema.get("nullable"):
            inner = dict(schema)
            inner.pop("nullable")
            if not inner:
                raise GrammarDerivationError("a bare `nullable` node has no base type")
            return self._define(key, f"{self.ref(inner)} | {self._lex('nullv')}")

        if "oneOf" in schema:
            arms = schema["oneOf"]
            if not arms:
                raise GrammarDerivationError(
                    "an empty `oneOf` accepts no string: it has no grammar")
            return self._define(key, " | ".join(self.ref(a) for a in arms))

        node_type = schema.get("type")

        if node_type == "string":
            if schema.get("contentEncoding") == "base64":
                return self._lex("b64string")
            return self._lex("string")
        if node_type == "integer":
            return self._lex("integer")
        if node_type == "number":
            return self._lex("number")
        if node_type == "boolean":
            return self._lex("boolean")
        if node_type == "null":
            return self._lex("nullv")

        if node_type == "array":
            items = schema.get("items")
            if not items:
                raise GrammarDerivationError("an array node with no `items`")
            item = self.ref(items)
            ws = self._lex("ws")
            body = (f'"[" {ws} "]" | "[" {ws} {item} '
                    f'({ws} "," {ws} {item})* {ws} "]"')
            return self._define(key, body)

        if node_type == "object":
            if "additionalProperties" in schema and "properties" not in schema:
                value = self.ref(schema["additionalProperties"])
                ws, st = self._lex("ws"), self._lex("string")
                entry = f'{st} {ws} ":" {ws} {value}'
                body = (f'"{{" {ws} "}}" | "{{" {ws} {entry} '
                        f'({ws} "," {ws} {entry})* {ws} "}}"')
                return self._define(key, body)
            if "properties" in schema:
                return self._define(key, self._record_body(schema))

        raise GrammarDerivationError(f"unrecognised schema node: {key}")

    def _record_body(self, schema: dict) -> str:
        """A closed object with a pinned member order.

        JSON objects are unordered, but a grammar that admits every permutation
        of n members has n! alternatives, so the derivation pins one order: the
        order the schema presents its properties in, which is the order the
        fields were declared. This NARROWS the wire form the decoder may
        produce; it does not narrow what the validator accepts, so a provider
        that honours the grammar cannot be refused for the ordering, and one
        that ignores it is still validated as before. See §5 of the design note.
        """
        props = schema.get("properties") or {}
        ws = self._lex("ws")
        if not props:
            return f'"{{" {ws} "}}"'
        parts = [f'"{{" {ws}']
        for i, (field, sub) in enumerate(props.items()):
            if i:
                parts.append(f'{ws} "," {ws}')
            parts.append(f'{_lit(field)} {ws} ":" {ws} {self.ref(sub)}')
        parts.append(f'{ws} "}}"')
        return " ".join(parts)

    def text(self, root_rule: str) -> str:
        lines = [f"{GRAMMAR_ROOT} ::= {root_rule}"]
        lines += [f"{name} ::= {body}" for name, body in self.rules]
        lines += [_LEXICAL[name] for name in _LEXICAL if name in self.lexical]
        return "\n".join(lines) + "\n"


def gbnf_from_schema(schema: dict) -> str:
    """Render a derived JSON Schema to GBNF text. Raises
    `GrammarDerivationError` on a node it does not recognise."""
    deriver = _Deriver()
    root_rule = deriver.ref(schema)
    return deriver.text(root_rule)


def grammar_digest(text: str) -> str:
    """A stable identity for a grammar's bytes.

    A digest, not a name: two crossings that derive the same grammar from
    different types share it, and a provider may cache a compiled grammar by it
    without revl having to say anything about how a grammar is compiled. It is
    the mirror image of item 515's `placement_digest`, which revl binds over
    fields the provider owns and never interprets; this one is over bytes the
    compiler owns and the provider never rewrites.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decode_grammar_for(schema: dict) -> dict:
    """The IR value bound at a `validated` crossing: the grammar the response
    type derives, its dialect, its start symbol and its digest.

    Every field is a compile-time constant. Nothing here is a call, a handle or
    a host reference, so the same four values reach all six tiers unchanged.
    """
    text = gbnf_from_schema(schema)
    return {
        "format": GRAMMAR_FORMAT,
        "root": GRAMMAR_ROOT,
        "text": text,
        "digest": grammar_digest(text),
    }
