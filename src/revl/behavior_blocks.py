"""Managed behavior comment blocks derived from the IR (issue #2856).

A behavior comment is hand-written prose that outlives the code. The part of
it that is a pure function of the component -- its effect summary, the
capabilities its emissions may cross, its realm placement and the extern
surface it reaches -- is derived from the lowered IR and committed between two
comment markers, with the sha256 of that IR. `revl docs check` refuses a block
whose digest no longer matches the IR, so the stale part of the comment is
regenerated rather than reviewed; hand-written prose outside the markers is
never touched.

The block sits directly above the declaration it describes:

    // the consumer: requires the `db` key, provides the `cache` key ...
    // revl:behavior begin
    // kind: component
    // effects: 2 reversible effects, 1 emission
    // capabilities: db
    // realms: shared
    // externs: Map.new
    // digest: sha256:5f0c...
    // revl:behavior end
    component UserCache requires db: Database provides cache: Cache { ... }

Adoption is incremental. A component with no block is not an error for
`revl docs check`; it becomes managed the first time `revl docs gen` writes a
block for it.

The digest is taken over the component's IR entry together with the lowered
entries of every module function and extern its body reaches (transitively, by
the same reference walk the G4 emission analysis uses), with the `source` path
and every source-position field removed. A move of the file, a reformat that
keeps the IR, or the insertion of the block itself does not stale the block,
while a change to the component's own behavior, to a function it calls or to
the host body an extern reaches does. What the digest does NOT cover is another
component or a service declaration: a service signature is spelled by the
`capabilities` fact, and another component carries its own block.
"""
from __future__ import annotations

import hashlib
import json

from .errors import RevlError
from .lexer import lex
from .parser import parse_text

_BOM = "\ufeff"
BEGIN = "// revl:behavior begin"
END = "// revl:behavior end"
_MARKER_PREFIX = "// revl:behavior"
_DIGEST_PREFIX = "// digest: sha256:"


class BehaviorBlockError(Exception):
    """A managed block that is malformed or no longer matches the IR.

    Carries the file, the 1-based line to point at and the message, so the CLI
    can print the same `path:line: reason` shape every other refusal uses
    without re-deriving either.
    """

    def __init__(self, path: str, line: int, message: str):
        super().__init__(f"{path}:{line}: {message}")
        self.path = path
        self.line = line
        self.message = message


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _declaration_lines(text: str, path: str) -> dict[str, int]:
    """Component name -> 1-based line of its `component` keyword."""
    program = parse_text(text, path)
    return {decl.name: decl.line for decl in program.components}


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _without_provenance(node, *, top: bool = False):
    """Drop the IR's source-POSITION facts before hashing.

    A spawn node, a break/continue step, a lease and a hole each carry the
    `line` they came from, and a hole carries the `file` it was read from.
    Inserting this very block above a component moves every later line, so a
    digest that included those facts would stale the block the moment it was
    written and regenerate forever.

    The drop is by SHAPE, not by key name. `requires`, `provides`, `isolate`
    and `routes` are maps keyed by user-chosen capability keys, so a component
    may legitimately declare `requires source: Db`; dropping every key named
    `source`, `line` or `file` at every depth would erase that entry and let a
    real behavior change hash identically. Only the component's own top-level
    `source` path, an integer-valued `line` (every positional `line` is an int,
    while a capability map's value is a service or realm string) and a `file`
    on a `kind: hole` node are provenance.
    """
    if isinstance(node, dict):
        payload = {}
        for key, value in node.items():
            if top and key == "source":
                continue
            if key == "line" and isinstance(value, int):
                continue
            if key == "file" and node.get("kind") == "hole":
                continue
            payload[key] = _without_provenance(value)
        return payload
    if isinstance(node, list):
        return [_without_provenance(value) for value in node]
    return node


def reached_declarations(ir: dict) -> dict[str, dict]:
    """Per component name, the module functions and externs its body reaches.

    The walk is `emission_analysis._calls_in`, the same reference collector the
    G4 emission fixed point and the G8 boundary walk use: a direct call, a
    first-class function value and a callee computed from other names all count,
    and a function's own body is walked in turn, so the reach is transitive.
    A local variable that happens to share a function's name over-approximates
    the reach, which only ever makes a digest stricter. Each reached entry is the
    declaration's whole lowered IR, so the body of a called function and the
    host body or pinned host-module hash of a reached extern are part of what the
    digest commits to.
    """
    from .emission_analysis import _calls_in  # noqa: PLC0415 - lazy, like boundary

    functions = ir.get("functions") or []
    if isinstance(functions, dict):
        functions = list(functions.values())
    functions_by_name = {fn["name"]: fn for fn in functions
                         if isinstance(fn, dict) and isinstance(fn.get("name"), str)}
    externs_by_name = {ext["name"]: ext for ext in ir.get("externs") or ()
                       if isinstance(ext, dict) and isinstance(ext.get("name"), str)}

    reach: dict[str, dict] = {}
    for component in ir.get("components") or ():
        found: set = set()
        values: set = set()
        _calls_in(component, found, values)
        pending = sorted(found | values)
        seen_functions: dict[str, dict] = {}
        seen_externs: dict[str, dict] = {}
        while pending:
            name = pending.pop()
            if name in functions_by_name and name not in seen_functions:
                seen_functions[name] = functions_by_name[name]
                nested: set = set()
                nested_values: set = set()
                _calls_in(functions_by_name[name], nested, nested_values)
                pending.extend(sorted(nested | nested_values))
            elif name in externs_by_name and name not in seen_externs:
                seen_externs[name] = externs_by_name[name]
        reach[component["name"]] = {
            "functions": [seen_functions[name] for name in sorted(seen_functions)],
            "externs": [seen_externs[name] for name in sorted(seen_externs)],
        }
    return reach


def _digest(component: dict, template: bool, reach: dict | None = None) -> str:
    """The sha256 of the component's lowered behavior, position-independent.

    The payload is the component's own IR entry plus the entries of the module
    functions and externs it reaches (`reached_declarations`). The component's
    `source` path and every source-position field are dropped, so a move of the
    file, a reformat that keeps the IR, or the insertion of the block itself
    does not change the digest; a change to the component's behavior, to a
    function it calls or to a reached extern's host body does. The template
    flag participates because which manifest list a component sits in is a
    fact of the composition, not of its body. A component that reaches no
    declaration hashes exactly as before this reach existed, so the key is added
    only when there is something in it.
    """
    payload = _without_provenance(component, top=True)
    payload["__template__"] = bool(template)
    if reach and (reach.get("functions") or reach.get("externs")):
        payload["__reach__"] = _without_provenance(reach)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _effect_summary(component: dict) -> str:
    """`N reversible, M emissions`, or `none`.

    A reversible acquisition is an `effect`/`let-effect` step (a `spawn` is one
    too: the handle's `dispose` is its inverse), and an emission is an `emit`
    step. Only the component's own body is counted; a spawned child's effects
    are the child's block.
    """
    reversible = emissions = 0
    for node in _walk(component):
        step = node.get("step")
        if step in ("effect", "let-effect"):
            reversible += 1
        elif step == "emit":
            emissions += 1
    if not reversible and not emissions:
        return "none"
    effect_word = "effect" if reversible == 1 else "effects"
    emission_word = "emission" if emissions == 1 else "emissions"
    return f"{reversible} reversible {effect_word}, {emissions} {emission_word}"


def _capabilities(surface: dict | None) -> list[str]:
    """The capability tokens the component's boundary crossings require.

    Both halves come from the G8 boundary walk, which is the authority on what
    a crossing names: the declared tokens of each emission call site (`*` for a
    service method declared `emission` with no token) and the declared scope of
    every reached extern (`emission[db] fn ...`). A built-in host call such as
    `Map.new` declares no token; it is named on the extern surface instead.
    """
    caps: set[str] = set()
    for declared in ((surface or {}).get("capabilities") or {}).values():
        caps.update(declared or ())
    for entry in (surface or {}).get("externs") or ():
        caps.update(entry.get("capabilities") or ())
    return sorted(caps)


def _externs(component: dict, surface: dict | None) -> list[str]:
    """The extern surface the component reaches.

    `_boundary` supplies the DECLARED externs, transitively through the module
    functions the component calls. It does not name the built-in host calls
    (`Map.new`, `Pool.open`), whose IR node is `kind: host` and whose contract
    is the host surface rather than an `extern` declaration, so those are added
    from the component's own body (the undo side included, since a teardown
    crossing is still a crossing).
    """
    names = {entry.get("name") for entry in (surface or {}).get("externs") or ()
             if entry.get("name")}
    for node in _walk(component):
        if node.get("kind") == "host" and node.get("fn"):
            names.add(node["fn"])
    return sorted(names)


def _realms(component: dict) -> str:
    parts: list[str] = []
    isolate = component.get("isolate") or {}
    if isolate:
        parts.append("isolate(" + ", ".join(
            f"{key} in {realm}" for key, realm in sorted(isolate.items())) + ")")
    routes = component.get("routes") or {}
    if routes:
        parts.append("routes(" + ", ".join(
            f"{key} in {'|'.join(route.get('realms') or ())}"
            for key, route in sorted(routes.items())) + ")")
    return "; ".join(parts) or "shared"


def derive(component: dict, *, template: bool, surface: dict | None,
           reach: dict | None = None) -> dict:
    """The derived facts for one component, in render order."""
    capabilities = _capabilities(surface)
    externs = _externs(component, surface)
    return {
        "kind": "template" if template else "component",
        "effects": _effect_summary(component),
        "capabilities": ", ".join(capabilities) or "none",
        "realms": _realms(component),
        "externs": ", ".join(externs) or "none",
        "digest": _digest(component, template, reach),
    }


def _render_lines(facts: dict, indent: str = "") -> list[str]:
    return [
        f"{indent}{BEGIN}",
        f"{indent}// kind: {facts['kind']}",
        f"{indent}// effects: {facts['effects']}",
        f"{indent}// capabilities: {facts['capabilities']}",
        f"{indent}// realms: {facts['realms']}",
        f"{indent}// externs: {facts['externs']}",
        f"{indent}{_DIGEST_PREFIX}{facts['digest']}",
        f"{indent}{END}",
    ]


def _split_lines(text: str) -> tuple[list[str], list[bool]]:
    """The lines of `text` without their line endings, and whether each one
    ended in a carriage return.

    The file is split on `\n` only and a trailing `\r` is peeled off and
    remembered, so every comparison below sees the same content whichever ending
    the file uses and a rewrite can put each unchanged line back byte for byte.
    """
    lines: list[str] = []
    carriage: list[bool] = []
    for raw in text.split("\n"):
        if raw.endswith("\r"):
            lines.append(raw[:-1])
            carriage.append(True)
        else:
            lines.append(raw)
            carriage.append(False)
    return lines, carriage


def _join_lines(lines: list[str], carriage: list[bool]) -> str:
    return "\n".join(line + "\r" if cr else line
                     for line, cr in zip(lines, carriage))


def _comment_run(lines: list[str], decl_index: int) -> tuple[int, int] | None:
    """The 0-based [start, end] of the contiguous `//` comment run directly
    above `decl_index`, or None when the line above it is not a comment."""
    index = decl_index - 1
    if index < 0 or not lines[index].lstrip().startswith("//"):
        return None
    run_end = index
    while index >= 0 and lines[index].lstrip().startswith("//"):
        index -= 1
    return index + 1, run_end


def _is_marker(line: str) -> bool:
    """Whether a line claims to be a managed-block marker.

    Matches the exact markers AND a marker that gained trailing text (for
    example `// revl:behavior begin (managed)`): such a line is a malformed
    marker this tool owns, not ordinary prose, so it is refused rather than
    silently ignored and duplicated by the next `gen`.
    """
    stripped = line.strip()
    return stripped in (BEGIN, END) or stripped.startswith(_MARKER_PREFIX + " ")


def _tokens_of(text: str, path: str):
    return [(token.kind, token.value, token.line) for token in lex(text, path)]


def comment_marker_lines(text: str, path: str) -> set[int]:
    """The 0-based lines that look like a marker AND that the lexer reads as a
    comment.

    A line that starts with `// revl:behavior` is only a marker when it is a
    comment. The same characters inside a multi-line string, a template or a
    host body (`@py { ... }`) are data, and refusing them as a stray marker
    would reject a file that is fine. The lexer is the one authority on which
    is which, so the question is put to it: blank the candidate lines and lex
    again. A comment contributes no token, so the token stream is unchanged; a
    line inside a token changes that token's value or stops it closing. All
    candidates are tried together first, which is one extra lex in the common
    case where every one of them is a real comment.
    """
    raw = text.split("\n")
    candidates = [index for index, line in enumerate(raw) if _is_marker(line)]
    if not candidates:
        return set()
    original = _tokens_of(text, path)

    def unchanged_without(indices: list[int]) -> bool:
        blanked = set(indices)
        probe = "\n".join("" if index in blanked else line
                          for index, line in enumerate(raw))
        try:
            return _tokens_of(probe, path) == original
        except RevlError:  # the probe no longer lexes: it was cutting into a token
            return False

    def comments_among(indices: list[int]) -> set[int]:
        """The candidates that are comments. A set blanked together leaves the
        tokens unchanged only if every member is a comment, so a failing set is
        halved until the data lines are isolated: `k` data lines among `n`
        candidates cost about `k log n` lexes, and none at all of the bisection
        when every candidate is a comment."""
        if not indices:
            return set()
        if unchanged_without(indices):
            return set(indices)
        if len(indices) == 1:
            return set()
        middle = len(indices) // 2
        return comments_among(indices[:middle]) | comments_among(indices[middle:])

    return comments_among(candidates)


def _markers_in(lines: list[str], run: tuple[int, int],
                real: set[int]) -> tuple[list[int], list[int], list[int]]:
    """The exact BEGIN lines, exact END lines and all marker-like comment lines
    of a comment run. `real` is `comment_marker_lines`."""
    start, end = run
    begins: list[int] = []
    ends: list[int] = []
    markers: list[int] = []
    for index in range(start, end + 1):
        if index not in real:
            continue
        stripped = lines[index].strip()
        if stripped == BEGIN:
            begins.append(index)
        elif stripped == END:
            ends.append(index)
        markers.append(index)
    return begins, ends, markers


def _block_span(lines: list[str], decl_index: int, path: str,
                real: set[int]) -> tuple[int, int] | None:
    """The 0-based [begin, end] line span of the managed block directly above
    `decl_index`, or None when there is none.

    The block must be the LAST thing in the contiguous `//` comment run above
    the declaration: its `end` marker is the line immediately above the
    component (a blank line breaks the run). Markers elsewhere in the run, or a
    marker with trailing text, make the block malformed rather than silently
    ignored, because a hand-edited marker is exactly the drift this checks
    for.
    """
    run = _comment_run(lines, decl_index)
    if run is None:
        return None
    begins, ends, markers = _markers_in(lines, run, real)
    if not markers:
        return None
    if (len(begins) == 1 and len(ends) == 1 and len(markers) == 2
            and ends[0] == run[1] and begins[0] < ends[0]):
        return begins[0], ends[0]
    raise BehaviorBlockError(
        path, decl_index + 1,
        "a behavior block must be exactly one `revl:behavior begin` .. "
        "`revl:behavior end` pair directly above the component")


def _committed_digest(block: list[str]) -> str | None:
    for line in block:
        stripped = line.strip()
        if stripped.startswith(_DIGEST_PREFIX):
            return stripped[len(_DIGEST_PREFIX):]
    return None


def _component_edits(text: str, path: str, components: list[dict],
                     templates: set[str], surfaces: dict,
                     reaches: dict) -> tuple[str, int]:
    """Apply every component's block to `text`.

    Returns the new text and the number of blocks written (created or
    changed). Edits are applied bottom-up, so a span that grows or shrinks
    cannot move a later edit. Only the block's own lines are rewritten: every
    other line keeps its ending, and a written block takes the ending of the
    declaration it sits above, so a CRLF file stays CRLF.
    """
    bom = text.startswith(_BOM)
    text = text[len(_BOM):] if bom else text
    lines, carriage = _split_lines(text)
    decls = _declaration_lines(text, path)
    real = comment_marker_lines(text, path)
    edits: list[tuple[int, int, list[str], bool]] = []
    written = 0
    for component in sorted(components, key=lambda c: decls.get(c["name"], 0),
                            reverse=True):
        line = decls.get(component["name"])
        if line is None:
            continue
        decl_index = line - 1
        indent = _indent(lines[decl_index])
        expected = _render_lines(
            derive(component, template=component["name"] in templates,
                   surface=surfaces.get(component["name"]),
                   reach=reaches.get(component["name"])), indent)
        span = _block_span(lines, decl_index, path, real)
        if span is None:
            edits.append((decl_index, decl_index - 1, expected,
                          _new_block_ending(carriage, decl_index)))
            written += 1
            continue
        if lines[span[0]:span[1] + 1] == expected:
            continue  # current: leave its bytes, whatever its endings are
        written += 1
        edits.append((span[0], span[1], expected, carriage[span[1]]))
    for start, end, block, ending in edits:
        lines[start:end + 1] = block
        carriage[start:end + 1] = [ending] * len(block)
    rewritten = _join_lines(lines, carriage)
    return (_BOM + rewritten if bom else rewritten), written


def _new_block_ending(carriage: list[bool], decl_index: int) -> bool:
    """The line ending a block inserted above the declaration takes.

    The declaration's own ending when it has one. A declaration on the last
    line of a file with no final newline has none to copy, so the nearest line
    above it speaks for the file, and a one-line file falls back to LF.
    """
    if decl_index < len(carriage) - 1:
        return carriage[decl_index]
    return carriage[decl_index - 1] if decl_index > 0 else False


def generate(text: str, path: str, components: list[dict], templates: set[str],
             surfaces: dict, reaches: dict | None = None) -> tuple[str, int]:
    """Return `text` with every component's block created or refreshed.

    `components` is the IR component list, `templates` the manifest's template
    names, `surfaces` the G8 boundary report keyed by component name and
    `reaches` the `reached_declarations` of the same IR.
    """
    return _component_edits(text, path, components, templates, surfaces,
                            reaches or {})


def check(text: str, path: str, components: list[dict], templates: set[str],
          surfaces: dict, reaches: dict | None = None) -> list[BehaviorBlockError]:
    """Every reason a committed block no longer matches the IR, in file order.

    An empty list is a pass. Components with no block are not errors: adoption
    is incremental, and a block appears when `revl docs gen` writes one. A
    block above no component IS an error -- it describes nothing and would
    otherwise pass forever.
    """
    reaches = reaches or {}
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    lines, _carriage = _split_lines(text)
    decls = _declaration_lines(text, path)
    real = comment_marker_lines(text, path)
    problems: list[BehaviorBlockError] = []
    owned: set[int] = set()
    for component in sorted(components, key=lambda c: decls.get(c["name"], 0)):
        line = decls.get(component["name"])
        if line is None:
            continue
        decl_index = line - 1
        try:
            span = _block_span(lines, decl_index, path, real)
        except BehaviorBlockError as error:
            problems.append(error)
            run = _comment_run(lines, decl_index)
            if run is not None:
                _begins, _ends, markers = _markers_in(lines, run, real)
                owned.update(markers)
            continue
        if span is None:
            continue
        owned.update(span)
        indent = _indent(lines[decl_index])
        facts = derive(component, template=component["name"] in templates,
                       surface=surfaces.get(component["name"]),
                       reach=reaches.get(component["name"]))
        expected = _render_lines(facts, indent)
        committed = lines[span[0]:span[1] + 1]
        if committed == expected:
            continue
        committed_digest = _committed_digest(committed)
        if committed_digest != facts["digest"]:
            problems.append(BehaviorBlockError(
                path, decl_index + 1,
                "behavior block is outdated: it was derived from IR digest "
                f"sha256:{committed_digest or '<missing>'}, and the component's "
                f"current IR has sha256:{facts['digest']} "
                f"(regenerate with `revl docs gen {path}`)"))
        else:
            problems.append(BehaviorBlockError(
                path, decl_index + 1,
                "behavior block does not match the facts derived from the IR "
                f"(regenerate with `revl docs gen {path}`)"))
    for index in sorted(real - owned):
        problems.append(BehaviorBlockError(
            path, index + 1,
            "behavior block marker is not directly above a component "
            f"declaration (remove it or regenerate with "
            f"`revl docs gen {path}`)"))
    return problems
