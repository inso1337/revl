"""Generate the ts witnessed-fs proof fixture (roadmap item 369).

One IR document carrying the REAL `stdlib/fs.rvl` externs (compiled from the
actual file text, so the witnessed `write`/`rm`/`move`/`mkdir` + their declared
inverses are present with the classification and lowered `undo` the runtime seam
keys off) plus two activation components exercising the catalog:

  * `FsCommit` — a clean activation: write (overwrite), write (create), rm,
    mkdir, move. Unloaded cleanly it is an implicit commit — every mutation
    PERSISTS, every transactional inverse discharged.
  * `FsAbort`  — the same sequence, then a `fail` step so the activation never
    commits — every inverse replays and the world reverts, residue-free.

This is the ts peer of the py proof in tests/test_fs_stdlib.py's
`test_h1_persists_on_clean_commit` / `test_h1_reverts_residue_free_on_abort`,
built with the same hand-assembled IR shape (mirrors
_gen_witnessed_teardown.py). Regenerate with:

    python3 backends/typescript/tests/fixtures/_gen_ts_witnessed_fs.py
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

# item 410 stage 5: `stdlib/fs.rvl`'s `@ts` externs are now `= @ts ref` imports
# of the per-extern entry points in `backends/typescript/revl_fs_ts.ts`, so the
# fixture must be compiled through `compile_files` with the module's REAL path
# (a bare-string `compile_source` has no root tree to jail the ref against). The
# file sits inside `stdlib_root()`, so it classifies stdlib-origin and its refs
# jail to the install tree and stamp `"root": "stdlib"` — exactly what the
# runner (and the vitest harness) resolves against `__REVL_STDLIB_REF_ROOT__`.
_BASE = compile_files([str(ROOT / "stdlib" / "fs.rvl")])


def _lit(v: str) -> dict:
    return {"kind": "lit", "value": v}


def _fn(name: str, *args: str) -> dict:
    return {"kind": "fn", "name": name, "args": [_lit(a) for a in args]}


def _effect(name: str, *args: str) -> dict:
    return {"step": "effect", "acquire": _fn(name, *args)}


# The witnessed sequence, identical for both components — one overwrite, one
# create, one remove, one mkdir, one move. The exact ops the shell classifier
# lowers `cp`/`touch`/`rm`/`mkdir`/`mv` onto (stdlib/shell.rvl item 252), so this
# also proves those lowered ops execute AND revert on ts.
_SEQUENCE = [
    _effect("write", "artifact.txt", "v2"),   # overwrite existing v1 (preimage)
    _effect("write", "fresh.txt", "new"),      # create (inverse deletes)
    _effect("rm", "stale.txt"),                # garbage-dir park
    _effect("mkdir", "newdir"),                # empty-dir create
    _effect("move", "movable.txt", "moved.txt"),  # rename
]


def _component(name: str, abort: bool) -> dict:
    body = list(_SEQUENCE)
    if abort:
        body.append({"step": "fail", "message": _lit("boom")})
    return {"name": name, "source": "fs.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


# Multi-op-on-the-SAME-path, to exercise the LIFO replay ordering an abort MUST
# honour (item 369's py bug: replaying inverses in REGISTRATION order instead of
# LIFO corrupts/loses data). Two independent same-path stacks:
#   A.txt: exists "orig" -> overwrite "v1" -> overwrite "v2".  Correct LIFO abort
#          restores v2->v1->orig == "orig"; registration order would leave "v1".
#   B.txt: absent -> create "b1" -> overwrite "b2".  Correct LIFO abort restores
#          the overwrite (b2->b1) THEN deletes the created file == absent;
#          registration order would delete first then restore, leaving "b1".
_SAME_PATH = [
    _effect("write", "A.txt", "v1"),
    _effect("write", "A.txt", "v2"),
    _effect("write", "B.txt", "b1"),
    _effect("write", "B.txt", "b2"),
]


def _samepath_component() -> dict:
    body = list(_SAME_PATH)
    body.append({"step": "fail", "message": _lit("boom")})
    return {"name": "FsAbortSamePath", "source": "fs.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


def build() -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [
        _component("FsCommit", abort=False),
        _component("FsAbort", abort=True),
        _samepath_component(),
    ]
    return ir


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "ts_witnessed_fs.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
