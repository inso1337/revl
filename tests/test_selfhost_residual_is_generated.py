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

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

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
def test_the_block_states_the_total_and_every_tier(tmp_path):
    body = docgen.block_selfhost_residual("", root=_tree(tmp_path))
    assert "| **total** | **10** | **10 (100%)** | **7 (70.0%)** |" in body
    assert "| py   |      4 |                    4 (100%) |              2 (50.0%) |" in body
    assert "all 3 residual documents" in body


def test_the_block_follows_the_ledger_with_no_hand_edit(tmp_path):
    """The point of the exercise. An open branch that closes gaps changes the
    ledger and runs `make docs-gen`; nobody has to know, or type, what the
    residual became. PR #1255 is the live case: it takes the residual down and
    zeroes a tier, and this is the assertion that says the documents follow it
    mechanically rather than being corrected again afterwards."""
    before = docgen.block_selfhost_residual("", root=_tree(tmp_path))
    assert "all 3 residual documents" in before

    closed = _LEDGER.replace('"py": ("a.rvl", "b.rvl"),', '"py": (),')
    after = docgen.block_selfhost_residual(
        before, root=_tree(tmp_path, ledger=closed))
    assert "all 1 residual documents" in after
    assert "| py   |      4 |                    4 (100%) |             4 (100.0%) |" in after


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
    assert "`go`, 0 residual of 1:" in body
    assert "- none; the fully-native chain reproduces the whole corpus." in body
    assert "d.rvl" not in body


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


def test_a_planted_figure_in_the_REAL_document_is_caught():
    """The collectors run against the live document, not only the fixtures."""
    rel = "docs/selfhost-compile.md"
    text = (ROOT / rel).read_text(encoding="utf-8")
    _, gap = docgen.residual_totals()
    assert f"all {gap} residual documents" in text
    broken = text.replace(f"all {gap} residual documents",
                          f"all {gap + 100} residual documents")
    paragraphs = [p for p, _ in docgen._paragraphs(broken)
                  if f"{gap + 100} residual documents" in p]
    assert paragraphs, "the planted figure did not land in a paragraph"
    assert docgen._RESIDUAL_ANCHOR.search(paragraphs[0]), (
        "the paragraph the residual lives in is no longer anchored, so the "
        "prose gate would not read it")


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
