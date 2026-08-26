"""Witnessed-inverse externs — roadmap item 243, SLICE 2a (py runtime seam).

Design: docs/design/243-witnessed-externs.md.

The one thing to get right, proven at RUNTIME here: a `witnessed` effect is a
TRANSACTION, not an `acquire` bracket. Its declared inverse replays on ABORT
ONLY; on a clean successful unload it is DISCHARGED (skipped, witness GC'd) and
the mutation — the deliverable — PERSISTS. A bracket, by contrast, replays on a
clean unload too. These tests drive real cordis-py compositions over a real
temp file and assert the observable difference three ways:

  * witnessed + clean unload   -> inverse does NOT replay, mutation persists
  * witnessed + mid-body abort -> inverse DOES replay, mutation reverted (A8),
                                  residue-free
  * acquire   + clean unload   -> inverse replays (bracket unchanged)

The witnessed call site is hand-built at the IR level: Slice 1 landed the
`witnessed` extern + its transactional IR descriptor, and the effect-position
call-site *surface* is a later slice (it needs the lowerer, out of Slice 2a's
scope). Slice 2a is the backend that consumes the IR the future lowerer will
emit, so the test crafts that IR directly — a standard `effect`/`let-effect`
step whose acquisition calls a witnessed extern, which is exactly the shape
emit.py keys the transactional registration off.

The toy witnessed extern renames a file (its `Ok` witness carries the paths) and
the inverse renames it back — a stand-in for item 244's real fs bodies, enough
to exercise the runtime path. The target path is passed through an env var
rather than component config so the harness stays a pure IR fixture (config
resolution is a separate cordis path, not what these tests exercise).
"""

import copy
import importlib.util
import os

import pytest

from revl.compiler import compile_source

# ---------------------------------------------------------------------------
# live runtime gate (mirrors tests/test_apply.py / test_crash_recovery.py)
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the transactional teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

_TARGET_ENV = "REVL_WIT_TARGET"


# ---------------------------------------------------------------------------
# the toy witnessed effect: a file rename with a data witness + named inverse
# ---------------------------------------------------------------------------

_EXTERNS = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    # the inverse: a named, WAL-reconstructible restore over the data witness.
    # Idempotent (rule 5) — a second replay is a no-op once the file is back.
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    # the witnessed mutation: rename path -> path.bak, returning the paths as
    # its data witness. Ok-conditional at the call site: an Err would register
    # nothing; the toy path always succeeds under the test's setup.
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return Ok({'path': path, 'bak': bak})\n"
    "}\n"
    # the BRACKET contrast: same mutation, but an `acquire` extern whose undo
    # replays on EVERY teardown. Emitted as a plain bracket, not transactional.
    "extern acquire fn stash_acq() -> Stash undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return {'path': path, 'bak': bak}\n"
    "}\n"
)

_BASE = compile_source(_EXTERNS, "witnessed.rvl")


def _witnessed_component(name: str, abort: bool) -> dict:
    """A component whose activation performs the witnessed effect, optionally
    followed by a `fail` step so activation aborts mid-body."""
    body = [{"step": "effect",
             "acquire": {"kind": "fn", "name": "stash", "args": []}}]
    if abort:
        body.append({"step": "fail", "message": {"kind": "lit", "value": "boom"}})
    return {"name": name, "source": "witnessed.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


def _acquire_component(name: str) -> dict:
    """A component whose activation performs the SAME mutation through an
    `acquire` bracket: a site-spelled undo that replays on clean unload too."""
    body = [{
        "step": "let-effect", "bind": "h",
        "acquire": {"kind": "fn", "name": "stash_acq", "args": []},
        "undo": {"kind": "fn", "name": "unstash", "args": [{"kind": "name", "id": "h"}]},
    }]
    return {"name": name, "source": "witnessed.rvl", "config": [],
            "requires": {}, "provides": {}, "body": body}


def _ir(component: dict) -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [component]
    return ir


@pytest.fixture
def target(tmp_path, monkeypatch):
    path = tmp_path / "artifact.txt"
    path.write_text("the deliverable", encoding="utf-8")
    monkeypatch.setenv(_TARGET_ENV, str(path))
    return str(path)


def _session():
    from revl.mcp.session import Session
    return Session()


def _sole_frame(session):
    """The single activation frame of the loaded composition, for introspection
    of its transactional entries."""
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


# ---------------------------------------------------------------------------
# 1. witnessed + clean unload: the mutation PERSISTS, the inverse is discharged
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_persists_on_clean_unload(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir(_witnessed_component("StashOk", abort=False)))

    # activation ran the mutation: original gone, backup present
    assert not os.path.exists(target)
    assert os.path.exists(bak)

    # the frame registered exactly one transactional entry, not yet resolved
    frame = _sole_frame(session)
    assert len(frame._transactional) == 1
    entry = frame._transactional[0]
    assert entry.discharged is False and entry.replayed is False

    session.unload()  # clean successful unload == implicit commit

    # the inverse did NOT replay: the mutation is the deliverable and persists
    assert not os.path.exists(target), "clean unload wrongly restored the file"
    assert os.path.exists(bak)
    # discharged, and the witness GC'd (dropped) — no live rollback state
    assert entry.discharged is True
    assert entry.replayed is False
    assert entry.witness is None
    assert entry._undo is None


# ---------------------------------------------------------------------------
# 2. witnessed + mid-activation abort: the inverse REPLAYS, mutation reverted
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_reverts_on_abort(target):
    bak = target + ".bak"
    session = _session()

    # the same witnessed effect, but a later step fails -> activation never
    # commits, so its transactional inverse replays on the unwind (A8). cordis
    # surfaces the mid-activation failure by landing the fiber FAILED (the
    # revert has already run by the time the state report comes back).
    report = session.load(_ir(_witnessed_component("StashAbort", abort=True)))
    assert report["components"] == [{"name": "StashAbort", "state": "FAILED"}]

    assert os.path.exists(target), "abort did not replay the inverse — mutation stuck"
    assert open(target).read() == "the deliverable"
    # residue-free: the backup the mutation created was cleaned up by the revert
    assert not os.path.exists(bak), "abort left rollback residue (A8 violation)"


# ---------------------------------------------------------------------------
# 3. acquire + clean unload: the bracket STILL reverts (unchanged)
# ---------------------------------------------------------------------------

@needs_cordis
def test_acquire_bracket_reverts_on_clean_unload(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir(_acquire_component("Acq")))

    assert not os.path.exists(target)  # acquired: mutation applied
    assert os.path.exists(bak)

    session.unload()  # clean unload

    # the bracket inverse replays on a clean unload — the file is restored.
    # This is exactly what a witnessed effect must NOT do (test 1), proving the
    # two entry kinds are distinct at runtime.
    assert os.path.exists(target), "acquire bracket failed to revert on clean unload"
    assert not os.path.exists(bak)
    assert open(target).read() == "the deliverable"


# ---------------------------------------------------------------------------
# emit seam (no cordis needed): the witnessed call site compiles to a
# transactional registration, the bracket compiles to a plain disposer.
# ---------------------------------------------------------------------------

def _emit_backend():
    spec = importlib.util.spec_from_file_location(
        "py_emit_witnessed", "backends/python/emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_witnessed_call_site_emits_ok_conditional_transactional():
    emit = _emit_backend()
    body = emit.emit(_ir(_witnessed_component("StashOk", abort=False)))
    # the mutation runs, then the DECLARED inverse (unstash) registers as a
    # transactional entry — only on the Ok branch, carrying the Ok payload.
    assert "isinstance(_revl_wit1, Ok)" in body
    assert "_revl_frame.transactional((lambda result: unstash(result)), _revl_wit1.value)" in body
    # it is NOT a bracket: no `yield lambda:` disposer for the witnessed step
    # (the only yields are the transactional one and the frame drain).
    assert "yield lambda:" not in body
    # Ok/Err are present because the witnessed extern returns Result
    assert "class Ok:" in body


def test_bracket_still_emits_a_plain_disposer():
    emit = _emit_backend()
    body = emit.emit(_ir(_acquire_component("Acq")))
    # the acquire bracket keeps its site-spelled, always-replaying disposer
    assert "h = stash_acq()" in body
    assert "yield lambda: unstash(h)" in body
    # and never becomes a transactional entry
    assert "transactional(" not in body


def test_non_witnessed_program_is_untouched():
    # a program with no witnessed extern must emit byte-identically to before:
    # neither the transactional call nor the witnessed-forced Result classes
    # appear, and no component even sees the witnessed table.
    emit = _emit_backend()
    ir = compile_source(
        "component C { effect Map.new() undo Map.new() }\n", "plain.rvl")
    body = emit.emit(ir)
    assert "transactional(" not in body
    assert "_revl_frame.transactional" not in body
