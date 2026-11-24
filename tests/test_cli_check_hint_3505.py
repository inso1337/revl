"""Issue #3505: the refusal for `revl check` names the forms that check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cli.parser import build_parser  # noqa: E402


def test_an_unknown_check_verb_names_compile_and_test_list(capsys):
    """`check` is not a verb. The refusal keeps the verb list and names the
    two forms that parse/admit without running: `compile` and `test --list`."""
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["check", "ok.rvl"])
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "revl compile <file>" in err
    assert "revl test <file> --list" in err
    assert "invalid choice: 'check'" in err


def test_another_unknown_verb_does_not_get_the_check_hint(capsys):
    """The hint is specific to `check`; an ordinary typo keeps the plain
    argparse refusal rather than a misleading suggestion."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["chek", "ok.rvl"])
    err = capsys.readouterr().err
    assert "revl compile <file>" not in err