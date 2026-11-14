"""Branch protection on main requires code-owner review (#3116).

That rule requires nothing when the repository has no CODEOWNERS file. Pin the
file, and pin that it names the owner for the paths that carry the release and
supply-chain trust: CI workflows, release tooling, the packaging hook and the
truc lock and update code.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

OWNER = "@inso1337"
REQUIRED = [
    "/.github/",
    "/tools/",
    "/pyproject.toml",
    "/hatch_build.py",
    "/src/revl/truc/",
    "/src/revl/update/",
]


def _rules():
    rules = {}
    for line in CODEOWNERS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            rules[pattern] = owners
    return rules


def test_codeowners_file_exists():
    assert CODEOWNERS.is_file()


def test_codeowners_names_owner_for_trust_paths():
    rules = _rules()
    for path in REQUIRED:
        assert rules.get(path) == [OWNER], path


def test_codeowners_paths_exist():
    for path in REQUIRED:
        assert (ROOT / path.lstrip("/")).exists(), path
