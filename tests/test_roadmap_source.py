"""The roadmap is one document in two places: docs/v2.0-roadmap.md holds what
is not closed, docs/roadmap-archive/NN-*.md holds the closed items (✅ landed,
➖ declined) moved verbatim. tools/roadmap_source.py joins them so that the
roadmap gates judge exactly what they judged before the split. These tests
pin the join and the two invariants the split relies on."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_roadmap_markers as markers  # noqa: E402
import roadmap_source  # noqa: E402

ROADMAP = ROOT / "docs" / "v2.0-roadmap.md"
ARCHIVE = ROOT / "docs" / roadmap_source.ARCHIVE_DIR_NAME

# The status as the file writes it: after the item number, an optional
# "(issue #N)" or "(private security advisories ...)" and a bold span.
_PREFIX = re.compile(
    r"^(?:\s|\*\*)*(?:\((?:issues?\s*#[^)]*|private security advisor[^)]*)\)"
    r"(?:\s|\*\*)*)*")


def _lead(rest: str) -> str:
    return _PREFIX.sub("", rest, count=1)[:1]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_roadmap_without_an_archive_reads_as_itself(tmp_path):
    main = tmp_path / "docs" / "v2.0-roadmap.md"
    _write(main, "# r\n\n## Open\n\n1. open item\n")
    src = roadmap_source.read_roadmap(main)
    assert src.text == main.read_text(encoding="utf-8")
    assert src.label(5) == "L5"


def test_the_archive_joins_from_its_first_section_heading(tmp_path):
    main = tmp_path / "docs" / "v2.0-roadmap.md"
    _write(main, "# r\n\n## Open\n\n2. open item\n")
    arch = tmp_path / "docs" / "roadmap-archive"
    _write(arch / "01-open.md",
           "# Archive: Open\n\npreamble, not roadmap content\n\n"
           "## Open\n\n1. \u2705 closed item\n")
    _write(arch / "README.md", "# index\n\n## not a section file\n")
    src = roadmap_source.read_roadmap(main)
    assert "preamble" not in src.text
    assert "not a section file" not in src.text
    assert src.text.endswith("## Open\n\n1. \u2705 closed item\n")
    # The closed item keeps its section and its own body, and the main
    # file's last item does not absorb anything from the archive.
    found = {it["number"]: it for it in markers.items(src.text)}
    assert found["1"]["section"] == "Open"
    assert found["1"]["status"] == "done"
    assert found["2"]["body"].rstrip() == "2. open item"
    # Line numbers map back to the file a reader has to open.
    closed_line = found["1"]["line"]
    assert src.locate(closed_line) == (arch / "01-open.md", 7)
    assert src.label(closed_line) == "roadmap-archive/01-open.md:L7"
    assert src.label(5) == "L5"
    assert src.relabel(f"L5: x; L{closed_line}: y; L2 stays") == (
        "L5: x; roadmap-archive/01-open.md:L7: y; L2 stays")
    # The duplicate-block finding joins labels with "/".
    assert src.relabel(f"L5/L{closed_line}") == (
        "L5/roadmap-archive/01-open.md:L7")


def test_every_archived_item_is_closed_and_keeps_its_section():
    """The archive is for closed items only: an open, partial or in-flight
    item moved there would drop out of every reader's view of the work."""
    main_sections = {line[3:].strip() for line in
                     ROADMAP.read_text(encoding="utf-8").splitlines()
                     if line.startswith("## ")}
    files = roadmap_source.archive_files(ROADMAP)
    assert files, "docs/roadmap-archive/ holds no NN-*.md file"
    for path in files:
        text = path.read_text(encoding="utf-8")
        start = text.index("\n## ") + 1
        part = text[start:]
        sections = re.findall(r"^## (.+)$", part, re.M)
        assert sections and set(sections) <= main_sections, path.name
        found = markers.items(part)
        assert found, path.name
        for it in found:
            assert _lead(it["text"]) in ("\u2705", "\u2796"), (
                f"{path.name}: item {it['number']} is not closed: "
                f"{it['text'][:80]!r}")


def test_the_index_lists_every_archive_file():
    index = (ARCHIVE / "README.md").read_text(encoding="utf-8")
    for path in roadmap_source.archive_files(ROADMAP):
        assert f"]({path.name})" in index, path.name


def test_the_gates_read_the_archive():
    """The citation gate's denominator includes the archive's citations: the
    combined document has more commit citations than the main file alone."""
    combined = roadmap_source.read_roadmap(ROADMAP).text
    alone = ROADMAP.read_text(encoding="utf-8")
    assert len(markers.cited_shas(combined)) > len(markers.cited_shas(alone))
