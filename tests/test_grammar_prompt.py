"""The prompt-pinnable grammar artifact (roadmap item 346).

`docs/syntax-2.0.prompt.txt` is meant for direct injection into an LLM
authoring system prompt: dense, complete, and small (roughly a hundred
lines). The content is duplicated as `grammar_summary.PROMPT_GRAMMAR` because
`docs/` is not packaged into the wheel (pyproject.toml only force-includes
`backends/` and `stdlib/`) — this test is the drift guard between the two
copies, and the CLI (`revl grammar --prompt`) reads the Python constant, never
the file, so it works identically from a checkout or an installed package.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402
from revl.grammar_summary import PROMPT_GRAMMAR, PROSE_GRAMMAR  # noqa: E402

PROMPT_FILE = ROOT / "docs" / "syntax-2.0.prompt.txt"


def test_prompt_file_matches_the_shipped_constant():
    # the file is the reviewable artifact; the constant is what ships in the
    # wheel — they must never drift apart.
    assert PROMPT_FILE.read_text() == PROMPT_GRAMMAR


def test_prompt_grammar_is_roughly_a_hundred_lines():
    lines = PROMPT_GRAMMAR.splitlines()
    assert 60 <= len(lines) <= 145, f"{len(lines)} lines — outside the prompt budget"


def test_prompt_grammar_covers_every_construct_in_the_delta_grammar():
    # docs/syntax-2.0.md §8's own "grammar deltas" summary names these
    # productions; the prompt artifact must be a superset (it is the FULL
    # grammar, not a delta), so every keyword named there must appear here.
    for keyword in ("use", "type", "fn", "service", "component", "extern",
                    "emission", "async", "commutative", "isolate", "intercept",
                    "config", "effect", "undo", "compensate", "emit", "fail",
                    "await", "provide", "requires", "provides", "match",
                    "hole", "lifecycle", "load", "unload", "call", "assert",
                    "fault"):
        assert keyword in PROMPT_GRAMMAR, f"missing construct: {keyword}"


def test_prompt_grammar_names_the_core_guarantees():
    for code in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "A1"):
        assert code in PROMPT_GRAMMAR


def test_cli_grammar_default_prints_the_prose_summary(capsys):
    assert main(["grammar"]) == 0
    assert capsys.readouterr().out == PROSE_GRAMMAR


def test_cli_grammar_prompt_flag_prints_the_dense_grammar(capsys):
    assert main(["grammar", "--prompt"]) == 0
    assert capsys.readouterr().out == PROMPT_GRAMMAR
