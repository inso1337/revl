"""Every rule in tools/check_roadmap_claims.py, seen to fail and seen to pass.

The discipline is the one tests/test_roadmap_gate_bites.py states: a gate
nobody has watched fail is not known to work, and a gate that reads prose with
regular expressions is the easiest kind to write so that it can never fire.

So each rule below gets a PAIR. One fixture is a stale claim, planted in the
shape the real roadmap uses — three of the four are reductions of sentences
that were actually stale on 2026-09-15. The other is the SAME fixture with the
one thing changed that should silence it, which pins the escape hatch as well:
what a writer has to do to satisfy the gate honestly, rather than by deleting
the sentence.

The last two tests run against the REAL roadmap and the REAL tree, because
every fixture here is synthetic and a collector whose regex stopped matching
the live document would still pass all of the pairs above.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_roadmap_claims as gate  # noqa: E402


# --------------------------------------------------------------------------
# A tiny tree, written to disk, because the checker resolves abbreviated paths
# by suffix over the real file list and that logic is worth exercising.
# --------------------------------------------------------------------------
@pytest.fixture
def tree(tmp_path: Path) -> gate.Tree:
    files = {
        "tests/test_streams.py": (
            "def test_source_only_program_still_emits():\n"
            "    assert True\n"
        ),
        "tests/test_selfhost_ir.py": (
            "EXTERN_DECL_GAP: dict = {}\n"
            "\n"
            "def test_corpus_agrees():\n"
            "    assert True\n"
        ),
        "src/revl/ownership.py": "def _v3_self_rebind_locals(body):\n    return {}\n",
        "backends/go/emit.py": "def _emit_go(ir):\n    return ''\n",
        "backends/typescript/runtime.ts": "export class StreamSource {}\n",
        "bench/codegen/typescript/runtime.ts": "// no Stream here\n",
        "selfhost/parser.rvl": "fn parse() -> Int { return 0 }\n",
        # `tools/` holds a tracked .py, so a bare `tools/*.py` citation is a
        # claim about this tree. `src/` holds only `src/revl/ownership.py` and
        # no file of its own, which is what tells `src/manifest.rvl` apart.
        "tools/docgen.py": "def main():\n    return 0\n",
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return gate.Tree(tmp_path, sorted(files))


def _findings(source: str, tree: gate.Tree, rule: str) -> list:
    _, findings, _ = gate.run(source, tree, [rule])
    return findings


def _one(source: str, tree: gate.Tree, rule: str) -> str:
    findings = _findings(source, tree, rule)
    assert len(findings) == 1, findings
    return findings[0][1]


# --------------------------------------------------------------------------
# (test) A cited pytest node that the tree no longer defines. This is item
# 416(a) as it reads on main today: the ts/java `Stream` refusal it landed was
# superseded when both tiers grew a real runtime, the test was deleted with it,
# and the "Reproducer + regression" sentence still names it.
# --------------------------------------------------------------------------
_TEST_BITES = (
    "416. (a) FIXED. Reproducer + regression: "
    "`tests/test_streams.py::test_source_only_program_is_refused` and "
    "`::test_source_only_program_still_emits`.\n"
)

def test_a_cited_pytest_node_that_no_longer_exists_bites(tree):
    assert "defines no test_source_only_program_is_refused" in _one(
        _TEST_BITES, tree, "test")


def test_a_cited_pytest_node_that_exists_passes(tree):
    source = ("416. (a) FIXED. Regression: "
              "`tests/test_streams.py::test_source_only_program_still_emits`.\n")
    assert _findings(source, tree, "test") == []


def test_a_bare_backticked_test_name_is_judged_too(tree):
    """The roadmap cites tests both ways and the bare form is the commoner."""
    assert "which the tree defines nowhere" in _one(
        "Locked by `test_an_await_body_reports_the_divergence`.\n", tree, "test")
    assert _findings("Locked by `test_corpus_agrees`.\n", tree, "test") == []


def test_a_bare_name_that_is_a_MODULE_passes(tree):
    """`test_selfhost_ir` names a file, not a function. The roadmap cites
    modules in the same backticks, so a module resolves the citation."""
    assert _findings("pinned as data in `test_selfhost_ir`.\n", tree, "test") == []


def test_module_shorthand_is_not_read_as_a_pytest_node(tree):
    """`lower.py::_link` is prose for "the `_link` function in lower.py". It is
    not a pytest node and is never judged as one, whether or not it resolves."""
    assert _findings("the sink is `lower.py::_link`.\n", tree, "test") == []


# --------------------------------------------------------------------------
# (symbol) A symbol cited at a file it has moved out of. This is item 434's
# `_v3_self_rebind_locals`, which item 445 lifted into a shared frontend module
# while four paragraphs kept pointing at the go emitter.
# --------------------------------------------------------------------------
_SYMBOL_BITES = (
    "434. (a) `_v3_self_rebind_locals` (`backends/go/emit.py`) decides UNIQUE "
    "OWNERSHIP over a whole function body.\n"
)

_SYMBOL_PASSES = _SYMBOL_BITES.replace("backends/go/emit.py", "src/revl/ownership.py")


def test_a_symbol_cited_at_the_wrong_file_bites(tree):
    assert "does not contain it" in _one(_SYMBOL_BITES, tree, "symbol")


def test_the_same_symbol_cited_at_its_real_home_passes(tree):
    assert _findings(_SYMBOL_PASSES, tree, "symbol") == []


def test_an_ambiguous_abbreviated_path_passes_if_any_candidate_carries_it(tree):
    """`typescript/runtime.ts` abbreviates two real files. Judging it against
    the wrong one would be a false positive, so any match satisfies it."""
    source = "F5's `StreamSource` in `typescript/runtime.ts` is the provider.\n"
    assert _findings(source, tree, "symbol") == []


def test_an_elided_path_is_not_judged_at_all(tree):
    """`go/.../bridge.go` elides a middle segment. No lookup can honestly
    expand it, so it is not counted in the denominator either."""
    source = "`seamFailure` in `go/.../bridge.go` redacts the argument.\n"
    denominator, findings, _ = gate.run(source, tree, ["symbol"])
    assert denominator["symbol"] == 0 and findings == []
    source = "the sink is `go/.../bridge.go:199`.\n"
    denominator, findings, _ = gate.run(source, tree, ["path"])
    assert denominator["path"] == 0 and findings == []


# --------------------------------------------------------------------------
# (path) A `path:line` citation whose file is gone.
# --------------------------------------------------------------------------
def test_a_file_line_citation_to_a_missing_file_bites(tree):
    assert "is not a file in the tree" in _one(
        "the choke point is `backends/go/bridge.go:199`.\n", tree, "path")


def test_a_file_line_citation_that_resolves_passes(tree):
    assert _findings("the choke point is `backends/go/emit.py:199`.\n",
                     tree, "path") == []


def test_a_drifted_line_number_is_not_a_finding(tree):
    """Line numbers move under every edit. A stale coordinate is not a false
    claim about the tree, and failing on one would fire on every commit."""
    assert _findings("`backends/go/emit.py:99999` is the sink.\n",
                     tree, "path") == []


# --------------------------------------------------------------------------
# (path, bare) A backticked path with NO line number. Issue #1233: until
# 2026-09-20 this shape was not collected at all, so the roadmap named
# `tools/gate_verdict_parity.py` as machinery "here" for a file that has never
# existed and the gate passed on it every day. The rule that replaced "do not
# look" asks whether the citation points into a directory this repository
# populates with files of that kind.
# --------------------------------------------------------------------------
_BARE_BITES = ("conformance and cross-tier divergence is "
               "`tools/gate_verdict_parity.py`; byte stability is elsewhere.\n")


def test_a_bare_backticked_path_into_a_populated_directory_bites(tree):
    """The issue-1233 sentence, reduced. `tools/` holds tracked `.py` files,
    so a `tools/*.py` citation is a claim about this tree."""
    assert "is not a file in the tree" in _one(_BARE_BITES, tree, "path")


def test_the_same_bare_path_passes_once_it_names_a_file_that_exists(tree):
    """The escape hatch: cite the machinery that does exist."""
    assert _findings(_BARE_BITES.replace("gate_verdict_parity", "docgen"),
                     tree, "path") == []


def test_a_hypothetical_user_project_path_is_not_judged(tree):
    """`src` carries `src/revl/ownership.py` and no file of its own, so
    `src/components/agent.rvl` and `src/manifest.rvl` name a directory this
    repository does not populate with `.rvl` files. Both are example user
    projects in the real roadmap."""
    for cited in ("src/components/agent.rvl", "src/manifest.rvl",
                  "root/app.rvl", "mtier/toolbox.rvl"):
        denominator, findings, _ = gate.run(
            "a workload lays out `%s`.\n" % cited, tree, ["path"])
        assert denominator["path"] == 0 and findings == [], cited


def test_a_reader_relative_path_is_not_judged(tree):
    """`./types.ts` and `../lib/x.rvl` are spelled relative to a reader sitting
    in some other project. Nothing in this repository is cited that way."""
    denominator, findings, _ = gate.run(
        "the generator writes `./types.ts` beside `../lib/x.rvl`.\n",
        tree, ["path"])
    assert denominator["path"] == 0 and findings == []


def test_a_bare_path_is_judged_on_its_extension_not_just_its_directory(tree):
    """`tools/` holds `.py` and, in this fixture, nothing else. A `.rvl` cited
    there is not a claim this rule can honestly resolve, because the tree gives
    no evidence that `tools/` is where such a file would live."""
    denominator, _, _ = gate.run("see `tools/plugin.rvl`.\n", tree, ["path"])
    assert denominator["path"] == 0
    denominator, _, _ = gate.run("see `tools/plugin.py`.\n", tree, ["path"])
    assert denominator["path"] == 1


def test_a_bare_path_inside_a_longer_backtick_run_is_not_a_citation(tree):
    """The collector anchors both backticks, which is what keeps `path:line`
    out of this shape and prose out of it entirely."""
    denominator, _, _ = gate.run(
        "run `python3 tools/gate_verdict_parity.py --check`.\n", tree, ["path"])
    assert denominator["path"] == 0


def test_a_citation_saying_the_file_never_existed_is_not_judged(tree):
    """The retrospective excision, in the form this rule needs. A roadmap that
    records a citation to a file which turned out never to have been written
    has to spell the file to say so, and that sentence is the last one a
    citation gate should red."""
    source = ("NOT the `tools/gate_verdict_parity.py` this item named when it "
              "was written: that file has never existed in this tree.\n")
    assert _findings(source, tree, "path") == []


def test_the_same_sentence_without_the_cue_IS_judged(tree):
    assert "is not a file in the tree" in _one(
        "the conformance component is `tools/gate_verdict_parity.py`.\n",
        tree, "path")


# --------------------------------------------------------------------------
# (absent) A scoped absence claim that the file refutes.
# --------------------------------------------------------------------------
def test_a_scoped_absence_claim_the_file_refutes_bites(tree):
    source = "`backends/typescript/runtime.ts` has no `StreamSource` at all.\n"
    assert "which contains it" in _one(source, tree, "absent")


def test_a_scoped_absence_claim_that_is_true_passes(tree):
    """Item 429's real sentence, and the control for this rule: the self-host
    parser genuinely carries no `spawn`."""
    source = "checked in the source: `selfhost/parser.rvl` has no `spawn` arm.\n"
    assert _findings(source, tree, "absent") == []


def test_an_UNSCOPED_absence_claim_is_never_judged(tree):
    """The rule this gate deliberately does not have. Measured on the real
    roadmap: 98 unscoped "no `X`" subjects, 58 identifier-shaped, and 54 of
    those 58 have a definition site — almost all of them correct sentences
    about the revl LANGUAGE, whose keywords a lexer necessarily names. A
    54-in-58 false-positive rate is a bigger tax than the one this gate
    removes, so absence is judged only where the sentence says absent FROM
    WHAT."""
    source = "There is no `spawn`, no `while`, and no `_v3_self_rebind_locals`.\n"
    denominator, findings, _ = gate.run(source, tree, ["absent"])
    assert denominator["absent"] == 0 and findings == []


# --------------------------------------------------------------------------
# The historical-citation excision, which is where a gate like this earns or
# loses its keep. `check_roadmap_markers.py` makes the same cut with
# RETROSPECTIVE_RE: a closed finding quotes its own original text, and
# punishing that teaches writers to delete the history.
# --------------------------------------------------------------------------
def test_a_citation_narrating_its_own_rename_is_not_judged(tree):
    source = ("`test_percent_is_escaped_for_string_format` is renamed to "
              "`test_percent_needs_no_escaping` and asserts the `%` rides "
              "through verbatim.\n")
    assert _findings(source, tree, "test") == []


def test_a_citation_narrating_its_own_deletion_is_not_judged(tree):
    source = ("The divergence pin `tests/test_streams.py::test_which_refusal_wins` "
              "was deleted in that PR, as its own instructions said to do.\n")
    assert _findings(source, tree, "test") == []


def test_the_same_citation_without_the_cue_IS_judged(tree):
    """The excision is a window around a cue, not a blanket amnesty for any
    sentence in the past tense."""
    source = ("The divergence pin `tests/test_streams.py::test_which_refusal_wins` "
              "anchors every recoverable refusal at the reference's line.\n")
    assert "defines no test_which_refusal_wins" in _one(source, tree, "test")


# --------------------------------------------------------------------------
# The allow-list.
# --------------------------------------------------------------------------
def test_an_allowlisted_claim_is_suppressed_and_an_unused_entry_is_reported(tree):
    entries = [
        {"rule": "symbol", "claim": "_v3_self_rebind_locals@backends/go/emit.py",
         "reason": "a reason"},
        {"rule": "symbol", "claim": "gone@nowhere/at/all.py", "reason": "a reason"},
    ]
    _, findings, unused = gate.run(_SYMBOL_BITES, tree, ["symbol"], entries)
    assert findings == []
    assert [e["claim"] for e in unused] == ["gone@nowhere/at/all.py"]


def test_an_allowlist_entry_without_a_reason_is_refused(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps({"entries": [{"rule": "path", "claim": "x.py"}]}))
    with pytest.raises(ValueError, match="written reason"):
        gate.load_allowlist(path)

    path.write_text(json.dumps(
        {"entries": [{"rule": "path", "claim": "x.py", "reason": "   "}]}))
    with pytest.raises(ValueError, match="empty reason"):
        gate.load_allowlist(path)


def test_the_shipped_allowlist_loads_and_every_entry_carries_a_reason():
    entries = gate.load_allowlist(gate.DEFAULT_ALLOWLIST)
    assert entries, "the shipped allow-list is empty; delete it or fill it"
    for entry in entries:
        assert entry["rule"] in gate.RULES
        assert len(entry["reason"].split()) >= 8, entry


# --------------------------------------------------------------------------
# Against the REAL document. Every fixture above is synthetic; a collector
# whose regex stopped matching the live roadmap would still pass all of them.
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real():
    tree = gate.Tree.from_git(ROOT)
    source = gate.DEFAULT_ROADMAP.read_text()
    return source, tree


def test_every_rule_finds_claims_in_the_real_roadmap(real):
    source, tree = real
    denominator, _, _ = gate.run(source, tree, gate.RULES)
    # Floors, not exact counts: the roadmap grows, and pinning the exact
    # number would red this test on every edit to a 1.4 MB document. They are
    # well under the 2026-09-15 reading (test 101, symbol 76, path 81,
    # absent 1) and well over zero, which is the failure this pins.
    assert denominator["test"] >= 40
    assert denominator["symbol"] >= 30
    assert denominator["path"] >= 250
    assert denominator["absent"] >= 1


def test_the_path_rule_judges_the_bulk_of_the_roadmap_s_bare_citations(real):
    """Issue #1233's measurement, turned into a floor.

    On 2026-09-20 the rule judged 408 path citations where the `path:line`
    shape alone had reached 79, and the bare paths it declines are the
    reader-relative and foreign-project spellings. The failure this pins is a
    regex that stops matching the live document and quietly returns the gate
    to judging 14 percent of its own citations, which is how it read for the
    five days before this test existed.
    """
    source, tree = real
    bare = [c for c in gate.collect_path_claims(source, tree)
            if not gate.PATH_LINE_RE.fullmatch(c.text.strip("`"))]
    assert len(bare) >= 250, len(bare)


def test_the_rule_declines_the_spellings_that_were_never_about_this_tree(real):
    """The other half of the same measurement. These are real roadmap
    citations: three example user projects, one sibling-repo layout and two
    reader-relative spellings. Each one resolves to nothing, and each one is
    correct prose. A rule that judged them would red CI on six true
    sentences."""
    source, tree = real
    for cited in ("root/app.rvl", "mtier/toolbox.rvl", "src/manifest.rvl",
                  "packages/host/plugin-inventory/src/index.ts",
                  "./types.ts", "../lib/x.rvl"):
        assert "`%s`" % cited in source, "citation moved; re-pick it: %s" % cited
        assert tree.resolve(cited) == [], cited
        assert not gate._cites_this_tree(cited, tree), cited


def test_renaming_a_cited_file_in_the_TREE_reds_the_gate(real):
    """The gate seen to fail for the reason it exists, against the live
    document, with a CONTROL rule that must not move.

    The mutation is on the TREE side rather than the document side: every
    other test here plants a bad sentence, and a gate can pass those while
    being blind to the change that actually happens, which is a file getting
    renamed under a citation nobody re-read.
    """
    source, tree = real
    allow = gate.load_allowlist(gate.DEFAULT_ALLOWLIST)
    # A citation the roadmap makes in the BARE shape, resolving to exactly one
    # file. Picked from the document rather than hard-coded, so an edit to any
    # one sentence re-picks instead of reddening.
    bare = [c.key for c in gate.collect_path_claims(source, tree)
            if not gate.PATH_LINE_RE.fullmatch(c.text.strip("`"))]
    cited = next(k for k in bare if tree.resolve(k) == [k])
    assert "`%s`" % cited in source and cited in tree.files

    def count(t, rules):
        _, findings, _ = gate.run(source, t, rules, allow)
        return len(findings)

    renamed = gate.Tree(ROOT, [f for f in tree.files if f != cited]
                        + [cited.replace(".", "_renamed.", 1)])
    assert count(renamed, ["path"]) > count(tree, ["path"]), (
        "renaming %s did not red the path rule" % cited)
    # The control: a rule that has nothing to do with the rename reads the
    # same on both trees. Without it, "the gate went red" proves only that
    # something went red.
    assert count(renamed, ["absent"]) == count(tree, ["absent"])
    assert count(renamed, ["test"]) == count(tree, ["test"])


def test_the_issue_1233_sentence_would_have_been_caught_when_it_was_written(real):
    """The regression this rule exists to prevent, quoted from item 536 as it
    read before the correction: a tool named as machinery `here` that has never
    existed in this tree, inside a sentence that claims all eight reward
    components are already built."""
    _, tree = real
    sentence = (
        "All eight components already have machinery here, and the mapping is "
        "the point rather than a formality: compiles is the crate build and "
        "the six-tier matrix; tests is the affected suite; conformance and "
        "cross-tier divergence is `tools/gate_verdict_parity.py`; byte "
        "stability of unrelated goldens is `tools/regen_goldens.py`.\n")
    _, findings, _ = gate.run(sentence, tree, gate.RULES,
                              gate.load_allowlist(gate.DEFAULT_ALLOWLIST))
    assert [c.key for c, _ in findings] == ["tools/gate_verdict_parity.py"]
    # `tools/regen_goldens.py`, cited in the same clause, resolves. The gate
    # separates the two halves of one sentence, which is the whole claim.
    assert tree.resolve("tools/regen_goldens.py")


def test_the_shipped_allowlist_still_matches_something(real):
    """An allow-list entry that stops matching has outlived its reason, which
    is the failure mode an allow-list has."""
    source, tree = real
    _, _, unused = gate.run(source, tree, gate.RULES,
                            gate.load_allowlist(gate.DEFAULT_ALLOWLIST))
    assert unused == [], "allow-list entries matching nothing: %s" % (
        [e["claim"] for e in unused],)


def test_a_stale_claim_planted_in_the_REAL_roadmap_is_caught(real):
    """Mutation evidence, against the live document and the live tree.

    The control is the roadmap as it stands, restricted to the citations this
    repo owns (the allow-list carries the three that name a sibling project).
    Each planted edit takes one sentence that currently RESOLVES and breaks it
    the way the real staleness broke: a test renamed out from under its
    citation, a function moved to another module, a file deleted.
    """
    source, tree = real
    allow = gate.load_allowlist(gate.DEFAULT_ALLOWLIST)

    def count(text):
        _, findings, _ = gate.run(text, tree, gate.RULES, allow)
        return len(findings)

    baseline = count(source)

    plants = [
        # test: a node id whose function was renamed out from under it.
        ("tests/test_gate_crate_drift.py::test_the_crate_is_admit_only_and_says_so",
         "tests/test_gate_crate_drift.py::test_the_crate_is_admit_only"),
        # symbol: a constant that moved to another module.
        ("`ADMITTED_LAYER` (`crates/revl-gate/src/lib.rs:295`)",
         "`ADMITTED_LAYER` (`crates/revl-gate/src/session.rs:295`)"),
        # path: a file:line whose file is gone.
        ("`crates/revl-gate/src/session.rs:1`",
         "`crates/revl-gate/src/layer2.rs:1`"),
        # path, bare: the shape issue #1233 added. No line number, nothing
        # around it but backticks, and a directory this repository populates.
        ("`tools/regen_goldens.py`", "`tools/regen_goldens_v2.py`"),
    ]
    for old, new in plants:
        assert old in source, "the control sentence moved; re-pick it: %r" % old
        assert count(source.replace(old, new, 1)) > baseline, (
            "planting %r did not produce a finding" % (new,))
