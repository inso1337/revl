"""issue #1356: the build control the carried set never had.

Since issue #1321, `backends/go/emit.py::emit` routes a document that declares a
top-level `fn`/`type`/`extern`/plain `test` beside an OBSERVABLE component to
`_emit_v3_combined`: the pure typed-core tier and the live stc-go components in
one package. Before that the pure path took those documents and DROPPED every
component it routed past, so the go tier answered with a module whose routes
were simply absent.

Carrying is the right answer, and this file is the argument for it: a carried
document that does not build says so on the line that is wrong, where a routing
refusal says nothing at all and a drop says nothing at all AND compiles. But
"carried" has to mean "builds", and nothing measured that. The drift gates
compare emitted BYTES, and `tools/validate.py` was never wired to this path, so
the only reason the missing `RevlFrame` preamble (PR #1355), the unpopulated
`_FN_RET` and the un-pinned item-280 interface (PR #1362) were found at all is
that two of them happened to sit under a conformance probe.

So: emit the whole carried set, one package per document, and `go build` it.
Every carried document builds unless `tests/fixtures/go_carried_build_known_bad.json`
names it and says why. That file is a ratchet; see its own note.
"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from revl import compile_files  # noqa: E402

sys.path.insert(0, str(ROOT))
from backends.go import emit as go_emit  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "go_carried_build_known_bad.json"


def _known_bad() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["known_bad"]


def _routes_combined(ir: dict) -> bool:
    """`_emit`'s ir_version-3 fork, read back: does this document reach the
    combined renderer? Kept as a reading of the routing rather than a pinned
    path list, so a document that starts or stops being carried shows up here
    rather than going quiet."""
    if ir.get("ir_version") != 3:
        return False
    if go_emit._document_holds_stream(ir):
        return False
    has_top_level = bool(ir.get("functions") or ir.get("types")
                         or ir.get("externs") or ir.get("tests"))
    has_lifecycle = any(t.get("lifecycle") for t in (ir.get("tests") or []))
    if not (not ir.get("components") or (has_top_level and not has_lifecycle)):
        return False
    return any(go_emit._component_is_observable(c)
               for c in (ir.get("components") or []))


def _package_for(index: int) -> str:
    # `tools/validate.py`'s go validator keys its results by the emitted
    # PACKAGE name and nests a collision one directory deeper, where the error
    # prefix no longer matches. One package per document, or the sweep
    # collapses to one result.
    return "carried%03d" % index


_CARRIED: list | None = None


def _carried_set():
    """(path, ir) for every carried document in the tree, compiled once."""
    global _CARRIED
    if _CARRIED is not None:
        return _CARRIED
    out = []
    for path in sorted(ROOT.rglob("*.rvl")):
        if ".git" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            ir = compile_files([str(path)])
        except Exception:  # noqa: BLE001 - a document this tier never sees
            continue
        try:
            if _routes_combined(ir):
                out.append((rel, ir))
        except Exception:  # noqa: BLE001
            continue
    _CARRIED = out
    return out


def _emitted_set():
    """(rel, package, source) for every carried document the go tier emits.

    A carried document may still be REFUSED by name (no `@go` extern body, a
    tier limit spelled out in the message). A refusal is an answer; this file
    is about the documents that answer with Go.
    """
    emitted = []
    for index, (rel, ir) in enumerate(_carried_set()):
        package = _package_for(index)
        try:
            emitted.append((rel, package, go_emit.emit(ir, package)))
        except go_emit.EmitError:
            continue
    return emitted


def test_the_carried_set_is_not_empty():
    # A routing change that quietly emptied this set would make every other
    # assertion below vacuous.
    carried = _carried_set()
    assert len(carried) > 100, (
        "only %d documents route to the combined renderer; the fork in "
        "`_emit` moved and this gate stopped measuring anything" % len(carried))


def test_every_known_bad_document_is_still_carried():
    missing = [rel for rel in _known_bad()
               if not (ROOT / rel).is_file()]
    assert not missing, (
        "%d known-bad document(s) no longer exist: %s. Drop them from %s."
        % (len(missing), ", ".join(sorted(missing)), FIXTURE.name))


def test_the_carried_set_builds_except_the_named_remainder():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)

    emitted = _emitted_set()
    assert emitted, "no carried document emitted Go"
    results = validator.check([(rel, src) for rel, _pkg, src in emitted])

    known_bad = _known_bad()
    failed = {rel: detail for rel, (status, detail) in results.items()
              if status != validate.OK}
    unexpected = {rel: detail for rel, detail in failed.items()
                  if rel not in known_bad}
    fixed = sorted(rel for rel in known_bad
                   if rel in results and rel not in failed)

    assert not unexpected, (
        "%d of %d carried document(s) emit Go that does not build and are not "
        "named in %s:\n%s"
        % (len(unexpected), len(emitted), FIXTURE.name,
           "\n".join("  %s: %s" % (rel, detail)
                     for rel, detail in sorted(unexpected.items()))))
    assert not fixed, (
        "%d document(s) named in %s now build. Remove them from the fixture: "
        "the list is a ratchet.\n%s"
        % (len(fixed), FIXTURE.name, "\n".join("  " + rel for rel in fixed)))

    # State the count rather than only the absence of a regression: the whole
    # point of this file is that the number is visible.
    assert len(emitted) - len(failed) == len(emitted) - len(known_bad), (
        "%d of %d carried documents build; %d are named known-bad"
        % (len(emitted) - len(failed), len(emitted), len(known_bad)))


# ---------------------------------------------------------------------------
# the state `_emit` establishes per emit, and the combined path did not
# ---------------------------------------------------------------------------
#
# The three defects found before this issue (the missing `RevlFrame` teardown
# preamble (#1355), the unpopulated `_FN_RET` and the un-pinned item-280
# interface, #1362) are one cause with three faces: `_emit_v3_combined` was
# factored out of the placement entry point and never mirrored what `_emit`
# sets up before it renders. Diffing the two mechanically (every global `_emit`
# assigns, against every global the combined path assigns) turns up five more,
# of which the witnessed registry below is the one that mattered: it does not
# make the module fail to build, it makes it build and be WRONG.

WITNESSED_DOC = """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }

extern pure fn unstash(w: Stash) -> Unit = @go {
	hostRecord("unstash")
	return
}

extern witnessed[fs] fn stash(p: Str) -> Result[Stash, FsError] undo unstash(result) = @go {
	hostRecord("stash")
	return RevlOk[Stash, FsError]{Value: Stash{Path: p, Bak: p + ".bak"}}
}

fn label(p: Str) -> Str = "stash:" + p

service Ops {
  emission fn run(p: Str)
}

component Agent provides ops: Ops {
  provide ops {
    fn run(p) {
      effect stash(label(p))
    }
  }
}
"""


def _emit_source(source: str) -> str:
    from revl import compile_source  # noqa: PLC0415

    return go_emit.emit(compile_source(source), "probe")


def test_a_carried_witnessed_effect_keeps_its_proof_inverse():
    # `_WITNESSED_EXTERNS` is how `_witnessed_extern` tells a transaction from
    # an ordinary bracket. `_emit` has built it since item 243; the combined
    # path never did, so the registry was empty (or held whatever the previous
    # emit left in it) and a witnessed effect in a carried component lowered as
    # a plain `ctx.Effect(func() stc.Inverse { stash(...); return nil })`.
    #
    # The proof inverse was dropped, no teardown frame was opened, and the
    # module COMPILED. That is the same shape as the silent component drop
    # issue #1321 fixed, one level down, and no build gate can see it, which
    # is exactly why the fixture above cannot be the only test here.
    out = _emit_source(WITNESSED_DOC)
    assert "registerMethodWitnessed(" in out, (
        "the witnessed effect must register its proof inverse; the combined "
        "renderer used to lower it as an ordinary bracket with a nil inverse")
    assert "unstash(result)" in out, "...and the inverse has to be the undo"
    assert "func newRevlFrame() *RevlFrame {" in out, (
        "a witnessed effect opens an activation frame, so the module has to "
        "define one")
    assert not re.search(r"stc\.Inverse \{\n\t\tstash\(", out), (
        "the old lowering: a bare bracket around the forward call")


def test_a_carried_witnessed_extern_gets_the_result_form_it_constructs():
    # The two tiers in this one package disagree about what a Result IS: the
    # pure typed-core renderer builds the flat struct (`.Ok` / `.OkV` /
    # `.ErrV`), the component tier and a witnessed `@go` body build the sealed
    # interface (`RevlOk` / `RevlErr`). Go declares one `RevlResult[T, E]` per
    # package. The combined path declared the struct form unconditionally, so
    # the extern body above did not resolve its own `RevlOk`.
    out = _emit_source(WITNESSED_DOC)
    assert "type RevlResult[T any, E any] interface{ isRevlResult() }" in out, (
        "the sealed form, because the extern body constructs RevlOk")
    assert "OkV" not in out, "...and not the flat-struct form beside it"


def test_go_builds_the_carried_witnessed_module():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    status, detail = validator.check(
        [("witnessed", _emit_source(WITNESSED_DOC))])["witnessed"]
    assert status == validate.OK, (
        "the carried witnessed module must build: %s" % detail)
