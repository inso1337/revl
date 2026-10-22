"""The issue-#2844 doc/comment reference checker: it resolves the spellings
this tree uses, it names a moved anchor and a missing file rather than a clean
run, and its ledger suppresses only what it records, with a reason.

The fixtures are tiny trees. One test also runs the scan over the real tree
against the committed ledger, because that is the contract the advisory
pre-PR step relies on; it is the only test that reads the whole tree.
"""

import json
from pathlib import Path

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    return load_by_path(
        "check_doc_refs", ROOT / "tools" / "check_doc_refs.py")


def _tree(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def _ref(path, line):
    """A `path:line` spelling, assembled so this test's own source does not
    contain one the real-tree scan would have to ledger."""
    return f"{path}:{line}"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _findings(root):
    tool = _tool()
    _, references, findings = tool.examine(root)
    return references, findings


def test_a_line_past_the_end_of_the_file_is_stale(tmp_path):
    root = _tree(tmp_path)
    _write(root, "src/thing.py", "one = 1\ntwo = 2\n")
    _write(root, "docs/page.md", f"see `{_ref('src/thing.py', 99)}` for the contract\n")
    references, findings = _findings(root)
    assert len(references) == 1
    assert [(f.kind, f.cited, f.line) for f in findings] == [
        ("stale", "src/thing.py", 99)]
    assert "2 lines" in findings[0].detail


def test_a_cited_file_that_does_not_resolve_is_missing(tmp_path):
    root = _tree(tmp_path)
    _write(root, "docs/page.md", f"see `{_ref('src/gone.py', 3)}` for the contract\n")
    _, findings = _findings(root)
    assert [(f.kind, f.cited, f.line) for f in findings] == [
        ("missing", "src/gone.py", 3)]


def test_a_clean_tree_has_no_findings_and_check_is_green(tmp_path):
    root = _tree(tmp_path)
    _write(root, "src/thing.py", "one = 1\ntwo = 2\n")
    # Root-relative and citing-file-relative: both spellings resolve.
    _write(root, "docs/page.md",
           f"root `{_ref('src/thing.py', 2)}` and sibling `{_ref('../src/thing.py', 1)}`\n")
    # A comment and a docstring are scanned too, not just Markdown.
    _write(root, "src/commented.py",
           f"# see {_ref('src/thing.py', 2)}\n"
           "def f():\n"
           f"    \"\"\"Docstring cites {_ref('src/thing.py', 1)}.\"\"\"\n"
           "    return 1\n")
    references, findings = _findings(root)
    assert len(references) == 4
    assert {r.source.rsplit(".", 1)[-1] for r in references} == {"md", "py"}
    assert findings == ()
    tool = _tool()
    ledger = tmp_path / "ledger.json"
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--write-ledger"]) == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["findings"] == []
    # --baseline is the same verb under the writer's other name.
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--baseline"]) == 0
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--check"]) == 0


def test_the_ledger_suppresses_exactly_what_it_records(tmp_path):
    root = _tree(tmp_path)
    _write(root, "src/thing.py", "one = 1\ntwo = 2\n")
    _write(root, "docs/page.md",
           f"stale `{_ref('src/thing.py', 99)}` and missing `{_ref('src/gone.py', 3)}`\n")
    # The same broken spelling from a second citing file is one entry, not two.
    _write(root, "docs/other.md", f"also `{_ref('src/gone.py', 3)}`\n")
    tool = _tool()
    ledger = tmp_path / "tools" / "doc_refs_ledger.json"
    ledger.parent.mkdir()
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--check"]) == 2  # no ledger is a RED, not a pass
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--write-ledger"]) == 0
    entries = json.loads(ledger.read_text(encoding="utf-8"))["findings"]
    assert [(e["path"], e["line"], e["kind"]) for e in entries] == [
        ("src/gone.py", 3, "missing"), ("src/thing.py", 99, "stale")]
    assert all(e["reason"].strip() for e in entries)
    _, _, findings = tool.examine(root)
    assert len(findings) == 3, "three occurrences, two distinct spellings"
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--check"]) == 0
    # A finding the ledger does not record is still a RED.
    _write(root, "docs/page.md",
           f"stale `{_ref('src/thing.py', 99)}`, "
           f"missing `{_ref('src/gone.py', 3)}`, "
           f"new `{_ref('src/thing.py', 4)}`\n")
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--check"]) == 1
    # The baseline can be re-authored, and a repaired entry drops out.
    _write(root, "docs/page.md", f"just `{_ref('src/gone.py', 3)}` now\n")
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--write-ledger"]) == 0
    entries = json.loads(ledger.read_text(encoding="utf-8"))["findings"]
    assert [(e["path"], e["line"]) for e in entries] == [("src/gone.py", 3)]
    # A missing -> stale flip gets that class's fresh reason, not the old one.
    _write(root, "src/gone.py", "one = 1\n")
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--write-ledger"]) == 0
    entries = json.loads(ledger.read_text(encoding="utf-8"))["findings"]
    gone = next(e for e in entries if e["path"] == "src/gone.py")
    assert gone["kind"] == "stale"
    assert gone["reason"].startswith("line past end of file")


def test_a_ledger_entry_without_a_reason_is_a_red(tmp_path):
    root = _tree(tmp_path)
    _write(root, "docs/page.md", f"see `{_ref('src/gone.py', 3)}`\n")
    tool = _tool()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"findings": [
        {"path": "src/gone.py", "line": 3}]}), encoding="utf-8")
    assert tool.main(["--root", str(root), "--ledger", str(ledger),
                      "--check"]) == 2


def test_a_vendored_or_hidden_checkout_is_not_scanned(tmp_path):
    root = _tree(tmp_path)
    # A vendored checkout is not part of the documented tree. The CI jobs that
    # run `backends/python/setup.sh` check `.cordis-py` out and the jobs that
    # do not run it do not, so a reference into it must be neither ok nor a
    # finding: otherwise the verdict, and the ledger entry that records it,
    # depend on which job ran. The same absent path under a normal directory
    # is still a finding.
    vendored = _ref("backends/python/.cordis-py/src/cordis/context.py", 74)
    plain = _ref("backends/python/src/cordis/context.py", 74)
    _write(root, "docs/page.md", f"vendored `{vendored}` and plain `{plain}`\n")
    # Nor is a file inside one scanned: its references are not the tree's.
    _write(root, "src/.venv/lib/parse.py", f"# sees {_ref('src/gone.py', 3)}\n")
    references, findings = _findings(root)
    assert [r.cited for r in references] == [
        "backends/python/src/cordis/context.py"]
    assert [(f.kind, f.cited) for f in findings] == [
        ("missing", "backends/python/src/cordis/context.py")]
    # A checkout that happens to exist does not turn the reference into an
    # anchor either, so the verdict stays the same either way.
    _write(root, "backends/python/.cordis-py/src/cordis/context.py",
           "one = 1\ntwo = 2\n" * 40)
    references, findings = _findings(root)
    assert [r.cited for r in references] == [
        "backends/python/src/cordis/context.py"]
    assert [(f.kind, f.cited) for f in findings] == [
        ("missing", "backends/python/src/cordis/context.py")]
    # A segment made only of dots is a relative step or an ellipsis shorthand,
    # not a hidden directory, and stays in the scan.
    _write(root, "docs/page.md", f"shorthand `{_ref('.../cordis/loader.py', 508)}`\n")
    references, findings = _findings(root)
    assert [(f.kind, f.cited) for f in findings] == [
        ("missing", ".../cordis/loader.py")]


def test_an_archived_document_is_a_frozen_quote_not_a_claim(tmp_path):
    root = _tree(tmp_path)
    # docs/roadmap-archive/ records what a file said at the time an item was
    # closed. A later change to the cited file must not turn the quote into a
    # red or an obsolete ledger entry, so archive documents are neither scanned
    # nor resolved; the same reference in live prose is still a finding.
    _write(root, "src/thing.py", "one = 1\n")
    quote = _ref("src/thing.py", 99)
    _write(root, "docs/roadmap-archive/11-old-item.md",
           f"then the file said `{quote}`\n")
    _write(root, "docs/live.md", f"see `{quote}` for the contract\n")
    references, findings = _findings(root)
    assert [r.source for r in references] == ["docs/live.md"]
    assert [(f.kind, f.cited) for f in findings] == [("stale", "src/thing.py")]


def test_the_committed_ledger_covers_the_real_tree():
    tool = _tool()
    report = tool.build_report(ROOT, tool.LEDGER)
    assert report.ledger_error is None, report.ledger_error
    # Vacuity first: a green verdict over an empty scan means nothing, and the
    # Python half must contribute references. If it stopped, the Markdown floor
    # alone would still pass while a block of ledger entries went obsolete.
    assert len(report.references) > 3000, len(report.references)
    assert len(report.findings) > 500, len(report.findings)
    assert any(r.source.endswith(".py") for r in report.references)
    assert any(r.source.endswith(".md") for r in report.references)
    assert report.new_findings == []
    assert report.obsolete == []
    assert {e.get("kind") for e in report.ledger} <= {"missing", "stale"}
    assert all(e["reason"].strip() for e in report.ledger)
