"""The java tier runs the character classifiers and the record literals two
records share the fields of (issue #3144).

The programs and the emitted-shape checks are in
tests/test_char_class_record_target_go_java_3144.py; this file executes the
same `test` blocks on the JVM. It sits under backends/java/ because the
`backend-java` job runs that directory with the JDK required, so a missing
toolchain is a failure there and not a green skip. The count asserted is the
number of `test` blocks in each program, which py runs to the same total in the
sibling file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from revl import compile_source  # noqa: E402
from revl.run_java import java_runtime_reason  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "revl_3144_programs",
    ROOT / "tests" / "test_char_class_record_target_go_java_3144.py")
programs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(programs)


def _run(src: str, name: str, count: int) -> None:
    status, message = RUNNERS["java"](compile_source(src, name))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert f"{count} test(s) passed" in message, message


@pytest.mark.skipif(java_runtime_reason() is not None,
                    reason="no working JDK for the java tier")
def test_java_runs_the_classifiers():
    _run(programs.CLASSES, "classes_3144.rvl", 6)


@pytest.mark.skipif(java_runtime_reason() is not None,
                    reason="no working JDK for the java tier")
def test_java_runs_the_record_targets():
    _run(programs.RECORDS, "records_3144.rvl", 3)
