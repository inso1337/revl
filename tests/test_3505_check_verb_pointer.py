"""`revl check` refusal names the forms that do check a file (#3505).

`check` is the first verb an author tries for "does this parse and admit, run
nothing" (the MCP verb is `revl_check`), but the CLI had no such verb and
argparse's invalid-choice refusal listed all 57 verbs with no pointer to the
forms that do it. The unknown-verb refusal for `check` now names
`revl compile file.rvl` (composition) and `revl test file.rvl --list` (test
file: parse error with file/line, or the collected test names, nothing
executed). Other unknown verbs keep argparse's default message.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402


def test_check_verb_names_compile_and_test_list(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["check", "ok.rvl"])
    assert stopped.value.code != 0
    err = capsys.readouterr().err
    assert "invalid choice: 'check'" in err
    assert "`revl compile file.rvl`" in err
    assert "`revl test file.rvl --list`" in err
    assert "executes nothing" in err or "nothing executes" in err


def test_another_unknown_verb_keeps_the_default_message(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["frobnicate", "x.rvl"])
    assert stopped.value.code != 0
    err = capsys.readouterr().err
    assert "invalid choice: 'frobnicate'" in err
    assert "revl test file.rvl --list" not in err


def test_a_known_verb_is_unaffected(tmp_path, capsys):
    doc = tmp_path / "ok.rvl"
    doc.write_text('test "t" { assert 1 == 1 }\n', encoding="utf-8")
    rc = main(["test", str(doc), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "t" in out
