"""Cross-module witnessed-extern import — roadmap item 315 (the H1
user-facing surface gap found by 244).

`src/revl/parser.py` used to gate a missing-`undo` witnessed effect call on a
per-FILE `_witnessed_names` set, populated only from same-file `extern`
decls. `use "stdlib/fs.rvl" { write }` + `effect write(...)` therefore could
never parse: `write`'s witnessed classification lives in another file, and
`use`/module resolution runs in compiler.py's `_ModuleLoader`, a pass AFTER
parsing (one file at a time) — the parser had no way to know.

The fix: a bare-name call missing `undo` is admitted by the parser
unconditionally (the same-file fast path and the deferred-to-import path
collapse to one), and the real gate moves to lower.py's `_lower_effect_step`,
which runs on the MERGED, post-import program and so sees local AND imported
witnessed externs alike (`env.witnessed_externs`, built by
`_witnessed_extern_names` over the merged `Program`).

This suite is the full user-facing H1 proof item 244 could not write: REAL
`.rvl` source with `use "stdlib/fs.rvl" { write, rm }` and `effect
write(...)` / `effect rm(...)` in a component activation body — parsed,
lowered, and (with a cordis venv) run live: persists on commit, reverts on
abort, exactly what tests/test_witnessed_lower_callsite.py proved for the
same-file case and tests/test_fs_stdlib.py proved from hand-built IR. Plus
the negative case: importing a PLAIN (non-witnessed) effect extern and
calling it with no `undo` is still refused — the gate is deferred, not
weakened.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_files
from revl.errors import RevlError

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402  (the confinement helper under test)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the transactional teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# A virtual path *inside the repo root*, so `use "stdlib/fs.rvl"` resolves
# (relative to the importing file's directory, compiler.py's `_ModuleLoader`)
# straight to the real, landed `stdlib/fs.rvl` on disk — no fixture stands in
# for it, and this file is untouched (roadmap 315 scope).
_CONSUMER_PATH = str(_ROOT / "test_witnessed_import_probe.rvl")


def _compile(source: str) -> dict:
    return compile_files([_CONSUMER_PATH], sources={_CONSUMER_PATH: source})


# ---------------------------------------------------------------------------
# the parse+lower surface: real `use` + real `effect write(...)` now parses
# ---------------------------------------------------------------------------

_IMPORT_SRC = (
    'use "stdlib/fs.rvl" { write, rm }\n'
    "component Agent {\n"
    '  effect write("artifact.txt", "v2")\n'
    '  effect rm("stale.txt")\n'
    "}\n"
)

_IMPORT_ABORT_SRC = (
    'use "stdlib/fs.rvl" { write, rm }\n'
    "component Agent {\n"
    '  effect write("artifact.txt", "v2")\n'
    '  effect rm("stale.txt")\n'
    '  fail "boom"\n'
    "}\n"
)


def test_imported_witnessed_effect_call_parses_and_lowers():
    # this used to be a PARSE-time refusal ("effect has no `undo` and `write`
    # is not pure") because `write`'s witnessed classification lives in
    # stdlib/fs.rvl, not the importing file — item 315's exact gap.
    ir = _compile(_IMPORT_SRC)
    agent = next(c for c in ir["components"] if c["name"] == "Agent")
    assert agent["body"] == [
        {"step": "effect", "acquire": {"kind": "fn", "name": "write",
         "args": [{"kind": "lit", "value": "artifact.txt"},
                  {"kind": "lit", "value": "v2"}]}},
        {"step": "effect", "acquire": {"kind": "fn", "name": "rm",
         "args": [{"kind": "lit", "value": "stale.txt"}]}},
    ]


def test_imported_witnessed_call_with_site_undo_still_refused():
    # the accumulator owns the inverse for a witnessed call whether it is
    # local or imported — a site-spelled `undo` is refused either way
    # (lower.py's `_lower_effect_step`, unaffected by this slice).
    src = (
        'use "stdlib/fs.rvl" { write, restore }\n'
        "component Bad {\n"
        '  effect write("artifact.txt", "v2") undo restore(result)\n'
        "}\n"
    )
    with pytest.raises(RevlError) as ei:
        _compile(src)
    assert "cannot declare a site `undo`" in str(ei.value)


# ---------------------------------------------------------------------------
# negative test: importing a PLAIN (non-witnessed) effect extern still
# requires its `undo` — the parser's deferral is not a global weakening of
# G4, only a postponement until imports are known.
# ---------------------------------------------------------------------------

_PLAIN_MOD_PATH = str(_ROOT / "test_witnessed_import_plainmod.rvl")
_PLAIN_MOD_SRC = (
    "pub extern acquire fn open_conn() -> Int undo close_conn(result) = @ts { return 1 }\n"
    "pub extern pure fn close_conn(n: Int) -> Unit = @ts { return }\n"
)


def _compile_with_plainmod(consumer_src: str) -> dict:
    return compile_files(
        [_CONSUMER_PATH],
        sources={_CONSUMER_PATH: consumer_src, _PLAIN_MOD_PATH: _PLAIN_MOD_SRC},
    )


def test_plain_imported_extern_missing_undo_is_still_refused():
    src = (
        'use "test_witnessed_import_plainmod.rvl" { open_conn }\n'
        "component C {\n"
        "  effect open_conn()\n"
        "}\n"
    )
    with pytest.raises(RevlError) as ei:
        _compile_with_plainmod(src)
    assert "effect has no `undo` and `open_conn` is not pure" in str(ei.value)


def test_plain_imported_extern_with_site_undo_compiles():
    # the accepting twin: the same imported plain extern, called with its
    # site-spelled `undo`, compiles exactly as an ordinary acquisition always
    # has — proving the refusal above is about the missing `undo`, not the
    # import.
    src = (
        'use "test_witnessed_import_plainmod.rvl" { open_conn, close_conn }\n'
        "component C {\n"
        "  let h = effect open_conn() undo close_conn(h)\n"
        "}\n"
    )
    ir = _compile_with_plainmod(src)
    comp = next(c for c in ir["components"] if c["name"] == "C")
    assert comp["body"] == [
        {"step": "let-effect", "bind": "h",
         "acquire": {"kind": "fn", "name": "open_conn", "args": []},
         "undo": {"kind": "fn", "name": "close_conn", "args": [{"kind": "name", "id": "h"}]}},
    ]


# ---------------------------------------------------------------------------
# H1, LIVE on cordis, driven from the full user-facing source surface: a
# consumer that IMPORTS the witnessed fs ops (not a same-file extern, not
# hand-built IR) — persists on commit, reverts on abort, residue-free.
# ---------------------------------------------------------------------------

def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "artifact.txt").write_text("v1", encoding="utf-8")
    (root / "stale.txt").write_text("junk", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


@needs_cordis
def test_h1_persists_on_clean_commit_from_imported_source(workspace):
    art = workspace / "artifact.txt"
    stale = workspace / "stale.txt"
    session = _session()
    session.load(_compile(_IMPORT_SRC))

    assert art.read_text() == "v2"
    assert not stale.exists()
    frame = _sole_frame(session)
    assert len(frame._transactional) == 2

    session.unload()  # clean unload == implicit commit

    assert art.read_text() == "v2", "commit wrongly reverted the imported write"
    assert not stale.exists(), "commit wrongly restored the imported rm"
    for entry in frame._transactional:
        assert entry.discharged is True and entry.replayed is False
        assert entry.witness is None


@needs_cordis
def test_h1_reverts_residue_free_on_abort_from_imported_source(workspace):
    art = workspace / "artifact.txt"
    stale = workspace / "stale.txt"
    session = _session()

    report = session.load(_compile(_IMPORT_ABORT_SRC))
    assert report["components"] == [{"name": "Agent", "state": "FAILED"}]

    assert art.read_text() == "v1", "abort did not restore the imported write's preimage"
    assert stale.read_text() == "junk", "abort did not un-remove the imported rm"
    garbage = workspace / ws.GARBAGE_DIRNAME
    preimage = workspace / ws.PREIMAGE_DIRNAME
    assert not any(garbage.iterdir()) if garbage.exists() else True
    assert not any(preimage.iterdir()) if preimage.exists() else True
