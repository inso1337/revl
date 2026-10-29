"""docs/deploy.md describes the deploy-map admission rules the code applies.

Issue #2716. The page still said a `via = ssh` target is refused outright,
while `admit_deploy_map` admits one that names a `host` and a pinned
`known_hosts`, and its admission table had no row for three of the rules the
code emits (`process-name`, `peer-pool-unavailable`, `remote-shape`).

The rule set is read from the source with `ast` (the second argument of each
`_map_refusal` call), so a new refusal rule with no documented row fails here.
The scan finds only rule names written as a string literal in that call: a rule
built through a variable or emitted by another helper is not seen.
`UNREACHABLE_RULES` is kept by hand.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = ROOT / "src" / "revl" / "deploy.py"
DEPLOY_DOC = ROOT / "docs" / "deploy.md"

#: Emitted by a branch that `KNOWN_VIA` makes unreachable (marked
#: `pragma: no cover` in the source), so no operator can see it.
UNREACHABLE_RULES = {"unhandled-via"}


def _emitted_rules() -> set[str]:
    tree = ast.parse(DEPLOY_SRC.read_text(encoding="utf-8"))
    rules: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_map_refusal" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            rules.add(node.args[1].value)
    return rules


def _admission_table() -> dict[str, str]:
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    section = text.split("## 5. The deploy map", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"\| `([a-z-]+)` \| (.*) \|$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def test_rule_scan_finds_the_known_rules():
    # Positive control: the scan must see rules that exist, or an empty set
    # would make the coverage check below pass vacuously.
    rules = _emitted_rules()
    assert {"unknown-via", "machine-boundary", "remote-shape"} <= rules


def test_every_reachable_admission_rule_has_a_documented_row():
    missing = sorted(_emitted_rules() - UNREACHABLE_RULES - set(_admission_table()))
    assert missing == [], f"docs/deploy.md has no admission row for {missing}"


def test_machine_boundary_row_names_the_pinned_host_key_condition():
    row = _admission_table()["machine-boundary"]
    assert "known_hosts" in row and "host" in row
    assert "not opened here at all" not in row
