"""Guard for the MCP answer ledger (issue #2859).

`tools/answer_ledger.py` commits the per-call cost of a representative MCP
answer set (`bench/results/answer-ledger.md`), which the handles and profile
work (#2852, #2853) is judged against. These tests keep the ledger honest and
cheap:

  1. one call against a temporary fixture component is measured, so the tool
     does not depend on the committed examples to do its job;
  2. two runs produce the identical table, so the committed numbers cannot
     drift with session state or build identity;
  3. `--compare` is green against a freshly rendered table and red when a
     committed row is wrong;
  4. the committed table itself reproduces, so a stale results file is a test
     failure rather than a review miss.

All of it runs in process, with no model, network or cordis.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import answer_ledger as ledger  # noqa: E402

#: A minimal provider/consumer pair, the same shape as examples/counter_pair.rvl
#: but deliberately not that file: the tool must work on any component.
FIXTURE = """service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 7
  }
}

component CounterUser requires counter: Counter {
}
"""


def _fixture_calls(tmp_path: Path) -> list[ledger.Call]:
    source = tmp_path / "fixture.rvl"
    source.write_text(FIXTURE, encoding="utf-8")
    # The calls read the file back, so the component under test is really the
    # temporary one, not an inline string that happens to match it.
    files = [str(source)]
    return [
        ledger.Call("fixture-check", "revl_check", source.name,
                    {"files": files}),
        ledger.Call("fixture-refusal", "revl_ship", source.name,
                    {"files": files}),
    ]


def test_one_call_is_measured_from_a_temp_component(tmp_path):
    rows = ledger.measure(_fixture_calls(tmp_path)[:1])

    assert len(rows) == 1
    row = rows[0]
    assert (row.id, row.verb, row.component) == (
        "fixture-check", "revl_check", "fixture.rvl")
    # A check answer carries the verdict, the guarantee list and the session
    # footer, so it is strictly larger than the source it was given.
    assert row.answer_bytes > len(FIXTURE)
    assert row.est_tokens > 0
    # `revl_check` returns a canonical digest, never the source back.
    assert row.echoed_chars == 0


def test_the_table_is_stable_across_two_runs(tmp_path):
    calls = _fixture_calls(tmp_path)

    first = ledger.measure(calls)
    second = ledger.measure(calls)

    assert first == second
    assert ledger.render(first) == ledger.render(second)


def test_compare_is_green_then_red_on_a_wrong_row(tmp_path, capsys):
    rows = ledger.measure(ledger.default_calls())
    committed = tmp_path / "committed.md"
    committed.write_text(ledger.render(rows), encoding="utf-8")

    assert ledger.main(["--compare", str(committed)]) == 0

    # Flip one measured figure in the committed copy: the CLI must notice.
    text = committed.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines()
               if line.startswith(f"| `{rows[0].id}` |"))
    committed.write_text(
        text.replace(row, row.replace(f"| {rows[0].answer_bytes} |",
                                      f"| {rows[0].answer_bytes + 1} |", 1)),
        encoding="utf-8")

    assert ledger.main(["--compare", str(committed)]) == 1
    assert "does not match a fresh run" in capsys.readouterr().err


def test_the_committed_table_reproduces(capsys):
    rows = ledger.measure(ledger.default_calls())

    assert ledger.compare(rows, ledger.DEFAULT_DOC) == 0
    assert "reproduced" in capsys.readouterr().out
