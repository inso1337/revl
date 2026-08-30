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

from revl.compiler import compile_source  # noqa: E402

_FS_SRC = (ROOT / "stdlib" / "fs.rvl").read_text(encoding="utf-8")
_BASE = compile_source(_FS_SRC, "fs.rvl")


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


def build() -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [
        _component("FsCommit", abort=False),
        _component("FsAbort", abort=True),
    ]
    return ir


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "ts_witnessed_fs.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
