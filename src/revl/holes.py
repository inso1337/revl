"""Typed holes — the obligation ledger (docs/holes.md).

A `hole` is a placeholder that has a type and no implementation. It satisfies
the checker, so the code *around* it is checked for real, and it is recorded
here as an unmet obligation. Compilation of a file with holes succeeds — it is
a draft — but two things must never happen quietly:

* a hole entering a **running composition** (the admission gate), and
* a hole being **emitted** to a backend, where a foreign compiler would be the
  one to complain, in its own vocabulary, about code revl knew was unfinished.

The first is enforced here and called from the admission paths; the second is
enforced inside each backend's `emit`, which is standalone by design and so
carries its own copy of the same walk.
"""

from __future__ import annotations

from .errors import RevlError

# The IR sections a backend actually lowers. The obligation walk is scoped to
# them so the compiler's own `holes` report (which is not IR) is never mistaken
# for a hole in the program.
EMITTABLE_SECTIONS = ("components", "functions", "tests", "externs")


def collect(ir: dict) -> list[dict]:
    """Every hole in an IR document, as {file, line, type, message}."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "hole":
                found.append({
                    "file": node.get("file"),
                    "line": node.get("line"),
                    "type": node.get("type"),
                    "message": node.get("message"),
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for section in EMITTABLE_SECTIONS:
        walk(ir.get(section))
    return sorted(found, key=lambda h: (str(h["file"]), h["line"] or 0))


def render(holes: list[dict]) -> list[str]:
    """One human line per obligation: `file:line: expects `T` — "why"`."""
    lines = []
    for hole in holes:
        line = f"{hole['file']}:{hole['line']}: expects `{hole['type']}`"
        if hole.get("message"):
            line += f' — "{hole["message"]}"'
        lines.append(line)
    return lines


def summarize(holes: list[dict], limit: int = 3) -> str:
    """A one-line census for an error message."""
    shown = ", ".join(
        f"{h['file']}:{h['line']} (`{h['type']}`)" for h in holes[:limit])
    rest = len(holes) - limit
    return shown + (f", and {rest} more" if rest > 0 else "")


def refuse_admission(ir: dict, holes: list[dict] | None = None) -> None:
    """The admission gate's hole rule: a draft may not enter a running system.

    Compilation of a draft succeeds on purpose — an agent needs the checker's
    verdict on the parts it *has* written. Admission is the other question
    ("may this run?"), and there the answer is no while any obligation is
    open: a hole has no implementation, so the composition it joins would be
    one method call away from a runtime that cannot answer.
    """
    holes = collect(ir) if holes is None else holes
    if not holes:
        return
    first = holes[0]
    plural = "s" if len(holes) > 1 else ""
    raise RevlError(
        first["file"] or "<candidate>", first["line"] or 1,
        f"admission refused: this candidate still has {len(holes)} typed "
        f"hole{plural} — {summarize(holes)}",
        hint="a hole is a recorded obligation, not code: it type-checks so the "
             "rest of the draft can be checked, but it has no implementation, "
             "so it may never enter a running composition. `revl compile` "
             "lists every hole; fill them, then admit (docs/holes.md)",
        code="T3", category="admission",
    )
