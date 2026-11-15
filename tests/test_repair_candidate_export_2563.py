"""`revl repair --candidate` judges a candidate file by its path (issue #2563).

The command used to read every `--candidate` file and join the texts into one
inline `source`. An inline source compiles under the label `<candidate>.rvl`,
a name no file answers to, so the export rule (`export_area.CliRule`) could not
see that a candidate lay under `.revl/export/`, and text an agent exported
compiled with the operator's trust. The command now hands the candidate files
over as files, and the compile that follows decides each by its path.

No cordis runtime: the first test replaces the session and the loop and reads
what the command hands to the loop; the second runs the compile the loop uses
for a candidate, which is the frontend alone.
"""

import json

import pytest

from revl.__main__ import main
from revl.errors import RevlError
from revl.mcp import repair as repair_mod

RUNNING = """\
service Greet {
  fn hello(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
  }
}
"""

# the operator may declare host code; an untrusted author may not
HOST_CODE = RUNNING.replace(
    "component Greeter",
    "extern emission[db] fn pg_write(row: Str) -> Int = @py { return 0 }\n"
    "component Greeter")

PROFILED = "untrusted-author profile forbids"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_repair_cli_hands_candidate_files_over_as_files(tmp_path, monkeypatch,
                                                         capsys):
    running = _write(tmp_path / "running.rvl", RUNNING)
    exported = _write(tmp_path / ".revl" / "export" / "cand.rvl", HOST_CODE)
    handed = {}

    class _Session:
        def load(self, ir, record=True):
            return None

    def _loop(session, arguments, over_the_transport=True):
        handed.update(arguments)
        return {"incident": {"status": "no-candidate"}}

    monkeypatch.setattr("revl.mcp.session.Session", _Session)
    monkeypatch.setattr(repair_mod, "run_repair", _loop)

    main(["repair", str(running), "--component", "Greeter",
          "--candidate", str(exported), "--json"])
    json.loads(capsys.readouterr().out)

    candidate = handed["candidate"]
    assert "source" not in candidate
    assert candidate["files"] == [str(exported)]


def test_repair_candidate_in_the_export_area_is_profiled(tmp_path):
    """The compile the loop runs for the candidate files, as the command runs
    it (`over_the_transport=False`): host code in an exported file is refused,
    the same bytes from a file outside the export area compile, so the answer
    follows the path and not the content."""
    exported = _write(tmp_path / ".revl" / "export" / "cand.rvl", HOST_CODE)
    own = _write(tmp_path / "src" / "cand.rvl", HOST_CODE)

    with pytest.raises(RevlError) as caught:
        repair_mod._compile({"files": [str(exported)]},
                            over_the_transport=False)
    assert PROFILED in str(caught.value)

    assert repair_mod._compile({"files": [str(own)]},
                               over_the_transport=False)["components"]


def test_one_exported_candidate_among_several_is_profiled(tmp_path):
    """The join this replaces lost every path; with files, one exported
    candidate among several still brings the profile."""
    own = _write(tmp_path / "src" / "own.rvl", RUNNING)
    exported = _write(tmp_path / ".revl" / "export" / "cand.rvl",
                      HOST_CODE.replace("Greeter", "Other").replace(
                          "Greet", "Hello"))

    with pytest.raises(RevlError) as caught:
        repair_mod._compile({"files": [str(own), str(exported)]},
                            over_the_transport=False)
    assert PROFILED in str(caught.value)
