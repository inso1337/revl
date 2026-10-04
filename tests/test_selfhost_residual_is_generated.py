"""The self-host residual is one number, generated, and gated (issue #1300).

The tree stated the residual in four places and gave three different answers:
`LOWER_GAP_DOCS` in `tests/test_selfhost_compile.py` measured 41 of 259,
`docs/v2.0-roadmap.md` item 146 said 59, and `docs/selfhost-compile.md` and
`docs/selfhost-findings.md` both said 63. Every prose figure was typed by hand
from a measurement taken on a day that had passed, which is the fifth instance
of that family found in this repository this month.

Correcting three numbers would have restarted the same clock, so the prose is
rendered from the ledger by `tools/docgen.py` and this module is what keeps it
that way. It holds three claims:

  * the committed blocks equal a fresh render (the drift gate `docgen --check`
    makes, made again HERE because `docgen --check` runs in the `frontend` job
    and a documentation-only pull request skips that job -- exactly the diff
    that moves a document. `tools/affected_tests.py` selects this module for
    any `.md` change, and the `root-suite-affected` job is ungated, so the
    check runs on the diff it is for);
  * a residual figure typed anywhere else in `docs/` that disagrees with the
    ledger is a RED, not a discovery;
  * both of those are SEEN TO FAIL, on a synthetic tree, one perturbation at a
    time. A gate nobody has watched fail is not known to work, which is the
    discipline `tests/test_check_vision_claims.py` states and the reason the
    three stale numbers survived beside a green suite for as long as they did.

The last claim is also the answer to "what happens when the residual moves".
`test_the_block_follows_the_ledger_with_no_hand_edit` changes only the ledger
and reads the new figure out of the rendered block: an open pull request that
closes gaps needs `make docs-gen` and no knowledge of what the number became.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import docgen  # noqa: E402

TIERS = ("py", "ts", "go", "java", "rust", "wasm")

# A stand-in tree: one ledger, six corpora, one document. Small enough to state
# the whole expected rendering in a test, so every assertion below pins the
# RULE rather than the repository's current residual.
_LEDGER = '''
LOWER_GAP_DOCS: dict[str, tuple[str, ...]] = {
    "py": ("a.rvl", "b.rvl"),
    "ts": ("c.rvl",),
    "go": (),
    "java": (),
    "rust": (),
    "wasm": (),
}
'''
_CORPUS = {
    "py": ["a.rvl", "b.rvl", "c.rvl", "d.rvl"],
    "ts": ["c.rvl", "d.rvl"],
    "go": ["e.rvl"],
    "java": ["f.rvl"],
    "rust": ["g.rvl"],
    "wasm": ["h.rvl"],
}


def _tree(tmp_path: Path, ledger: str = _LEDGER, corpus=None) -> Path:
    corpus = corpus or _CORPUS
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_selfhost_compile.py").write_text(
        ledger, encoding="utf-8")
    for tier, docs in corpus.items():
        (tmp_path / "tests" / f"test_selfhost_emit_{tier}.py").write_text(
            f"CORPUS = {docs!r}\n", encoding="utf-8")
    return tmp_path


def _doc(tree: Path, body: str, name: str = "selfhost-compile.md") -> None:
    (tree / "docs" / name).write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------
# The measurement itself, on the stand-in tree.
# --------------------------------------------------------------------------
def test_the_residual_is_counted_per_tier_from_the_two_committed_tables(tmp_path):
    tree = _tree(tmp_path)
    assert docgen.selfhost_residual(tree) == [
        ("py", 4, 2), ("ts", 2, 1), ("go", 1, 0),
        ("java", 1, 0), ("rust", 1, 0), ("wasm", 1, 0),
    ]
    assert docgen.residual_totals(tree) == (10, 3)


def test_a_tier_missing_from_the_ledger_is_a_loud_failure(tmp_path):
    """Absent must not read as zero. A tier that drops out of the ledger would
    otherwise shrink the residual silently, which is the one direction a
    residual figure must never move on its own."""
    tree = _tree(tmp_path, ledger=_LEDGER.replace('"wasm": (),', ""))
    with pytest.raises(docgen.SelfhostResidualError) as exc:
        docgen.selfhost_residual(tree)
    assert "wasm" in str(exc.value)


def test_a_residual_document_outside_its_corpus_is_a_loud_failure(tmp_path):
    """The residual is a fraction of the corpus only while the two tables
    enumerate the same documents. A typo in a path would otherwise render a
    perfectly well-formed, wrong percentage."""
    tree = _tree(tmp_path, ledger=_LEDGER.replace('"a.rvl"', '"zz.rvl"'))
    with pytest.raises(docgen.SelfhostResidualError) as exc:
        docgen.selfhost_residual(tree)
    assert "zz.rvl" in str(exc.value)


def test_a_ledger_tier_the_table_does_not_print_is_a_loud_failure(tmp_path):
    tree = _tree(tmp_path, ledger=_LEDGER.replace(
        '"wasm": (),', '"wasm": (), "zig": ("q.rvl",),'))
    with pytest.raises(docgen.SelfhostResidualError) as exc:
        docgen.selfhost_residual(tree)
    assert "zig" in str(exc.value)


# --------------------------------------------------------------------------
# The generated block.
# --------------------------------------------------------------------------
def test_the_report_states_the_total_and_every_tier(tmp_path):
    """The counts, one command away (`docgen --show selfhost-residual`)."""
    body = docgen.residual_report(root=_tree(tmp_path))
    assert "| **total** | **10** | **10 (100%)** | **7 (70.0%)** |" in body
    assert "| py   |      4 |                    4 (100%) |              2 (50.0%) |" in body
    assert "all 3 residual documents" in body


def test_the_report_follows_the_ledger_with_no_hand_edit(tmp_path):
    """The point of the exercise. An open branch that closes gaps changes the
    ledger and runs `make docs-gen`; nobody has to know, or type, what the
    residual became. PR #1255 is the live case: it takes the residual down and
    zeroes a tier, and this is the assertion that says the documents follow it
    mechanically rather than being corrected again afterwards."""
    tree = _tree(tmp_path)
    assert "all 3 residual documents" in docgen.residual_report(root=tree)
    before = docgen.block_selfhost_residual("", root=tree)
    assert "chain: `go`, `java`, `rust`, `wasm`." in before

    closed = _LEDGER.replace('"py": ("a.rvl", "b.rvl"),', '"py": (),')
    tree = _tree(tmp_path, ledger=closed)
    report = docgen.residual_report(root=tree)
    assert "all 1 residual documents" in report
    assert "| py   |      4 |                    4 (100%) |             4 (100.0%) |" in report
    after = docgen.block_selfhost_residual(before, root=tree)
    assert "chain: `py`, `go`, `java`, `rust`, `wasm`." in after


def test_the_committed_blocks_store_no_count(tmp_path):
    """Issue #1768. The corpus column and the totals moved with every corpus
    document a pull request added, so any two such pull requests conflicted
    in both documents. Driven: the blocks over a tree whose corpora grew are
    the blocks over the tree before, and no figure in them is a count."""
    tree = _tree(tmp_path)
    blocks = (docgen.block_selfhost_residual("", root=tree),
              docgen.block_selfhost_residual_docs("", root=tree))
    grown = {tier: docs + [f"{tier}_new_{i}.rvl" for i in range(3)]
             for tier, docs in _CORPUS.items()}
    tree = _tree(tmp_path, corpus=grown)
    assert (docgen.block_selfhost_residual("", root=tree),
            docgen.block_selfhost_residual_docs("", root=tree)) == blocks
    import re  # noqa: PLC0415
    for body in blocks:
        assert not re.search(r"\b\d+\b", body.replace("emit_<tier>", "")), body


def test_two_corpus_additions_merge_in_the_documents(tmp_path):
    """The exit test of issue #1768 for these documents, through `git
    merge-tree`: two pull requests that each add a corpus document to the py
    tier leave the committed blocks unchanged and merge. The table the blocks
    used to carry conflicted on the same pair."""
    from _merge_tree import git_has_merge_tree, merge  # noqa: PLC0415
    if not git_has_merge_tree():
        pytest.skip("git merge-tree --write-tree needs git 2.38")

    def doc(corpus, render) -> dict[str, str]:
        tree = _tree(tmp_path / str(len(corpus["py"])) / render.__name__,
                     corpus=corpus)
        return {"docs/selfhost-compile.md":
                "intro\n\n" + render(tree) + "\n\nafter\n"}

    left = dict(_CORPUS, py=_CORPUS["py"] + ["e.rvl"])
    right = dict(_CORPUS, py=_CORPUS["py"] + ["e.rvl", "f.rvl"])

    def block(tree):
        return docgen.block_selfhost_residual("", root=tree)

    def table(tree):
        return docgen.residual_report(root=tree)

    clean, conflicted = merge(doc(_CORPUS, block), doc(left, block),
                              doc(right, block))
    assert clean, conflicted
    clean, conflicted = merge(doc(_CORPUS, table), doc(left, table),
                              doc(right, table))
    assert not clean and conflicted == ["docs/selfhost-compile.md"]


def test_the_block_regenerates_to_itself(tmp_path):
    tree = _tree(tmp_path)
    once = docgen.block_selfhost_residual("", root=tree)
    assert docgen.block_selfhost_residual(once, root=tree) == once
    docs = docgen.block_selfhost_residual_docs("", root=tree)
    assert docgen.block_selfhost_residual_docs(docs, root=tree) == docs


def test_the_document_list_names_every_residual_document_and_no_others(tmp_path):
    body = docgen.block_selfhost_residual_docs("", root=_tree(tmp_path))
    assert body.count("\n- `") == 3
    for doc in ("a.rvl", "b.rvl", "c.rvl"):
        assert f"- `{doc}`" in body
    assert "`go`:" in body
    assert "- none; the fully-native chain reproduces the whole corpus." in body
    assert "d.rvl" not in body


def test_the_blocks_still_fail_loudly_on_a_malformed_ledger(tmp_path):
    """Storing no count does not make the block a constant: it is rendered
    from the ledger, so a ledger the corpora do not cover still refuses."""
    tree = _tree(tmp_path, ledger=_LEDGER.replace('"wasm": (),', ""))
    for render in (docgen.block_selfhost_residual,
                   docgen.block_selfhost_residual_docs):
        with pytest.raises(docgen.SelfhostResidualError):
            render("", root=tree)


# --------------------------------------------------------------------------
# The prose gate, seen to fail. Each pair is one perturbation and the same
# text with the one thing changed that should silence it.
# --------------------------------------------------------------------------
def _claims(tree: Path) -> list[str]:
    return docgen.check_residual_claims(tree)


@pytest.mark.parametrize("wrong,right", [
    ("So all 9 residual documents are lower.rvl gaps.",
     "So all 3 residual documents are lower.rvl gaps."),
    ("It reports 9 residuals against the native chain.",
     "It reports 3 residuals against the native chain."),
    ("Only 4 survive the fully-native chain.",
     "Only 7 survive the fully-native chain."),
    ("4 of 10 documents survive the fully-native chain.",
     "7 of 10 documents survive the fully-native chain."),
    ("The 9 py documents the native chain does not reproduce are listed below.",
     "The 2 py documents the native chain does not reproduce are listed below."),
])
def test_a_hand_typed_residual_figure_is_red(tmp_path, wrong, right):
    tree = _tree(tmp_path)
    _doc(tree, wrong)
    found = _claims(tree)
    assert len(found) == 1, found
    assert "docs/selfhost-compile.md:1" in found[0]
    _doc(tree, right)
    assert _claims(tree) == []


def test_a_hand_copied_table_row_is_red(tmp_path):
    """The shape the two documents were actually in: a whole table retyped
    beside the generated one, its corpus column a year out of date."""
    tree = _tree(tmp_path)
    table = ("| tier | corpus | the fully-native chain |\n"
             "|---|---|---|\n"
             "| py | %d | 2 |\n")
    _doc(tree, table % 9)
    found = _claims(tree)
    assert len(found) == 1, found
    assert "the py corpus is 4" in found[0]
    _doc(tree, table % 4)
    assert _claims(tree) == []


def test_the_gate_says_which_number_is_right_and_how_to_fix_it(tmp_path):
    tree = _tree(tmp_path)
    _doc(tree, "So all 9 residual documents are lower.rvl gaps.")
    verdict = _claims(tree)[0]
    assert "says `9 residual documents`" in verdict
    assert "the residual is 3" in verdict
    assert "docgen:selfhost-residual" in verdict
    assert "LOWER_GAP_DOCS" in verdict


def test_a_residual_figure_in_a_paragraph_about_something_else_is_not_read(tmp_path):
    """`residual` is this repository's word for a dozen unrelated leftovers. A
    rule that read all of them would fire on prose it knows nothing about, and
    a gate that fires on innocent text gets bypassed."""
    tree = _tree(tmp_path)
    _doc(tree, "Item 424 leaves 9 residual risks, disclosed in section 7.\n")
    assert _claims(tree) == []


def test_the_limit_of_the_prose_gate_stated_rather_than_implied(tmp_path):
    """What this cannot know, pinned so nobody reads more into a green run than
    is there. A figure in a paragraph that never names the native chain, the
    ledger or a residual document is not read, and no regular expression over
    prose could promise otherwise. The broad claim is made structurally by the
    generated block instead: the documents that state the residual state it
    from `LOWER_GAP_DOCS`, so there is nothing left to retype."""
    tree = _tree(tmp_path)
    _doc(tree, "The compiler leaves 9 of them behind.\n")
    assert _claims(tree) == []


def test_the_roadmap_is_out_of_scope_and_stays_out(tmp_path):
    """`docs/v2.0-roadmap.md` records what was true when an item was written and
    is appended to by nearly every pull request, so it is excluded here for the
    same reason `DOC_STATUS_EXCLUDED` excludes it from the inventory block. This
    test is the reminder that item 146's own figure is therefore NOT gated by
    this check; `tools/check_roadmap_claims.py` owns the roadmap's citations."""
    tree = _tree(tmp_path)
    _doc(tree, "So all 9 residual documents are native chain gaps.",
         name="v2.0-roadmap.md")
    assert _claims(tree) == []
    assert "docs/v2.0-roadmap.md" in docgen.RESIDUAL_PROSE_EXCLUDED


# --------------------------------------------------------------------------
# The real tree. Every fixture above is synthetic, and a rule that stopped
# matching the live documents would still pass all of them.
# --------------------------------------------------------------------------
def test_the_real_documents_carry_the_block_and_it_is_current():
    for rel, key in (("docs/selfhost-compile.md", "selfhost-residual"),
                     ("docs/selfhost-findings.md", "selfhost-residual"),
                     ("docs/selfhost-findings.md", "selfhost-residual-docs")):
        text = (ROOT / rel).read_text(encoding="utf-8")
        render = (docgen.block_selfhost_residual if key == "selfhost-residual"
                  else docgen.block_selfhost_residual_docs)
        committed = docgen.extract(text, key)
        assert committed == render(committed), (
            f"{rel}: the `{key}` block is stale. fix: {docgen.WRITE_HINT}")


def test_the_real_tree_states_one_residual_everywhere_it_states_one():
    assert docgen.check_residual_claims() == []


def test_the_real_ledger_agrees_with_the_real_corpora():
    """Non-vacuity for the two tests above: they would both pass on a tree
    whose ledger and corpora had drifted apart, because the rendering and the
    prose would be wrong together. This is the assertion that the measurement
    is well formed at all."""
    rows = docgen.selfhost_residual()
    assert [t for t, _, _ in rows] == list(TIERS)
    for tier, corpus, gap in rows:
        assert 0 <= gap <= corpus and corpus > 0, (tier, corpus, gap)


def test_a_planted_figure_in_the_REAL_document_is_caught(tmp_path):
    """The collectors run against the live document, not only the fixtures.
    The committed block states no figure since issue #1768, so a wrong one is
    planted into a copy of the real document beside it, in the sentence shape
    the document used to carry, and the gate over that copy has to name it."""
    rel = "docs/selfhost-compile.md"
    text = (ROOT / rel).read_text(encoding="utf-8")
    _, gap = docgen.residual_totals()
    planted = (f"So all {gap + 100} residual documents are "
               "`selfhost/lower.rvl` gaps, the native IR producer.")
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "tests", tree / "tests",
                    ignore=shutil.ignore_patterns("fixtures", "__pycache__"))
    (tree / "docs").mkdir()
    (tree / "docs" / "selfhost-compile.md").write_text(
        text.replace("<!-- docgen:selfhost-residual end -->",
                     "<!-- docgen:selfhost-residual end -->\n\n" + planted),
        encoding="utf-8")
    found = docgen.check_residual_claims(tree)
    assert len(found) == 1 and f"{gap + 100} residual documents" in found[0], found
    assert docgen.check_residual_claims(ROOT) == []


def test_this_module_runs_on_a_documentation_only_diff():
    """The hazard this module exists to survive. `docgen --check` runs in the
    `frontend` job, which a documentation-only pull request skips, so the one
    diff that moves a document is the one its gate does not run on. The fix is
    the selector: any `.md` path must select this module, and the job that
    consumes the selection (`root-suite-affected`) is ungated."""
    sys.path.insert(0, str(ROOT / "tools"))
    import affected_tests  # noqa: PLC0415

    result = affected_tests.select(["docs/selfhost-compile.md"], ROOT)
    assert result["full"] is False, result
    assert "tests/test_selfhost_residual_is_generated.py" in result["pytest"], (
        result["pytest"])
