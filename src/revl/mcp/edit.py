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

Nor is it bypassed by the *acting* gates. A patch that compiles clean is
hot-swapped in, so an edit is a swap by another name: an enforced component
lease and a required quarantine are checked on the patched source before
anything swaps, exactly as ``revl_swap`` checks them on a resent document. Only
one of the three gates ran here before, which made `revl_edit` a way around the
other two.
"""

from __future__ import annotations

import copy
import os
import re

from ..compiler import compile_source
from ..diagnostics import report
from ..errors import RevlError
from . import fillspec
from .authoring_loop import blast_radius
from .persist import (ORIGIN_FILES, ORIGIN_FILES_CONTENT, ORIGIN_MODULES,
                      ORIGIN_SOURCE)

_WORD_HOLE = re.compile(r"\bhole\b")


class EditError(RuntimeError):
    """A patch could not be applied to the server-side source (bad range, no
    hole on the addressed line, unknown buffer). A *result* an agent reads —
    the running composition and the working buffer are untouched."""


# ---------------------------------------------------------------- buffers

def virtual_source(session) -> dict:
    """The server-side working source set for `session`.

    Seeded from the running composition's admission inputs (`session.origin`)
    and carried on ``session.draft`` across edits, so an agent edits a source
    the server already holds instead of resending it.

    Two shapes. An inline composition is ``{source, modules}``. A composition
    loaded from `files` is ``{files, files_content, modules}``: one buffer per
    loaded file, keyed by the path it was loaded under (issue #1690). Its text is
    the text the session holds: read at load (issue #1842), and the text it last
    swapped in once an edit has run. Disk is read only for an origin that holds
    no text for a file (one restored from an older snapshot). Disk is never
    written: the edited text lives on the session, and `revl_snapshot` carries
    it.
    """
    draft = getattr(session, "draft", None)
    if draft is not None:
        return draft
    origin = getattr(session, "origin", None) or {}
    if origin.get("source") is None and origin.get("files"):
        return _files_source(origin)
    return {"source": origin.get("source"),
            "modules": dict(origin.get("modules") or {})}


def _files_source(origin: dict) -> dict:
    files = list(origin[ORIGIN_FILES])
    held = origin.get(ORIGIN_FILES_CONTENT) or {}
    return {ORIGIN_SOURCE: None, ORIGIN_FILES: files,
            ORIGIN_FILES_CONTENT: {path: held[path] if path in held else _read_disk(path)
                              for path in files},
            ORIGIN_MODULES: dict(origin.get(ORIGIN_MODULES) or {})}


def _read_disk(path: str) -> str | None:
    """A loaded file's text on disk, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _match_file(files: list, target: str) -> str | None:
    """The loaded path `target` names: the same spelling, or the same file."""
    if target in files:
        return target
    wanted = os.path.realpath(os.path.abspath(target))
    for path in files:
        if os.path.realpath(os.path.abspath(path)) == wanted:
            return path
    return None


def _editable(vs: dict) -> list[str]:
    names = ["source"] if vs.get("source") is not None else []
    return names + list(vs.get("files") or []) + sorted(vs.get("modules") or {})


def _resolve_buffer(vs: dict, target: str | None) -> tuple[str, str]:
    """Which buffer an edit addresses, as ``(kind, key)``. `None`/"source" is the
    main inline source, or the one loaded file when there is exactly one; a
    file path names a loaded file; anything else must name an in-memory module."""
    files = vs.get("files") or []
    if target in (None, "source"):
        if vs.get("source") is not None:
            return "source", "source"
        if target is None and len(files) == 1:
            return "file", files[0]
        if files:
            raise EditError(
                f"this composition was loaded from {len(files)} files; name the "
                f"one to edit in `target`: {', '.join(files)}")
        raise EditError("the running composition has no source buffer to edit")
    path = _match_file(files, target)
    if path is not None:
        return "file", path
    if target in (vs.get("modules") or {}):
        return "module", target
    raise EditError(
        f"no server-side source buffer named {target!r}; "
        f"editable buffers: {', '.join(_editable(vs)) or 'none'}")


def _get_text(vs: dict, buffer: tuple[str, str]) -> str:
    kind, key = buffer
    if kind == "source":
        return vs["source"]
    if kind == "file":
        text = vs["files_content"].get(key)
        if text is None:
            raise EditError(f"the loaded file {key!r} cannot be read, so there is "
                            "no text to patch")
        return text
    return vs["modules"][key]


def _set_text(vs: dict, buffer: tuple[str, str], text: str) -> None:
    kind, key = buffer
    if kind == "source":
        vs["source"] = text
    elif kind == "file":
        vs["files_content"][key] = text
    else:
        vs["modules"][key] = text


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


def _apply_to_buffers(vs: dict, edits: list, default_target) \
        -> tuple[list[dict], list[tuple[str, str]]]:
    """Apply every edit in order, each to the buffer its own `target` names (or
    the call's), so one call can change several files at once: a new import
    and the definition it needs land together or not at all. Returns the echo
    of each edit and the buffers they touched."""
    applied: list[dict] = []
    touched: list[tuple[str, str]] = []
    for edit in edits:
        if isinstance(edit, dict) and "symbol" in edit:
            buffer, text, echo = _apply_symbol(vs, edit)
        else:
            target = edit.get("target", default_target) \
                if isinstance(edit, dict) else default_target
            buffer = _resolve_buffer(vs, target)
            text, echo = _apply_one(_get_text(vs, buffer), edit)
        _set_text(vs, buffer, text)
        if buffer[0] != "source":
            echo["target"] = buffer[1]
        applied.append(echo)
        if buffer not in touched:
            touched.append(buffer)
    return applied, touched


def _apply_symbol(vs: dict, edit: dict) -> tuple[tuple[str, str], str, dict]:
    """``{symbol, replacement}``: replace one top-level declaration, addressed
    by name (issue #1714). The symbol may be qualified as `<buffer>:Name`."""
    from . import symbols  # noqa: PLC0415

    removing = edit.get("remove") is True
    if not removing and not isinstance(edit.get("replacement"), str):
        raise EditError("a symbol edit needs `replacement`, the declaration's "
                        "new text, or `remove: true`")
    symbol = edit["symbol"]
    if edit.get("target") and ":" not in str(symbol):
        symbol = f"{edit['target']}:{symbol}"
    try:
        if removing:
            return symbols.remove(vs, symbol)
        return symbols.replace(vs, symbol, edit["replacement"])
    except symbols.SymbolError as error:
        raise EditError(str(error)) from None


# ---------------------------------------------------------------- the path jail

def _use_paths(text: str, name: str) -> list[str]:
    """Every `use` path in `text`, read off the tokens, so a `use` anywhere
    (top level or inside a block) is seen and nothing has to parse past it.
    Raises EditError when the text does not lex: then its imports cannot be
    read, and the edit is refused rather than compiled (issue #1709)."""
    from ..lexer import lex  # noqa: PLC0415

    try:
        tokens = lex(text, name)
    except RevlError as error:
        raise _JailError(
            f"the patched {name} does not lex, so its `use` paths cannot be "
            f"checked against the sanctioned roots ({error}); nothing was "
            "compiled") from None
    return [after.value for tok, after in zip(tokens, tokens[1:])
            if tok.kind == "kw" and tok.value == "use" and after.kind == "string"]


class _JailError(EditError):
    """A patched buffer names a `use` path outside the sanctioned roots, or
    cannot be read for its `use` paths. Nothing is compiled."""


def _in_memory(vs: dict) -> set[str]:
    """The absolute paths a compile reads from the session, never the disk."""
    return ({os.path.abspath(k) for k in vs.get("modules") or {}}
            | {os.path.abspath(p) for p in vs.get("files") or []})


def _escaping_from_file(path: str, uses: list[str], vs: dict) -> list[str]:
    """A loaded file's imports resolve against the file's own directory. One
    that leaves the sanctioned roots is refused unless the operator's file on
    disk already names it."""
    from .server import _file_roots, _within_roots  # noqa: PLC0415 — cycle

    on_disk = _read_disk(path)
    operators = set(_use_paths(on_disk, path)) if on_disk is not None else set()
    roots, base = _file_roots(), os.path.dirname(os.path.abspath(path))
    escaping = []
    for use in uses:
        resolved = os.path.normpath(os.path.join(base, use))
        if use in operators or resolved in _in_memory(vs):
            continue
        if not _within_roots(resolved, roots):
            escaping.append(use)
    return escaping


def _escaping_inline(uses: list[str], vs: dict) -> list[str]:
    """Inline text resolves from the server's directory, as at load, so the
    load-time rule applies to the patched text: an absolute or upward path that
    no in-memory module supplies is refused."""
    from .server import _escaping_use  # noqa: PLC0415 — cycle

    return [use for use in uses if _escaping_use(use)
            and os.path.abspath(use) not in _in_memory(vs)]


def check_imports(vs: dict, touched: list[tuple[str, str]]) -> None:
    """Refuse the patch when a touched buffer's text, as it is AFTER every edit,
    names a `use` path that leaves the sanctioned roots (issue #1709).

    The pre-dispatch jail reads each edit's `replacement` on its own, and only
    when it parses as a program by itself. What the compile reads is the
    patched buffer, so an import split across two edits, or completed by text
    already in the buffer, reached the compiler unchecked. This reads the
    patched buffer instead, and fails closed when it cannot."""
    escaping = []
    for kind, key in touched:
        text = _get_text(vs, (kind, key))
        uses = _use_paths(text, key if kind != "source" else "source")
        escaping += (_escaping_from_file(key, uses, vs) if kind == "file"
                     else _escaping_inline(uses, vs))
    if escaping:
        named = ", ".join(f'`use "{p}"`' for p in sorted(set(escaping)))
        raise _JailError(
            f"refused: the patched source's {named} leaves the "
            "operator-sanctioned root(s) — an import an edit writes may not name "
            "an absolute path or a file outside them; nothing was compiled")


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
    uses, so the admission gate is literally identical.

    "Literally identical" now includes the AUTHORING TRUST the server runs
    under: `revl_edit` is an authoring verb like `revl_swap`, and a patch that
    inserts an `extern ... = @py { ... }` has to be refused for exactly the
    reason the same text sent to `revl_swap` is. The profile is READ from the
    server rather than passed in, so trust is decided in one place."""
    from .server import AUTHORING  # noqa: PLC0415 — lazy, avoids an import cycle

    if vs.get("source") is not None:
        return compile_source(vs["source"], "<candidate>.rvl", manifest=manifest,
                              replacing=replacing, modules=vs.get("modules") or None,
                              profile=AUTHORING.profile())
    if vs.get("files"):
        from .server import compile_under_authoring  # noqa: PLC0415 — cycle

        return compile_under_authoring(None, list(vs["files"]), manifest=manifest,
                                       modules=file_modules(vs) or None,
                                       replacing=replacing)
    raise EditError("the working source set has no source to compile")


def file_modules(vs: dict) -> dict:
    """A files-loaded working set as `compile_under_authoring` takes it: every
    buffer whose text is not what the file holds on disk, keyed by absolute path,
    beside any in-memory modules. The compiler reads a path from this map before
    the disk, so a `use` between two edited files resolves to the edited text.

    Only an edited buffer rides here, and that is what decides trust: text in
    this map arrived over the transport, so the compile runs under the
    authoring profile, exactly as `revl_swap` with `modules` does. A
    composition whose files are all unedited compiles as the operator's own
    jailed files, as at load."""
    modules = dict(vs.get("modules") or {})
    for path, text in (vs.get("files_content") or {}).items():
        if text is not None and text != _read_disk(path):
            modules[os.path.abspath(path)] = text
    return modules


def candidate_arguments(vs: dict, replacing: tuple = ()) -> dict:
    """The working set in the argument shape `revl_swap` takes, for the gates
    that read a candidate from its arguments (the lease derivation, the
    quarantine run). A files-loaded set carries its edited text as `modules`,
    so a gate compiles the patched files, never the stale ones on disk."""
    if vs.get("files"):
        arguments = {"files": list(vs["files"])}
        modules = file_modules(vs)
        if modules:
            arguments["modules"] = modules
    else:
        arguments = {k: v for k, v in vs.items() if k in ("source", "modules")}
    return {**arguments, "replacing": list(replacing)}


def _origin_from(vs: dict) -> dict:
    origin: dict = {}
    if vs.get(ORIGIN_SOURCE) is not None:
        origin[ORIGIN_SOURCE] = vs[ORIGIN_SOURCE]
    if vs.get(ORIGIN_FILES):
        origin[ORIGIN_FILES] = list(vs[ORIGIN_FILES])
        origin[ORIGIN_FILES_CONTENT] = dict(vs.get(ORIGIN_FILES_CONTENT) or {})
    if vs.get(ORIGIN_MODULES):
        origin[ORIGIN_MODULES] = dict(vs[ORIGIN_MODULES])
    return origin


# ---------------------------------------------------------------- the verb

def apply_edit(session, arguments: dict, verify=None, *, commit: bool = True,
               base: dict | None = None) -> dict:
    """Patch the server-side source of the running composition, then re-admit.

    Returns the admission verdict / open holes / diagnostic — never the whole
    source. Mirrors `revl_swap`'s gate exactly (admit against the running
    composition, then recompile the whole composition, then the acting gates —
    an enforced component lease and a required quarantine), so a patch that
    breaks a guarantee is refused with its diagnostic and the running system is
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
    # A speculative edit (issue #1696) starts from the caller's proposal.
    before = base if base is not None else virtual_source(session)
    vs = copy.deepcopy(before)
    applied, touched = _apply_to_buffers(
        vs, edits, arguments.get("target") or arguments.get("component"))
    try:
        check_imports(vs, touched)
    except _JailError as error:
        return _jail_refused(str(error))

    replacing = tuple(arguments.get("replacing") or ())
    return admit(session, vs, before, applied, replacing, verify, commit=commit)


def admit(session, vs: dict, before: dict, applied: list, replacing: tuple,
          verify=None, *, commit: bool = True) -> dict:
    """Compile, admit and gate a working set, then swap it in, or, with
    `commit=False`, stop short of the swap (issue #1696): the verdict comes back
    with `speculative: true` and the working set under `_proposal`, for the
    server to hold as the caller's proposal. Nothing about the session changes
    on that path."""
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
    from .server import _untrusted_author  # noqa: PLC0415 - no import cycle
    holes = (fillspec.enrich(ir, untrusted=_untrusted_author())
             if ir.get("holes") else [])
    if holes:
        if commit:
            session.draft = vs
        else:
            return {"ok": True, "edited": True, "swapped": False, "admitted": False,
                    "speculative": True, "_proposal": vs, "applied": applied,
                    "touched": _touched(before, vs), "holes": holes,
                    "blastRadius": blast_radius(session.ir, ir), **_summary(ir),
                    "note": f"proposed with {len(holes)} open hole(s); fill them "
                            "before it can commit"}
        return {"ok": True, "edited": True, "swapped": False, "admitted": False,
                "applied": applied, "touched": _touched(before, vs), "holes": holes,
                "blastRadius": blast_radius(session.ir, ir),
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
    # exactly as revl_swap does on a full-source resend — which means it has to
    # answer to exactly what a full-source resend answers to. Two of those gates
    # were wired only into `server._tool_swap` (and `_tool_repair`), so an
    # enforced component lease and a required quarantine were both bypassable by
    # *editing* rather than swapping: same component, same replacement, a
    # different verb. Both run here, after the candidate is known to be
    # admissible and before anything is swapped, so a refusal leaves the running
    # composition and the working buffer exactly as they were.
    from . import server as _srv  # noqa: PLC0415 — lazy, avoids an import cycle

    # The gate sees the PATCHED source set in the shape a swap carries it, so
    # the lease derivation scopes the edit's real replacement targets (and falls
    # back to the whole composition, i.e. fails closed, when they cannot be
    # derived).
    gate_arguments = candidate_arguments(vs, replacing)
    refusal = _srv._leases.check_swap(session, gate_arguments)
    if refusal is not None:
        return _srv._refused_by_lease(refusal)
    quarantined = _srv._quarantine.gate_swap(session, gate_arguments)
    if quarantined is not None:  # required quarantine: not proved, not swapped
        return {**quarantined, "edited": False, "applied": applied}
    # a caller's extra verification (revl_change's gauntlet, issue #1695): it
    # runs on the exact candidate, after every gate, before anything swaps
    refused = verify(gate_arguments) if verify is not None else None
    if refused is not None:
        return {**refused, "edited": False, "swapped": False, "applied": applied,
                "touched": _touched(before, vs)}

    # issue #1704: the cascade of what this edit replaces, read off the
    # composition that is running now, so preflight comes with the change. A
    # proposal carries it too: that is where preflight is worth the most.
    radius = blast_radius(session.ir, ir)
    if not commit:
        return {"ok": True, "edited": True, "admitted": True, "swapped": False,
                "speculative": True, "_proposal": vs, "applied": applied,
                "touched": _touched(before, vs), "blastRadius": radius,
                **_summary(ir),
                "note": "proposed and verified: admission and every gate passed "
                        "against the running composition, which is unchanged. "
                        "Commit it, or discard it"}
    state = session.swap(ir, origin=_origin_from(vs))
    session.draft = None  # committed; re-derives from the new running source
    return {"ok": True, "edited": True, "admitted": True, "swapped": True,
            "applied": applied, "touched": _touched(before, vs),
            "blastRadius": radius, **_summary(ir), **state}


def running_source(session) -> dict:
    """The running composition's working set, ignoring any draft."""
    draft, session.draft = getattr(session, "draft", None), None
    try:
        return virtual_source(session)
    finally:
        session.draft = draft


def _touched(before: dict, after: dict) -> list[dict]:
    """The top-level symbols a change added, changed or removed (issue #1714)."""
    from . import symbols  # noqa: PLC0415

    return symbols.touched(before, after)


def _jail_refused(message: str) -> dict:
    return {"ok": False, "edited": False, "admitted": False, "swapped": False,
            "diagnostics": [{"severity": "error", "code": "REVL",
                             "category": "admission", "message": message}],
            "note": "nothing was read or compiled; the running composition and "
                    "the server-side source are untouched"}


def _summary(ir: dict) -> dict:
    from .server import _summary as _s  # noqa: PLC0415 — lazy, avoids import cycle

    return _s(ir)


__all__ = ["apply_edit", "virtual_source", "compile_virtual", "file_modules",
           "candidate_arguments", "EditError"]
