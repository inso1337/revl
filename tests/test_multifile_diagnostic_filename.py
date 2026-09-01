"""Roadmap 312: a multi-file `audit`/`compile` must name the file that
actually holds the offending span, not the first source argument.

When several sources are passed together the compiler merges them into one
`Program` whose `filename` is only `paths[0]`. A diagnostic whose span lives
in a LATER file used to render that first argument's name with the real file's
line number, so the two halves of the location disagreed (services.rvl:103 for
a line that lives in durable_sessions.rvl). Both halves must point at the true
source.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.diagnostics import report  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# The lighthouse-workload repro shape: a short first file, a middle file, and a
# LATER file whose component body references an undeclared requirement. The
# offending line (8) does not even exist in the first file (3 lines long), so a
# location pinned to the first argument is impossible on its face.
_SERVICES = """\
service Ledger {
  fn record(k: Str, v: Str)
}
"""

_SESSION_LEDGER = """\
component SessionLedger provides ledger: Ledger {
  isolate ledger in realm("main")
  let store = effect Map.new() undo store.drop()
  provide ledger {
    fn record(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
  }
}
"""

_DURABLE_SESSIONS = """\
// durable sessions component
// padding line 2
// padding line 3
// padding line 4
component DurableSessions requires ledger: Ledger {
  isolate ledger in realm("main")
  effect ledger.record("id", "x") undo ledger.record("id", "")
  effect gone.record("id", "y") undo gone.record("id", "")
}
"""


def _write_composition(tmp_path: Path) -> list[str]:
    (tmp_path / "src" / "components").mkdir(parents=True)
    services = tmp_path / "src" / "services.rvl"
    session = tmp_path / "src" / "components" / "session_ledger.rvl"
    durable = tmp_path / "src" / "components" / "durable_sessions.rvl"
    services.write_text(_SERVICES)
    session.write_text(_SESSION_LEDGER)
    durable.write_text(_DURABLE_SESSIONS)
    return [str(services), str(session), str(durable)]


def test_component_body_diagnostic_names_its_own_file(tmp_path, monkeypatch):
    """The rejection lives in the THIRD file; both filename and line must name
    it, not the first argument (`services.rvl`)."""
    monkeypatch.chdir(tmp_path)
    paths = _write_composition(tmp_path)

    with pytest.raises(RevlError) as excinfo:
        compile_files(paths)

    error = excinfo.value
    assert error.message == "`gone` is not a declared requirement of DurableSessions"
    # The bug printed `services.rvl` (paths[0]) here.
    assert error.filename.endswith("durable_sessions.rvl"), error.filename
    assert not error.filename.endswith("services.rvl"), error.filename
    # The line was always correct; assert both halves agree on the real file.
    assert error.line == 8
    # services.rvl is only 3 lines long, so line 8 there is impossible.
    assert len(_SERVICES.splitlines()) < error.line

    # The agent-facing JSON projection carries the same corrected location.
    diag = report(error)["diagnostics"][0]
    assert diag["file"].endswith("durable_sessions.rvl")
    assert diag["line"] == 8


def test_reordering_arguments_does_not_move_the_reported_filename(tmp_path, monkeypatch):
    """The bug tracked argv[0]: reordering the argument list used to move the
    reported filename while the line kept naming the true source. The location
    must be stable regardless of argument order."""
    monkeypatch.chdir(tmp_path)
    services, session, durable = _write_composition(tmp_path)

    for order in ([services, session, durable], [durable, session, services],
                  [session, durable, services]):
        with pytest.raises(RevlError) as excinfo:
            compile_files(order)
        assert excinfo.value.filename.endswith("durable_sessions.rvl"), order
        assert excinfo.value.line == 8


def test_duplicate_top_level_fn_names_the_later_file(tmp_path, monkeypatch):
    """The item's minimal repro: two files that each declare a top-level `fn`
    of the same name, compiled together. The duplicate is detected on the
    SECOND file's declaration, so it must name that file, not the first."""
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "first.rvl"
    second = tmp_path / "second.rvl"
    first.write_text("pub fn shared(x: Int) -> Int { return x }\n")
    # padding so the duplicate sits at a line that does not exist in first.rvl
    second.write_text(
        "// second module\n"
        "// padding\n"
        "// padding\n"
        "pub fn shared(x: Int) -> Int { return x }\n"
    )

    with pytest.raises(RevlError) as excinfo:
        compile_files([str(first), str(second)])

    error = excinfo.value
    # roadmap 394: a cross-file duplicate now names BOTH declaring files by
    # absolute path (so a two-copies-of-one-module situation is self-evident);
    # the diagnostic is still *located* on the second file's declaration.
    assert error.message.startswith("duplicate function `shared`")
    assert str(first.resolve()) in error.message, error.message
    assert str(second.resolve()) in error.message, error.message
    assert error.filename.endswith("second.rvl"), error.filename
    assert error.line == 4
    assert len(first.read_text().splitlines()) < error.line
