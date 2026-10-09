"""The roadmap as ONE document: docs/v2.0-roadmap.md plus its archive.

Closed items (✅ landed, ➖ declined) live in `docs/roadmap-archive/NN-*.md`,
one file per section of the main file, moved verbatim with their section
heading and numbers. The gates that read the roadmap
(`check_roadmap_markers.py`, `check_roadmap_claims.py`, `tier_guarantees.py`)
judge the main file and the archive together, exactly as they judged the one
file before the split, so a citation or marker cannot escape a gate by being
archived.

The combined text is the main file followed by each archive file from its
first "## " heading on, in file-name order. The lines above that heading are
the archive file's own preamble and are not roadmap content. Line numbers in
the combined text equal the main file's own for the main file's lines;
`label`/`locate` turn a combined line number back into a file and line.

The archive sits next to the roadmap file, so a test that writes a roadmap
into a temporary directory with no `roadmap-archive/` reads that file alone.
"""
from __future__ import annotations

import re
from pathlib import Path

ARCHIVE_DIR_NAME = "roadmap-archive"
ARCHIVE_GLOB = "[0-9][0-9]-*.md"


def archive_files(roadmap: Path) -> list[Path]:
    return sorted((Path(roadmap).parent / ARCHIVE_DIR_NAME).glob(ARCHIVE_GLOB))


class RoadmapSource:
    def __init__(self, roadmap: Path) -> None:
        roadmap = Path(roadmap)
        self.roadmap = roadmap
        lines = roadmap.read_text(encoding="utf-8").splitlines()
        self.main_lines = len(lines)
        # (first combined line, path, line in that file of the first line)
        self.segments: list[tuple[int, Path, int]] = [(1, roadmap, 1)]
        for path in archive_files(roadmap):
            own = path.read_text(encoding="utf-8").splitlines()
            start = next((i for i, l in enumerate(own) if l.startswith("## ")),
                         len(own))
            self.segments.append((len(lines) + 1, path, start + 1))
            lines += own[start:]
        self.text = "\n".join(lines) + "\n"

    def locate(self, line: int) -> tuple[Path, int]:
        """Combined line number -> (file, line in that file)."""
        for first, path, local in reversed(self.segments):
            if line >= first:
                return path, line - first + local
        return self.roadmap, line

    def label(self, line: int) -> str:
        """`L<n>` for the main file (unchanged), `<archive>.md:L<n>` otherwise."""
        path, local = self.locate(line)
        if path == self.roadmap:
            return f"L{local}"
        return f"{ARCHIVE_DIR_NAME}/{path.name}:L{local}"

    def relabel(self, report: str) -> str:
        """Rewrite every `L<n>` past the main file's end in a printed report.

        Only numbers beyond the main file are touched, so an `L<n>` that is
        already a main-file line, or a word like `L2`, is left alone."""
        def sub(m: re.Match) -> str:
            n = int(m.group(1))
            return self.label(n) if n > self.main_lines else m.group(0)
        return re.sub(r"(?<![\w/.:-])L(\d+)\b", sub, report)


def read_roadmap(roadmap: Path) -> RoadmapSource:
    return RoadmapSource(roadmap)
