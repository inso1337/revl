"""`examples/mediator.rvl` - a component that alone holds a raw capability
and exposes a narrow wrapper (issue #2697, slice 1).

The G8 boundary is the proof this example exists for: the raw `fs` capability
appears on `FsMediator` and nowhere else in the composition, and a consumer that
tries to reach `fs.write` without being wired to it is refused by name.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "mediator.rvl")


def _boundary():
    return audit_report(compile_files([EXAMPLE]))["boundary"]


def _reached(stats: dict) -> set:
    """The capability tokens a component's G8 surface reaches."""
    return {cap for scope in (stats.get("capabilities") or {}).values()
            for cap in (scope or [])}


#: The consumer, rewritten to reach the raw boundary directly. `fs` is not one
#: of its declared requirements, so the gate refuses the composition rather than
#: inventing authority for it.
DIRECT_REACH = """
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }
service Notes { emission fn save(data: Str) -> Int }

component DirectConsumer requires writer: AppendWriter provides notes: Notes {
  provide notes {
    fn save(data) -> Int {
      emit fs.write("notes/daily.log", data)
      return 0
    }
  }
}
"""


def test_the_example_is_admitted():
    ir = compile_files([EXAMPLE])
    assert [c["name"] for c in ir["components"]] == ["FsMediator", "NotesConsumer"]


def test_g8_shows_the_raw_capability_on_the_mediator_only():
    boundary = _boundary()
    # the raw boundary is the mediator's alone ...
    assert _reached(boundary["FsMediator"]) == {"fs"}
    assert boundary["FsMediator"]["emissions"] == ["fs.write"]
    # ... and the consumer reaches the wrapper, not `fs`. A bare `emission`
    # (`emission fn append`) names its destination `*`: the wrapper crosses a
    # boundary the mediator owns, and the raw token never enters the consumer's
    # declared scope.
    assert "fs" not in _reached(boundary["NotesConsumer"])
    assert boundary["NotesConsumer"]["emissions"] == ["writer.append"]


def test_the_consumer_is_wired_through_the_wrapper_not_the_raw_capability():
    ir = compile_files([EXAMPLE])
    by_name = {c["name"]: c for c in ir["components"]}
    assert by_name["NotesConsumer"]["requires"] == {"writer": "AppendWriter"}
    assert by_name["FsMediator"]["requires"] == {"fs": "FileStore"}
    assert by_name["FsMediator"]["provides"] == {"writer": "AppendWriter"}


def test_a_consumer_reaching_fs_write_directly_is_refused():
    with pytest.raises(RevlError) as refused:
        compile_source(DIRECT_REACH, "<direct-reach>.rvl")
    assert refused.value.code == "G1"
    assert "fs" in refused.value.message
    assert "not a declared requirement" in refused.value.message
