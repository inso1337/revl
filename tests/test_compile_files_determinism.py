"""Regression tests for deterministic multi-module compilation."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import compile_files  # noqa: E402


def test_compiling_same_file_repeatedly_is_byte_identical():
    path = Path(__file__).resolve().parents[1] / "selfhost" / "checker.rvl"
    expected = json.dumps(compile_files([str(path)]), sort_keys=True)

    for _ in range(5):
        assert json.dumps(compile_files([str(path)]), sort_keys=True) == expected
