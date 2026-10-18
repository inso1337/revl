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
  {with_knowledge: true}`, each record as a new file created exclusively,
  never over one already there (issue #2534): disk is an export (issue #1696).
  A composition loaded from an export copy also reads the sidecar of the
  file it copies, and writes only its own (issue #2575). An id is `k_` and
  twelve lower-case hex digits; a record file holding any other id is
  ignored, and no file name is built from one (issue #2564);
* TRUST follows authorship. Under the untrusted-author profile an agent's note
  is `trust: untrusted`, and by default only an evidence-backed untrusted note
  rides with its body; an evidence-free one rides as its id and kind, with the
  body on request (`revl_knowledge {op: "query", id}`). Knowledge is data: it
  never changes what admits or swaps, and the server never puts a body
  anywhere but the `knowledge` field. No author confirms, retires or
  supersedes a note whose author is trusted further (issue #2575);
* STALENESS is the slice-2 rule: a note whose anchor's code fingerprint
  changes goes `stale`; `confirm` re-fingerprints it;
* the ROUND TRIP: an export renders each live note as a marked comment block
  above its anchor (`// note k_<id>:`, the body, then `// [k_<id>]`), and a
  load reads marked blocks back. A body that differs from the record becomes a
  supersede (or, when the record's author is trusted further than the file's
  writer, a note beside it naming it in `changes`, issue #2577), and a marked
  block with no record a new note, authored by whoever wrote the file (issue
  #2550): an untrusted agent for a file under
  `.revl/export/` or for an adopted copy of an export elsewhere (issue
  #2577), an untrusted vendor for a vendored truc's component, a
  human at operator trust for any other file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

from .. import knowledge as _k
from .. import export_area as _export_area

KINDS = ("rationale", "invariant", "trap", "alternative-rejected", "purpose", "doc")
EVIDENCE_KINDS = ("diagnostic", "query", "audit", "test", "issue")
BODY_LIMIT = 2048
PER_ANCHOR_LIMIT = 16
SIDECAR = os.path.join(".revl", "knowledge")
#: the export directory's parts, below a root (`export_area.EXPORT_DIR`)
_EXPORT_PARTS = tuple(_export_area.EXPORT_DIR.split(os.sep))
#: a record id, the only one the store mints and the only one a sidecar file
#: name is built from (issue #2564)
RECORD_ID = re.compile(r"k_[0-9a-f]{12}")
TRAILER = re.compile(rf"^\[({RECORD_ID.pattern})\]$")

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
    """Record a note anchored to `symbol` in `vs`, by `author` (the session's
    trust when omitted). A supersede of a note whose author ranks above
    `author` is refused (`_check_rank`)."""
    _validate(note)
    author = author or _trust(session)
    if supersedes is not None:
        if supersedes not in notes(session):
            raise NoteError(f"no note `{supersedes}`")
        _check_rank(author, notes(session)[supersedes], "supersede")
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
              "author": author, "created": created,
              "supersedes": supersedes}
    return _store(session, record)


def op_record(session, op: str, target: str, **fields) -> dict:
    """Record `op` (confirm, retire) on the note `target`, by the session. A
    note whose author ranks above the session is refused (`_check_rank`)."""
    if target not in notes(session):
        raise NoteError(f"no note `{target}`")
    author = _trust(session)
    _check_rank(author, notes(session)[target], op)
    created = _now()
    return _store(session, {"op": op, "id": _new_id(op, target, created),
                            "target": target, "created": created,
                            "author": author, **fields})


#: how far a note's author is trusted. A trust this does not name ranks
#: lowest as the one acting and as an operator's as the one acted on
TRUST_RANK = {"untrusted": 0, "publisher": 1, "operator": 2}


def _check_rank(author: dict, target: dict, op: str) -> None:
    """Refuse `author` retiring, confirming or superseding `target` when the
    target's author is trusted further (issue #2575): an untrusted author
    may not take an operator's or a publisher's note away, nor change it.
    The fold applies every record it is given, so the rule is here, where
    a record is made."""
    mine, owner = _trust_of(author), _trust_of(target.get("author"))
    if TRUST_RANK.get(owner, TRUST_RANK["operator"]) > TRUST_RANK.get(mine, 0):
        raise NoteError(f"note `{target.get('id')}` has author trust "
                        f"`{owner or 'unlabelled'}`; an author at trust "
                        f"`{mine or 'unlabelled'}` cannot {op} it")


def _trust_of(author) -> str | None:
    return author.get("trust") if isinstance(author, dict) else None


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
    """`<dir of the first loaded file>/.revl/knowledge`, or None inline: the
    sidecar this composition's records are written to. For an export copy
    that is the copy's own, inside the export area (issue #2575)."""
    files = vs.get("files") or []
    return os.path.join(os.path.dirname(os.path.abspath(files[0])), SIDECAR) \
        if files else None


def sidecar_dirs(vs: dict) -> list[str]:
    """Every sidecar this composition reads, first to last. For an export copy
    the sidecar of the file it copies comes first, then the copy's own
    (`sidecar_dir`): a block rendered into the copy then finds its record,
    where reading the copy's own alone imported each block again as a new
    note with a fresh id, and every export rendered it once more (issue
    #2575). Reads only: the copy's records are written to its own sidecar,
    and reach the operator's only by adoption."""
    own = sidecar_dir(vs)
    if own is None:
        return []
    copied = _copied_file(vs["files"][0])
    shared = os.path.join(os.path.dirname(copied), SIDECAR) if copied else None
    return [shared, own] if shared not in (None, own) else [own]


def _copied_file(path: str) -> str | None:
    """`<root>/<rel>` when `path` resolves to `<root>/.revl/export/<rel>` under
    a sanctioned root, else None: the inverse of `export_jail.export_copy`.
    The root is one the server sanctioned, never inferred from the path's
    spelling, and the export directory is matched case-folded, as
    `export_area.under_export_dir` matches it."""
    from .server import _file_roots  # noqa: PLC0415 (import cycle)

    parts = _export_area.real(path).split(os.sep)
    for root in _file_roots():
        base = root.rstrip(os.sep).split(os.sep)
        start, end = len(base), len(base) + len(_EXPORT_PARTS)
        if (parts[:start] == base and len(parts) > end
                and tuple(_export_area.fold(p) for p in parts[start:end])
                == _EXPORT_PARTS):
            return os.path.join(root, *parts[end:])
    return None


def load_sidecar(session, vs: dict) -> int:
    """Read every record of the composition's sidecars (`sidecar_dirs`) into
    the session. A record whose id is not a record id is ignored (issue
    #2564), and one whose id an earlier sidecar already gave is not read
    again: the copied file's record stands over a copy's (issue #2575)."""
    session.note_records = {}
    count = 0
    for directory in sidecar_dirs(vs):
        count += _read_records(_records(session), directory)
    return count


def _read_records(store: dict, directory: str) -> int:
    """Add each record file of `directory` to `store`, unless an earlier
    directory gave its id. Returns how many were added."""
    if not os.path.isdir(directory):
        return 0
    earlier = set(store)
    count = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and is_record_id(record.get("id"))
                and record["id"] not in earlier):
            store[record["id"]] = record
            count += 1
    return count


def is_record_id(value) -> bool:
    """Whether `value` is a record id: `k_` and twelve lower-case hex digits."""
    return isinstance(value, str) and RECORD_ID.fullmatch(value) is not None


def record_path(directory: str, record_id) -> str | None:
    """`<directory>/<record_id>.json`, or None unless `record_id` is a record
    id (issue #2564). A record id holds no separator and no `..`, so the name
    stays in `directory`; where that directory resolves to is the export's
    root check (`_within_roots`), and the new-file-only writer refuses a file
    or a link already at the path."""
    return os.path.join(directory, f"{record_id}.json") \
        if is_record_id(record_id) else None


def is_record_path(path: str, directory: str | None) -> bool:
    """Whether `path` is a sidecar record file of `directory`."""
    name = os.path.basename(path)
    return (directory is not None and name.endswith(".json")
            and record_path(directory, name[:-len(".json")]) == path)


def sidecar_writes(session, vs: dict,
                   skipped: list | None = None) -> list[tuple[str, str]]:
    """(path, text) of every record not yet in the sidecar. A vendored record is
    the vendor's, so it never lands in the project's sidecar. A record whose
    id is not a record id names no file: it is added to `skipped`, never
    written (issue #2564). A record one of the sidecars read already holds
    is not written again: an export copy's session never copies the copied
    file's records into its own sidecar (issue #2575)."""
    directory = sidecar_dir(vs)
    if directory is None:
        return []
    read = sidecar_dirs(vs)
    out = []
    for record in _records(session).values():
        if record.get("vendored"):
            continue
        path = record_path(directory, record.get("id"))
        if path is None:
            if skipped is not None:
                skipped.append(f"knowledge record {record.get('id')!r}")
            continue
        if not any(os.path.exists(record_path(d, record["id"])) for d in read):
            out.append((path, json.dumps(record, indent=2, sort_keys=True) + "\n"))
    return out


# ---------------------------------------------------------------- vendored trucs

def vendored_path(path: str) -> tuple[str, str, str] | None:
    """(truc name, path, project dir) when `path` is a vendored truc's
    `trucs/<name>/component.rvl`, else `None` (issue #1769).

    Decided the way `export_area.in_export_area` decides an export (issue
    #2550): on the path as named (absolute, `..` folded away) and as it
    resolves through symbolic links, its parts compared case-folded. On a
    case-folding filesystem `TRUCS/<name>/Component.rvl` IS the vendored
    file, and a link to one holds the publisher's bytes, so either spelling
    makes it vendored. The name and project dir come from the spelling that
    matched. Counting a file as vendored never raises a trust: a record it
    brings earns `publisher` only by the lock's measured signature over those
    exact records and the lock's pin of the exact bytes loaded."""
    parts = _vendored_parts(path)
    if parts is None:
        return None
    return (parts[-2], path, os.sep.join(parts[:-3]) or os.sep)


def _vendored_parts(path: str) -> list[str] | None:
    """The parts of the first spelling of `path` (as named, then as it
    resolves) that is a `trucs/<name>/component.rvl`, compared case-folded,
    or `None`."""
    fold = _export_area.fold
    for spelling in (os.path.normpath(os.path.abspath(path)),
                     _export_area.real(path)):
        parts = spelling.split(os.sep)
        if len(parts) >= 3 and fold(parts[-1]) == "component.rvl" \
                and fold(parts[-3]) == "trucs":
            return parts
    return None


def _vendored_dir(path: str) -> str:
    """The directory of the vendored component `path` reaches, from the
    spelling that matched: for a link, the component's own directory, never
    the link's, so the records read are the ones shipped beside it."""
    parts = _vendored_parts(path)
    return os.sep.join(parts[:-1]) if parts is not None else os.path.dirname(path)


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
        doc = registry.load_knowledge(_vendored_dir(path), regular_only=True)
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
    skipping a note whose trailer the text already carries and one whose id
    is not a record id."""
    from . import symbols  # noqa: PLC0415

    present = {_marked(b)[0] for b in _k.comment_blocks(text)} - {None}
    lines = text.split("\n")
    inserts: dict[int, list[str]] = {}
    for note in file_notes:
        # the store is not trusted to hold only record ids: a comment block is
        # drawn only for one (issue #2564)
        if (not is_record_id(note["id"]) or note["id"] in present
                or note["status"] in ("retired", "superseded")):
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


def _import_author(path: str, text: str, roots=()) -> dict:
    """Who a changed or unrecorded marked block in the loaded file `path`,
    whose loaded text is `text`, is attributed to (issue #2550): the writer
    of the file, never the operator by default.

    A file inside any `.revl/export/` directory is the author's by its path
    alone, the same rule that compiles it under the authoring profile
    (`export_area.in_export_area`, issues #2524 and #2547): `revl_export`
    writes there and nowhere else. Its manifest records the bytes an export
    wrote, not the trust of the session that wrote them, so a block there is
    an untrusted agent's, whatever trust the loading session has, and rides
    as an untrusted add does (its body withheld until it carries evidence).

    A vendored truc's `trucs/<name>/component.rvl` holds the publisher's bytes
    as `truc add` wrote them, so a block there is the publisher's, and
    untrusted. That is the floor `load_vendored` gives the publisher's own
    records: they earn `publisher` only from a signature measured over the
    records themselves, and no signature covers a comment block (nor does an
    export ever render one into a vendored component).

    A file anywhere else whose loaded text is the text of any copy an export
    ever wrote under one of the sanctioned `roots`, as its manifest and
    digest record keep them (`export_jail.exported_as`), is an adopted copy
    (issue #2577): the operator copied an export out of `.revl/export/`
    unchanged, line ends aside, which adopts its code, not the author's notes.
    A later export of the same copy does not end the match.
    A block there is the untrusted agent's, as in the export directory, with
    `adoptedFrom` naming the export copy it matches, and the load lists it
    (`adopted_blocks`). The operator who wants such a note as their own adds
    it as their own; no verb promotes it.

    Any other file holds bytes no MCP verb wrote, so its block is attributed
    to the operator who keeps that disk: a human note at operator trust, as
    before. That includes an adopted copy the operator then edited, by even
    one byte inside or outside a block, a byte-order mark or an added newline
    included: its text matches no recorded export (line ends aside). An
    unchanged block keeps its record and that record's trust, wherever the
    file lies."""
    if _export_area.in_export_area(path):
        return {"kind": "agent", "trust": "untrusted"}
    if vendored_path(path) is not None:
        return {"kind": "vendor", "trust": "untrusted"}
    from . import export_jail  # noqa: PLC0415 - cycle

    copied = export_jail.exported_as(text, roots)
    if copied is not None:
        return {"kind": "agent", "trust": "untrusted", "adoptedFrom": copied}
    return {"kind": "human", "trust": "operator"}


def adopted_blocks(created: list[dict]) -> list[dict]:
    """The records among `created` (what `import_marked` returned) read from
    an adopted export copy (issue #2577), as the load reports them: the new
    note, its symbol and file, the export it came from, and whether it
    changed a recorded note's body, naming that note as the one it
    `supersedes` or, for a note trusted further, the one it `changes`, or
    had no record. The body is not repeated: it rides by the note's trust."""
    out = []
    for record in created:
        source = (record.get("author") or {}).get("adoptedFrom")
        if source is None:
            continue
        changed = record.get("supersedes") or record.get("changes")
        entry = {"id": record["id"], "symbol": record["anchor"]["symbol"],
                 "file": record["anchor"]["path"], "adoptedFrom": source,
                 "block": "changed" if changed else "unrecorded"}
        for field in ("supersedes", "changes"):
            if record.get(field):
                entry[field] = record[field]
        out.append(entry)
    return out


def _mark_vendored(session, record: dict, path: str) -> dict:
    """`record`, marked `vendored` with the truc's name when `path` is a
    vendored component, as `load_vendored` marks the publisher's own records:
    a vendor's note never lands in the project's sidecar (`sidecar_writes`)."""
    found = vendored_path(path)
    if found is None:
        return record
    record = {**record, "vendored": found[0]}
    _records(session)[record["id"]] = record
    return record


def _chain(current: dict, note_id: str) -> list[dict]:
    """The note `note_id` names and each note its supersede chain leads to,
    in order, so the last is the one a block marked `note_id` now stands for
    (issue #2577). A block whose body is any of theirs is unchanged: the
    original's, as an old copy still carries it, or the supersede an import
    of that block already recorded, so reloading the same copy records
    nothing more."""
    chain, seen = [], set()
    note = current.get(note_id)
    while note is not None and note["id"] not in seen:
        chain.append(note)
        seen.add(note["id"])
        note = current.get(note.get("supersededBy"))
    return chain


def _beside(current: dict, chain: list[dict]) -> list[dict]:
    """The notes recorded beside one of `chain`'s in place of a supersede
    (`changes`, issue #2577). A block whose body is one of theirs was read
    back already, so reloading the same copy records nothing more."""
    ids = {note["id"] for note in chain}
    return [note for note in current.values() if note.get("changes") in ids]


def _outranks(owner, writer) -> bool:
    """Whether a note by `owner` is trusted further than `writer`, the author
    of a block read back over it (issue #2577), in the order `_check_rank`
    refuses a supersede by (`TRUST_RANK`; an unlabelled owner ranks as an
    operator, an unlabelled writer lowest): an untrusted author's block
    never supersedes an operator's or a publisher's note, through an export
    copy or an adopted one. Decided before any supersede is made, so the
    block is kept beside the note rather than lost to a refused supersede."""
    return (TRUST_RANK.get(_trust_of(owner), TRUST_RANK["operator"])
            > TRUST_RANK.get(_trust_of(writer), 0))


def _import_block(session, vs: dict, existing: dict | None, body: str,
                  symbol: str, author: dict) -> dict:
    """Record a block's `body`, read back over `existing` (the last note on
    its chain, or None for a block with no record), by `author`: a supersede
    of `existing`, unless its author is trusted further (`_outranks`). Then
    the block is a note beside it naming it in `changes` (issue #2577): the
    original stays live, and the author's text rides as the author's trust
    allows. Raises `NoteError` as `add` does."""
    kind = existing["kind"] if existing is not None else "doc"
    beside = existing is not None and _outranks(existing.get("author"), author)
    record = add(session, vs, {"kind": kind, "body": body}, symbol,
                 author=dict(author),
                 supersedes=existing["id"] if existing and not beside else None)
    if beside:
        record = {**record, "changes": existing["id"]}
        _records(session)[record["id"]] = record
    return record


def import_marked(session, vs: dict, roots=()) -> list[dict]:
    """Read the marked blocks of the loaded files back. A body that differs
    from every note on its id's supersede chain (`_chain`), and from every
    note beside one (`_beside`), is recorded over the last of them
    (`_import_block`), and a block with no record becomes a note, authored
    by whoever wrote the file (`_import_author`, decided once per file); an
    adopted export copy's records are the ones `adopted_blocks` lists.
    `roots` are the sanctioned roots whose export records name an adopted
    copy (the server passes its own); with none, no file counts as one."""
    current = notes(session)
    created = []
    for name, text in ((p, (vs.get("files_content") or {}).get(p))
                       for p in vs.get("files") or []):
        if text is None:
            continue
        parsed = None  # (program, decls, nodes), once a marked block needs it
        author = None  # who wrote this file, once a marked block needs it
        for block in _k.comment_blocks(text):
            note_id, body = _marked(block)
            if note_id is None:
                continue
            chain = _chain(current, note_id)
            if any(note["body"] == body
                   for note in chain + _beside(current, chain)):
                continue
            if parsed is None:
                parsed = _parsed(text, name)
            program, decls, nodes = parsed
            symbol = _k.anchor_of(text, name, block, decls, nodes, program)[0]
            if symbol is None:
                continue
            if author is None:
                author = _import_author(name, text, roots)
            try:
                record = _import_block(session, vs, chain[-1] if chain else None,
                                       body, symbol, author)
            except NoteError:
                continue
            created.append(_mark_vendored(session, record, name))
    return created


def _parsed(text: str, name: str) -> tuple:
    """(program, top-level declarations, anchor cache) of one file: parsed
    once and shared by every marked block in it (issue #2250)."""
    from .. import nested  # noqa: PLC0415
    from . import symbols  # noqa: PLC0415

    program = nested.parse(text, name)
    decls = symbols.declarations(text, name, program) if program is not None else []
    return program, decls, {}


__all__ = ["KINDS", "EVIDENCE_KINDS", "query_verbs", "RERUNNABLE", "run_evidence",
           "NoteError", "notes", "add", "op_record",
           "refresh", "served", "concerning", "load_sidecar", "sidecar_writes",
           "sidecar_dirs",
           "RECORD_ID", "is_record_id", "record_path", "is_record_path",
           "load_vendored", "vendored_trucs", "vendored_path",
           "render", "import_marked", "adopted_blocks"]
