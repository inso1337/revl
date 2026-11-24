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


# ---------------------------------------------- witnesses pytest really runs

ROOT = Path(__file__).resolve().parents[1]


def _witness_findings(root: Path, target: str) -> list[Finding]:
    return check([ClaimSite("docs/d.md", 1, "ok")], {"ok": Witness("test", target)},
                 root)


def _test_file(tmp_path: Path, name: str, body: str) -> Path:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_a_parameter_the_real_parametrize_does_not_produce_is_refused():
    # the function exists; the parameter id does not, so running the
    # registered node would run nothing
    findings = _witness_findings(
        ROOT, "tests/test_frontend.py::test_rejection[no_such_file.rvl]")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
    assert "not one of its" in findings[0].detail


def test_the_registered_rejection_ids_are_real_parameter_ids():
    for name in ("g4_emission_widens_declared_capability.rvl",
                 "g9_service_return_launders_taint.rvl",
                 "gsecret_service_return_discloses.rvl"):
        assert _witness_findings(
            ROOT, f"tests/test_frontend.py::test_rejection[{name}]") == [], name


_PARAMETRIZED = '''import pytest

CASES = {"a.rvl": "x", "b.rvl": "y"}


@pytest.mark.parametrize("name,text", sorted(CASES.items()), ids=sorted(CASES))
def test_named(name, text):
    assert name


@pytest.mark.parametrize("name,text", [("a", "x"), ("b", "y")])
def test_default(name, text):
    assert name


@pytest.mark.parametrize("n", [1, 2])
@pytest.mark.parametrize("m", [3])
def test_stacked(n, m):
    assert n


def test_plain():
    assert True
'''


@pytest.mark.parametrize("node,ok", [
    ("test_named[a.rvl]", True),
    ("test_named[c.rvl]", False),
    ("test_default[a-x]", True),
    ("test_default[a]", False),
    ("test_stacked[3-1]", False),     # stacked ids are not read: refused
    ("test_stacked", True),
    ("test_plain[a]", False),         # not parametrized at all
    ("test_named", True),
])
def test_a_bracketed_witness_is_checked_against_the_parameter_ids(
        tmp_path, node, ok):
    root = _test_file(tmp_path, "test_p.py", _PARAMETRIZED)
    findings = _witness_findings(root, f"tests/test_p.py::{node}")
    assert (findings == []) is ok, findings


def test_a_parametrized_fixture_makes_a_bracketed_witness_unreadable(tmp_path):
    root = _test_file(tmp_path, "test_p.py", _PARAMETRIZED)
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture(params=[1, 2])\ndef n(request):\n"
        "    return request.param\n", encoding="utf-8")
    findings = _witness_findings(root, "tests/test_p.py::test_named[a.rvl]")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


def test_a_mutated_module_literal_makes_the_ids_unreadable(tmp_path):
    root = _test_file(tmp_path, "test_p.py", _PARAMETRIZED + 'CASES["c.rvl"] = "z"\n')
    findings = _witness_findings(root, "tests/test_p.py::test_named[a.rvl]")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
    assert "cannot be read statically" in findings[0].detail


def test_a_test_function_in_a_file_pytest_does_not_collect_is_refused(tmp_path):
    root = _test_file(tmp_path, "helper_probe.py",
                      "def test_never_collected():\n    pass\n")
    for target in ("tests/helper_probe.py::test_never_collected",
                   "tests/helper_probe.py"):
        findings = _witness_findings(root, target)
        assert [finding.code for finding in findings] == ["claim-witness-missing"]
        assert "not collected by pytest" in findings[0].detail


def test_a_suffix_test_file_is_collected(tmp_path):
    root = _test_file(tmp_path, "probe_test.py", "def test_x():\n    pass\n")
    assert _witness_findings(root, "tests/probe_test.py::test_x") == []


@pytest.mark.parametrize("body,target", [
    ("import pytest\n@pytest.mark.skip(reason='x')\ndef test_s():\n    pass\n",
     "test_s"),
    ("import pytest\n@pytest.mark.skip\ndef test_s():\n    pass\n", "test_s"),
    ("import pytest\n@pytest.mark.skip\nclass TestK:\n    def test_s(self):\n"
     "        pass\n", "TestK::test_s"),
    ("import pytest\npytestmark = pytest.mark.skip\ndef test_s():\n    pass\n",
     "test_s"),
    ("import pytest\npytestmark = [pytest.mark.skip(reason='x')]\n"
     "def test_s():\n    pass\n", "test_s"),
    ("import pytest\npytest.skip('x', allow_module_level=True)\n"
     "def test_s():\n    pass\n", "test_s"),
    ("import pytest\n@pytest.mark.skip\ndef test_s():\n    pass\n", None),
], ids=["mark-call", "mark-bare", "class-mark", "pytestmark", "pytestmark-list",
        "module-skip", "file-witness"])
def test_an_unconditionally_skipped_witness_is_refused(tmp_path, body, target):
    root = _test_file(tmp_path, "test_s.py", body)
    node = "tests/test_s.py" + (f"::{target}" if target else "")
    findings = _witness_findings(root, node)
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
    assert "skipped" in findings[0].detail or "no test function that runs" \
        in findings[0].detail


def test_a_conditional_skip_is_not_decided(tmp_path):
    # the documented limit: a `skipif` condition is evaluated only by pytest
    root = _test_file(
        tmp_path, "test_s.py",
        "import sys\nimport pytest\n@pytest.mark.skipif(sys.platform == 'x', "
        "reason='x')\ndef test_s():\n    pass\n")
    assert _witness_findings(root, "tests/test_s.py::test_s") == []


# ----------------------------------------------------- inline code in Markdown

def test_markdown_inline_code_syntax_documentation_is_not_a_claim():
    text = ("Inline: `<!-- revl:claim ghost -->` is how you write one.\n"
            "Double: `` <!-- revl:claim ghost2 --> `` too.\n")
    assert parse_markdown_claims("d.md", text) == []


def test_markdown_comment_after_a_code_span_is_still_a_claim():
    text = "Use `x` here.\n<!-- revl:claim real -->\n"
    assert parse_markdown_claims("d.md", text) == [ClaimSite("d.md", 2, "real")]


def test_markdown_backtick_inside_a_comment_does_not_hide_it():
    text = "<!-- the `x` path; revl:claim real -->\nand a `span`\n"
    assert [s.id for s in parse_markdown_claims("d.md", text)] == ["real"]


def test_markdown_unclosed_backtick_is_literal():
    text = "a stray ` tick\n\n<!-- revl:claim real -->\n"
    assert [s.id for s in parse_markdown_claims("d.md", text)] == ["real"]


# ------------------------------------------------ entries no annotation names

def test_a_registry_entry_no_annotation_names_is_still_resolved(tmp_path):
    root = _good_tree(tmp_path)
    registry = {
        "named": Witness("test", "tests/test_a.py"),
        "orphan-dead": Witness("test", "tests/gone.py::test_gone"),
        "orphan-live": Witness("test", "tests/test_a.py::test_x"),
    }
    findings = check([ClaimSite("src/a.rvl", 1, "named")], registry, root)
    assert [(f.code, f.where) for f in findings] == \
        [("claim-witness-missing", "claims.toml")]
    assert "orphan-dead" in findings[0].detail


def test_an_entry_on_an_annotation_s_chain_is_reported_once(tmp_path):
    root = _good_tree(tmp_path)
    registry = {"a": Witness("claim", "b"), "b": Witness("test", "tests/gone.py")}
    findings = check([ClaimSite("src/a.rvl", 1, "a")], registry, root)
    assert [(f.code, f.where) for f in findings] == \
        [("claim-witness-missing", "src/a.rvl:1")]


# ------------------------------------- fail closed on marks it cannot read

_GOOD_FN = "def test_ok():\n    assert True\n"


@pytest.mark.parametrize("body,target", [
    ("import pytest as pt\n@pt.mark.skip\n" + _GOOD_FN, "test_ok"),
    ("from pytest import mark as m\n@m.skip\n" + _GOOD_FN, "test_ok"),
    ("import pytest\nskip = pytest.mark.skip\n@skip\n" + _GOOD_FN, "test_ok"),
    ("import pytest\nclass TestK:\n    pytestmark = pytest.mark.skip\n"
     "    def test_ok(self):\n        pass\n", "TestK::test_ok"),
    ("import pytest\n@pytest.mark.xfail(run=False)\n" + _GOOD_FN, "test_ok"),
    ("import functools\n@functools.lru_cache\n" + _GOOD_FN, "test_ok"),
    ("import pytest as pt\npytestmark = pt.mark.skip\n" + _GOOD_FN, "test_ok"),
], ids=["aliased-module", "aliased-mark", "mark-in-a-variable",
        "class-pytestmark-skip", "xfail-run-false", "other-decorator",
        "aliased-pytestmark"])
def test_a_skip_or_mark_the_check_cannot_read_refuses_the_witness(
        tmp_path, body, target):
    root = _test_file(tmp_path, "test_s.py", body)
    for node in (f"tests/test_s.py::{target}", "tests/test_s.py"):
        findings = _witness_findings(root, node)
        assert [finding.code for finding in findings] == \
            ["claim-witness-missing"], node


def test_literal_non_skipping_marks_are_accepted(tmp_path):
    root = _test_file(
        tmp_path, "test_s.py",
        "import pytest\npytestmark = [pytest.mark.slow]\n"
        "@pytest.mark.xfail(run=True)\n@pytest.mark.timeout(5)\n" + _GOOD_FN)
    assert _witness_findings(root, "tests/test_s.py::test_ok") == []


@pytest.mark.parametrize("body,node", [
    ("import pytest\nclass TestK:\n    pytestmark = pytest.mark.parametrize("
     "'y', [1, 2])\n    @pytest.mark.parametrize('x', ['a'])\n"
     "    def test_p(self, x, y):\n        pass\n", "TestK::test_p[a]"),
    ("import pytest\nimport pytest as pt\n@pt.mark.parametrize('y', [1])\n"
     "@pytest.mark.parametrize('x', ['a'])\ndef test_p(x, y):\n    pass\n",
     "test_p[a]"),
    ("import pytest\n@pytest.mark.parametrize('y', [1, 2])\nclass TestK:\n"
     "    @pytest.mark.parametrize('x', ['a'])\n"
     "    def test_p(self, x, y):\n        pass\n", "TestK::test_p[a]"),
], ids=["class-pytestmark", "aliased-stacked", "class-decorator"])
def test_a_parametrization_not_on_the_function_refuses_a_bracketed_witness(
        tmp_path, body, node):
    root = _test_file(tmp_path, "test_p.py", body)
    findings = _witness_findings(root, f"tests/test_p.py::{node}")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]


@pytest.mark.parametrize("decorator", [
    "@pytest.mark.parametrize('x', 5)",
    "@pytest.mark.parametrize(5, ['a'])",
    "@pytest.mark.parametrize('x', ['a'], ids=sorted(5))",
    "@pytest.mark.parametrize('x', sorted([1, 'a']))",
], ids=["argvalues-int", "argnames-int", "ids-sorted-int", "sorted-mixed"])
def test_a_malformed_parametrize_is_unreadable_not_a_crash(tmp_path, decorator):
    root = _test_file(tmp_path, "test_p.py",
                      f"import pytest\n{decorator}\ndef test_p(x):\n    pass\n")
    findings = _witness_findings(root, "tests/test_p.py::test_p[a]")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
    assert "cannot be read statically" in findings[0].detail


@pytest.mark.parametrize("text", [
    "Use the ` key.\n<!-- revl:claim real -->\nThe `x` thing.\n",
    "# A ` tick\n<!-- revl:claim real -->\nmore ` text\n",
    "- a ` tick\n- <!-- revl:claim real -->\n- b ` tick\n",
], ids=["paragraph-line", "heading", "list-items"])
def test_an_odd_backtick_does_not_hide_a_comment_that_opens_a_block(text):
    assert [s.id for s in parse_markdown_claims("d.md", text)] == ["real"]


def test_a_code_span_may_still_cross_a_plain_line_break():
    text = "a `<!-- revl:claim ghost\n--> still code` here\n"
    assert parse_markdown_claims("d.md", text) == []


@pytest.mark.parametrize("body,extra", [
    ("import pytest\nclass Base:\n    pytestmark = pytest.mark.skip\n"
     "class TestK(Base):\n    def test_ok(self):\n        pass\n", None),
    ("import pytest\n@pytest.mark.skip\nclass Base:\n    pass\n"
     "class TestK(Base):\n    def test_ok(self):\n        pass\n", None),
    ("from tests.helper import Base\nclass TestK(Base):\n"
     "    def test_ok(self):\n        pass\n",
     "import pytest\n@pytest.mark.skip\nclass Base:\n    pass\n"),
], ids=["base-pytestmark", "base-decorator", "imported-base"])
def test_a_witness_class_with_a_base_that_can_carry_marks_is_refused(
        tmp_path, body, extra):
    root = _test_file(tmp_path, "test_k.py", body)
    if extra is not None:
        (root / "tests" / "helper.py").write_text(extra, encoding="utf-8")
    findings = _witness_findings(root, "tests/test_k.py::TestK::test_ok")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
    assert "inherits from" in findings[0].detail


def test_a_unittest_or_object_base_is_read(tmp_path):
    root = _test_file(
        tmp_path, "test_k.py",
        "import unittest\nclass TestK(unittest.TestCase):\n"
        "    def test_ok(self):\n        pass\nclass TestJ(object):\n"
        "    def test_ok(self):\n        pass\n")
    for node in ("TestK::test_ok", "TestJ::test_ok"):
        assert _witness_findings(root, f"tests/test_k.py::{node}") == [], node


@pytest.mark.parametrize("body", [
    "import pytest\npytestmark = []\npytestmark.append(pytest.mark.skip)\n",
    "import pytest\nglobals()['pytestmark'] = pytest.mark.skip\n",
    "import pytest\npytestmark = []\npytestmark += [pytest.mark.skip]\n",
    "import pytest\n@pytest.mark.xfail(**{'run': False})\n",
], ids=["append", "globals", "augmented", "star-star-keywords"])
def test_a_pytestmark_reference_or_mark_keywords_it_cannot_read_refuse(
        tmp_path, body):
    root = _test_file(tmp_path, "test_s.py", body + _GOOD_FN)
    findings = _witness_findings(root, "tests/test_s.py::test_ok")
    assert [finding.code for finding in findings] == ["claim-witness-missing"]
