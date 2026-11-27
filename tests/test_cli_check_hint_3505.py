"""Issue #3505: the unknown-verb refusal for `check` names the CLI forms that
do check a file (`revl compile FILE`, `revl test FILE --list`) instead of only
listing every verb — `check` is the first verb an author tries for "does this
parse and admit, run nothing", and the MCP verb is `revl_check`."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402


def test_an_unknown_check_verb_names_the_forms_that_do_check(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "ok.rvl"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'check'" in err
    assert "revl compile FILE" in err
    assert "revl test FILE --list" in err


def test_an_unknown_verb_other_than_check_gets_no_check_hint(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "revl compile FILE" not in err
