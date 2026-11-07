"""`revl:claim` parsing, the registry and the named findings (issue #2846).

The repository gate that adds the affected selection is
`tests/test_claims_gate.py`; this module holds the compiler-side rules that need
no tree walk, so a parser or registry regression names itself instead of
appearing as a red gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from revl.claims import (
    ClaimSite,
    Finding,
    RegistryError,
    Witness,
    check,
    discover,
    load_registry,
    parse_markdown_claims,
    parse_rvl_claims,
    witness_selected,
)


# --------------------------------------------------------------- .rvl comments

def test_rvl_claim_is_parsed_from_a_line_comment():
    sites = parse_rvl_claims("a.rvl", "// revl:claim sidecar-no-read\nfn f() {}\n")
    assert sites == [ClaimSite("a.rvl", 1, "sidecar-no-read")]


def test_rvl_claim_can_sit_after_other_comment_text():
    sites = parse_rvl_claims(
        "a.rvl", "// the path reads no bytes; revl:claim sidecar-no-read\n")
    assert sites[0].id == "sidecar-no-read"


def test_rvl_claim_inside_a_string_or_template_is_not_a_comment():
    text = (
        'fn f() -> Str { return "// revl:claim ghost" }\n'
        "fn g() -> Str { return `// revl:claim ghost` }\n"
        'fn h() -> Str { return """// revl:claim ghost""" }\n'
    )
    assert parse_rvl_claims("a.rvl", text) == []


def test_rvl_claim_inside_a_single_quoted_string_is_not_a_comment():
    text = "fn f() -> Str { return '// revl:claim ghost' }\n// revl:claim real\n"
    assert [site.id for site in parse_rvl_claims("a.rvl", text)] == ["real"]


def test_an_unmatched_quote_does_not_hide_a_later_comment():
    # a host-body apostrophe opens a one-line string in the raw scan; the
    # single-line string state must not carry over to the next line.
    text = "@py { # don't }\n// revl:claim real\n"
    assert [site.id for site in parse_rvl_claims("a.rvl", text)] == ["real"]


def test_rvl_claim_after_a_triple_quoted_string_spanning_lines_is_found():
    text = 'fn f() -> Str { return """line\n// revl:claim ghost\n""" }\n' \
           "// revl:claim real\n"
    assert [site.id for site in parse_rvl_claims("a.rvl", text)] == ["real"]


def test_rvl_claim_without_an_id_is_malformed():
    sites = parse_rvl_claims("a.rvl", "// revl:claim\nfn f() {}\n")
    assert sites == [ClaimSite("a.rvl", 1, None)]


def test_rvl_mention_that_is_not_the_annotation_is_prose():
    sites = parse_rvl_claims("a.rvl", "// revl:claims are documented elsewhere\n")
    assert sites == []


# -------------------------------------------------------------- Markdown docs

def test_markdown_claim_is_parsed_from_an_html_comment():
    sites = parse_markdown_claims(
        "d.md", "intro\n<!-- revl:claim table-generated -->\ntext\n")
    assert sites == [ClaimSite("d.md", 2, "table-generated")]


def test_markdown_fenced_syntax_documentation_is_not_a_claim():
    text = "```md\n<!-- revl:claim ghost -->\n```\n"
    assert parse_markdown_claims("d.md", text) == []


def test_markdown_prose_that_names_the_annotation_is_not_a_claim():
    assert parse_markdown_claims("d.md", "Write `revl:claim <id>` in a comment.\n") == []


def test_markdown_html_comment_without_an_id_is_malformed():
    assert parse_markdown_claims("d.md", "<!-- revl:claim -->\n") == \
        [ClaimSite("d.md", 1, None)]


# ------------------------------------------------------------------- registry

def _registry(tmp_path: Path, body: str):
    path = tmp_path / "claims.toml"
    path.write_text(body, encoding="utf-8")
    return load_registry(path)


def test_registry_decodes_every_witness_kind(tmp_path):
    registry = _registry(
        tmp_path,
        '[claims]\n'
        'a = "test:tests/a.py"\n'
        'b = "test:tests/a.py::test_x[p]"\n'
        'c = "docgen:guarantees"\n'
        'd = "claim:a"\n')
    assert registry == {
        "a": Witness("test", "tests/a.py"),
        "b": Witness("test", "tests/a.py::test_x[p]"),
        "c": Witness("docgen", "guarantees"),
        "d": Witness("claim", "a"),
    }


@pytest.mark.parametrize("value", [
    '"somewhere"',
    '"witness:tests/a.py"',
    '"test:"',
])
def test_registry_refuses_a_value_it_cannot_judge(tmp_path, value):
    with pytest.raises(RegistryError):
        _registry(tmp_path, f'[claims]\na = {value}\n')


def test_registry_refuses_a_file_without_a_claims_table(tmp_path):
    with pytest.raises(RegistryError):
        _registry(tmp_path, '[not-claims]\na = "test:tests/a.py"\n')


def test_registry_refuses_a_non_string_value(tmp_path):
    with pytest.raises(RegistryError):
        _registry(tmp_path, "[claims]\na = 1\n")


# ------------------------------------------------------------------ discovery

def test_discover_reads_rvl_and_markdown_and_skips_vcs_caches(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rvl").write_text("// revl:claim a\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text("<!-- revl:claim b -->\n",
                                            encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "d.md").write_text("<!-- revl:claim ghost -->\n",
                                            encoding="utf-8")
    assert sorted(site.id for site in discover(tmp_path)) == ["a", "b"]


# -------------------------------------------------------------------- findings

def _good_tree(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_a_resolvable_claim_with_a_selected_witness_is_clean(tmp_path):
    root = _good_tree(tmp_path)
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py::test_x")},
        root, selected=lambda _path: {"tests/test_a.py"})
    assert findings == []


def test_an_unknown_id_is_a_named_failure(tmp_path):
    findings = check([ClaimSite("src/a.rvl", 3, "gone")], {}, tmp_path)
    assert [finding.code for finding in findings] == ["claim-unknown-id"]
    assert findings[0].where == "src/a.rvl:3"


def test_a_missing_test_file_is_a_named_failure(tmp_path):
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/gone.py")}, tmp_path)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_renamed_test_function_is_a_named_failure(tmp_path):
    root = _good_tree(tmp_path)
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py::test_renamed")}, root)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_missing_docgen_block_is_a_named_failure(tmp_path):
    findings = check(
        [ClaimSite("docs/d.md", 1, "ok")],
        {"ok": Witness("docgen", "not-a-block")}, tmp_path,
        docgen_blocks=frozenset({"guarantees"}))
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_test_file_with_no_test_function_is_a_failure(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("VALUE = 1\n", encoding="utf-8")
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py")}, tmp_path)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_non_python_test_witness_is_a_failure(tmp_path):
    (tmp_path / "README.md").write_text("not a test\n", encoding="utf-8")
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "README.md")}, tmp_path)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_class_node_witness_resolves(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "class TestK:\n    def test_y(self):\n        assert True\n",
        encoding="utf-8")
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py::TestK::test_y")}, tmp_path)
    assert findings == []


def test_a_non_test_function_node_is_a_failure(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def helper():\n    assert True\n", encoding="utf-8")
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py::helper")}, tmp_path)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_docgen_block_witness_resolves(tmp_path):
    findings = check(
        [ClaimSite("docs/d.md", 1, "ok")],
        {"ok": Witness("docgen", "guarantees")}, tmp_path,
        docgen_blocks=frozenset({"guarantees"}))
    assert findings == []


def test_a_claim_chain_resolves_and_a_cycle_is_a_failure(tmp_path):
    root = _good_tree(tmp_path)
    chain = {
        "a": Witness("claim", "b"),
        "b": Witness("test", "tests/test_a.py"),
    }
    assert check([ClaimSite("src/a.rvl", 1, "a")], chain, root) == []
    cycle = {"a": Witness("claim", "b"), "b": Witness("claim", "a")}
    findings = check([ClaimSite("src/a.rvl", 1, "a")], cycle, root)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_witness_dropped_from_the_selection_is_a_named_failure(tmp_path):
    root = _good_tree(tmp_path)
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py")}, root,
        selected=lambda _path: {"tests/test_other.py"})
    assert [finding.code for finding in findings] == ["claim-witness-not-selected"]


def test_a_claim_chain_to_a_test_witness_is_selection_checked(tmp_path):
    root = _good_tree(tmp_path)
    chain = {"a": Witness("claim", "b"), "b": Witness("test", "tests/test_a.py")}
    findings = check(
        [ClaimSite("src/a.rvl", 1, "a")], chain, root,
        selected=lambda _path: set())
    assert [finding.code for finding in findings] == ["claim-witness-not-selected"]


def test_a_full_selection_runs_every_witness(tmp_path):
    root = _good_tree(tmp_path)
    findings = check(
        [ClaimSite("src/a.rvl", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py")}, root,
        selected=lambda _path: None)
    assert findings == []


def test_selection_is_not_judged_for_a_markdown_claim(tmp_path):
    root = _good_tree(tmp_path)
    findings = check(
        [ClaimSite("docs/d.md", 1, "ok")],
        {"ok": Witness("test", "tests/test_a.py")}, root,
        selected=lambda _path: set())
    assert findings == []


@pytest.mark.parametrize("target,selected,expected", [
    ("tests/test_a.py", {"tests/test_a.py"}, True),
    ("tests/test_a.py::test_x", {"tests/test_a.py"}, True),
    ("tests/test_a.py::test_x[p]", {"tests/test_a.py::test_x"}, True),
    ("tests/test_a.py::test_x[p]", {"tests/test_a.py::test_y"}, False),
    ("tests/test_a.py::test_x", {"tests/"}, True),
    ("tests/test_a.py::test_x", {"tests/test_b.py"}, False),
])
def test_witness_selected_matches_a_file_a_bare_node_and_a_prefix(
        target, selected, expected):
    assert witness_selected(target, selected) is expected


def test_finding_renders_its_code_and_site():
    assert str(Finding("claim-unknown-id", "a.rvl:2", "no entry")) == \
        "claim-unknown-id: a.rvl:2: no entry"
