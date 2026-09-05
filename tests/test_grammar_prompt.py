"""The prompt-pinnable grammar artifact (roadmap item 346).

`docs/syntax-2.0.prompt.txt` is meant for direct injection into an LLM
authoring system prompt: dense, complete, and small. The content is duplicated
as `grammar_summary.PROMPT_GRAMMAR` because
`docs/` is not packaged into the wheel (pyproject.toml's wheel target only
maps in `src/revl`, `backends/` and `stdlib/`) — this test is the drift guard between the two
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


def test_prompt_grammar_fits_the_authoring_context_character_budget():
    # Keep the injected grammar below 12,000 characters (~3,000 tokens at the
    # usual four characters per token), leaving the rest of a 16k-token
    # authoring context for instructions and the user's source.
    budget = 12_000
    size = len(PROMPT_GRAMMAR)
    assert size <= budget, (
        f"{size} characters — exceeds the 12,000-character grammar budget "
        "for a 16k-token authoring context"
    )


def test_prompt_grammar_budget_allows_dense_growth_but_rejects_doubling():
    budget = 12_000
    dense_growth = PROMPT_GRAMMAR + "\n" + "\n".join("x" * 100 for _ in range(3))
    assert len(dense_growth) <= budget
    assert len(PROMPT_GRAMMAR + PROMPT_GRAMMAR) > budget


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
