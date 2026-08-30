"""Roadmap 386, Stage 1: report ALL refusals per compile, not just the first.

The frontend used to abort on the FIRST `RevlError` raised anywhere in the
pipeline, so an author fixing N independent refusals paid N full recompile
round-trips. Stage 1 collects every recoverable refusal (per-component G/A
refusals and the whole-composition post-passes) and reports them together,
while keeping `diagnostics[0]` byte-identical to what today's single-error
compile reports for the same input.

These tests pin the Stage-1 contract AND the five Fable-review corrections
(header-stub topology completeness, partial-component robustness, plan()
multi-diagnostic, diagnostics[0] stability, and post-pass crash guarding).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.diagnostics import report  # noqa: E402
from revl.errors import RevlError  # noqa: E402


_LEDGER = """\
service Ledger {
  fn record(k: Str, v: Str)
}
"""

# Three components, each with ONE distinct undeclared-requirement refusal
# (G1). Today only the first would be reported; Stage 1 reports all three.
_THREE_REFUSALS = _LEDGER + """\
component A requires ledger: Ledger {
  effect foo.record("a", "1") undo foo.record("a", "")
}
component B requires ledger: Ledger {
  effect bar.record("b", "1") undo bar.record("b", "")
}
component C requires ledger: Ledger {
  effect baz.record("c", "1") undo baz.record("c", "")
}
"""


def _compile_expecting_refusal(tmp_path, monkeypatch, text, name="prog.rvl"):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(path)])
    return excinfo.value


def test_three_components_three_distinct_refusals_one_pass(tmp_path, monkeypatch):
    """The headline H38-in-miniature regression: three components, three
    distinct refusals, compiled once, must yield three diagnostics with three
    distinct locations."""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, _THREE_REFUSALS)

    diags = report(error)["diagnostics"]
    messages = sorted(d["message"] for d in diags)
    assert len(diags) == 3, messages
    assert any("foo" in m for m in messages), messages
    assert any("bar" in m for m in messages), messages
    assert any("baz" in m for m in messages), messages
    # three distinct locations (distinct lines in one file)
    assert len({d["line"] for d in diags}) == 3, diags
