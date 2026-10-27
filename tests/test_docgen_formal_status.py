"""The guarantee glossary's formal-status column follows its ledger."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import docgen  # noqa: E402


_STATUS = """## What the layer covers, and what it does not

| Code | Formal status | Theorems | Oracle | Gap |
|---|---|---|---|---|
| **G1** declared access | partial | 1 | no | gap |
| **G2** disjointness | full over the lattice | 1 | no | gap |
"""

_CURRENT = """| Code | Guarantee (one line) | Enforced at | Origin |
| ---- | -------------------- | ----------- | ------ |
| [G1](rejections.md#g1) | declared access | lower | Cordis |
| [G2](rejections.md#g2) | disjointness | linker | Cordis |
"""


def _root(tmp_path: Path) -> Path:
    path = tmp_path / "formal" / "STATUS.md"
    path.parent.mkdir(parents=True)
    path.write_text(_STATUS, encoding="utf-8")
    return tmp_path


def test_formal_status_is_generated_and_human_columns_are_carried(tmp_path):
    root = _root(tmp_path)
    generated = docgen.block_formal_guarantees(_CURRENT, root=root)

    assert "| [G1](rejections.md#g1) | declared access | lower | Cordis | partial |" in generated
    assert "| [G2](rejections.md#g2) | disjointness | linker | Cordis | full |" in generated
    assert docgen.block_formal_guarantees(generated, root=root) == generated


def test_a_ledger_status_change_moves_the_generated_column(tmp_path):
    root = _root(tmp_path)
    before = docgen.block_formal_guarantees(_CURRENT, root=root)
    status_path = root / "formal" / "STATUS.md"
    status_path.write_text(
        _STATUS.replace(
            "| **G2** disjointness | full over the lattice |",
            "| **G2** disjointness | partial |",
        ),
        encoding="utf-8",
    )

    after = docgen.block_formal_guarantees(before, root=root)
    assert before != after
    assert "| [G2](rejections.md#g2) | disjointness | linker | Cordis | partial |" in after


def test_a_new_ledger_guarantee_arrives_with_curatorial_placeholders(tmp_path):
    root = _root(tmp_path)
    status_path = root / "formal" / "STATUS.md"
    status_path.write_text(_STATUS + "| **G3** acyclic | full | 1 | no | gap |\n", encoding="utf-8")

    generated = docgen.block_formal_guarantees(_CURRENT, root=root)
    assert "| G3 | TODO: describe the guarantee | TODO: name enforcement site | " \
           "TODO: identify origin | full |" in generated


def test_the_real_guarantees_table_is_wired_to_docgen():
    assert any(
        key == "formal-guarantees"
        and rel == "docs/guarantees.md"
        and render is docgen.block_formal_guarantees
        for key, rel, _source, render in docgen.BLOCKS
    )
