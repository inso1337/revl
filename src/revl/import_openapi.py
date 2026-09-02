"""OpenAPI 3.x -> revl source.

`revl import openapi` is the third and last member of the import codegen
family (docs/v2.0-roadmap.md §4), after `revl mcp import` and
`revl import wit`. It reads one OpenAPI 3.x document and emits revl: revl
`type` declarations for `components/schemas`, one `service` for the API, an
`extern` per operation, and a provider component that wires the two together.

The emission question — and here, unlike WIT, there is real evidence.

A WIT world says nothing at all about reversibility, so `revl import wit`
classifies everything `emission` and requires an out-of-band human assertion
to weaken it. OpenAPI is different: the HTTP method is *in band*, and
RFC 9110 §9.2.1 defines `GET`, `HEAD`, `OPTIONS` and `TRACE` as **safe** —
"essentially read-only". An author who writes `get:` is adopting that
contract for that operation. That is the explicit, positive, in-band claim
the family's rule asks for, so this importer honours it:

    **Every imported operation is `emission` unless the HTTP method is safe
    by specification, or a recorded human claim says otherwise.**

Three things matter about that sentence, and all three are stated in the
generated header rather than left implicit:

  * Safe-by-spec is a **claim by the API author about their server**, not a
    proof. `GET /logout`, a `GET` that writes an audit row, a `GET` whose
    query parameters trigger a job — all are legal HTTP, all violate §9.2.1,
    and nothing in the document reveals them.
  * Safe is not idempotent and idempotent is not reversible. `PUT` and
    `DELETE` are idempotent by specification and are still `emission`:
    repeating an operation harmlessly says nothing about undoing it.
  * revl's `pure` means "no observable effect" (docs/syntax-2.0.md §6) — which
    is exactly HTTP's "safe". It does not mean deterministic, and a `GET` is
    not deterministic. The two notions line up on the axis revl cares about.

Both directions can be overridden, and every override is recorded next to the
operation it changed:

  * `--emission <op>` (or `x-revl-emission: true` in the document)
    re-strengthens a safe verb the importing engineer knows writes;
  * `--pure <op>` (or `x-revl-emission: false`) weakens an unsafe verb — the
    `POST /search` case.

An explicit *emission* claim always wins, from any source: weakening requires
unanimity, strengthening requires only one voice.

What this importer refuses rather than approximating: `allOf`, `anyOf`, `not`,
`oneOf` without a `$ref`-based `discriminator`, free-form objects, non-string
enums, header/cookie parameters, non-JSON bodies and responses, `$ref`s
outside `#/components/schemas/`, and OpenAPI 2.0 documents. Each refusal names
the construct, its JSON pointer (and a best-effort line), and the way forward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import RevlError
from .lexer import KEYWORDS

# ---------------------------------------------------------------- the mapping

#: JSON Schema scalar -> revl surface type. `integer` becomes `Int` and
#: `number` becomes `Float` regardless of `format`: revl has one integer and
#: one float type, so `format: int64`/`double` are carried by the header note
#: rather than by a distinct type.
_SCALARS = {
    "string": "Str",
    "integer": "Int",
    "number": "Float",
    "boolean": "Bool",
}

#: HTTP methods that RFC 9110 §9.2.1 defines as *safe* — the only in-band
#: evidence in an OpenAPI document that an operation does not change anything.
_SAFE_METHODS = ("get", "head", "options", "trace")

#: HTTP methods that RFC 9110 §9.2.2 defines as *idempotent* among those that
#: are also emissions (i.e. not safe): PUT and DELETE. Repeating one has the
#: same server-visible effect as issuing it once — `f(f(x)) == f(x)` — which is
#: exactly the delivery property `emission idempotent` promotes to a checked IR
#: flag (docs/delivery-semantics.md, roadmap item 44). POST and PATCH are not
#: idempotent by specification. Safe methods are idempotent too, but they import
#: as plain `fn`, not `emission`, so delivery does not arise for them.
_IDEMPOTENT_EMISSION_METHODS = ("put", "delete")

#: every method an OpenAPI 3.x path item may carry.
_METHODS = (*_SAFE_METHODS, "put", "post", "delete", "patch")

#: item 254 (compensate-grade network effects, Slice 1). A COMPENSATE-grade
#: reversal is item 247's best-effort follow-up to a one-way emission, NOT a
#: proof-surface witness: it emits through the compensate slot at teardown,
#: lands on the AUDIT surface, and never contributes to a `noResidue`/witness
#: proof (docs/design/254-witnessed-network.md, "Revision" §). The annotations
#: mirror the `x-revl-emission` override family; the engineer `--compensate`/
#: `--preimage`/`--undo` flags are their out-of-band equivalents.
#:
#: Slice 1 honours the promotion ONLY on `PUT` (the verb gate): `PUT` is
#: idempotent by RFC 9110 §9.2.2, so the annotation can only NARROW within an
#: already-idempotent verb, never invent idempotence on a `POST`/`PATCH`. The
#: `DELETE`-recreate form is Slice 2. Absent the annotation, a `PUT` imports
#: exactly as item 44 makes it (`emission idempotent fn`), byte-identically.
_COMPENSATE_METHODS = ("put",)
_COMPENSATE_KEY = "x-revl-compensate"
_PREIMAGE_KEY = "x-revl-preimage"
_UNDO_KEY = "x-revl-undo"
_IF_MATCH_KEY = "x-revl-if-match"

#: path-item fields that are not operations.
_PATH_ITEM_FIELDS = {"summary", "description", "servers", "parameters"}

#: revl's built-in type names — a schema named `Result` or `List` would shadow
#: one, so it is renamed (and the rename is reported in the header).
_BUILTIN_TYPES = {"Str", "Int", "Float", "Bool", "Bytes", "Unit",
                  "List", "Map", "Opt", "Result"}

#: revl's built-in value constructors — an enum value or discriminator key
#: that lands on one of these shadows it inside the generated module.
_BUILTIN_CASES = {"Ok", "Err", "Some", "None"}

#: host-block comment marker per backend. `wasm` is deliberately absent: an
#: HTTP client has no meaning on the i32-only cordis-wasm tier.
_BACKEND_COMMENT = {"ts": "//", "py": "#", "rust": "//"}

_REF_PREFIX = "#/components/schemas/"


# --------------------------------------------------------------- name mapping

def _pascal(name: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name or "") if p]
    out = "".join(p[0].upper() + p[1:] for p in parts)
    if not out:
        out = "X"
    if not re.match(r"^[A-Za-z]", out):
        out = f"T{out}"
    return out


def _snake(name: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9]+", "_", name or "")
    ident = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", ident)
    ident = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ident)
    ident = re.sub(r"_+", "_", ident).strip("_").lower()
    if not ident:
        ident = "x"
    if not re.match(r"^[A-Za-z_]", ident):
        ident = f"n_{ident}"
    return f"{ident}_" if ident in KEYWORDS else ident


def _authority_split(server: str) -> tuple[str, str]:
    """Split a URL into `(userinfo, hostport)` from its authority.

    The authority is everything after `scheme://` and before the first `/`,
    `?` or `#`. Per RFC 3986 §3.2.1, userinfo is everything before the LAST
    `@` in the authority: a password may itself legally contain `@` or `:`,
    so the first `@`/`:` is not a safe split point. `hostport` is returned
    with the userinfo (and its `@`) stripped; a bracketed IPv6 literal
    (`[::1]:8080`) is left intact for the caller to unwrap."""
    if not server or "://" not in server:
        return "", server or ""
    authority = server.split("://", 1)[-1]
    for sep in ("/", "?", "#"):
        authority = authority.split(sep, 1)[0]
    if "@" in authority:
        userinfo, hostport = authority.rsplit("@", 1)
        return userinfo, hostport
    return "", authority


def _authority_host(server: str) -> str:
    """The bare HOST from a URL's authority: never the userinfo, never the
    port. Used to derive a `net.<host>` capability token (item 421 F4): a
    credential in a URL's userinfo must never be mistaken for the boundary
    it authenticates to, and two different credentials against two different
    hosts must never collapse onto one token."""
    _userinfo, hostport = _authority_split(server)
    if hostport.startswith("["):
        end = hostport.find("]")
        return hostport[1:end] if end != -1 else hostport[1:]
    return hostport.split(":", 1)[0]


def _redact_userinfo(url: str) -> str:
    """`url` with any authority userinfo (`user:pass@`) removed.

    A URL credential is a live secret, not an identifier: it must never be
    echoed verbatim into generated source, a comment, or any other
    diagnostic surface (item 421 F4)."""
    if not url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    cut = len(rest)
    for sep in ("/", "?", "#"):
        idx = rest.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    authority, tail = rest[:cut], rest[cut:]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[-1]
    return f"{scheme}://{authority}{tail}"


_COMMENT_LINE_BREAK = re.compile("[\r\n  ]+")


def _comment_safe(text: object) -> str:
    """Collapse every line terminator in `text` to a single space.

    Item 416f (OpenAPI codegen injection). The generated `.rvl` carries
    document-derived strings — `info.title`, `info.version`, `openapi`, the
    source filename, resolved server URLs, schema-note text — verbatim inside
    `//` comments and inside `@<backend> { ... }` stub bodies. A `//` comment
    runs to the next newline (`lexer.py:351`), so a newline embedded in ANY of
    those strings ends the comment and drops whatever follows it onto its own
    line of compiled source. An attacker who supplies the OpenAPI document (the
    ordinary case: a vendor's published spec imported with `revl import
    openapi`) could therefore inject a top-level `component`, or an
    `extern ... = @py { ... }` whose host body runs arbitrary code the first
    time the importing developer does `revl run`. Demonstrated end to end via
    `info.title`, `info.version`, `openapi` and `servers[].url`.

    A comment is single-line by construction, so the fix is to make every
    document-derived value that reaches a comment single-line too: CR, LF and
    the Unicode line separators U+2028/U+2029/U+0085 (honoured by some readers
    and by `str.splitlines`) all become a space. This only narrows the comment
    text; it changes nothing about the typed boundary the importer generates,
    which never interpolates these strings."""
    return _COMMENT_LINE_BREAK.sub(" ", str(text))


def _pointer(*parts: object) -> str:
    """A JSON pointer, RFC 6901-escaped.

    An OpenAPI document has no line numbers that survive `json.loads`, so a
    pointer is the authoritative "where" in every refusal; `_line_of` adds a
    best-effort line on top of it.
    """
    out = "#"
    for part in parts:
        token = str(part).replace("~", "~0").replace("/", "~1")
        out += f"/{token}"
    return out


def _line_of(source: str, pointer: str) -> int:
    """Best-effort source line for a JSON pointer.

    A greedy forward scan for each key in turn: good enough to put a reader's
    cursor in the right region of a hand-written document, and never trusted
    for more than that — the pointer in the message is the precise location.
    """
    if not source:
        return 0
    position = 0
    for token in pointer.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        found = source.find(json.dumps(key), position)
        if found < 0:
            break
        position = found
    return source.count("\n", 0, position) + 1 if position else 0


# ------------------------------------------------------------------ refusals

class _Reporter:
    """Everything that can raise, so that every message has the same shape:
    what the construct was, where it appeared, and what to do about it."""

    def __init__(self, filename: str, source: str) -> None:
        self.filename = filename
        self.source = source

    def fail(self, pointer: str, what: str, hint: str) -> RevlError:
        return RevlError(self.filename, _line_of(self.source, pointer),
                         f"unsupported OpenAPI construct: {what} at {pointer}",
                         hint=hint)

    def bad(self, pointer: str, what: str, hint: str) -> RevlError:
        """A malformed document, as opposed to a construct revl cannot hold."""
        return RevlError(self.filename, _line_of(self.source, pointer),
                         f"{what} at {pointer}", hint=hint)


# ------------------------------------------------------------- the type space

@dataclass
class _Names:
    """OpenAPI schema names -> revl type names, remembering every rename so
    the generated header can report it (a silent rename is a trap)."""

    renames: list[str] = field(default_factory=list)

    def type_name(self, schema_name: str) -> str:
        name = _pascal(schema_name)
        if name not in _BUILTIN_TYPES:
            return name
        note = (f"schema `{schema_name}` -> `{name}Ty` "
                f"(`{name}` is a revl built-in)")
        if note not in self.renames:
            self.renames.append(note)
        return f"{name}Ty"


class _TypeSpace:
    """Resolves JSON Schema fragments to revl types, declaring a revl `type`
    for every named record, enum and discriminated union it meets."""

    def __init__(self, doc: dict, reporter: _Reporter) -> None:
        self.doc = doc
        self.report = reporter
        components = doc.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        self.schemas: dict = schemas if isinstance(schemas, dict) else {}
        self.names = _Names()
        self.decls: dict[str, str] = {}      # revl type name -> declaration text
        self.origin: dict[str, str] = {}     # revl type name -> defining pointer
        self.owner: dict[str, str] = {}      # revl type name -> owning schema name
        self.pending: set[str] = set()       # names mid-declaration (recursion)
        self.notes: list[str] = []

    # -- bookkeeping -------------------------------------------------------
    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def declare(self, name: str, text: str, pointer: str) -> str:
        previous = self.origin.get(name)
        if previous is not None and previous != pointer:
            raise self.report.bad(
                pointer, f"two schemas both generate the revl type `{name}`",
                hint=f"the other is at {previous}; revl types share one module "
                     "namespace, so rename one of them in the document")
        self.decls[name] = text
        self.origin[name] = pointer
        return name

    # -- the resolver ------------------------------------------------------
    def resolve(self, schema: object, pointer: str, hint: str) -> str:
        if not isinstance(schema, dict):
            raise self.report.bad(
                pointer, f"a schema that is not an object (found "
                         f"{type(schema).__name__})",
                hint="every schema position in OpenAPI 3.x holds an object; "
                     "check the indentation or the surrounding key")

        if "$ref" in schema:
            return self.resolve_ref(schema["$ref"], pointer)

        for combinator, remedy in (
            ("allOf", "flatten the members into one schema with all their "
                      "properties, or `$ref` the base schema as a single "
                      "property; merging them here would have to guess what a "
                      "conflicting property means"),
            ("anyOf", "`anyOf` admits *several* members at once, which is not a "
                      "sum type and has no revl spelling; if you meant exactly "
                      "one, write `oneOf` with a `discriminator`"),
            ("not", "revl types are structural, with no negation; name the "
                    "shapes you do accept"),
        ):
            if combinator in schema:
                raise self.report.fail(
                    _pointer_join(pointer, combinator), f"`{combinator}`", remedy)

        if "oneOf" in schema:
            return self.resolve_one_of(schema, pointer, hint)

        nullable = schema.get("nullable") is True
        json_type = schema.get("type")

        if isinstance(json_type, list):
            # OpenAPI 3.1 / JSON Schema 2020-12 spells nullability this way.
            concrete = [t for t in json_type if t != "null"]
            nullable = nullable or len(concrete) != len(json_type)
            if len(concrete) != 1:
                raise self.report.fail(
                    _pointer_join(pointer, "type"),
                    f"a multi-type schema `{json.dumps(json_type)}`",
                    hint="revl values have one type; `[\"T\", \"null\"]` is "
                         "understood as `Opt[T]`, anything wider needs a "
                         "`oneOf` with a `discriminator`")
            json_type = concrete[0]

        if "enum" in schema:
            inner = self.resolve_enum(schema, pointer, hint, json_type)
        elif json_type == "object" or (json_type is None and "properties" in schema):
            inner = self.resolve_object(schema, pointer, hint)
        elif json_type == "array":
            items = schema.get("items")
            if not isinstance(items, dict):
                raise self.report.bad(
                    pointer, "an `array` schema with no `items`",
                    hint="revl's `List[T]` is homogeneous and needs its element "
                         "type; add `items`")
            inner = f"List[{self.resolve(items, _pointer_join(pointer, 'items'), hint + 'Item')}]"
        elif json_type == "string":
            if schema.get("format") in ("binary", "byte"):
                inner = "Bytes"
            else:
                inner = "Str"
        elif json_type in _SCALARS:
            inner = _SCALARS[json_type]
            if json_type == "integer" and schema.get("format") == "int64":
                self.note("`format: int64` maps to revl `Int` like every other "
                          "integer; revl has one integer type")
        elif json_type is None:
            raise self.report.fail(
                pointer, "a schema with no `type` and no `properties`, `enum`, "
                         "`$ref` or `oneOf` (a free-form value)",
                hint="revl has no dynamic value type, so an unconstrained "
                     "schema has nothing to become; give it a `type`, or model "
                     "the payload as `Str` and parse it in the host body")
        elif json_type == "null":
            raise self.report.fail(
                pointer, "`type: \"null\"`",
                hint="absence in revl is `Opt[T]` and needs a `T`; write "
                     "`nullable: true` on the type that can be absent")
        else:
            raise self.report.fail(
                pointer, f"an unknown schema type `{json_type}`",
                hint=f"known: {', '.join(sorted((*_SCALARS, 'array', 'object')))}")

        if nullable and not inner.startswith("Opt["):
            return f"Opt[{inner}]"
        if nullable:
            self.note("`Opt[T]` does not nest, so a value that is nullable at "
                      "two levels collapses to one `Opt[T]`")
        return inner

    def resolve_ref(self, ref: object, pointer: str) -> str:
        if not isinstance(ref, str):
            raise self.report.bad(pointer, "a `$ref` that is not a string",
                                  hint="write `{\"$ref\": \"#/components/schemas/X\"}`")
        if not ref.startswith(_REF_PREFIX):
            where = "another document" if not ref.startswith("#") else "this document"
            raise self.report.fail(
                pointer, f"a `$ref` to `{ref}`",
                hint=f"`revl import openapi` resolves `{_REF_PREFIX}*` only, "
                     f"inside one document; this one points into {where}. "
                     "Inline the target, or bundle the spec first "
                     "(`redocly bundle`, `swagger-cli bundle`)")

        raw = ref[len(_REF_PREFIX):].replace("~1", "/").replace("~0", "~")
        if raw not in self.schemas:
            raise self.report.bad(
                pointer, f"`$ref` to an undefined schema `{raw}`",
                hint=f"`components/schemas` defines "
                     f"{', '.join(sorted(self.schemas)) or '(nothing)'}")

        name = self.names.type_name(raw)
        owner = self.owner.setdefault(name, raw)
        if owner != raw:
            # Checked here rather than in `declare`, because the memo below
            # would otherwise let the second schema quietly reuse the first.
            raise self.report.bad(
                pointer, f"two schemas both generate the revl type `{name}`",
                hint=f"`{owner}` and `{raw}` differ only in a way revl's naming "
                     "does not keep; revl types share one module namespace, so "
                     "rename one of them in the document")
        if name in self.decls or name in self.pending:
            # Already declared, or being declared right now — the second case
            # is a recursive schema, which revl records hold natively.
            return name

        target = _pointer("components", "schemas", raw)
        self.pending.add(name)
        try:
            resolved = self.resolve(self.schemas[raw], target, name)
        finally:
            self.pending.discard(name)

        if resolved == name and name not in self.decls:
            # The schema resolved to its own name without declaring anything:
            # `A: {$ref: A}`, or a cycle of `$ref`s with no record, enum or
            # union anywhere in it to break on.
            raise self.report.bad(
                target, f"schema `{raw}` is a `$ref` cycle with no shape in it",
                hint="a cycle is fine once something in it is a record, an enum "
                     "or a discriminated union — revl records may name "
                     "themselves. A cycle of bare `$ref`s has no type to become")

        if resolved != name:
            # A component schema that is a scalar, list or map is not a *named*
            # revl type: revl's `type X = Y` declares a one-case variant, not an
            # alias (docs/syntax-2.0.md §2), so it is inlined at its use sites.
            self.note(f"`{_REF_PREFIX}{raw}` is `{resolved}` — inlined at its "
                      "use sites (revl has no type alias: `type X = Y` would "
                      "declare a variant)")
        return resolved

    def resolve_object(self, schema: dict, pointer: str, hint: str) -> str:
        properties = schema.get("properties")
        extra = schema.get("additionalProperties")

        if not properties:
            if isinstance(extra, dict) and extra:
                value = self.resolve(extra, _pointer_join(pointer, "additionalProperties"),
                                     hint + "Value")
                return f"Map[Str, {value}]"
            raise self.report.fail(
                pointer, "a free-form `object` (no `properties`)",
                hint="revl records have fixed, typed fields. Either list the "
                     "`properties`, or give `additionalProperties` a schema so "
                     "it becomes `Map[Str, T]`")

        if extra not in (None, False):
            raise self.report.fail(
                _pointer_join(pointer, "additionalProperties"),
                "an object with both `properties` and open `additionalProperties`",
                hint="a revl record holds exactly its declared fields and cannot "
                     "carry the extras. Set `additionalProperties: false`, or "
                     "drop `properties` so the whole thing becomes `Map[Str, T]`")

        if not isinstance(properties, dict):
            raise self.report.bad(pointer, "`properties` that is not an object",
                                  hint="it maps property names to schemas")

        required = schema.get("required") or []
        required = set(required) if isinstance(required, list) else set()

        fields: list[str] = []
        seen: dict[str, str] = {}
        for prop, sub in properties.items():
            field_name = _snake(prop)
            if field_name in seen:
                raise self.report.bad(
                    _pointer_join(pointer, "properties", prop),
                    f"properties `{seen[field_name]}` and `{prop}` both become "
                    f"the revl field `{field_name}`",
                    hint="revl field names are snake_case; rename one property")
            seen[field_name] = prop

            sub_pointer = _pointer_join(pointer, "properties", prop)
            revl_type = self.resolve(sub, sub_pointer, hint + _pascal(prop))
            if prop not in required and not revl_type.startswith("Opt["):
                revl_type = f"Opt[{revl_type}]"
            elif prop not in required:
                self.note("a property that is both optional and `nullable` is one "
                          "`Opt[T]`: revl's `Opt` does not nest, so \"absent\" and "
                          "\"null\" are the same value on this side")
            fields.append(f"{field_name}: {revl_type}")

        return self.declare(hint, f"type {hint} = {{ {', '.join(fields)} }}", pointer)

    def resolve_enum(self, schema: dict, pointer: str, hint: str,
                     json_type: object) -> str:
        values = schema.get("enum")
        if not isinstance(values, list) or not values:
            raise self.report.bad(pointer, "an empty or malformed `enum`",
                                  hint="it is a non-empty list of values")
        if json_type not in (None, "string") or not all(isinstance(v, str) for v in values):
            kinds = sorted({type(v).__name__ for v in values})
            raise self.report.fail(
                _pointer_join(pointer, "enum"),
                f"a non-string `enum` (values of type {', '.join(kinds)})",
                hint="a revl ADT case is a *name*, so only string values can "
                     "become one. Drop the `enum` to keep the underlying scalar "
                     f"(`{_SCALARS.get(str(json_type), 'Str')}`), or make the "
                     "values strings")

        cases: list[str] = []
        seen: dict[str, str] = {}
        for value in values:
            case = _pascal(value)
            if case in seen:
                raise self.report.bad(
                    _pointer_join(pointer, "enum"),
                    f"enum values `{seen[case]}` and `{value}` both become the "
                    f"revl case `{case}`",
                    hint="ADT case names are PascalCase; rename one value")
            seen[case] = value
            if case in _BUILTIN_CASES:
                self.note(f"`{hint}.{case}` shadows revl's built-in `{case}` "
                          f"constructor inside this module — rename the enum "
                          f"value `{value}` if this module also writes "
                          "`Opt`/`Result` values")
            cases.append(case)

        return self.declare(hint, f"type {hint} = {' | '.join(cases)}", pointer)

    def resolve_one_of(self, schema: dict, pointer: str, hint: str) -> str:
        members = schema.get("oneOf")
        discriminator = schema.get("discriminator")
        if not isinstance(members, list) or not members:
            raise self.report.bad(pointer, "an empty or malformed `oneOf`",
                                  hint="it is a non-empty list of schemas")

        if not isinstance(discriminator, dict) or not discriminator.get("propertyName"):
            raise self.report.fail(
                pointer, "a `oneOf` with no `discriminator`",
                hint="a revl ADT case is chosen by name, so the union needs a "
                     "tag. Add `discriminator: {propertyName: <field>}` (with a "
                     "`mapping` if the case names differ from the schema "
                     "names), or replace the `oneOf` with one named schema")

        mapping = discriminator.get("mapping")
        mapping = mapping if isinstance(mapping, dict) else {}
        by_ref = {ref: key for key, ref in mapping.items()}

        cases: list[str] = []
        seen: dict[str, str] = {}
        for index, member in enumerate(members):
            member_pointer = _pointer_join(pointer, "oneOf", index)
            if not isinstance(member, dict) or "$ref" not in member:
                raise self.report.fail(
                    member_pointer, "a `oneOf` member that is not a `$ref`",
                    hint="each case of a discriminated union must name a schema "
                         "so the revl case can be named after it; lift the "
                         "inline schema into `components/schemas`")
            payload = self.resolve_ref(member["$ref"], member_pointer)
            label = by_ref.get(member["$ref"])
            if label is None:
                label = str(member["$ref"]).rsplit("/", 1)[-1]
            case = _pascal(label)
            if case in seen:
                raise self.report.bad(
                    member_pointer,
                    f"`oneOf` members `{seen[case]}` and `{label}` both become "
                    f"the revl case `{case}`",
                    hint="give one of them a distinct `discriminator.mapping` key")
            seen[case] = label
            if case in _BUILTIN_CASES:
                self.note(f"`{hint}.{case}` shadows revl's built-in `{case}` "
                          "constructor inside this module")
            cases.append(f"{case}({payload})")

        self.note(f"`{hint}` is a `oneOf` discriminated on "
                  f"`{discriminator['propertyName']}`: the tag field stays "
                  "inside each case's record, as OpenAPI defines it — revl's "
                  "`match` uses the case, not the field")
        return self.declare(hint, f"type {hint} = {' | '.join(cases)}", pointer)


def _pointer_join(pointer: str, *parts: object) -> str:
    tail = _pointer(*parts)
    return pointer + tail[1:]


# ------------------------------------------------------------- the operations

@dataclass
class _Operation:
    method: str
    path: str
    name: str                       # revl operation name
    pointer: str
    summary: str
    params: list[tuple[str, str]]   # (revl name, revl type)
    returns: str | None
    emission: bool
    reason: str                     # why it is (or is not) an emission
    idempotent: bool = False        # RFC 9110 §9.2.2, spec's claim (item 44)
    # item 254 (compensate-grade network effect, Slice 1). `compensate` records
    # that this PUT was promoted to carry an item-247 COMPENSATE slot on its
    # emission extern; `preimage`/`undo` name the operations the reversal reads
    # and re-issues; `if_match` records whether the author claims a version/ETag
    # token for the reversal (HIGH 1). None/False leaves the operation exactly
    # as item 44 emits it.
    compensate: bool = False
    preimage: str | None = None     # the safe GET op that reads the preimage
    undo: str | None = None         # the op that writes the preimage back (the PUT)
    if_match: bool = False          # author claims a version/ETag concurrency token


class _Generator:
    def __init__(self, doc: dict, filename: str, source: str, backend: str,
                 service: str | None, pure: set[str], emission: set[str],
                 compensate: set[str] | None = None,
                 preimage: dict[str, str] | None = None,
                 undo: dict[str, str] | None = None,
                 if_match: set[str] | None = None) -> None:
        self.doc = doc
        self.filename = filename
        self.backend = backend
        self.report = _Reporter(filename, source)
        self.types = _TypeSpace(doc, self.report)
        self.pure_requested = pure
        self.emission_requested = emission
        # item 254: the engineer `--compensate`/`--preimage`/`--undo`/`--if-match`
        # out-of-band equivalents of the `x-revl-*` annotations. `compensate` and
        # `if_match` are op-handle sets (like `--pure`); `preimage`/`undo` are
        # `OP -> TARGET_OP` maps (the reversal route the flag names out of band).
        self.compensate_requested = compensate or set()
        self.preimage_requested = preimage or {}
        self.undo_requested = undo or {}
        self.if_match_requested = if_match or set()
        # item 254: set once any operation is promoted compensate-grade, so the
        # header gains its claim-vs-proof paragraph ONLY then — a document with
        # no `x-revl-compensate` annotation emits byte-identically to before.
        self._has_compensation = False
        self.used: set[str] = set()
        self.notes: list[str] = []
        self.service = self._service_name(service)
        self.key = _snake(self.service)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- the document ------------------------------------------------------
    def _service_name(self, override: str | None) -> str:
        if override:
            name = _pascal(override)
        else:
            info = self.doc.get("info")
            title = info.get("title") if isinstance(info, dict) else None
            if not isinstance(title, str) or not title.strip():
                raise self.report.bad(
                    _pointer("info", "title"),
                    "this document has no `info.title` to name the service after",
                    hint="OpenAPI 3.x requires `info.title`; add it, or pass "
                         "`--service NAME`")
            name = _pascal(title)
        if name in _BUILTIN_TYPES:
            raise self.report.bad(
                _pointer("info", "title"),
                f"the service would be named `{name}`, which is a revl built-in type",
                hint="pass `--service NAME` with something else")
        return name

    def check_version(self) -> None:
        if "swagger" in self.doc:
            raise self.report.fail(
                _pointer("swagger"),
                f"an OpenAPI 2.0 (Swagger {self.doc['swagger']}) document",
                hint="`revl import openapi` reads 3.x only — 2.0 spells bodies, "
                     "responses and schemas differently. Convert it first "
                     "(`swagger2openapi`, or the Swagger Editor's "
                     "\"Convert to OpenAPI 3\")")
        version = self.doc.get("openapi")
        if not isinstance(version, str) or not version.startswith("3."):
            raise self.report.bad(
                _pointer("openapi"),
                f"not an OpenAPI 3.x document (`openapi` is "
                f"{json.dumps(version)})",
                hint="the top-level `openapi` key must be a 3.x version string, "
                     "e.g. \"3.0.3\" or \"3.1.0\"")

    def collect(self) -> list[_Operation]:
        paths = self.doc.get("paths")
        if not isinstance(paths, dict) or not paths:
            raise self.report.bad(
                _pointer("paths"), "this document declares no `paths` to import",
                hint="`revl import openapi` projects operations to service "
                     "methods; a document of schemas alone has nothing to project")

        operations: list[_Operation] = []
        claimed: dict[str, _Operation] = {}

        for path, item in paths.items():
            path_pointer = _pointer("paths", path)
            if not isinstance(item, dict):
                raise self.report.bad(path_pointer, "a path item that is not an object",
                                      hint="it maps HTTP methods to operations")
            if "$ref" in item:
                raise self.report.fail(
                    path_pointer, "a `$ref` at the path-item level",
                    hint="this importer resolves `$ref` inside "
                         f"`{_REF_PREFIX}` only; inline the path item")

            shared = item.get("parameters")
            shared = shared if isinstance(shared, list) else []

            for verb, operation in item.items():
                if verb in _PATH_ITEM_FIELDS or verb.startswith("x-"):
                    continue
                if verb.lower() not in _METHODS:
                    raise self.report.fail(
                        _pointer_join(path_pointer, verb),
                        f"an unknown path-item field `{verb}`",
                        hint=f"expected an HTTP method "
                             f"({', '.join(sorted(_METHODS))}), one of "
                             f"{', '.join(sorted(_PATH_ITEM_FIELDS))}, or an "
                             "`x-` extension")
                if not isinstance(operation, dict):
                    raise self.report.bad(_pointer_join(path_pointer, verb),
                                          "an operation that is not an object",
                                          hint="it holds `parameters`, "
                                               "`requestBody`, `responses`")
                built = self.operation(verb.lower(), path,
                                       _pointer_join(path_pointer, verb),
                                       operation, shared)
                previous = claimed.get(built.name)
                if previous is not None:
                    raise self.report.bad(
                        built.pointer,
                        f"`{built.method.upper()} {built.path}` and "
                        f"`{previous.method.upper()} {previous.path}` both "
                        f"become the revl operation `{built.name}`",
                        hint="a service's methods share one namespace; give one "
                             "of them a distinct `operationId`")
                claimed[built.name] = built
                operations.append(built)

        if not operations:
            raise self.report.bad(
                _pointer("paths"), "this document declares no operations to import",
                hint="every path item here is empty; add at least one HTTP method")
        return operations

    # -- one operation -----------------------------------------------------
    def operation(self, method: str, path: str, pointer: str, operation: dict,
                  shared: list) -> _Operation:
        operation_id = operation.get("operationId")
        name = self.op_name(method, path, operation_id, pointer)
        params = self.parameters(method, path, pointer, operation, shared, name)

        body = self.request_body(pointer, operation, name)
        if body is not None:
            body_name = "body"
            taken = {pname for pname, _ in params}
            if body_name in taken:
                body_name = "request_body"
                self.note(f"`{name}` has a parameter named `body`, so the request "
                          "body is the parameter `request_body`")
            params.append((body_name, body))

        returns = self.response(pointer, operation, name)
        emission, reason = self.classify(method, path, operation, name, operation_id)

        summary = operation.get("summary") or operation.get("description") or ""
        summary = str(summary).strip().splitlines()[0][:88] if summary else ""

        # Delivery evidence (item 44): PUT/DELETE are idempotent by RFC 9110
        # §9.2.2. Only claim it on an operation that actually imports as an
        # emission — a verb the author weakened to plain `fn` has no delivery.
        idempotent = emission and method in _IDEMPOTENT_EMISSION_METHODS

        compensate, preimage, undo, if_match = self.compensation(
            method, path, pointer, operation, name, operation_id, emission)

        return _Operation(method=method, path=path, name=name, pointer=pointer,
                          summary=summary, params=params, returns=returns,
                          emission=emission, reason=reason, idempotent=idempotent,
                          compensate=compensate, preimage=preimage, undo=undo,
                          if_match=if_match)

    # -- item 254: the compensate-grade promotion -------------------------
    def compensation(self, method: str, path: str, pointer: str, operation: dict,
                     name: str, operation_id: object,
                     emission: bool) -> tuple[bool, str | None, str | None, bool]:
        """`(compensate, preimage, undo, if_match)` — the item-254 Slice 1
        promotion of a `PUT` to a COMPENSATE-grade network effect.

        The promotion attaches an item-247 `compensate` slot (a best-effort,
        audit-surface reversal) to the operation's `emission` extern. It is NOT
        a proof-surface witness: it never claims `noResidue`, and the endpoint
        stays an `emission`. Two guards keep optimism out, exactly as the
        `emission` override family does:

          * the VERB GATE (item 254 verb gate / attack 3): the promotion is
            honoured ONLY on `PUT`. `PUT` is idempotent by RFC 9110 §9.2.2, so
            the annotation NARROWS within an already-idempotent verb; it cannot
            invent idempotence on a `POST`/`PATCH`, which is a HARD ERROR. The
            `DELETE`-recreate form is Slice 2, refused here rather than
            half-emitted;
          * the operation must import as an `emission` — a verb weakened to a
            plain `fn` (`--pure`) crosses no boundary, so there is nothing to
            compensate.
        """
        handles = {name, f"{method.upper()} {path}"}
        if isinstance(operation_id, str) and operation_id.strip():
            handles.add(operation_id)

        ext_compensate = operation.get(_COMPENSATE_KEY)
        forced = handles & self.compensate_requested
        requested = bool(forced) or ext_compensate is True
        if forced:
            self.used |= forced
        if not requested:
            return False, None, None, False

        # the verb gate — a HARD ERROR off `PUT`, never a silent drop
        if method not in _COMPENSATE_METHODS:
            if method in ("post", "patch"):
                raise self.report.bad(
                    pointer,
                    f"`{method.upper()} {path}` is asked to carry a compensate "
                    f"reversal, but `{method.upper()}` is not idempotent by RFC "
                    f"9110 §9.2.2",
                    hint="the compensate promotion is honoured only on `PUT`, a "
                         "verb the RFC defines idempotent; it can NARROW within "
                         "an already-idempotent verb but cannot invent "
                         "idempotence on a POST/PATCH. Drop the annotation, or "
                         "model the offset as a plain emission")
            if method == "delete":
                raise self.report.bad(
                    pointer,
                    f"`DELETE {path}` compensate-grade removal (recreate-on-abort) "
                    "is Slice 2, not Slice 1",
                    hint="Slice 1 ships only the `PUT`-with-`GET`-preimage form; a "
                         "`DELETE` reversal recreates via a documented create op "
                         "and rides the same audit surface in a later slice "
                         "(docs/design/254-witnessed-network.md §7)")
            raise self.report.bad(
                pointer,
                f"`{method.upper()} {path}` cannot carry a compensate reversal — "
                f"`{method.upper()}` is safe by RFC 9110 §9.2.1 (read-only)",
                hint="a compensation offsets a WRITE; a safe operation crosses no "
                     "boundary to offset. Drop the annotation")

        if not emission:
            raise self.report.bad(
                pointer,
                f"`{name}` is asked to carry a compensate reversal, but it was "
                "weakened to a plain `fn` (`--pure`/`x-revl-emission: false`)",
                hint="a compensation offsets a one-way boundary crossing; an "
                     "operation that crosses nothing has nothing to compensate. "
                     "Drop one of the two conflicting claims")

        # the reversal route: `preimage` (the safe GET read) and `undo` (the op
        # that writes the captured preimage back — the PUT itself, by default).
        preimage = self.preimage_requested.get(name)
        if preimage is None:
            ext_preimage = operation.get(_PREIMAGE_KEY)
            preimage = ext_preimage if isinstance(ext_preimage, str) else None
        if not preimage:
            raise self.report.bad(
                pointer,
                f"`{name}` is compensate-grade but names no preimage source",
                hint=f"declare `{_PREIMAGE_KEY}: <getOp>` (or `--preimage "
                     f"{name}=<getOp>`) — the safe `GET` whose response the "
                     "reversal `PUT`s back to restore server state (item 254 §1.2)")

        undo = self.undo_requested.get(name)
        if undo is None:
            ext_undo = operation.get(_UNDO_KEY)
            undo = ext_undo if isinstance(ext_undo, str) else None
        if not undo:
            # for a PUT the undo IS the PUT itself (item 254 §1.2)
            undo = name

        if_match = (bool(handles & self.if_match_requested)
                    or operation.get(_IF_MATCH_KEY) is True)
        if handles & self.if_match_requested:
            self.used |= handles & self.if_match_requested

        return True, _snake(preimage), _snake(undo), if_match

    def op_name(self, method: str, path: str, operation_id: object,
                pointer: str) -> str:
        """`operationId` if the author gave one, else `<verb>_<path>`.

        `GET /pets/{petId}` -> `get_pets_by_pet_id`. Template segments become
        `by_<name>` so that `GET /pets/{id}` and `GET /pets/mine` stay
        distinct, and every segment is kept so that the rule is total: two
        different (method, path) pairs cannot produce one name.
        """
        if isinstance(operation_id, str) and operation_id.strip():
            return _snake(operation_id)
        if operation_id is not None and not isinstance(operation_id, str):
            raise self.report.bad(_pointer_join(pointer, "operationId"),
                                  "an `operationId` that is not a string",
                                  hint="remove it, or make it a string")
        parts = [method]
        for segment in path.strip("/").split("/"):
            if not segment:
                continue
            if segment.startswith("{") and segment.endswith("}"):
                parts.append(f"by_{_snake(segment[1:-1])}")
            else:
                parts.append(_snake(segment))
        name = "_".join(parts)
        return f"{name}_" if name in KEYWORDS else name

    def parameters(self, method: str, path: str, pointer: str, operation: dict,
                   shared: list, name: str) -> list[tuple[str, str]]:
        declared = list(shared) + list(operation.get("parameters") or [])
        template = re.findall(r"\{([^{}]+)\}", path)

        path_params: dict[str, tuple[str, str]] = {}
        query_params: list[tuple[str, str]] = []
        seen: dict[str, str] = {}

        for index, spec in enumerate(declared):
            spec_pointer = _pointer_join(pointer, "parameters", index)
            if isinstance(spec, dict) and "$ref" in spec:
                raise self.report.fail(
                    spec_pointer, f"a `$ref` parameter (`{spec['$ref']}`)",
                    hint="this importer resolves `$ref` inside "
                         f"`{_REF_PREFIX}` only; inline the parameter")
            if not isinstance(spec, dict) or not spec.get("name"):
                raise self.report.bad(spec_pointer, "a parameter with no `name`",
                                      hint="every parameter needs `name` and `in`")

            location = spec.get("in")
            raw_name = spec["name"]
            if location in ("header", "cookie"):
                raise self.report.fail(
                    spec_pointer,
                    f"an `in: {location}` parameter (`{raw_name}`) on "
                    f"`{method.upper()} {path}`",
                    hint="a header or cookie is transport, not an operation "
                         "argument, and dropping it silently would change what "
                         "the operation means. Set it inside the generated "
                         f"`extern`'s `@{self.backend}` body (that is what the "
                         "host block is for), then delete the parameter from "
                         "the document")
            if location not in ("path", "query"):
                raise self.report.bad(
                    _pointer_join(spec_pointer, "in"),
                    f"a parameter with `in: {json.dumps(location)}`",
                    hint="expected `path`, `query`, `header` or `cookie`")

            schema = spec.get("schema")
            if not isinstance(schema, dict):
                raise self.report.fail(
                    spec_pointer,
                    f"a parameter (`{raw_name}`) with no `schema`",
                    hint="revl types the boundary, so every parameter needs a "
                         "type. `content`-typed parameters are not supported — "
                         "give it a `schema`")

            revl_name = _snake(raw_name)
            if revl_name in seen:
                raise self.report.bad(
                    spec_pointer,
                    f"parameters `{seen[revl_name]}` and `{raw_name}` both "
                    f"become the revl parameter `{revl_name}`",
                    hint="rename one of them in the document")
            seen[revl_name] = raw_name

            revl_type = self.types.resolve(
                schema, _pointer_join(spec_pointer, "schema"),
                _pascal(name) + _pascal(raw_name))

            if location == "path":
                if raw_name not in template:
                    raise self.report.bad(
                        spec_pointer,
                        f"path parameter `{raw_name}` does not appear in the "
                        f"path template `{path}`",
                        hint=f"the template holds "
                             f"{', '.join('{' + t + '}' for t in template) or '(nothing)'}")
                if spec.get("required") is not True:
                    self.note(f"path parameter `{raw_name}` on "
                              f"`{method.upper()} {path}` is not marked "
                              "`required: true`; OpenAPI requires it to be, and "
                              "it is treated as required")
                path_params[raw_name] = (revl_name, revl_type)
            else:
                if spec.get("required") is not True and not revl_type.startswith("Opt["):
                    revl_type = f"Opt[{revl_type}]"
                query_params.append((revl_name, revl_type))

        missing = [t for t in template if t not in path_params]
        if missing:
            raise self.report.bad(
                pointer,
                f"the path template `{path}` uses "
                f"{', '.join('{' + m + '}' for m in missing)}, which "
                f"{'is' if len(missing) == 1 else 'are'} not declared as "
                "`in: path` parameters",
                hint="an undeclared template variable has no type, so the "
                     "generated operation could not be called; declare it under "
                     "`parameters`")

        # path parameters first, in template order: the operation reads like
        # the URL it stands for.
        return [path_params[t] for t in template] + query_params

    def request_body(self, pointer: str, operation: dict, name: str) -> str | None:
        body = operation.get("requestBody")
        if body is None:
            return None
        body_pointer = _pointer_join(pointer, "requestBody")
        if not isinstance(body, dict):
            raise self.report.bad(body_pointer, "a `requestBody` that is not an object",
                                  hint="it holds `content` and `required`")
        if "$ref" in body:
            raise self.report.fail(
                body_pointer, f"a `$ref` request body (`{body['$ref']}`)",
                hint=f"this importer resolves `$ref` inside `{_REF_PREFIX}` "
                     "only; inline the request body")

        schema, schema_pointer = self.json_schema(
            body.get("content"), body_pointer, "request body")
        revl_type = self.types.resolve(schema, schema_pointer, _pascal(name) + "Body")
        if body.get("required") is not True and not revl_type.startswith("Opt["):
            revl_type = f"Opt[{revl_type}]"
        return revl_type

    def response(self, pointer: str, operation: dict, name: str) -> str | None:
        responses = operation.get("responses")
        if not isinstance(responses, dict) or not responses:
            raise self.report.bad(
                _pointer_join(pointer, "responses"),
                "an operation with no `responses`",
                hint="OpenAPI requires at least one; revl needs it to type the "
                     "operation's result")

        successes = sorted(code for code in responses
                           if isinstance(code, str) and code.isdigit()
                           and 200 <= int(code) < 300)
        if not successes:
            raise self.report.bad(
                _pointer_join(pointer, "responses"),
                f"an operation with no 2xx response (declared: "
                f"{', '.join(sorted(str(c) for c in responses))})",
                hint="the generated operation returns the success payload; a "
                     "range like `2XX` or a `default`-only document does not "
                     "say what that is. Declare the concrete success code")

        code = successes[0]
        if len(successes) > 1:
            self.note(f"`{name}` declares 2xx responses "
                      f"{', '.join(successes)}; the operation returns the "
                      f"payload of `{code}` (revl operations return one type)")

        response = responses[code]
        response_pointer = _pointer_join(pointer, "responses", code)
        if not isinstance(response, dict):
            raise self.report.bad(response_pointer, "a response that is not an object",
                                  hint="it holds `description` and `content`")
        if "$ref" in response:
            raise self.report.fail(
                response_pointer, f"a `$ref` response (`{response['$ref']}`)",
                hint=f"this importer resolves `$ref` inside `{_REF_PREFIX}` "
                     "only; inline the response")

        content = response.get("content")
        if not content:
            return None  # `204 No Content` and friends: the operation returns nothing

        schema, schema_pointer = self.json_schema(
            content, response_pointer, f"`{code}` response")
        return self.types.resolve(schema, schema_pointer, _pascal(name) + "Response")

    def json_schema(self, content: object, pointer: str,
                    what: str) -> tuple[dict, str]:
        content_pointer = _pointer_join(pointer, "content")
        if not isinstance(content, dict) or not content:
            raise self.report.bad(content_pointer, f"a {what} with no `content`",
                                  hint="it maps a media type to a schema")

        media = next((m for m in content
                      if m == "application/json"
                      or m.startswith("application/json;")
                      or m.endswith("+json")), None)
        if media is None:
            raise self.report.fail(
                content_pointer,
                f"a {what} carrying only {', '.join(sorted(content))}",
                hint="this importer types JSON payloads. Represent the payload "
                     "as `application/json`, or drop it from the document and "
                     "handle the encoding inside the generated `extern`'s host "
                     "body")
        if len(content) > 1:
            self.note(f"{what}: `{media}` is used; the other media types "
                      f"({', '.join(sorted(set(content) - {media}))}) are the "
                      "host body's business")

        entry = content[media]
        if not isinstance(entry, dict) or not isinstance(entry.get("schema"), dict):
            raise self.report.bad(
                _pointer_join(content_pointer, media),
                f"a {what} with no `schema` for `{media}`",
                hint="revl types the boundary; give the media type a `schema`")
        return entry["schema"], _pointer_join(content_pointer, media, "schema")

    # -- the emission rule -------------------------------------------------
    def classify(self, method: str, path: str, operation: dict, name: str,
                 operation_id: object) -> tuple[bool, str]:
        """`(is_emission, why)` — the one judgement in this importer.

        An explicit *emission* claim wins over everything: strengthening takes
        one voice, weakening takes unanimity.
        """
        handles = {name, f"{method.upper()} {path}"}
        if isinstance(operation_id, str) and operation_id.strip():
            handles.add(operation_id)

        extension = operation.get("x-revl-emission")

        forced = handles & self.emission_requested
        if forced:
            self.used |= forced
            return True, (f"`emission` by assertion: `--emission "
                          f"{sorted(forced)[0]}` at import time")
        if extension is True:
            return True, ("`emission` by assertion: `x-revl-emission: true` in "
                          "the document")

        weakened = handles & self.pure_requested
        if weakened:
            self.used |= weakened
            return False, (f"plain by assertion: `--pure {sorted(weakened)[0]}` at "
                           f"import time — HTTP does not call `{method.upper()}` safe")
        if extension is False:
            return False, ("plain by assertion: `x-revl-emission: false` in the "
                           f"document — HTTP does not call `{method.upper()}` safe")

        if method in _SAFE_METHODS:
            return False, (f"`{method.upper()}` is safe by RFC 9110 §9.2.1: the "
                           "API author's claim about their server, not a "
                           "property this compiler checked")
        return True, (f"`{method.upper()}` is not safe by specification: "
                      "`emission` (G8)")

    # -- emission ----------------------------------------------------------
    def _host_comment(self, text: str) -> str:
        marker = _BACKEND_COMMENT[self.backend]
        # braces would close the `@<backend> { ... }` block early; a newline
        # (item 416f) would end the comment and inject the rest as source. The
        # stub body is a single-line comment, so both are neutralised here.
        safe = _comment_safe(text).replace("{", "(").replace("}", ")")
        return f"{marker} {safe}"

    def _net_cap(self, server: str) -> str:
        """The `net.<host>` capability token for a compensate-grade crossing
        (item 254 §4). The scope is a NETWORK cap, never host-confined: this is
        load-bearing for the item-250 fork rewind, which enumerates-not-runs any
        reversal whose scope crosses the network (a speculative remote `PUT` per
        explored branch is exactly the residue item 245 prevents). The token is
        the server host, realm-style dotted (`net.api_example_com`); with no
        `servers` block it falls back to `net.<service_key>` so the crossing is
        still a named net cap, never a bare emission.

        The host comes from `_authority_host`, which parses the authority
        properly (userinfo split on the LAST `@`, IPv6 literal brackets
        respected) rather than by naively splitting on `:`: a naive split
        that runs `:` before `@` turns a URL credential into the token
        itself (item 421 F4)."""
        host = _authority_host(server) if server else ""
        host = _snake(host) if host else self.key
        return f"net.{host}"

    def _validate_compensations(self, operations: list) -> None:
        """Cross-op checks for the item-254 compensate route (§1.2): the
        `preimage` names a real, SAFE operation (an emission read is not a safe
        read), and the `undo` names a real operation. Runs after `collect` so
        the whole operation table is visible."""
        by_name = {op.name: op for op in operations}
        for op in operations:
            if not op.compensate:
                continue
            pre = by_name.get(op.preimage)
            if pre is None:
                raise self.report.bad(
                    op.pointer,
                    f"`{op.name}` names preimage `{op.preimage}`, which is not an "
                    "operation in this document",
                    hint="the preimage is the safe `GET` the reversal reads; spell "
                         "it as the generated revl name or `operationId`")
            if pre.emission:
                raise self.report.bad(
                    op.pointer,
                    f"`{op.name}`'s preimage `{op.preimage}` is an `emission`, not "
                    "a safe read",
                    hint="a preimage must be a SAFE operation (a `GET`): an "
                         "emission read cannot serve as the pre-state the reversal "
                         "restores (item 254 §1.2)")
            if op.undo not in by_name:
                raise self.report.bad(
                    op.pointer,
                    f"`{op.name}` names undo `{op.undo}`, which is not an operation "
                    "in this document",
                    hint="the undo writes the captured preimage back; for a `PUT` "
                         "it is the operation itself")

    def emit(self) -> str:
        self.check_version()
        operations = self.collect()
        self._validate_compensations(operations)

        unused = sorted((self.pure_requested | self.emission_requested
                         | self.compensate_requested | self.if_match_requested)
                        - self.used)
        if unused:
            raise self.report.bad(
                _pointer("paths"),
                f"--pure/--emission/--compensate named "
                f"{', '.join(repr(n) for n in unused)}, "
                "which is not an operation in this document",
                hint="spell it as the generated revl name (`get_pets_by_id`), "
                     "the `operationId`, or `\"GET /pets/{id}\"`; a typo here "
                     "would silently leave the operation classified by its verb")

        server = ""
        servers = self.doc.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            server = str(servers[0].get("url") or "")
        net_cap = self._net_cap(server)

        ops: list[str] = []
        externs: list[str] = []
        methods: list[str] = []

        for operation in operations:
            signature = ", ".join(f"{pname}: {ptype}" for pname, ptype in operation.params)
            returns = f" -> {operation.returns}" if operation.returns else ""
            head = f"{operation.method.upper()} {operation.path}"

            ops.append(f"  // {head}" + (f" — {operation.summary}" if operation.summary else ""))
            ops.append(f"  // {operation.reason}")
            if operation.idempotent:
                # the delivery claim, stated as a claim — same footing as the
                # `emission`/`safe` evidence above it (item 44)
                ops.append(
                    f"  // `idempotent` by RFC 9110 §9.2.2: `{operation.method.upper()}` is "
                    "idempotent by specification — the author's claim about their "
                    "server, letting the runtime auto-retry a transient failure")
            if operation.compensate:
                self._has_compensation = True
                # item 254: the per-operation claim-vs-proof block for a
                # compensate-grade reversal. It states the classification
                # ceiling (compensate, NOT witnessed / NOT a proof surface), the
                # §3 observability caveat verbatim, and the HIGH 1 concurrency
                # posture (If-Match required, or best-effort-may-clobber).
                clobber = ("with `If-Match`/`ETag` (the author claims a version "
                           "token), so a reversal that would clobber an "
                           "intervening write FAILS LOUDLY"
                           if operation.if_match else
                           "best-effort-may-clobber: this endpoint exposes no "
                           "version/ETag token, so a reversal cannot detect a "
                           "racing writer and may clobber an intervening write")
                ops.append(
                    f"  // COMPENSATE-grade (item 247): a best-effort reversal is "
                    f"attached to the emission — it PUTs the `{operation.preimage}` "
                    f"preimage back via `{operation.undo}` on abort. It lands on "
                    "the AUDIT surface, NOT a proof surface: it makes NO "
                    "`noResidue`/witness claim and restores SERVER STATE only.")
                ops.append(
                    "  // a rewound `PUT` does not unsend what a webhook "
                    "subscriber already saw. The reversal is itself an outbound "
                    f"crossing on the `{net_cap}` cap, enumerated at teardown.")
                ops.append(f"  // reversal issued {clobber}.")

            modifiers = ""
            if operation.emission:
                modifiers = "emission idempotent " if operation.idempotent else "emission "
            ops.append(f"  {modifiers}fn {operation.name}({signature}){returns}")

            extern = f"http_{self.key}_{operation.name}"
            # a URL credential must never be echoed into the generated
            # source, including here, where the host comment describing the
            # stub is otherwise a verbatim copy of the document's URL
            # (item 421 F4).
            target = (f"{_redact_userinfo(server)}{operation.path}" if server
                      else operation.path)
            if operation.compensate:
                # item 254 Slice 1: the forward emission carries a NETWORK cap
                # scope and an item-247 `compensate` slot. The reversal extern is
                # NULLARY — the extern-level `compensate` slot binds no variables
                # (not `result`, not the forward parameters; lower.py
                # `_check_extern_undo`), so the captured preimage (resolved URL +
                # bytes + reversal method/headers) rides the compensation closure
                # at runtime, not the call. This LOWERS cleanly precisely because
                # it is a `compensate` on an `emission`, never a witnessed
                # `undo` — the rule-3 `_check_witnessed_inverse` (lower.py:2153)
                # is not on this path.
                reversal = f"{extern}_compensate"
                if_match_note = ("issue the reversal PUT WITH `If-Match`/`ETag`"
                                 if operation.if_match else
                                 "no version token: best-effort, may clobber a "
                                 "concurrent write")
                externs.append(
                    f"extern emission[{net_cap}] fn "
                    f"{extern}({signature}){returns}\n"
                    f"  compensate {reversal}()\n"
                    f"  = @{self.backend} {{ "
                    f"{self._host_comment(f'{operation.method.upper()} {target} — GET the {operation.preimage} preimage, send the request, decode the JSON, and stash the preimage for the compensation')}"
                    f" }}")
                externs.append(
                    f"extern emission[{net_cap}] fn {reversal}() -> Unit\n"
                    f"  = @{self.backend} {{ "
                    f"{self._host_comment(f'best-effort reversal: PUT the stashed preimage back via {operation.undo} to {target} ({if_match_note}). Lands on the audit surface; never claims noResidue')}"
                    f" }}")
            else:
                externs.append(
                    f"extern {'emission' if operation.emission else 'pure'} fn "
                    f"{extern}({signature}){returns}\n"
                    f"  = @{self.backend} {{ "
                    f"{self._host_comment(f'{operation.method.upper()} {target} — send the request and decode the JSON response here')}"
                    f" }}")
            args = ", ".join(pname for pname, _ in operation.params)
            methods.append(f"    fn {operation.name}({args}) = {extern}({args})")

        parts = [self.header(server)]
        if self.types.decls:
            parts.append("\n".join(self.types.decls.values()))
        parts.append(f"service {self.service} {{\n" + "\n".join(ops) + "\n}")
        parts.extend(externs)
        parts.append(
            f"component {self.service}Provider provides {self.key}: {self.service} {{\n"
            f"  provide {self.key} {{\n" + "\n".join(methods) + "\n  }\n}")
        return "\n\n".join(parts) + "\n"

    def header(self, server: str) -> str:
        info = self.doc.get("info") if isinstance(self.doc.get("info"), dict) else {}
        # every document-derived value on these lines is `_comment_safe`d: a
        # newline in any of them would end the `//` comment and inject the rest
        # as compiled source (item 416f), so the filename, `openapi`, `title`
        # and `version` are all forced single-line.
        lines = [
            "// Generated by `revl import openapi` — do not edit by hand.",
            f"// Source document: {_comment_safe(self.filename)}",
            f"// OpenAPI: {_comment_safe(self.doc.get('openapi'))}",
        ]
        title = info.get("title")
        if title:
            lines.append(f"// API: {_comment_safe(title)}"
                         + (f" {_comment_safe(info['version'])}"
                            if info.get("version") else ""))
        if server:
            # never echo a URL credential into the generated header comment
            # (item 421 F4): this is the top of the file, the first thing a
            # reviewer scrubbing for secrets reads. `_comment_safe` additionally
            # keeps a newline in the URL from ending the comment (item 416f).
            lines.append(f"// Server: {_comment_safe(_redact_userinfo(server))}")
        lines += [
            "//",
            "// This boundary is TRUSTED, NOT CHECKED (G8). What is trusted here is",
            "// narrow and worth naming: RFC 9110 §9.2.1 defines GET/HEAD/OPTIONS/TRACE",
            "// as *safe* — essentially read-only — so an author who writes `get:` is",
            "// making that claim about their server. This importer honours the claim",
            "// and emits a plain `fn`; every other method is `emission`.",
            "//",
            "// The claim is not a proof. `GET /logout`, a GET that writes an audit",
            "// row, a GET whose query parameters start a job: all legal HTTP, all",
            "// invisible in this document. A service declaration is an upper bound on",
            "// every provider of it (G4), so an under-declared operation silently",
            "// breaks every consumer's audit. Read the implementation before you",
            "// trust a plain `fn` below, and re-import with `--emission <op>` (or set",
            "// `x-revl-emission: true`) for any that writes.",
            "//",
            "// A second claim rides alongside `safe`: RFC 9110 §9.2.2 defines PUT",
            "// and DELETE as *idempotent* — repeating one has the same effect as",
            "// issuing it once. They are still `emission` (idempotent is not",
            "// reversible: repeating safely says nothing about undoing), so this",
            "// importer emits `emission idempotent fn` for them. Like `safe`, the",
            "// claim is the spec's, not a proof — but it is a checked IR property",
            "// once written, and it earns the runtime the right to auto-retry a",
            "// transient failure. POST and PATCH are not idempotent and stay a bare",
            "// `emission fn`. Delete the `idempotent` word from any operation whose",
            "// real server does not honour the guarantee.",
            "//",
            "// The extern bodies are stubs: the typed boundary is generated, the HTTP",
            "// call is not. Fill each one in — including URL construction, headers,",
            "// auth and status handling — then `revl audit` shows the surface.",
            "//",
            "// Numbers are lossy in one direction: every `integer` becomes revl `Int`",
            "// and every `number` becomes `Float`, so `format` distinctions (`int32`",
            "// vs `int64`, `float` vs `double`) do not survive the trip.",
        ]
        if self._has_compensation:
            lines += [
                "//",
                "// item 254 (compensate-grade network effects). An operation marked",
                "// `x-revl-compensate` (a `PUT`, honoured on that verb only) carries an",
                "// item-247 COMPENSATE slot on its `emission[net.<host>]` extern: a",
                "// best-effort reversal that PUTs the captured GET preimage back on",
                "// abort. This is a THIRD, narrower claim on top of safe/idempotent, and",
                "// it is the CEILING — compensate-grade, NOT witnessed. The reversal",
                "// lands on the AUDIT surface (intention), never a PROOF surface",
                "// (guarantee): it makes NO `noResidue`/witness claim, restores SERVER",
                "// STATE only, and is best-effort — it may 5xx or time out.",
                "//",
                "// A rewound `PUT` does not unsend what a webhook subscriber, a replica,",
                "// a cache, or a human already saw. The reversal is itself an outbound",
                "// crossing on the `net.<host>` cap and is enumerated at teardown as its",
                "// own event — never a silent un-crossing. Where the endpoint exposes a",
                "// version/ETag token, mark `x-revl-if-match: true` so the reversal PUTs",
                "// with `If-Match` and fails loudly on a racing writer; without one the",
                "// reversal is best-effort-may-clobber, and the header says which.",
            ]
        for note in self.notes + self.types.notes + self.types.names.renames:
            # notes interpolate document-derived names (`$ref` targets, schema
            # and enum-case names); `_comment_safe` keeps a newline in any of
            # them from ending the comment and injecting source (item 416f).
            lines.append(f"// note: {_comment_safe(note)}")
        return "\n".join(lines)


# ----------------------------------------------------------------- public API

def _pair_map(items: object, flag: str) -> dict[str, str]:
    """Parse a list of engineer `OP=TARGET` strings into an `{OP: TARGET}` map
    (item 254 `--preimage`/`--undo`). A dict passes through unchanged."""
    if isinstance(items, dict):
        return dict(items)
    out: dict[str, str] = {}
    for item in items or ():
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip() or not value.strip():
            raise RevlError(
                "<args>", 0,
                f"`{flag} {item}` is not an `OP=TARGET` pair",
                hint=f"write `{flag} setConfig=getConfig` — the operation and the "
                     "reversal route operation it names")
        out[_snake(key.strip())] = value.strip()
    return out


def import_openapi(document: object, *, filename: str = "<openapi>",
                   source: str = "", backend: str = "ts",
                   service: str | None = None, pure: object = (),
                   emission: object = (), compensate: object = (),
                   preimage: object = (), undo: object = (),
                   if_match: object = ()) -> str:
    """Project an already-parsed OpenAPI 3.x `document` to revl source.

    Raises `RevlError` — naming the construct, its JSON pointer and the way
    forward — for anything this importer refuses to translate.

    `compensate`/`preimage`/`undo`/`if_match` are the item-254 engineer
    equivalents of the `x-revl-compensate`/`x-revl-preimage`/`x-revl-undo`/
    `x-revl-if-match` annotations (Slice 1, `PUT` only).
    """
    if backend not in _BACKEND_COMMENT:
        raise RevlError(filename, 0, f"unknown host backend {backend!r}",
                        hint=f"known: {', '.join(sorted(_BACKEND_COMMENT))}. "
                             "There is deliberately no `wasm`: the cordis-wasm "
                             "tier is i32-only, so it cannot hold an HTTP client")
    if not isinstance(document, dict):
        raise RevlError(filename, 0,
                        f"the OpenAPI document is not an object (found "
                        f"{type(document).__name__})",
                        hint="the top level of an OpenAPI document is a mapping "
                             "with `openapi`, `info` and `paths`")
    return _Generator(document, filename, source, backend, service,
                      set(pure or ()), set(emission or ()),
                      compensate=set(compensate or ()),
                      preimage=_pair_map(preimage, "--preimage"),
                      undo=_pair_map(undo, "--undo"),
                      if_match=set(if_match or ())).emit()


def load_document(text: str, *, filename: str = "<openapi>") -> object:
    """Parse an OpenAPI document from `text`.

    JSON is parsed with the standard library. YAML is *delegated*, never
    approximated: revl has no dependencies, and a hand-rolled YAML subset
    would mis-read an anchor or a folded scalar silently, which is exactly the
    failure mode this family exists to avoid. If PyYAML happens to be
    importable it is used; otherwise the refusal says how to convert.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise RevlError(filename, error.lineno,
                            f"this document is not valid JSON: {error.msg}",
                            hint="check the comma or brace at this line") from None

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        raise RevlError(
            filename, 0, "this document is not JSON, and no YAML parser is available",
            hint="revl has no dependencies, and this importer will not guess at "
                 "YAML rather than parse it. Convert the spec once — "
                 "`python -c \"import sys,yaml,json; "
                 "json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)\" "
                 "spec.yaml > spec.json` — or install PyYAML, which this "
                 "importer will use if it is importable") from None
    try:
        return yaml.safe_load(text)
    except Exception as error:  # pragma: no cover - depends on PyYAML
        raise RevlError(filename, 0, f"this document is not valid YAML: {error}",
                        hint="check the indentation") from None


def import_openapi_file(path: str, *, backend: str = "ts",
                        service: str | None = None, pure: object = (),
                        emission: object = (), compensate: object = (),
                        preimage: object = (), undo: object = (),
                        if_match: object = ()) -> str:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    document = load_document(text, filename=path)
    return import_openapi(document, filename=path, source=text, backend=backend,
                          service=service, pure=pure, emission=emission,
                          compensate=compensate, preimage=preimage, undo=undo,
                          if_match=if_match)
