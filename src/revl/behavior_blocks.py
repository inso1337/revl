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

The digest is taken over the component's IR document with its `source` path
and every source-position field removed, so a move of the file, a reformat
that keeps the IR, or the insertion of the block itself does not stale the
block while a change to the component's lowered behavior does.
"""
from __future__ import annotations

import hashlib
import json

from .parser import parse_text

BEGIN = "// revl:behavior begin"
END = "// revl:behavior end"
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


def _without_provenance(node):
    """Drop the IR's source-POSITION keys before hashing.

    A spawn node, a break/continue step, a lease and a hole each carry the
    `line` they came from. Inserting this very block above a component moves
    every later line, so a digest that included them would stale the block the
    moment it was written, and regenerate forever. `file` is the same kind of
    fact (the spelling a module was reached under), not part of the behavior
    the block describes.
    """
    if isinstance(node, dict):
        return {key: _without_provenance(value) for key, value in node.items()
                if key not in ("line", "file", "source")}
    if isinstance(node, list):
        return [_without_provenance(value) for value in node]
    return node


def _digest(component: dict, template: bool) -> str:
    """The sha256 of the component's lowered behavior, position-independent.

    The `source` path and every `line`/`file` provenance field are dropped, so
    a move of the file, a reformat that keeps the IR, or the insertion of the
    block itself does not change the digest; a change to the component's
    lowered behavior does. The template flag participates because which
    manifest list a component sits in is a fact of the composition, not of its
    body.
    """
    payload = _without_provenance(component)
    payload["__template__"] = bool(template)
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


def derive(component: dict, *, template: bool, surface: dict | None) -> dict:
    """The derived facts for one component, in render order."""
    capabilities = _capabilities(surface)
    externs = _externs(component, surface)
    return {
        "kind": "template" if template else "component",
        "effects": _effect_summary(component),
        "capabilities": ", ".join(capabilities) or "none",
        "realms": _realms(component),
        "externs": ", ".join(externs) or "none",
        "digest": _digest(component, template),
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


def _blocks_in(lines: list[str], run: tuple[int, int]) -> tuple[list[int], list[int]]:
    start, end = run
    return (
        [i for i in range(start, end + 1) if lines[i].strip() == BEGIN],
        [i for i in range(start, end + 1) if lines[i].strip() == END],
    )


def _block_span(lines: list[str], decl_index: int, path: str) -> tuple[int, int] | None:
    """The 0-based [begin, end] line span of the managed block directly above
    `decl_index`, or None when there is none.

    The block must be the LAST thing in the contiguous `//` comment run above
    the declaration: its `end` marker is the line immediately above the
    component (a blank line breaks the run). Markers elsewhere in the run make
    the block malformed rather than silently ignored, because a hand-edited
    marker pair is exactly the drift this checks for.
    """
    run = _comment_run(lines, decl_index)
    if run is None:
        return None
    begins, ends = _blocks_in(lines, run)
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and ends[0] == run[1] and begins[0] < ends[0]:
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
                     templates: set[str], surfaces: dict) -> tuple[str, int]:
    """Apply every component's block to `text`.

    Returns the new text and the number of blocks written (created or
    changed). Edits are applied bottom-up, so a span that grows or shrinks
    cannot move a later edit.
    """
    lines = text.split("\n")
    decls = _declaration_lines(text, path)
    edits: list[tuple[int, int, list[str]]] = []
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
                   surface=surfaces.get(component["name"])), indent)
        span = _block_span(lines, decl_index, path)
        if span is None:
            edits.append((decl_index, decl_index - 1, expected))
            written += 1
            continue
        if lines[span[0]:span[1] + 1] != expected:
            written += 1
        edits.append((span[0], span[1], expected))
    for start, end, block in edits:
        lines[start:end + 1] = block
    return "\n".join(lines), written


def generate(text: str, path: str, components: list[dict], templates: set[str],
             surfaces: dict) -> tuple[str, int]:
    """Return `text` with every component's block created or refreshed.

    `components` is the IR component list, `templates` the manifest's template
    names and `surfaces` the G8 boundary report keyed by component name.
    """
    return _component_edits(text, path, components, templates, surfaces)


def check(text: str, path: str, components: list[dict], templates: set[str],
          surfaces: dict) -> list[BehaviorBlockError]:
    """Every reason a committed block no longer matches the IR, in file order.

    An empty list is a pass. Components with no block are not errors: adoption
    is incremental, and a block appears when `revl docs gen` writes one. A
    block above no component IS an error -- it describes nothing and would
    otherwise pass forever.
    """
    lines = text.split("\n")
    decls = _declaration_lines(text, path)
    problems: list[BehaviorBlockError] = []
    owned: set[int] = set()
    for component in sorted(components, key=lambda c: decls.get(c["name"], 0)):
        line = decls.get(component["name"])
        if line is None:
            continue
        decl_index = line - 1
        try:
            span = _block_span(lines, decl_index, path)
        except BehaviorBlockError as error:
            problems.append(error)
            run = _comment_run(lines, decl_index)
            if run is not None:
                begins, _ends = _blocks_in(lines, run)
                owned.update(begins)
            continue
        if span is None:
            continue
        owned.add(span[0])
        indent = _indent(lines[decl_index])
        facts = derive(component, template=component["name"] in templates,
                       surface=surfaces.get(component["name"]))
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
    for index, line in enumerate(lines):
        if line.strip() == BEGIN and index not in owned:
            problems.append(BehaviorBlockError(
                path, index + 1,
                "behavior block is not directly above a component declaration "
                f"(remove it or regenerate with `revl docs gen {path}`)"))
    return problems
