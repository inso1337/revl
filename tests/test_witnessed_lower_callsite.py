"""Witnessed-inverse externs — roadmap item 243, SLICE 2 (lower.py call-site
enablement).

Design: docs/design/243-witnessed-externs.md, "Slice 2a as implemented" #4:
Slice 2a proved the py runtime seam against a HAND-BUILT transactional IR
step, because `src/revl/lower.py` did not yet emit that shape from real
source — a witnessed call in effect position (`effect stash()` / `let w =
effect stash()`, no site undo) was refused at parse time. This slice closes
that gap: the grammar now admits the omitted `undo` for a call to a
`witnessed`-classified extern (`Parser._witnessed_names`, tracked
declare-before-use), and `_lower_effect_step` (src/revl/lower.py) lowers it
to the exact transactional step shape Slice 2a's tests hand-built — proven
here end-to-end from SOURCE through the py runtime, not by re-hand-building
the IR.

The toy witnessed extern is the same rename-with-a-data-witness stand-in
tests/test_witnessed_runtime.py uses, reused verbatim so the two suites stay
comparable.
"""

import copy
import importlib.util
import os

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

# ---------------------------------------------------------------------------
# live runtime gate (mirrors tests/test_witnessed_runtime.py)
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the transactional teardown is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

_TARGET_ENV = "REVL_WIT_TARGET"

# ---------------------------------------------------------------------------
# the toy witnessed effect (tests/test_witnessed_runtime.py's fixture) plus
# REAL component sources exercising the call site — this is the part Slice 2a
# could not test: no hand-built IR steps below, only `.rvl` text through the
# real parser + lower.py.
# ---------------------------------------------------------------------------

_EXTERNS = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash() -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return Ok({'path': path, 'bak': bak})\n"
    "}\n"
    # the BRACKET contrast: an `acquire` extern with the identical mutation,
    # so the same source proves witnessed and acquire diverge at runtime.
    "extern acquire fn stash_acq() -> Stash undo unstash(result) = @py {\n"
    "    import os\n"
    "    path = os.environ['REVL_WIT_TARGET']\n"
    "    bak = path + '.bak'\n"
    "    os.replace(path, bak)\n"
    "    return {'path': path, 'bak': bak}\n"
    "}\n"
)

# every call site here is source syntax — `effect stash()` and
# `let w = effect stash()` with NO `undo` clause — which used to be a parse
# error (G4: "effect has no `undo`"). This is exactly the surface Slice 2a
# deferred (docs/design/243-witnessed-externs.md, "Deferred" #4).
_SOURCE = _EXTERNS + (
    "component StashOk {\n"
    "  effect stash()\n"
    "}\n"
    "component StashAbort {\n"
    "  effect stash()\n"
    '  fail "boom"\n'
    "}\n"
    "component StashLet {\n"
    "  let w = effect stash()\n"
    "}\n"
    "component Acq {\n"
    "  let h = effect stash_acq() undo unstash(h)\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "witnessed_callsite.rvl")


def _ir(component_name: str) -> dict:
    """One component's IR, filtered from the shared `_BASE` compile — mirrors
    tests/test_witnessed_runtime.py's `_ir` helper, extended to also filter
    the manifest (this fixture compiles four components together, unlike
    Slice 2a's one-component-per-fixture hand-built IR)."""
    ir = copy.deepcopy(_BASE)
    ir["components"] = [c for c in ir["components"] if c["name"] == component_name]
    manifest = ir.get("manifest")
    if manifest is not None:
        manifest["components"] = [c for c in manifest["components"]
                                  if c["name"] == component_name]
        manifest["loadOrder"] = [n for n in manifest["loadOrder"] if n == component_name]
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
    """The single activation frame of the loaded composition, for
    introspection of its transactional entries."""
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    return driver.runtime._frame_for_ctx(fiber.ctx)


# ---------------------------------------------------------------------------
# the lowered shape: real source through the real parser + lower.py produces
# exactly the step Slice 2a's tests hand-built
# ---------------------------------------------------------------------------

def test_source_lowers_to_the_hand_built_transactional_step_shape():
    body = _ir("StashOk")["components"][0]["body"]
    assert body == [
        {"step": "effect", "acquire": {"kind": "fn", "name": "stash", "args": []}},
    ]


def test_let_effect_form_also_omits_the_site_undo():
    body = _ir("StashLet")["components"][0]["body"]
    assert body == [
        {"step": "let-effect", "acquire": {"kind": "fn", "name": "stash", "args": []},
         "bind": "w"},
    ]


def test_acquire_bracket_keeps_its_site_spelled_undo():
    # the contrast case: an ordinary acquire still requires (and keeps) the
    # site undo — only a witnessed call's grammar changed.
    body = _ir("Acq")["components"][0]["body"]
    assert body == [
        {"step": "let-effect", "bind": "h",
         "acquire": {"kind": "fn", "name": "stash_acq", "args": []},
         "undo": {"kind": "fn", "name": "unstash", "args": [{"kind": "name", "id": "h"}]}},
    ]


def test_site_spelled_undo_on_a_witnessed_call_is_refused():
    # "No site-spelled undo; the accumulator owns the inverse" is enforced,
    # not just silently ignored if a program spells one anyway.
    with pytest.raises(RevlError) as ei:
        compile_source(_EXTERNS + (
            "component Bad {\n"
            "  effect stash() undo unstash(result)\n"
            "}\n"
        ), "t.rvl")
    assert "cannot declare a site `undo`" in str(ei.value)


def test_witnessed_in_provide_method_body_is_refused_not_crashed():
    # out of this slice's scope (activation body only); must refuse cleanly,
    # not raise trying to lower a step with no undo it wasn't taught to build.
    with pytest.raises(RevlError) as ei:
        compile_source(_EXTERNS + (
            "service Ops { fn go() -> Unit }\n"
            "component P provides ops: Ops {\n"
            "  provide ops {\n"
            "    fn go() {\n"
            "      effect stash()\n"
            "    }\n"
            "  }\n"
            "}\n"
        ), "t.rvl")
    assert "not yet supported inside a provide-method body" in str(ei.value)


def test_bare_witnessed_call_outside_effect_position_still_refused():
    # Slice 1's rule 1 refusal is unaffected by the grammar relaxation: a
    # witnessed call with no accumulator around it is still rejected.
    with pytest.raises(RevlError) as ei:
        compile_source(_EXTERNS + 'fn f() -> Result[Stash, FsError] { return stash() }',
                       "t.rvl")
    assert "cannot be called in the body of fn `f`" in str(ei.value)


# ---------------------------------------------------------------------------
# emit seam (no cordis needed): source -> lower.py -> emit.py produces the
# same registration Slice 2a's emit-only tests assert on hand-built IR
# ---------------------------------------------------------------------------

def _emit_backend():
    spec = importlib.util.spec_from_file_location(
        "py_emit_witnessed_callsite", "backends/python/emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_emitted_body_matches_slice2a_shape_from_source():
    emit = _emit_backend()
    body = emit.emit(_ir("StashOk"))
    assert "isinstance(_revl_wit1, Ok)" in body
    assert "_revl_frame.transactional((lambda result: unstash(result)), _revl_wit1.value)" in body
    assert "yield lambda:" not in body


# ---------------------------------------------------------------------------
# end-to-end runtime, driven from SOURCE (not hand-built IR): the exact three
# observations tests/test_witnessed_runtime.py proved against its fixture IR,
# now proved against the real lowering path.
# ---------------------------------------------------------------------------

@needs_cordis
def test_witnessed_persists_on_clean_unload_from_source(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir("StashOk"))

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


@needs_cordis
def test_witnessed_reverts_on_abort_from_source(target):
    bak = target + ".bak"
    session = _session()

    report = session.load(_ir("StashAbort"))
    assert report["components"] == [{"name": "StashAbort", "state": "FAILED"}]

    assert os.path.exists(target), "abort did not replay the inverse — mutation stuck"
    assert open(target).read() == "the deliverable"
    # residue-free: the backup the mutation created was cleaned up by the revert
    assert not os.path.exists(bak), "abort left rollback residue (A8 violation)"


@needs_cordis
def test_acquire_bracket_reverts_on_clean_unload_from_source(target):
    bak = target + ".bak"
    session = _session()
    session.load(_ir("Acq"))

    assert not os.path.exists(target)  # acquired: mutation applied
    assert os.path.exists(bak)

    session.unload()  # clean unload

    # the bracket inverse replays on a clean unload — the file is restored,
    # exactly what the witnessed effect (above) must NOT do.
    assert os.path.exists(target), "acquire bracket failed to revert on clean unload"
    assert not os.path.exists(bak)
    assert open(target).read() == "the deliverable"
