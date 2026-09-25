"""Issue #989: the ruleset digest did not cover `retention.py`.

`attest.RULESET_MODULES` is the list of modules whose bytes
:func:`attest.ruleset_digest` folds into the ruleset identity an attestation
carries. The list was assembled from the modules that RAISE a `(Gn)`-tagged
refusal, and `retention.py` raises none — so it was left out.

That is the wrong test for membership. `retention.PERSISTENCE_SINK_SCOPES` is
not documentation: `taint.py` reads it (through
`retention.persistence_sink_of`, taint.py:706) to decide whether a
past-deadline `Retained[T, P]` value is refused at a crossing at all. Edit the
set and you have changed which programs the frontend refuses — a rule change by
any honest reading — while the digest that identifies the ruleset sat still.

So two artifacts could carry the same `checker.ruleset` digest having been
admitted under different retention rules. These tests pin the fix from both
sides:

* the digest MOVES when the sink set is edited (the exit criterion in #989);
* the edit used to move it is one that really does change refusals, so the
  coverage is not protecting a constant nobody reads;
* and listing `retention` adds nothing to the CITED codes, because it cites no
  `(Gn)` tag — the digest grows by a module, the attested guarantee list does
  not.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl import attest  # noqa: E402
from revl import retention as R  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

# The same two programs `tests/test_retention_472.py` uses, cut to the two
# shapes that matter here: a retained value that is REFUSED at a persistence
# sink, and the sink set that decides it.
PAST = '''retention customer_pii {
  until: "2020-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo, subject
  derivatives: summary, index
}
'''

FLOW = '''extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py {{ return k }}
extern emission[{cap}] fn sink(row: Str) -> Int = @py {{ return 0 }}
service Ops {{ emission fn go(k: Str) -> Int }}
component Store provides ops: Ops {{
  provide ops {{
    fn go(k) {{
      let row = load(k)
      let n = emit sink(row)
      return n
    }}
  }}
}}
'''


# ===========================================================================
# 1. The exit criterion: `retention` is in the ruleset, and its bytes count.
# ===========================================================================

def test_retention_is_listed_in_the_modules_the_ruleset_digest_identifies():
    """The one-line form of the fix. Membership here is what puts a module's
    bytes into the digest; absence is the drift #989 reports."""
    assert "retention" in attest.RULESET_MODULES


def _scratch_ruleset(tmp_path: Path, name: str) -> Path:
    """A copy of the shipped ruleset as a directory `_read_ruleset` can be
    pointed at. Every module in the list is copied, so the copy's digest is the
    shipped digest and the only variable in a later comparison is the edit."""
    here = Path(attest.__file__).resolve().parent
    target = tmp_path / name
    target.mkdir()
    for module in attest.RULESET_MODULES:
        shutil.copyfile(here / f"{module}.py", target / f"{module}.py")
    return target


def _digest_of(monkeypatch, directory: Path) -> tuple[str, tuple[str, ...]]:
    """`(digest, cited)` for a ruleset directory, read through the real
    `_read_ruleset` — the cache is cleared so each call re-reads the bytes."""
    monkeypatch.setattr(attest, "__file__", str(directory / "attest.py"))
    monkeypatch.setattr(attest, "_ruleset_cache", None)
    return attest._read_ruleset()


def test_an_edit_to_the_retention_sink_set_moves_the_ruleset_digest(tmp_path, monkeypatch):
    """#989's exit criterion, stated as the comparison a verifier would make.

    The scratch copy is byte-identical to the shipped ruleset, so its digest is
    the shipped digest — that self-check is what stops this test passing because
    the harness is broken. Then ONE line changes in the retention copy: the
    `wal` scope leaves the sink set. The digest must move, because the set of
    programs the frontend refuses moved with it.
    """
    shipped = attest.ruleset_digest()  # read before `__file__` is repointed
    before_dir = _scratch_ruleset(tmp_path, "before")
    after_dir = _scratch_ruleset(tmp_path, "after")

    retention_copy = after_dir / "retention.py"
    lines = retention_copy.read_text().splitlines(keepends=True)
    kept = [line for line in lines if not line.strip().startswith('"wal",')]
    assert len(kept) == len(lines) - 1, "the sink set moved; re-anchor this edit"
    retention_copy.write_text("".join(kept))

    before, cited_before = _digest_of(monkeypatch, before_dir)
    after, cited_after = _digest_of(monkeypatch, after_dir)

    assert before == shipped, (
        "the scratch copy must reproduce the shipped digest, or the comparison "
        "below proves nothing about the shipped ruleset"
    )
    assert before != after, (
        "an edit to `retention.PERSISTENCE_SINK_SCOPES` left the ruleset digest "
        "unchanged: `retention` is not in `RULESET_MODULES` (#989)"
    )
    # ...and it moved for the retention bytes, not because a copy was disturbed:
    # no other module was touched, and retention cites no `(Gn)` tag, so the
    # cited-code set is identical on both sides of the edit.
    assert cited_before == cited_after


def test_the_edit_that_moves_the_digest_is_one_that_changes_what_is_refused(monkeypatch):
    """Non-vacuity: the set the digest now covers is load-bearing.

    Digesting a constant nobody reads would be busywork. So prove the reader:
    with the shipped set the expired value is refused at the `wal` crossing, and
    with `wal` removed from the set the SAME program compiles. The refusal
    follows the set, which is why the set's bytes are a rule.
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAST + FLOW.format(cap="wal"), "retain.rvl")
    assert excinfo.value.code == "G-RETAIN"
    assert "`wal` crossing" in excinfo.value.message

    monkeypatch.setattr(R, "PERSISTENCE_SINK_SCOPES",
                        frozenset(R.PERSISTENCE_SINK_SCOPES - {"wal"}))
    compile_source(PAST + FLOW.format(cap="wal"), "retain.rvl")


def test_the_set_the_digest_covers_is_the_one_the_refusal_reads():
    """The coupling, asserted rather than assumed: `taint` classifies a
    crossing's sink-ness through `retention.persistence_sink_of`, which reads
    the module global. A refactor that copied the set into `taint.py` would
    leave the digest covering the copy and not the rule — this fails then."""
    for scope in sorted(R.PERSISTENCE_SINK_SCOPES):
        assert R.persistence_sink_of([f"{scope}.insert"]) == scope
    assert R.persistence_sink_of(["log.emit"]) is None


# ===========================================================================
# 2. The addition is digest-only: the attested guarantee list does not move.
# ===========================================================================

def test_listing_retention_adds_a_digest_input_without_adding_a_cited_code(monkeypatch):
    """Why #989 could be fixed by adding one string.

    `discharged_guarantees` is derived from the `(Gn)` tags the listed modules
    cite. `retention` cites none — it refuses under `G-RETAIN`, which is a code
    `taint` raises — so putting it in the list moves the digest and leaves the
    attested list alone. If a later change gives `retention` its own tags, this
    test says so rather than letting the two meanings of membership drift apart.
    """
    with_retention = attest.discharged_guarantees()

    monkeypatch.setattr(
        attest, "RULESET_MODULES",
        tuple(m for m in attest.RULESET_MODULES if m != "retention"),
    )
    monkeypatch.setattr(attest, "_ruleset_cache", None)
    without_retention = attest.discharged_guarantees()

    assert with_retention == without_retention
    assert "G-RETAIN" not in with_retention, (
        "`G-RETAIN` is a taint-raised code; it should be attested through "
        "`taint`, and its presence here would mean `retention` now cites it"
    )


def test_the_digest_is_an_identity_of_the_ruleset_and_not_of_its_neighbours(tmp_path, monkeypatch):
    """The other half of "identifies the ruleset": a module outside the list is
    not an input. `policy.py` sits beside `retention.py` and is deliberately not
    a member (it raises a DSL `PolicyError`, not an admission refusal), so
    editing it must not move the digest — otherwise the list means nothing."""
    here = Path(attest.__file__).resolve().parent
    directory = _scratch_ruleset(tmp_path, "neighbours")
    before = _digest_of(monkeypatch, directory)[0]

    shutil.copyfile(here / "policy.py", directory / "policy.py")
    (directory / "policy.py").write_text((directory / "policy.py").read_text() + "\n")

    after = _digest_of(monkeypatch, directory)[0]
    assert before == after


def test_a_missing_ruleset_module_is_refused_rather_than_digested(tmp_path, monkeypatch):
    """A ruleset the process cannot read is not one it can name — the digest
    must not silently narrow to whatever happens to be on disk."""
    directory = _scratch_ruleset(tmp_path, "holed")
    (directory / "compiler.py").unlink()
    monkeypatch.setattr(attest, "__file__", str(directory / "attest.py"))
    monkeypatch.setattr(attest, "_ruleset_cache", None)

    with pytest.raises(RevlError) as excinfo:
        attest.ruleset_digest()
    assert "compiler" in str(excinfo.value.message)
