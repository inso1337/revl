"""The minimal correct idiom for each construct (issue #1701).

An agent filling one hole, or editing one construct, needs the smallest correct
example of THAT construct in context: not the whole grammar, and not a whole
document. Each idiom here is one admitted revl document, kept as a `.rvl` file
under `src/revl/idioms/`, with a header that names it and states the one or two
rules that make it correct:

    // idiom: emission-method
    // summary: the body of a provide method whose operation is `emission[...]`
    // rule: G4: a crossing is marked: `emit <key>.<operation>(...)`, ...
    // rule: a crossing is a permission, not an obligation ...
    // fill: emit db.put(key, "saved")
    // type: Str

`fill` is an INTERNAL marker: the text that stands at the construct's position
in the example, used to cut the hole (`with_hole`) and to check the header is
well formed. It is NOT a fill an author can submit: it is lifted verbatim from
the example, so it carries the example's own free names (`store.drop()` names
`store`, which belongs to the example's component, not to the author's — issue
#2115). Every door therefore serves it under the honest name
**`exampleExpression`** (`served`), never under `fill`. `type` is the type a
hole there has. Every construct a fillSpec names
(`mcp.fillspec.CONSTRUCTS`) has an idiom with a `type`; a few more (`spawn`,
`subscribe`, `match`, `timer`, `try`, `str-concat`, `str-format`) are served by
name only and carry none. The files are revl source like any other, so
the corpus sweeps compile and parse them with the rest of the tree, and
tests/test_idioms_1701.py checks each one three ways:
it compiles and admits; with its `fill` replaced by `hole[<type>]` the fillSpec
of that hole names this very construct; and every construct a fillSpec can name
has an entry. So the table cannot drift from the compiler or from the fillSpec.

The fillSpec of every open hole carries the idiom of its construct
(`mcp.fillspec`), so `revl_check`, `revl_scaffold` and `revl_edit` deliver it
with the hole. The MCP `revl_idiom` tool and `revl idiom` serve one by name.
"""

from __future__ import annotations

from pathlib import Path

IDIOM_DIR = Path(__file__).resolve().with_name("idioms")

#: The header fields an idiom file carries, in order. `rule` repeats.
FIELDS = ("idiom", "summary", "rule", "fill", "type")


class IdiomError(ValueError):
    """An idiom file whose header is malformed."""


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    entry: dict = {"rules": []}
    body_start = 0
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.startswith("// "):
            body_start = index
            break
        key, sep, value = line[3:].partition(": ")
        if not sep or key not in FIELDS:
            raise IdiomError(f"{path.name}: unexpected header line {line!r}")
        if key == "rule":
            entry["rules"].append(value)
        else:
            entry[{"idiom": "name"}.get(key, key)] = value
    missing = [k for k in ("name", "summary", "fill") if k not in entry]
    if missing or not entry["rules"]:
        raise IdiomError(f"{path.name}: header lacks {missing or ['rule']}")
    if entry["name"] != path.stem:
        raise IdiomError(f"{path.name}: names itself `{entry['name']}`")
    entry["example"] = "\n".join(lines[body_start:]).strip("\n") + "\n"
    if entry["fill"] not in entry["example"]:
        raise IdiomError(f"{path.name}: `fill` does not occur in the example")
    return entry


_TABLE: dict = {}


def table() -> dict:
    """`{name: entry}` for every idiom file, read once per process."""
    if not _TABLE:
        for path in sorted(IDIOM_DIR.glob("*.rvl")):
            entry = _parse(path)
            _TABLE[entry["name"]] = entry
    return _TABLE


#: The name every door serves the example's expression position under. The
#: internal `fill` marker is an example-internal position, not a fill an author
#: can submit (issue #2115), so the served surface never calls it one.
SERVED_EXPRESSION_KEY = "exampleExpression"


def served(entry: dict) -> dict:
    """An idiom as every door serves it (the fillSpec, `revl_idiom`, `revl
    idiom --json`): one shape, so the doors cannot disagree about it.

    The expression position inside the example is served as
    `exampleExpression`, never as `fill`: it is lifted verbatim from the
    example and carries that example's free names, so it is not a fill for the
    author's component (issue #2115). The submit-ready fills are the hole's
    `fillSpec.fillable.producers[].write`."""
    return {
        "name": entry["name"],
        "summary": entry["summary"],
        "rules": entry["rules"],
        SERVED_EXPRESSION_KEY: entry["fill"],
        "example": entry["example"],
    }


def get(name: str) -> dict | None:
    """The idiom named `name`, or None."""
    return table().get(name)


def names() -> list[str]:
    return sorted(table())


def with_hole(entry: dict) -> str:
    """The example with its `fill` replaced by a typed hole: the document an
    agent would be filling at this construct. Only a construct idiom (one a
    fillSpec names) carries the `type` this needs."""
    if "type" not in entry:
        raise IdiomError(f"idiom {entry['name']} names no hole type")
    hole = f'hole[{entry["type"]}] "{entry["name"]}"'
    return entry["example"].replace(entry["fill"], hole, 1)


def render(entry: dict) -> str:
    """One idiom as text, for `revl idiom NAME`."""
    lines = [f"{entry['name']}: {entry['summary']}", ""]
    lines.extend(f"- {rule}" for rule in entry["rules"])
    typed = f"    (a hole here has type {entry['type']})" if "type" in entry else ""
    lines.extend([
        "",
        f"example expression: {entry['fill']}{typed}",
        "  (the expression this example stands at the construct's position — "
        "example-internal, not a fill to submit; see the hole's "
        "fillable.producers for those)",
        "",
        entry["example"].rstrip("\n"),
    ])
    return "\n".join(lines) + "\n"


__all__ = ["IDIOM_DIR", "IdiomError", "SERVED_EXPRESSION_KEY", "get", "names",
           "render", "served", "table", "with_hole"]
