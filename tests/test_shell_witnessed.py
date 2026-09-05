"""Shell-to-witnessed lowering, end to end (roadmap item 252).

The pure classifier (`revl_shell_classify`, proven exhaustively in
`tests/test_shell_classify.py`) decides which commands lower onto the witnessed
catalog. This suite proves the OTHER half — that a lowered plan actually
executes through the REAL `stdlib/fs.rvl` witnessed path (item 244) with the real
inverse, and that an `emission`-verdict command genuinely stays one opaque
`emission` — closing the loop the classifier opens:

  * classify("mv a b") names `fs.move`; the SAME `effect move(...)` call the plan
    names, driven live on cordis through the imported catalog, PERSISTS on commit
    and REVERTS residue-free on abort (the witnessed guarantee item 252 buys);
  * classify("rm x") names `fs.rm` (the garbage-dir park) — same commit/abort
    proof;
  * `stdlib/shell.rvl`'s `sh` extern is a plain `emission` (no witness, no
    inverse): the unrecognized tail stays one honest prompt, registering NOTHING
    transactional;
  * the plan the classifier emits and the catalog op the consumer imports agree
    on the witnessed extern NAME — a drift-catching cross-check so the classifier
    can never name an op the catalog does not carry.

The live cordis parts mirror `tests/test_witnessed_import_callsite.py`; they run
the actual `stdlib/fs.rvl` bodies, not a stand-in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_files

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402
import revl_shell_classify as sc  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the witnessed teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# A consumer path inside the repo root so `use \"stdlib/fs.rvl\"` /
# `use \"stdlib/shell.rvl\"` resolve to the real landed modules on disk.
_CONSUMER = str(_ROOT / "test_shell_witnessed_probe.rvl")


def _compile(source: str) -> dict:
    return compile_files([_CONSUMER], sources={_CONSUMER: source})


# ---------------------------------------------------------------------------
# the classifier plan and the catalog agree on the witnessed extern name
# ---------------------------------------------------------------------------

# The lowering table, as the classifier emits it — the single source the
# integration below and the catalog cross-check both read.
LOWERINGS = [
    ("mv a b", "move", "unmove"),
    ("rm x", "rm", "unrm"),
    ("mkdir d", "mkdir", "rmdir_if_empty"),
    ("cp a b", "write", "restore"),
    ("touch f", "write", "restore"),
]


@pytest.mark.parametrize("cmd,witnessed,inverse", LOWERINGS)
def test_plan_names_the_catalog_op_and_inverse(cmd, witnessed, inverse):
    plan = sc.classify(cmd)
    assert plan["verdict"] == "witnessed", plan
    op = plan["ops"][0]
    assert op["witnessed"] == witnessed
    assert op["inverse"] == inverse


def test_every_named_witnessed_op_exists_in_the_fs_catalog():
    # a drift guard: the classifier must never name an op the catalog does not
    # export. Parse the real stdlib/fs.rvl and confirm each named extern is there.
    from revl.compiler import compile_files

    # item 410 stage 5: fs.rvl's `@ts` externs are `= @ts ref` imports now, so it
    # must be compiled from its real path (a bare-string compile has no root tree
    # to jail the ref against).
    fs_ir = compile_files([str(_ROOT / "stdlib" / "fs.rvl")])
    fs_names = {e["name"] for e in fs_ir.get("externs", [])}
    for _cmd, witnessed, inverse in LOWERINGS:
        assert witnessed in fs_names, f"{witnessed} missing from fs.rvl"
        assert inverse in fs_names, f"{inverse} missing from fs.rvl"


# ---------------------------------------------------------------------------
# the emission fallback is a plain emission — no witness, no inverse
# ---------------------------------------------------------------------------

def test_shell_module_sh_is_a_plain_emission():
    # shell.rvl's `sh` @py body imports a backend module (revl_shell_host), which
    # the #302 confinement refuses for a context-free / user-origin compile. It is
    # first-party stdlib, so compile it through the stdlib-aware path (its real
    # path under stdlib_root()) where the install-origin exemption applies.
    ir = compile_files([str(_ROOT / "stdlib" / "shell.rvl")])
    sh = next(e for e in ir["externs"] if e["name"] == "sh")
    assert sh["class"] == "emission"
    # a plain emission carries no declared `undo` — nothing transactional.
    assert not sh.get("undo")


def test_shell_classify_is_pure():
    # compile via the stdlib-aware path so the #302 backend-import exemption for
    # first-party stdlib applies (see test_shell_module_sh_is_a_plain_emission).
    ir = compile_files([str(_ROOT / "stdlib" / "shell.rvl")])
    cl = next(e for e in ir["externs"] if e["name"] == "classify")
    assert cl["class"] == "pure"


# ---------------------------------------------------------------------------
# LIVE on cordis: the lowered plan executes through the real fs catalog
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
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "doomed.txt").write_text("junk", encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    return root


# The consumer lowers `mv a.txt b.txt` and `rm doomed.txt` — the exact ops the
# classifier's plan for those commands names — through the imported fs catalog.
_MV_RM_SRC = (
    'use "stdlib/fs.rvl" { move, rm }\n'
    "component Shell {\n"
    '  effect move("a.txt", "b.txt")\n'
    '  effect rm("doomed.txt")\n'
    "}\n"
)
_MV_RM_ABORT_SRC = (
    'use "stdlib/fs.rvl" { move, rm }\n'
    "component Shell {\n"
    '  effect move("a.txt", "b.txt")\n'
    '  effect rm("doomed.txt")\n'
    '  fail "boom"\n'
    "}\n"
)


@needs_cordis
def test_lowered_shell_ops_persist_on_commit(workspace):
    # precondition: the classifier really does lower these two commands.
    assert sc.classify("mv a.txt b.txt")["ops"][0]["witnessed"] == "move"
    assert sc.classify("rm doomed.txt")["ops"][0]["witnessed"] == "rm"

    a, b = workspace / "a.txt", workspace / "b.txt"
    doomed = workspace / "doomed.txt"
    session = _session()
    session.load(_compile(_MV_RM_SRC))

    assert not a.exists() and b.read_text() == "hello"
    assert not doomed.exists()
    frame = _sole_frame(session)
    assert len(frame._transactional) == 2

    session.unload()  # clean unload == commit

    assert b.read_text() == "hello", "commit wrongly reverted the lowered mv"
    assert not doomed.exists(), "commit wrongly restored the lowered rm"
    for entry in frame._transactional:
        assert entry.discharged is True and entry.replayed is False


@needs_cordis
def test_lowered_shell_ops_revert_on_abort(workspace):
    a, b = workspace / "a.txt", workspace / "b.txt"
    doomed = workspace / "doomed.txt"
    session = _session()

    report = session.load(_compile(_MV_RM_ABORT_SRC))
    assert report["components"] == [{"name": "Shell", "state": "FAILED"}]

    # the witnessed inverses ran: mv undone (b gone, a back), rm undone (doomed
    # back), residue-free.
    assert a.read_text() == "hello", "abort did not reverse the lowered mv"
    assert not b.exists(), "abort left the moved file behind"
    assert doomed.read_text() == "junk", "abort did not un-remove the lowered rm"
    garbage = workspace / ws.GARBAGE_DIRNAME
    if garbage.exists():
        assert not any(garbage.iterdir()), "rm garbage residue left after abort"


@needs_cordis
def test_emission_fallback_registers_nothing_transactional(workspace):
    # `sh` is a plain emission: an unrecognized command crosses once and leaves
    # no transactional teardown entry (nothing to revert — irreversible by
    # design). We `emit` a harmless classify-refused command.
    assert sc.classify("echo hi | cat")["verdict"] == "emission"
    src = (
        'use "stdlib/shell.rvl" { sh }\n'
        "component Term {\n"
        '  emit sh("true")\n'
        "}\n"
    )
    session = _session()
    session.load(_compile(src))
    frame = _sole_frame(session)
    assert len(frame._transactional) == 0, "an emission wrongly registered a teardown"
    session.unload()
