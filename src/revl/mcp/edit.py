"""revl_edit — deltas, not documents (roadmap item 50).

The token-surface audit (`bench/results/token-surface-audit.md`, finding #1)
measured that `revl_swap` re-sends the *whole* composition source on every
hot-swap: the cost scales with the size of the running system, not the size of
the change. That is the textbook "documents, not deltas" tax, and it grows
precisely as self-evolving compositions get larger.

The house principle is "agents pass **names**, not **contents**". The session
already holds the admission inputs of the running composition server-side
(`session.origin` — the sources a live `revl_load`/`revl_swap` was given, kept
so the composition can be snapshotted). This module makes that server-side
source *addressable and editable*: an agent sends a small structured patch
against a named buffer, and the server applies it, recompiles, and re-admits —
without the client ever re-serializing the file.

The working buffer (`session.draft`)
-------------------------------------
Edits accumulate on a working copy of the source, carried on the session across
calls so an agent iterates without resending anything. The invariant:

* the working source is seeded from the *running* composition (``session.origin``);
* an edit that **compiles** advances the working source (so successive edits —
  e.g. filling one hole at a time — build on each other);
* an edit that **breaks admission** advances nothing: the diagnostic comes back
  and both the running composition and the working buffer are left untouched;
* an edit that compiles **clean** (no open holes) is re-admitted against the
  running composition through the *same* gate ``revl_swap`` uses and hot-swapped
  in — the working buffer then re-derives from the new running source.

The patch model
---------------
Two forms cover the common cases, and each keeps the wire small by sending only
the change, never the file:

* ``{"hole": <line>, "expr": "<fill>"}`` — fill the typed hole on that source
  line. This pairs directly with `revl_check`'s `fillSpec` obligations, which
  report each open hole's `line` and its expected type / in-scope bindings
  (docs/holes.md §8): read the spec, send the expression.
* ``{"range": [start, end], "replacement": "<text>"}`` — replace the half-open
  character span ``[start, end)`` of the buffer (``end`` omitted → an insertion
  at ``start``). The fully general, precise text edit.

An ``{"anchor": "<literal>", "replacement": "<text>"}`` convenience is also
accepted: it replaces occurrences of a literal substring, so an agent can send
"change *this snippet* to *that*" without computing character offsets that shift
under earlier edits. It is sugar over ``range`` and reports how many sites it
touched.

Re-admission is never bypassed: every form ends at ``compile_source`` /
``compile_files`` with ``manifest=session.ir`` — the identical admission gate a
human's ``revl compile`` and the existing ``revl_swap`` run — so a patch that
would violate a guarantee is refused with its structured diagnostic, exactly as
a full-source swap of the same bytes would be.
"""

from __future__ import annotations

import copy
import re

from ..compiler import compile_source
from ..diagnostics import report
from ..errors import RevlError
from . import fillspec

_WORD_HOLE = re.compile(r"\bhole\b")


class EditError(RuntimeError):
    """A patch could not be applied to the server-side source (bad range, no
    hole on the addressed line, unknown buffer). A *result* an agent reads —
    the running composition and the working buffer are untouched."""


# ---------------------------------------------------------------- buffers

def virtual_source(session) -> dict:
    """The server-side working source set for `session`: ``{source, modules}``.

    Seeded from the running composition's admission inputs (`session.origin`)
    and carried on ``session.draft`` across edits, so an agent edits a source
    the server already holds instead of resending it. Only inline buffers are
    editable — the `source` string and any in-memory `modules` — because those
    are the ones the audit's #1 finding re-serializes and the ones a snapshot
    can faithfully reproduce. A file-backed composition edits on disk and swaps.
    """
    draft = getattr(session, "draft", None)
    if draft is not None:
        return draft
    origin = getattr(session, "origin", None) or {}
    return {"source": origin.get("source"),
            "modules": dict(origin.get("modules") or {})}


def _resolve_buffer(vs: dict, target: str | None) -> str:
    """Which named buffer an edit addresses. `None`/"source" is the main inline
    source; anything else must name an in-memory module."""
    if target in (None, "source"):
        if vs.get("source") is None:
            raise EditError(
                "there is no inline `source` buffer to edit — this composition "
                "was not loaded from inline source, so revl_edit has nothing "
                "server-side to patch. Edit the file(s) and revl_swap, or "
                "revl_load an inline `source` to iterate on it with revl_edit")
        return "source"
    if target in vs.get("modules", {}):
        return target
    available = ["source"] if vs.get("source") is not None else []
    available += sorted(vs.get("modules") or {})
    raise EditError(
        f"no server-side source buffer named {target!r}; "
        f"editable buffers: {', '.join(available) or 'none'}")


def _get_text(vs: dict, buffer: str) -> str:
    return vs["source"] if buffer == "source" else vs["modules"][buffer]


def _set_text(vs: dict, buffer: str, text: str) -> None:
    if buffer == "source":
        vs["source"] = text
    else:
        vs["modules"][buffer] = text


# ---------------------------------------------------------------- patching

def _line_span(text: str, line: int) -> tuple[int, int]:
    """The half-open character span of 1-based `line` in `text`."""
    lines = text.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        raise EditError(
            f"line {line} is out of range (the buffer has {len(lines)} line(s))")
    start = sum(len(lines[i]) for i in range(line - 1))
    return start, start + len(lines[line - 1])


def _hole_span(text: str, line: int) -> tuple[int, int]:
    """The character span of the typed-hole token on 1-based `line`.

    A hole is ``hole`` optionally followed by a bracketed type (`hole[T]`, with
    balanced brackets so `hole[Map[Str, Int]]` scans whole) and optionally a
    message string (`hole "why"`) — docs/holes.md §1. The whole token is what a
    fill replaces. The first hole on the line is taken."""
    lo, hi = _line_span(text, line)
    segment = text[lo:hi]
    match = _WORD_HOLE.search(segment)
    if match is None:
        raise EditError(
            f"no `hole` on line {line} to fill — revl_check reports each open "
            f"hole's line in its fillSpec; address that line")
    start = lo + match.start()
    pos = lo + match.end()
    after = _skip_ws(text, pos, hi)
    if after < hi and text[after] == "[":  # `hole[T]` — an explicit type
        pos = _match_brackets(text, after, hi)
    after = _skip_ws(text, pos, hi)
    if after < hi and text[after] == '"':  # `hole "why"` — a message
        pos = _match_string(text, after, hi)
    return start, pos


def _skip_ws(text: str, pos: int, hi: int) -> int:
    while pos < hi and text[pos] in " \t":
        pos += 1
    return pos


def _match_brackets(text: str, pos: int, hi: int) -> int:
    depth = 0
    while pos < hi:
        if text[pos] == "[":
            depth += 1
        elif text[pos] == "]":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    raise EditError("unbalanced `[` in the hole's type annotation")


def _match_string(text: str, pos: int, hi: int) -> int:
    pos += 1  # opening quote
    while pos < hi:
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == '"':
            return pos + 1
        pos += 1
    raise EditError("unterminated string in the hole's message")


def _apply_one(text: str, edit: dict) -> tuple[str, dict]:
    """Apply one patch to `text`, returning the new text and an echo of what it
    did (never the whole buffer — deltas, not documents, on the way back too)."""
    if not isinstance(edit, dict):
        raise EditError(f"each edit must be an object, got {type(edit).__name__}")

    if "hole" in edit:
        if "expr" not in edit:
            raise EditError("a hole edit needs `expr` (the fill expression)")
        start, end = _hole_span(text, int(edit["hole"]))
        expr = str(edit["expr"])
        return text[:start] + expr + text[end:], {
            "form": "hole", "line": int(edit["hole"]),
            "replaced": text[start:end], "expr": expr}

    if "anchor" in edit:
        anchor = str(edit["anchor"])
        if not anchor:
            raise EditError("an anchor edit needs a non-empty `anchor` string")
        replacement = str(edit.get("replacement", ""))
        occurrences = text.count(anchor)
        if occurrences == 0:
            raise EditError(f"anchor {anchor!r} does not occur in the buffer")
        count = edit.get("count")
        if count is None:
            new_text = text.replace(anchor, replacement)
            touched = occurrences
        else:
            new_text = text.replace(anchor, replacement, int(count))
            touched = min(int(count), occurrences)
        return new_text, {"form": "anchor", "anchor": anchor,
                          "replacement": replacement, "sites": touched}

    if "range" in edit:
        rng = edit["range"]
        if (not isinstance(rng, (list, tuple)) or not rng
                or len(rng) > 2):
            raise EditError("`range` must be [start] or [start, end]")
        start = int(rng[0])
        end = int(rng[1]) if len(rng) == 2 else start
        if not (0 <= start <= end <= len(text)):
            raise EditError(
                f"range [{start}, {end}] is out of bounds for a "
                f"{len(text)}-character buffer")
        replacement = str(edit.get("replacement", ""))
        return text[:start] + replacement + text[end:], {
            "form": "range", "range": [start, end],
            "replaced": text[start:end], "replacement": replacement}

    raise EditError(
        "each edit must carry one of `hole`, `anchor` or `range` "
        f"(got keys: {', '.join(sorted(edit)) or 'none'})")


def _apply_edits(text: str, edits: list) -> tuple[str, list[dict]]:
    """Apply every edit in order. Ranges refer to offsets in the text *as each
    edit sees it*, so an agent that sends offset-based edits should order them
    from the end of the buffer backwards; `hole`/`anchor` forms are position
    independent."""
    applied: list[dict] = []
    for edit in edits:
        text, echo = _apply_one(text, edit)
        applied.append(echo)
    return text, applied


# ---------------------------------------------------------------- compile

def compile_virtual(vs: dict, *, manifest: dict | None = None,
                    replacing: tuple = ()) -> dict:
    """Compile a working source set through the same entry points a full swap
    uses, so the admission gate is literally identical."""
    if vs.get("source") is not None:
        return compile_source(vs["source"], "<candidate>.rvl", manifest=manifest,
                              replacing=replacing, modules=vs.get("modules") or None)
    raise EditError("the working source set has no inline `source` to compile")


def _origin_from(vs: dict) -> dict:
    origin: dict = {}
    if vs.get("source") is not None:
        origin["source"] = vs["source"]
    if vs.get("modules"):
        origin["modules"] = dict(vs["modules"])
    return origin


# ---------------------------------------------------------------- the verb

def apply_edit(session, arguments: dict) -> dict:
    """Patch the server-side source of the running composition, then re-admit.

    Returns the admission verdict / open holes / diagnostic — never the whole
    source. Mirrors `revl_swap`'s gate exactly (admit against the running
    composition, then recompile the whole composition), so a patch that breaks
    a guarantee is refused with its diagnostic and the running system is
    untouched — the gate is not bypassed by editing rather than swapping.
    """
    from .session import SessionError  # noqa: PLC0415 — avoid an import cycle

    if not session.loaded:
        raise SessionError("nothing is loaded — revl_edit patches the source of "
                           "a running composition; call revl_load first")
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        raise EditError("`edits` must be a non-empty array of patch operations")

    # Work on a copy: nothing about the session changes until an edit compiles.
    vs = copy.deepcopy(virtual_source(session))
    buffer = _resolve_buffer(vs, arguments.get("target") or arguments.get("component"))
    new_text, applied = _apply_edits(_get_text(vs, buffer), edits)
    _set_text(vs, buffer, new_text)

    replacing = tuple(arguments.get("replacing") or ())

    # (1) compile the patched source on its own. This surfaces open holes as a
    # result (a draft compiles; admission is what refuses it) and catches any
    # parse/type error independent of the running composition. A failure here
    # is a refusal: nothing was admitted, and the server-side source is left at
    # its last good state.
    try:
        ir = compile_virtual(vs)
    except RevlError as error:
        rejected = report(error)
        rejected["edited"] = False
        rejected["swapped"] = False
        rejected["note"] = ("the patch does not compile — the running composition "
                            "and the server-side source are untouched")
        return rejected

    # (2) open holes -> checked, not admissible. Advance the working buffer so
    # the next edit builds on it (fill holes one at a time), but swap nothing.
    holes = fillspec.enrich(ir) if ir.get("holes") else []
    if holes:
        session.draft = vs
        return {"ok": True, "edited": True, "swapped": False, "admitted": False,
                "applied": applied, "holes": holes,
                **_summary(ir),
                "note": f"{len(holes)} open hole(s) remain — the edit was applied "
                        "to the server-side source and it compiles, but a hole may "
                        "not enter a running composition; fill them, then it swaps"}

    # (3) no holes: admit against the running composition — the SAME gate
    # revl_swap runs. A patch that breaks a guarantee is refused here with its
    # diagnostic; the running system and the server-side source are untouched.
    try:
        compile_virtual(vs, manifest=session.ir, replacing=replacing)
    except RevlError as error:
        rejected = report(error)
        rejected["edited"] = False
        rejected["admitted"] = False
        rejected["swapped"] = False
        rejected["note"] = ("the patch does not admit against the running "
                            "composition — it is untouched, and the server-side "
                            "source is unchanged")
        return rejected

    # (4) admitted and hole-free: hot-swap the whole recompiled composition in,
    # exactly as revl_swap does on a full-source resend.
    state = session.swap(ir, origin=_origin_from(vs))
    session.draft = None  # committed; re-derives from the new running source
    return {"ok": True, "edited": True, "admitted": True, "swapped": True,
            "applied": applied, **_summary(ir), **state}


def _summary(ir: dict) -> dict:
    from .server import _summary as _s  # noqa: PLC0415 — lazy, avoids import cycle

    return _s(ir)


__all__ = ["apply_edit", "virtual_source", "compile_virtual", "EditError"]
