"""Agent notes: knowledge a session adds, kept, served and round-tripped
(issue #1754; knowledge slice 3 of the design study).

Slice 2 indexes the comments a composition already has. This module holds the
notes an agent (or a human, through an export) adds on top:

* a NOTE is a record anchored to a declaration's symbol path, with a kind, a
  bounded body, optional evidence, an author and a trust level. Records are
  append-only: retiring, confirming and superseding a note are new records;
* the SIDECAR is one JSON file per record under
  `<composition dir>/.revl/knowledge/<id>.json`, so two lanes never write the
  same file. It is read at `revl_load` and written only by `revl_export
  {with_knowledge: true}`: disk is an export (issue #1696);
* TRUST follows authorship. Under the untrusted-author profile an agent's note
  is `trust: untrusted`, and by default only an evidence-backed untrusted note
  rides with its body; an evidence-free one rides as its id and kind, with the
  body on request (`revl_knowledge {op: "query", id}`). Knowledge is data: it
  never changes what admits or swaps, and the server never puts a body
  anywhere but the `knowledge` field;
* STALENESS is the slice-2 rule: a note whose anchor's code fingerprint
  changes goes `stale`; `confirm` re-fingerprints it;
* the ROUND TRIP: an export renders each live note as a marked comment block
  above its anchor (`// note k_<id>:`, the body, then `// [k_<id>]`), and a
  load reads marked blocks back. A body that differs from the record becomes a supersede by
  `author: human`; a marked block with no record becomes a human note.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

from .. import knowledge as _k

KINDS = ("rationale", "invariant", "trap", "alternative-rejected", "purpose", "doc")
EVIDENCE_KINDS = ("diagnostic", "query", "audit", "test", "issue")
BODY_LIMIT = 2048
PER_ANCHOR_LIMIT = 16
SIDECAR = os.path.join(".revl", "knowledge")
TRAILER = re.compile(r"^\[(k_[0-9a-f]{12})\]$")

#: the ride policy and `served` live in `revl.knowledge`, outside `revl.mcp`, so
#: the registry serves a shipped record by the same rule (issue #1780)
RIDE_POLICY = _k.RIDE_POLICY
served = _k.served


class NoteError(ValueError):
    """A note that cannot be accepted. Nothing was recorded."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _new_id(*parts: str) -> str:
    return "k_" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- the store

def _records(session) -> dict:
    store = getattr(session, "note_records", None)
    if store is None:
        store = {}
        session.note_records = store
    return store


def notes(session) -> dict:
    """id -> the current state of every note, folded from the records, with
    `stale` from the last `refresh`."""
    folded = _fold(session)
    stale = getattr(session, "note_stale", None) or set()
    refuted = getattr(session, "note_refuted", None) or set()

    def status(note_id: str, note: dict) -> dict:
        if note["status"] != "live":
            return note
        if note_id in refuted:
            return {**note, "status": "refuted"}
        if note_id in stale:
            return {**note, "status": "stale"}
        return note
    return {i: status(i, n) for i, n in folded.items()}


def _fold(session) -> dict:
    folded: dict = {}
    for record in sorted(_records(session).values(),
                         key=lambda r: (r["created"], r.get("seq", 0), r["id"])):
        op = record.get("op", "note")
        if op == "note":
            folded[record["id"]] = {**record, "status": "live"}
            target = record.get("supersedes")
            if target in folded:
                folded[target] = {**folded[target], "status": "superseded",
                                  "supersededBy": record["id"]}
        elif record.get("target") in folded:
            target = folded[record["target"]]
            if op == "retire":
                folded[record["target"]] = {**target, "status": "retired",
                                            "retired": record.get("reason")}
            elif op == "confirm":
                folded[record["target"]] = {
                    **target, "status": "live",
                    "anchor": {**target["anchor"],
                               "fingerprint": record["fingerprint"]}}
    return folded


def clear(session) -> None:
    session.note_records = {}
    session.note_stale = set()
    session.note_refuted = set()
    session.note_rebased = {}


def _store(session, record: dict) -> dict:
    """Append a record. `seq` orders records written within one timestamp, so a
    confirm or retire always folds after the note it targets."""
    records = _records(session)
    record = {**record, "seq": 1 + max((r.get("seq", 0) for r in records.values()),
                                       default=0)}
    records[record["id"]] = record
    return record


# ---------------------------------------------------------------- anchoring

def _anchor_of(session, vs: dict, symbol: str) -> dict:
    """The anchor record for `symbol` in the held source: path, symbol path,
    and the fingerprint of its comment-free code."""
    from . import symbols  # noqa: PLC0415

    try:
        buffer, text, decl, found = symbols.locate(vs, symbol)
    except symbols.SymbolError as error:
        raise NoteError(str(error)) from None
    if found is not None:
        first, last, path = found.first, found.last, found.path
    else:
        first, last, path = decl.start, decl.end, decl.name
    return {"path": buffer[1], "symbol": path,
            "fingerprint": _k.fingerprint(text, buffer[1], first, last)}


def _trust(session) -> dict:
    from .server import AUTHORING  # noqa: PLC0415 — cycle

    return {"kind": "agent",
            "trust": "untrusted" if AUTHORING.profile() is not None else "operator"}


# ---------------------------------------------------------------- evidence (slice 4)

def query_verbs() -> dict:
    """verb -> (argument name, query function): the static query verbs a
    `query` evidence may name, read-only by construction. Read off
    `query.QUERIES`, the one table of those verbs, and each function's own
    second parameter, so no second copy of the vocabulary lives here."""
    import inspect  # noqa: PLC0415

    from .. import query as Q  # noqa: PLC0415

    return {verb: (list(inspect.signature(fn).parameters)[1], fn)
            for verb, fn in Q.QUERIES.items()}
#: evidence the server re-runs; `test` and `issue` are citations
RERUNNABLE = ("diagnostic", "query", "audit")


def _validate_evidence(item) -> None:
    if not isinstance(item, dict) or item.get("kind") not in EVIDENCE_KINDS:
        raise NoteError("evidence is a list of {kind, ...} with kind one of "
                        f"{', '.join(EVIDENCE_KINDS)}: read-only checks the "
                        "server may run, never a call or a swap")
    kind = item["kind"]
    if kind == "query":
        verbs = query_verbs()
        if item.get("verb") not in verbs:
            raise NoteError("a `query` evidence names one of the read-only query "
                            f"verbs ({', '.join(verbs)}); "
                            f"{item.get('verb')!r} is not one")
        arg = verbs[item["verb"]][0]
        if not isinstance((item.get("args") or {}).get(arg), str):
            raise NoteError(f"a `{item['verb']}` query evidence needs `args.{arg}`")
        if not isinstance(item.get("expect"), dict) or not item["expect"]:
            raise NoteError("a `query` evidence needs `expect`: the fields of the "
                            "query's answer it asserts, e.g. {cascade: [...]}")
    elif kind == "diagnostic":
        expect = item.get("expect")
        if not isinstance(item.get("source"), str) or not isinstance(expect, dict) \
                or not isinstance(expect.get("code"), str):
            raise NoteError("a `diagnostic` evidence is {source, expect: {code}}: "
                            "inline source the compiler refuses with that code")
    elif kind == "audit":
        if not isinstance(item.get("component"), str) \
                or not isinstance(item.get("expect"), dict):
            raise NoteError("an `audit` evidence is {component, expect: {emissions?, "
                            "capabilities?}}")


def _project(value, like):
    """`value` reduced to the shape of `like`, so an expectation names only the
    fields it asserts: a list of dicts becomes the list of their `component`
    (or `key`, or `name`) when `like` is a list of strings."""
    if isinstance(like, list) and all(isinstance(x, str) for x in like) \
            and isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                item = next((item[k] for k in ("component", "key", "name") if k in item),
                            None)
            out.append(item)
        return sorted(out, key=str)
    return value


def run_evidence(session, item: dict) -> bool | None:
    """Re-run one evidence item against the running composition: True when it
    still holds, False when it does not, None when it is a citation."""
    kind = item.get("kind")
    if kind not in RERUNNABLE:
        return None
    try:
        if kind == "query":
            arg, fn = query_verbs()[item["verb"]]
            answer = fn(session.ir, item["args"][arg])
            if answer.get("ok") is False:
                return False
            return all(_project(answer.get(key), want) ==
                       (sorted(want, key=str) if isinstance(want, list) else want)
                       for key, want in item["expect"].items())
        if kind == "diagnostic":
            from .server import compile_under_authoring  # noqa: PLC0415
            from ..diagnostics import report  # noqa: PLC0415
            from ..errors import RevlError  # noqa: PLC0415

            try:
                compile_under_authoring(item["source"], None)
            except RevlError as error:
                return report(error).get("diagnostics", [{}])[0].get("code") \
                    == item["expect"]["code"]
            return False
        if kind == "audit":
            from ..boundary import _boundary  # noqa: PLC0415

            row = _boundary(session.ir).get(item["component"])
            if row is None:
                return False
            return all(sorted(row.get(key) or []) == sorted(want)
                       if isinstance(want, list) else row.get(key) == want
                       for key, want in item["expect"].items())
    except Exception:  # noqa: BLE001 — evidence that cannot run does not hold
        return False
    return None


def _validate(note: dict) -> None:
    if not isinstance(note, dict):
        raise NoteError("a note is {kind, body, evidence?, symbol?}")
    if note.get("kind") not in KINDS:
        raise NoteError(f"a note's `kind` is one of {', '.join(KINDS)}")
    body = note.get("body")
    if not isinstance(body, str) or not body.strip():
        raise NoteError("a note needs a non-empty `body`")
    if len(body) > BODY_LIMIT:
        raise NoteError(f"a note's body is at most {BODY_LIMIT} characters; this "
                        f"one is {len(body)}. Refused, not truncated")
    for item in note.get("evidence") or []:
        _validate_evidence(item)


def add(session, vs: dict, note: dict, symbol: str, *, author=None,
        supersedes: str | None = None) -> dict:
    """Record a note anchored to `symbol` in `vs`."""
    _validate(note)
    anchor = _anchor_of(session, vs, symbol)
    live = [n for n in notes(session).values()
            if n["anchor"]["symbol"] == anchor["symbol"]
            and n["status"] in ("live", "stale")]
    if len(live) >= PER_ANCHOR_LIMIT:
        raise NoteError(f"`{anchor['symbol']}` already has {len(live)} notes; "
                        "retire or supersede one first")
    created = _now()
    record = {"op": "note", "id": _new_id(anchor["symbol"], note["body"], created),
              "anchor": anchor, "kind": note["kind"], "body": note["body"],
              "evidence": list(note.get("evidence") or []),
              "author": author or _trust(session), "created": created,
              "supersedes": supersedes}
    return _store(session, record)


def op_record(session, op: str, target: str, **fields) -> dict:
    if target not in notes(session):
        raise NoteError(f"no note `{target}`")
    created = _now()
    return _store(session, {"op": op, "id": _new_id(op, target, created),
                            "target": target, "created": created,
                            "author": _trust(session), **fields})


# ---------------------------------------------------------------- staleness

def refresh(session, vs: dict) -> None:
    """Recompute every live note's status against the running composition.

    A note whose evidence the server can re-run (slice 4: diagnostic, query,
    audit) is re-checked every time, because a query's answer depends on the
    whole composition and not only on the anchor: all checks hold, it is live
    and re-fingerprinted; any fails, it is refuted. A note with only citations
    (test, issue) or none goes stale when its anchor's code changed (or the
    anchor vanished) since it was written or last confirmed. Records are never
    rewritten."""
    stale, refuted = set(), set()
    rebased = dict(getattr(session, "note_rebased", None) or {})
    for note_id, note in _fold(session).items():
        if note["status"] != "live":
            continue
        try:
            now = _anchor_of(session, vs, note["anchor"]["symbol"])["fingerprint"]
        except NoteError:
            now = None
        checks = [run_evidence(session, item) for item in note.get("evidence") or []]
        checks = [c for c in checks if c is not None]
        if checks:
            if all(checks) and now is not None:
                rebased[note_id] = now
            else:
                refuted.add(note_id)
            continue
        if now != rebased.get(note_id, note["anchor"]["fingerprint"]):
            stale.add(note_id)
    session.note_stale, session.note_refuted = stale, refuted
    session.note_rebased = rebased


# ---------------------------------------------------------------- serving

def concerning(session, symbols: list[str]) -> list[dict]:
    out = []
    for note in notes(session).values():
        anchor = note["anchor"]["symbol"]
        if note["status"] in ("retired", "superseded"):
            continue
        if any(anchor == s or anchor.startswith(s + ".") or s.startswith(anchor + ".")
               for s in symbols):
            out.append(served(note))
    return out


# ---------------------------------------------------------------- the sidecar

def sidecar_dir(vs: dict) -> str | None:
    """`<dir of the first loaded file>/.revl/knowledge`, or None inline."""
    files = vs.get("files") or []
    return os.path.join(os.path.dirname(os.path.abspath(files[0])), SIDECAR) \
        if files else None


def load_sidecar(session, vs: dict) -> int:
    """Read every record of the composition's sidecar into the session."""
    session.note_records = {}
    directory = sidecar_dir(vs)
    if directory is None or not os.path.isdir(directory):
        return 0
    count = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            _records(session)[record["id"]] = record
            count += 1
    return count


def sidecar_writes(session, vs: dict) -> list[tuple[str, str]]:
    """(path, text) of every record not yet in the sidecar. A vendored record is
    the vendor's, so it never lands in the project's sidecar."""
    directory = sidecar_dir(vs)
    if directory is None:
        return []
    out = []
    for record in _records(session).values():
        if record.get("vendored"):
            continue
        path = os.path.join(directory, f"{record['id']}.json")
        if not os.path.exists(path):
            out.append((path, json.dumps(record, indent=2, sort_keys=True) + "\n"))
    return out


# ---------------------------------------------------------------- vendored trucs

def vendored_path(path: str) -> tuple[str, str, str] | None:
    """(truc name, path, project dir) when `path` is a vendored truc's
    `trucs/<name>/component.rvl`, else `None` (issue #1769)."""
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    if len(parts) >= 3 and parts[-1] == "component.rvl" and parts[-3] == "trucs":
        return (parts[-2], path, os.sep.join(parts[:-3]) or os.sep)
    return None


def vendored_trucs(vs: dict) -> list[tuple[str, str, str]]:
    """(truc name, loaded path, project dir) of every loaded file that is a
    vendored truc's `trucs/<name>/component.rvl`.

    Both the `files` the session was loaded with and the READ-ONLY dependency
    buffers a `use` resolved to are walked (issue #1779): a truc reached
    through `use` is loaded, and its records are served on the same terms."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for path in [*(vs.get("files") or []), *(vs.get("dependencies") or {})]:
        found = vendored_path(path)
        norm = os.path.normpath(os.path.abspath(path))
        if found is not None and norm not in seen:
            seen.add(norm)
            out.append(found)
    return out


def _lock_row(project: str, name: str) -> dict | None:
    try:
        with open(os.path.join(project, "truc.lock"), encoding="utf-8") as handle:
            lock = json.load(handle)
    except (OSError, ValueError):
        return None
    rows = lock.get("trucs") if isinstance(lock, dict) else None
    return next((r for r in rows or [] if isinstance(r, dict)
                 and r.get("name") == name), None)


def _holds_pinned_bytes(row: dict | None, text: str | None) -> bool:
    """Whether the loaded component is the one the lock pins."""
    pinned = (row or {}).get("sourceHash")
    return bool(pinned) and text is not None \
        and hashlib.sha256(text.encode("utf-8")).hexdigest() == pinned


def _vendored_trust(row: dict | None, doc: dict, held: bool) -> str:
    """`publisher` only when the add measured a signature over these records
    and neither they nor the component changed since; `untrusted` otherwise."""
    from .. import registry  # noqa: PLC0415

    pin = (row or {}).get("knowledge") or {}
    signed = pin.get("signed") is True and pin.get("hash") == registry._facet_hash(doc)
    return "publisher" if signed and held else "untrusted"


def load_vendored(session, vs: dict) -> int:
    """Read the knowledge each loaded vendored truc carries (issue #1769).

    A record is anchored to the loaded `component.rvl` and marked `vendored`
    with the truc's name. While the component holds the bytes the lock pins, the
    records are re-based on it, since they shipped with exactly that code; an
    edit to it in the session then stales them as usual. A record
    whose id the project already has is the project's, and is skipped.

    A truc reached through `use` rather than named in `files` counts as loaded
    (issue #1779): its bytes ride the read-only dependency buffers, which hold
    the exact text the compile read, so `held` — and therefore the publisher
    trust the lock can earn — is measured against what compiled, not against a
    fresh read of the disk."""
    from .. import registry  # noqa: PLC0415

    count = 0
    for name, path, project in vendored_trucs(vs):
        doc = registry.load_knowledge(os.path.dirname(path), regular_only=True)
        if doc is None:
            continue
        row = _lock_row(project, name)
        text = (vs.get("files_content") or {}).get(path)
        if text is None:
            text = (vs.get("dependencies") or {}).get(path)
        held = _holds_pinned_bytes(row, text)
        trust = _vendored_trust(row, doc, held)
        for record in doc["records"]:
            if record["id"] in _records(session):
                continue
            anchor = {**(record.get("anchor") or {}), "path": path}
            if held:
                try:
                    anchor["fingerprint"] = _anchor_of(
                        session, vs, anchor.get("symbol") or "")["fingerprint"]
                except NoteError:
                    pass
            _records(session)[record["id"]] = {
                **record, "anchor": anchor, "vendored": name,
                "author": {**(record.get("author") or {}), "trust": trust}}
            count += 1
    return count


# ---------------------------------------------------------------- round trip

def _marked(block) -> tuple[str | None, str]:
    """(note id, body) of a block ending in a rendered note, or (None, "")."""
    _rest, note = _k.split_note(block)
    return note if note is not None else (None, "")


def render(text: str, name: str, file_notes: list[dict]) -> str:
    """`text` with each note rendered as a marked block above its anchor,
    skipping a note whose trailer the text already carries."""
    from . import symbols  # noqa: PLC0415

    present = {_marked(b)[0] for b in _k.comment_blocks(text)} - {None}
    lines = text.split("\n")
    inserts: dict[int, list[str]] = {}
    for note in file_notes:
        if note["id"] in present or note["status"] in ("retired", "superseded"):
            continue
        try:
            _b, _t, decl, found = symbols.locate(
                {"source": None, "files": [name], "files_content": {name: text},
                 "modules": {}}, note["anchor"]["symbol"])
        except symbols.SymbolError:
            continue
        line = found.first if found is not None else decl.start
        indent = re.match(r"\s*", lines[line - 1]).group(0)
        block = ([f"{indent}// note {note['id']}:"]
                 + [f"{indent}// {body_line}".rstrip()
                    for body_line in note["body"].split("\n")]
                 + [f"{indent}// [{note['id']}]"])
        inserts.setdefault(line, []).extend(block)
    out = []
    for number, line in enumerate(lines, 1):
        out.extend(inserts.get(number, []))
        out.append(line)
    return "\n".join(out)


def import_marked(session, vs: dict) -> list[dict]:
    """Read the marked blocks of the loaded files back. A body a human changed
    supersedes its note (author human); a block with no record becomes one."""
    current = notes(session)
    created = []
    for name, text in ((p, (vs.get("files_content") or {}).get(p))
                       for p in vs.get("files") or []):
        if text is None:
            continue
        for block in _k.comment_blocks(text):
            note_id, body = _marked(block)
            if note_id is None:
                continue
            existing = current.get(note_id)
            if existing is not None and existing["body"] == body:
                continue
            symbol = _k.anchor_of(text, name, block,
                                  _declarations(text, name), {})[0]
            if symbol is None:
                continue
            human = {"kind": "human", "trust": "operator"}
            kind = existing["kind"] if existing is not None else "doc"
            try:
                created.append(add(session, vs, {"kind": kind, "body": body},
                                   symbol, author=human,
                                   supersedes=note_id if existing else None))
            except NoteError:
                continue
    return created


def _declarations(text: str, name: str) -> list:
    from . import symbols  # noqa: PLC0415

    try:
        return symbols.declarations(text, name)
    except symbols.SymbolError:
        return []


__all__ = ["KINDS", "EVIDENCE_KINDS", "query_verbs", "RERUNNABLE", "run_evidence",
           "NoteError", "notes", "add", "op_record",
           "refresh", "served", "concerning", "load_sidecar", "sidecar_writes",
           "load_vendored", "vendored_trucs", "vendored_path",
           "render", "import_marked"]
