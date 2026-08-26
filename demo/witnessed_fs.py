#!/usr/bin/env python
"""H1 demo — witnessed filesystem operations (roadmap item 244).

Runs the REAL stdlib/fs.rvl `@py` bodies on the live cordis-py tier and shows
the H1 story end to end:

  1. an agent activation writes a file and removes another inside a workspace;
  2. on a CLEAN commit the mutations persist (they ARE the deliverable) — no
     prompt, because they are revertible by construction;
  3. on an ABORT the same activation reverts residue-free — the write's
     preimage is restored, the removed file is put back, no snapshot or garbage
     is left behind;
  4. a write whose target escapes the session workspace root is REFUSED;
  5. the WAL descriptors enumerate exactly the witnessed fs crossings — the
     residue is exactly enumerable.

The component's witnessed call sites are built as the IR lower.py emits (the
cross-module `use "stdlib/fs.rvl"` parse surface is a later frontend slice; see
docs/witnessed-fs.md). Everything else — the fs bodies, the confinement guard,
the transactional teardown — is exercised for real.

Run:  backends/python/.venv/bin/python demo/witnessed_fs.py
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl.compiler import compile_source  # noqa: E402
import replay  # noqa: E402
import revl_fs_workspace as ws  # noqa: E402
from revl.recovery import recover  # noqa: E402

_BASE = compile_source((ROOT / "stdlib" / "fs.rvl").read_text(encoding="utf-8"), "fs.rvl")


def _lit(v):
    return {"kind": "lit", "value": v}


def _effect(name, *args):
    return {"step": "effect",
            "acquire": {"kind": "fn", "name": name, "args": [_lit(a) for a in args]}}


def _ir(name, body, abort=False):
    steps = list(body)
    if abort:
        steps.append({"step": "fail", "message": _lit("boom")})
    ir = copy.deepcopy(_BASE)
    ir["components"] = [{"name": name, "source": "fs.rvl", "config": [],
                         "requires": {}, "provides": {}, "body": steps}]
    return ir


def _session():
    from revl.mcp.session import Session
    return Session()


def _new_workspace(stack):
    root = Path(tempfile.mkdtemp(prefix="revl-fs-demo-"))
    stack.append(root)
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    (root / "stale.txt").write_text("junk", encoding="utf-8")
    os.environ[ws.WORKSPACE_ENV] = str(root)
    return root


AGENT_BODY = [_effect("write", "artifact.txt", "v2"), _effect("rm", "stale.txt")]


def demo_commit(stack):
    root = _new_workspace(stack)
    session = _session()
    session.load(_ir("Agent", AGENT_BODY))
    session.unload()  # clean unload == implicit commit
    print("1. COMMIT: the mutation is the deliverable, no prompt")
    print(f"     artifact.txt = {(root / 'artifact.txt').read_text()!r} (was 'v1')")
    print(f"     stale.txt exists: {(root / 'stale.txt').exists()} (removed, stays removed)")


def demo_abort(stack):
    root = _new_workspace(stack)
    session = _session()
    report = session.load(_ir("Agent", AGENT_BODY, abort=True))
    state = report["components"][0]["state"]
    garbage = root / ws.GARBAGE_DIRNAME
    preimage = root / ws.PREIMAGE_DIRNAME
    residue = [str(p) for d in (garbage, preimage) if d.exists() for p in d.iterdir()]
    print(f"2. ABORT ({state}): reverts residue-free")
    print(f"     artifact.txt = {(root / 'artifact.txt').read_text()!r} (restored)")
    print(f"     stale.txt = {(root / 'stale.txt').read_text()!r} (un-removed)")
    print(f"     leftover snapshot/garbage residue: {residue or 'none'}")


def demo_confinement(stack):
    root = _new_workspace(stack)
    escapee = root.parent / f"{root.name}-escape.txt"
    session = _session()
    session.load(_ir("Rogue", [_effect("write", f"../{escapee.name}", "pwned")]))
    driver = session._driver
    ((_n, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    print("3. CONFINEMENT: a write outside the workspace root is refused")
    print(f"     escape file created: {escapee.exists()} (refused)")
    print(f"     inverses registered: {len(frame._transactional)} (Ok-conditional: none)")
    session.unload()


def demo_enumerate(stack):
    root = _new_workspace(stack)
    wal = str(root / "session.wal")
    real = replay.Recorder.instrument

    def _open_then(self, *a, **k):
        self.open_wal(wal, generation=1)
        return real(self, *a, **k)

    replay.Recorder.instrument = _open_then
    try:
        session = _session()
        session.load(_ir("Agent", AGENT_BODY), record=True)
        session.unload()
    finally:
        replay.Recorder.instrument = real

    descriptors = [r for r in replay.WriteAheadLog.read(wal)["records"]
                   if r.get("record") == "discharge-descriptor"]
    print("4. ENUMERATE: the residue surface names every witnessed fs crossing")
    for d in descriptors:
        w = d["witness"]
        print(f"     seq {d['seq']}: {d['call']['method']}  witness={w}")
    rep = recover(wal)
    print(f"     recover verdict={rep['verdict']!r}  committed-skipped="
          f"{[s['seq'] for s in rep['dischargedSkipped']]}  clean={rep['residue']['clean']}")


def main():
    stack = []
    try:
        demo_commit(stack)
        print()
        demo_abort(stack)
        print()
        demo_confinement(stack)
        print()
        demo_enumerate(stack)
    finally:
        import shutil
        for d in stack:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
