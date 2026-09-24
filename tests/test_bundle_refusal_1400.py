"""Issue #1400: a bundle that omits a tier because that tier REFUSED the
composition must say so, and must not exit 0.

`bundle._emit_files` returned one `None` for two unrelated facts, and its own
docstring said so: "the emitter is absent **or refuses this IR**". The bundler
then recorded both as `skippedBackends: {<tier>: "emitter unavailable or refused
this IR"}` and exited 0. An author bundling six tiers, one of which refuses, got
a bundle for five and a success exit, indistinguishable from a bundle that was
only ever meant to have five.

That is the quiet twin of issue #1393. #1393 delivered a refusal as a traceback,
which is ugly but visible; this one delivers it as nothing at all, in the one
place where it matters most, because a bundle is a shipping artifact. The
refuse-by-name work that landed this week (#1381, #1391, #1355, #1317, #1384)
exists so a tier that cannot lower a construct says which construct and why. If
the bundler eats that sentence, none of it reaches the person shipping.

Measured on `origin/main` at e0d5066b9, over the FLOAT composition below (wasm
refuses a `Float` return; the other five tiers emit it):

    $ revl bundle app.rvl --out app.revlbundle
    wrote bundle app.revlbundle
    $ echo $?
    0
    $ ls app.revlbundle/emitted
    go  java  python  rust  typescript

and `runtime-manifest.json` said, of a tier that had written a precise sentence
about this composition, only `"wasm": "emitter unavailable or refused this IR"`.

What this file pins:

  * a refusal is recorded under `refusedBackends` with the emitter's OWN
    sentence, verbatim, and `revl bundle` exits 4;
  * an ABSENT emitter is still a quiet omission at exit 0, so a machine missing
    one tier does not turn every bundle into a failure;
  * `verify` re-checks a recorded refusal, so the record is evidence rather than
    a comment;
  * and the controls, below.

THE CONTROLS. `EmitError` is a `ValueError` defined INSIDE each dynamically
loaded backend `emit.py`, so it cannot be imported from `src/` and the catch has
to be read off the module object that raised. The lazy version of this fix is
`except ValueError`, which passes every green assertion here and silently eats
half of each emitter's real internal faults, turning an emitter BUG into the
same silent omission this issue is about. So every assertion that a refusal is
recorded is paired with one that a bare `ValueError` from the same call is NOT
recorded and escapes instead. The old code failed that control twice over: its
`except Exception` swallowed the genuine crash as well.
"""

from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The backend emitters live under <root>/backends; the tiers here actually emit.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revl import bundle as B  # noqa: E402
from revl.__main__ import main  # noqa: E402


# wasm refuses a `Float` return by name; python/typescript/rust/java/go all emit
# this composition. One document, one refusing tier, five emitting ones, which
# is exactly the shape the issue describes.
FLOAT = """
service Temp {
  fn read() -> Float
}

component TempSvc provides temp: Temp {
  provide temp {
    fn read() = 1.5
  }
}
"""

# The same shape with an Int return: every tier emits it, so a report over this
# document carries no `refused [...]` tier at all.
INT = """
service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}
"""

ABSENT_REASON = "emitter unavailable here"

#: The wasm backend's own `EmitError` class, bound ONCE at import.
#:
#: It has to be the class object from the module that raises: `emit.py` is
#: loaded by path, so loading it twice makes two unrelated classes and an
#: `except` against one does not catch the other (issue #1393). Binding it here
#: also keeps the fakes below from calling `B._emitter`, which several tests
#: monkeypatch.
WASM_EMIT_ERROR = B._emitter("wasm").EmitError


@pytest.fixture(autouse=True)
def _restore_emitter_cache():
    """`bundle._EMITTERS` is a module-global cache. A test that repoints
    `_BACKENDS_ROOT` has to clear it to be served the tree it points at, so put
    it back afterwards.

    It is restored rather than cleared up front, and that matters: clearing it
    reloads `emit.py` and produces a SECOND, unrelated `EmitError` class, which
    `WASM_EMIT_ERROR` would then no longer match. That is the same fact the
    `_refusal_class` docstring records, met here by accident first."""
    saved = dict(B._EMITTERS)
    yield
    B._EMITTERS.clear()
    B._EMITTERS.update(saved)


def _write(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def _manifest(out: Path) -> dict:
    return json.loads((out / B.RUNTIME_MANIFEST).read_text(encoding="utf-8"))


def _norm_ir(source: str) -> dict:
    from revl import compile_source

    return B._canonical_ir(compile_source(source, "app.rvl"))


def _wasm_refusal_sentence() -> str:
    """The sentence the wasm emitter itself raises for FLOAT.

    Taken from the emitter rather than written out here on purpose: the point of
    the issue is that the bundle carries the EMITTER'S sentence, so a test that
    hard-coded its own copy would still pass if the bundle substituted a generic
    one that happened to match today's wording.
    """
    module = B._emitter("wasm")
    with pytest.raises(WASM_EMIT_ERROR) as caught:
        module.emit(copy.deepcopy(_norm_ir(FLOAT)))
    return str(caught.value)


def _fake_emitter(exc: BaseException, *, with_emit_error: bool = True):
    """A stand-in emitter module whose `emit` raises *exc*.

    It carries the REAL wasm `EmitError` class, so the fake is caught exactly as
    the genuine module would be. The controls below hand it a bare `ValueError`,
    which is not that class and must therefore escape.
    """
    fake = types.ModuleType("emit")
    if with_emit_error:
        fake.EmitError = WASM_EMIT_ERROR

    def emit(ir, *args, **kwargs):
        raise exc

    fake.emit = emit
    return fake


def _backends_root_without(tmp_path: Path, missing: str) -> Path:
    """A backends root that is the real one minus ONE tier's `emit.py`.

    This is the genuinely-absent-toolchain control, and it is built by leaving a
    file out rather than by patching a flag, so the code under test reaches the
    same `path.exists()` it would on a machine where that tier is not installed.
    """
    root = tmp_path / "backends"
    root.mkdir()
    for backend in B.DEFAULT_BACKENDS:
        if backend == missing:
            (root / backend).mkdir()
            continue
        (root / backend).symlink_to(B._BACKENDS_ROOT / backend)
    B._EMITTERS.clear()  # or the cache answers from the real tree
    return root


class _Args:
    """The attribute surface `run_bundle` reads off an argparse namespace."""

    def __init__(self, files, out, **kw):
        self.files = list(files)
        self.out = str(out)
        self.backend = kw.get("backend", [])
        self.topology = None
        self.one_file = None
        self.force = False
        self.json = kw.get("json", False)


# ------------------------------------------------------------------- anchor

def test_the_wasm_tier_still_refuses_this_composition_by_name():
    """Everything below is about how this sentence is CARRIED, so it is worth
    nothing if the sentence stops being raised."""
    sentence = _wasm_refusal_sentence()
    assert "Float" in sentence
    assert "not lowerable" in sentence


def test_five_tiers_do_emit_this_composition():
    """The defect is a bundle that looks complete. That only bites when the
    other tiers succeed, so pin that they do."""
    norm = _norm_ir(FLOAT)
    emitted = [b for b in B.DEFAULT_BACKENDS
               if B._emit_files(b, norm)[0] is not None]
    assert emitted == ["python", "typescript", "rust", "java", "go"]


# ------------------------------------------------- the two cases, separated

def test_a_refusal_is_recorded_with_the_emitters_own_sentence(tmp_path):
    src = _write(tmp_path, "app.rvl", FLOAT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out))

    manifest = _manifest(out)
    assert manifest["refusedBackends"] == {"wasm": _wasm_refusal_sentence()}
    # and the conflated sentence is gone from the skip reason
    assert manifest["skippedBackends"]["wasm"].startswith("emitter refused this IR: ")
    assert "unavailable or refused" not in json.dumps(manifest)
    assert not (out / "emitted" / "wasm").exists()


def test_an_absent_emitter_is_recorded_as_absent_and_nothing_else(tmp_path, monkeypatch):
    """THE QUIET CASE, which must stay quiet. A tier that is not installed
    decided nothing about this composition."""
    monkeypatch.setattr(B, "_BACKENDS_ROOT", _backends_root_without(tmp_path, "wasm"))
    src = _write(tmp_path, "app.rvl", INT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out))

    manifest = _manifest(out)
    assert manifest["refusedBackends"] == {}
    assert manifest["skippedBackends"] == {"wasm": ABSENT_REASON}
    assert sorted(manifest["backends"]) == ["go", "java", "python", "rust", "typescript"]


def test_an_internal_emitter_fault_is_not_recorded_as_a_refusal(tmp_path, monkeypatch):
    """THE CONTROL, and the sharpest one in the file.

    `EmitError` IS a `ValueError`, so `except ValueError` would pass every other
    assertion here while folding a genuine emitter crash into the same silent
    omission issue #1400 is about. A bundle would then be missing a tier because
    that tier's emitter has a BUG, and would say `refused` about it, or nothing
    at all. This test fails against that version, and against the `except
    Exception` that was there before.
    """
    monkeypatch.setattr(
        B, "_emitter",
        lambda backend: _fake_emitter(ValueError("internal emitter fault")))
    src = _write(tmp_path, "app.rvl", INT)
    with pytest.raises(ValueError, match="internal emitter fault"):
        B.build_bundle([src], str(tmp_path / "app.revlbundle"))


def test_emit_files_separates_refused_absent_and_emitted(monkeypatch):
    """The three outcomes at the one function the issue names, including the
    bare-`ValueError` control for the same call."""
    norm = _norm_ir(FLOAT)

    files, refusal = B._emit_files("python", norm)
    assert refusal is None and files and "components.py" in files

    files, refusal = B._emit_files("wasm", norm)
    assert files is None and refusal == _wasm_refusal_sentence()

    monkeypatch.setattr(B, "_emitter", lambda backend: None)
    assert B._emit_files("wasm", norm) == (None, None)

    monkeypatch.setattr(
        B, "_emitter",
        lambda backend: _fake_emitter(ValueError("internal emitter fault")))
    with pytest.raises(ValueError, match="internal emitter fault"):
        B._emit_files("wasm", norm)


def test_an_emitter_with_no_emiterror_class_cannot_swallow_anything(monkeypatch):
    """`_refusal_class` reads the class off the MODULE, so a module that defines
    none catches nothing rather than falling back to something wider."""
    module = _fake_emitter(RuntimeError("boom"), with_emit_error=False)
    assert B._refusal_class(module) is None
    monkeypatch.setattr(B, "_emitter", lambda backend: module)
    with pytest.raises(RuntimeError, match="boom"):
        B._emit_files("wasm", _norm_ir(INT))


# --------------------------------------------------------------- exit codes

def test_revl_bundle_exits_4_and_prints_the_emitters_sentence(tmp_path, capsys):
    src = _write(tmp_path, "app.rvl", FLOAT)
    code = B.run_bundle(_Args([src], tmp_path / "app.revlbundle"))
    assert code == B.REFUSED_EXIT == 4

    captured = capsys.readouterr()
    assert "wrote bundle" in captured.out, "the bundle is still written"
    assert "refused: the wasm tier refused this composition" in captured.err
    assert _wasm_refusal_sentence() in captured.err
    assert "Traceback" not in captured.err


def test_revl_bundle_exits_0_when_a_toolchain_is_merely_absent(tmp_path, capsys,
                                                               monkeypatch):
    """THE CONTROL for the exit code. A missing tier must not become a hard
    failure; nothing was refused, so nothing is reported."""
    monkeypatch.setattr(B, "_BACKENDS_ROOT", _backends_root_without(tmp_path, "wasm"))
    src = _write(tmp_path, "app.rvl", INT)
    code = B.run_bundle(_Args([src], tmp_path / "app.revlbundle"))
    assert code == 0
    assert "refused" not in capsys.readouterr().err


def test_a_bundle_nothing_refuses_still_exits_0(tmp_path, capsys):
    src = _write(tmp_path, "app.rvl", INT)
    code = B.run_bundle(_Args([src], tmp_path / "app.revlbundle"))
    assert code == 0
    assert capsys.readouterr().err == ""
    assert _manifest(tmp_path / "app.revlbundle")["refusedBackends"] == {}


def test_the_cli_returns_the_refusal_exit_code(tmp_path, capsys):
    """Through `revl.__main__.main`, so the code the shell sees is the one
    `run_bundle` returns and nothing flattens it on the way out."""
    src = _write(tmp_path, "app.rvl", FLOAT)
    code = main(["bundle", src, "--out", str(tmp_path / "app.revlbundle")])
    assert code == 4
    assert _wasm_refusal_sentence() in capsys.readouterr().err


def test_json_output_carries_the_refusal_and_still_exits_4(tmp_path, capsys):
    src = _write(tmp_path, "app.rvl", FLOAT)
    code = B.run_bundle(_Args([src], tmp_path / "app.revlbundle", json=True))
    assert code == 4
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["manifest"]["refusedBackends"] == {"wasm": _wasm_refusal_sentence()}
    # a piped `--json` run still says it on stderr, in the emitter's own words
    assert _wasm_refusal_sentence() in captured.err


# ------------------------------------------------------ verify re-checks it

def _tier(report, name: str):
    for check in report.checks:
        if check.tier == name:
            return check
    return None


def test_verify_re_checks_a_recorded_refusal(tmp_path):
    src = _write(tmp_path, "app.rvl", FLOAT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out))

    report = B.verify_bundle(str(out))
    check = _tier(report, "refused [wasm]")
    assert check is not None and check.status == B.OK
    assert _wasm_refusal_sentence() in check.detail
    assert report.ok


def test_a_recorded_refusal_that_no_longer_holds_is_a_mismatch(tmp_path):
    """The record is evidence, not a comment. A bundle claiming a tier refused,
    when that tier emits the rebuilt IR fine, has to read as a divergence."""
    src = _write(tmp_path, "app.rvl", INT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out), backends=("python",))
    manifest = _manifest(out)
    manifest["refusedBackends"] = {"go": "go cannot lower this, allegedly"}
    (out / B.RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = B.verify_bundle(str(out))
    check = _tier(report, "refused [go]")
    assert check is not None and check.status == B.MISMATCH
    assert "no longer refuses" in check.detail
    assert not report.ok


def test_a_recorded_refusal_with_a_different_diagnostic_is_a_mismatch(tmp_path):
    src = _write(tmp_path, "app.rvl", FLOAT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out))
    manifest = _manifest(out)
    manifest["refusedBackends"] = {"wasm": "wasm said something else entirely"}
    (out / B.RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = B.verify_bundle(str(out))
    check = _tier(report, "refused [wasm]")
    assert check is not None and check.status == B.MISMATCH
    assert check.recorded == "wasm said something else entirely"
    assert check.rebuilt == _wasm_refusal_sentence()
    assert not report.ok


def test_a_committed_artifact_whose_tier_now_refuses_is_a_mismatch(tmp_path,
                                                                   monkeypatch):
    """The other direction: the bundle committed a wasm artifact and the emitter
    refuses the rebuilt IR. That is a MISMATCH carrying the emitter's sentence,
    not the `cannot verify` line an absent emitter earns."""
    src = _write(tmp_path, "app.rvl", INT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out), backends=("wasm",))

    real = B._emitter
    monkeypatch.setattr(
        B, "_emitter",
        lambda backend: (_fake_emitter(WASM_EMIT_ERROR("wasm refuses it now"))
                         if backend == "wasm" else real(backend)))
    report = B.verify_bundle(str(out))
    check = _tier(report, "emitted [wasm]")
    assert check is not None and check.status == B.MISMATCH
    assert "wasm refuses it now" in check.detail
    assert not report.ok


def test_verify_still_lets_an_internal_emitter_fault_escape(tmp_path, monkeypatch):
    """THE CONTROL, verify half. Same reason as the build half: a widened catch
    here would report an emitter bug as a tidy MISMATCH sentence."""
    src = _write(tmp_path, "app.rvl", INT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out), backends=("wasm",))

    monkeypatch.setattr(
        B, "_emitter",
        lambda backend: _fake_emitter(ValueError("internal emitter fault")))
    with pytest.raises(ValueError, match="internal emitter fault"):
        B.verify_bundle(str(out))


def test_a_refusal_free_bundle_reports_no_refused_tier(tmp_path):
    """A bundle nothing refused reads exactly as it did before this change."""
    src = _write(tmp_path, "app.rvl", INT)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out))

    report = B.verify_bundle(str(out))
    assert [c for c in report.checks if c.tier.startswith("refused [")] == []
    assert "refused [" not in B.render(report)
    assert report.ok
