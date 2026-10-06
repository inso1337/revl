"""An activation-body effect's `undo` is judged, and what it cannot prove is
NAMED in the teardown report (issue #1859, the activation-body slice).

The issue: `let h = effect r.open_res() undo r.other(h)` compiled with no
judgment on the `undo` at all, so the bracket ran `r.other(h)` at teardown,
released nothing, and `unload` still reported `noResidue: true` with
`checks.effects: true`. A report that says "clean" over an `undo` that was never
the acquire's inverse is a FALSE PROOF, and that proof is the whole claim of the
reversible-effects story — so the subject of this fix is the report, not the
annoyance of a missing check.

Slices 1 to 3 of the issue landed on main and closed the two families the site
gate can PROVE: a host acquisition's `undo` must be its family's release
(`tests/test_host_undo_release_1859.py`), and an `extern acquire`'s must be the
inverse its declaration names (`tests/test_extern_acquire_inverse_1859.py`).
Issue #1945 extended the same judgment to the provide-METHOD bracket
(`tests/test_method_effect_inverse_1945.py`). The ACTIVATION body's brackets
were still admitted with no `undo` judgment and — the part that made the report
false rather than merely incomplete — with no record that there had been none.

THE FIX SHAPE (chosen deliberately over "refuse every unproved inverse"):
a hybrid.

* Fail-closed where revl can PROVE the inverse. A host WRITE at activation scope
  is the case a report cannot excuse: the write stays in the table and the table
  is not restored, so `effect m.insert("k", "v") undo m.remove("x")` is refused
  with G4 naming `m.remove("k")` — the same table rule the provide method
  already applies. `_method_effect_inverse` owns that table; the activation body
  now calls it too. This half is defence in depth rather than the false proof
  itself: on base the host stub already fails the program at RUNTIME ("an undo
  did not reverse its write", R4), but only once the program is RUN, so the
  verdict arrives after the host state exists. The fix moves it to admission.
* Honest where only the author can assert it. A service call, a `spawn`, and a
  non-host acquisition have no host stub for revl to pair a release against, so
  refusing them would refuse the `verified effect` feature itself — whose
  documented point is an author-asserted reversal. They are stamped
  `inverse: asserted` (or `declared` for an extern whose declaration names its
  inverse) and listed in the teardown report under `trustTheAuthor`, which is
  what the issue asks for: "Reversion is unverified, so this one is labelled
  `asserted`, not proven."

So `noResidue` keeps its meaning — it judges what the runtime can OBSERVE — and
the unproved half is NAMED beside it instead of being absent from the report.
Refusing the whole service bracket instead would be the dishonest option: the
acquisition would still run at load, so a refusal at admission and a clean
report at teardown would be two different stories about the same bracket.

What is PROVEN stays unmarked: a proved family stamps no `inverse` key at all,
so activation-body IR for the brackets slices 1 to 3 closed is byte-identical.
`tests/test_method_effect_inverse_1945.py::test_activation_body_ir_carries_no_inverse_key`
carries the pre-fix rationale ("provide-method steps only, so activation-body IR
is byte-identical") in its docstring; its program holds only proved host
acquisitions, so its assertion still holds and the test is untouched. The
general claim in that docstring is what this change supersedes, and this file is
where the new shape is pinned.

The self-host half is pinned by `tests/test_selfhost_lower_ir.py`, which
byte-compares `selfhost/lower.rvl::lower_to_ir` against this reference over the
emit corpus: reverting `selfhost/lower.rvl` alone fails 6 of its 676 tests, so
the fix is a two-sided change and cannot be landed half-applied.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]

#: Compile-only prelude. `H` is an opaque handle so the `extern acquire` is a
#: legal declaration (item 308, R0), and `Child` provides a DIFFERENT key from
#: `Holder`'s own `s` so the spawn does not collide on a provision (G2).
_PRELUDE = (
    "type H = Opaque\n"
    "service Res {\n"
    "  fn open_res() -> H\n"
    "  fn release_res(h: H) -> Unit\n"
    "  fn other(h: H) -> Unit\n"
    "}\n"
    "service S { fn go(n: Int) -> Int }\n"
    "extern pure fn close_h(h: H) -> Unit = @py { return None }\n"
    "extern acquire fn open_h() -> H undo close_h(result) = @py { return 1 }\n"
    "component Child provides cs: S {\n"
    "  provide cs { fn go(n) = n }\n"
    "}\n"
)

#: `requires r: Res` is what lets the activation body call the service at all:
#: a component cannot call a key it provides itself.
_HEAD = "component Holder requires r: Res provides s: S {\n"
_TAIL = "  provide s { fn go(n) = n }\n}\n"


def _source(body: str) -> str:
    return _PRELUDE + _HEAD + body + _TAIL


def _activation(source: str) -> list:
    """The `Holder` component's activation-body steps."""
    ir = compile_source(source, "t.rvl")
    [holder] = [c for c in ir["components"] if c["name"] == "Holder"]
    return holder["body"]


def _inverses(source: str) -> list:
    """`(step, inverse-or-None)` for each activation-body effect bracket."""
    return [(s.get("step"), s.get("inverse")) for s in _activation(source)
            if "acquire" in s]


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return excinfo.value


# ------------------------------- what the author must be trusted for (D4)

#: The activation-body brackets whose `undo` revl cannot prove, and the
#: provenance each must now carry. Every one of these compiled with NO `inverse`
#: key at all before this change — that absence is the base-fail this table
#: pins, because the key is the only thing `_trust_the_author` can read.
UNPROVED = [
    ("a service call undone by another service call",
     "  let h = effect r.open_res() undo r.other(h)\n", "asserted"),
    ("a service call undone by its declared release",
     "  let h = effect r.open_res() undo r.release_res(h)\n", "asserted"),
    ("a spawn undone by a method call",
     "  let ch = effect spawn Child undo ch.dispose()\n", "asserted"),
    ("an extern acquire undone by its declared inverse",
     "  let e = effect open_h() undo close_h(e)\n", "declared"),
]


@pytest.mark.parametrize("body, expected", [(b, e) for _n, b, e in UNPROVED],
                         ids=[n for n, _b, _e in UNPROVED])
def test_an_unproved_activation_inverse_records_what_it_is(body, expected):
    """Admitted — an author-asserted reversal is a documented feature — but no
    longer admitted ANONYMOUSLY."""
    assert _inverses(_source(body)) == [("let-effect", expected)]


@pytest.mark.parametrize("body, expected", [(b, e) for _n, b, e in UNPROVED],
                         ids=[n for n, _b, _e in UNPROVED])
def test_the_provenance_rides_the_step_the_report_reads(body, expected):
    """The key IS the fix: the report walks the IR and lists the steps carrying
    it, so a step without one is a reversal nothing can name."""
    steps = [s for s in _activation(_source(body)) if "acquire" in s]
    assert steps[0]["inverse"] == expected


def test_a_service_reversal_is_asserted_not_declared():
    """`asserted` and `declared` are different words on purpose: `declared` is a
    host body the language promised, `asserted` is the author's word with
    nothing behind it. Collapsing them would overstate the service case."""
    assert _inverses(_source("  let h = effect r.open_res() undo r.other(h)\n")
                     ) == [("let-effect", "asserted")]


# ----------------------------------- what revl PROVES stays unmarked (no
#                                    over-marking: a proved family must not
#                                    appear as trusted to the author)

_MAP = "  let m = effect Map.new() undo m.drop()\n"


def test_a_proved_host_acquisition_stamps_nothing():
    """`Map.new`/`drop` and `Pool.open`/`close` are the families slices 1 to 3
    proved; the site gate already refused any other `undo` for them, so there is
    no unproved reversal to name and their IR is unchanged."""
    assert _inverses(_source(
        _MAP + "  let p = effect Pool.open(\"db://orders\", 2) undo p.close()\n"
    )) == [("let-effect", None), ("let-effect", None)]


def test_a_proved_host_write_is_table_not_asserted():
    """The table inverse is proven — revl owns the stub — so it is NOT listed as
    trusted to the author. Only the unprovable half is."""
    assert _inverses(_source(
        _MAP + "  effect m.insert(\"k\", \"v\") undo m.remove(\"k\")\n"
    )) == [("let-effect", None), ("effect", "table")]


def test_the_provide_step_is_not_an_effect_bracket():
    """Guard on the report's shape: the `provide` step carries no provenance, so
    the walk cannot mistake it for an effect bracket."""
    provides = [s for s in _activation(_source(_MAP))
                if s.get("step") == "provide"]
    assert provides and "inverse" not in provides[0]


# -------------------------------- fail-closed: the activation host write (G4)

_WRITE = "  effect m.insert(\"k\", \"v\") undo {undo}\n"

NEEDS_REMOVE = ("the `undo` of `effect m.insert(...)` must be its inverse on the "
                "same handle and key: write `undo m.remove(\"k\")`")


@pytest.mark.parametrize("undo",
                         ['m.remove("x")', 'm.get("k")', "m.drop()",
                          'm.insert("k", "v")', "1", 'n.remove("k")'],
                         ids=["another key", "a read", "the handle's drop",
                              "the same write", "a literal", "a sibling map"])
def test_a_host_write_whose_undo_is_not_its_inverse_is_refused(undo):
    """Fail-closed, and this is the case that cannot be reported away: the write
    stays in the table, the table is not restored, and the pre-fix report called
    that clean. The refusal names the inverse the site needs, exactly as the
    provide-method rule does."""
    body = _MAP
    if undo == 'n.remove("k")':
        body += "  let n = effect Map.new() undo n.drop()\n"
    error = _refusal(_source(body + _WRITE.format(undo=undo)))
    assert error.code == "G4"
    assert error.category == "inverse"
    assert NEEDS_REMOVE in str(error)


def test_the_same_host_write_is_admitted_on_the_correct_key():
    """No over-rejection beside the refusals above: the pairing the refusal names
    compiles and is proven."""
    assert _inverses(_source(_MAP + _WRITE.format(undo='m.remove("k")'))) == [
        ("let-effect", None), ("effect", "table")]


# ------------------------------------------------ the teardown report (D4)

#: The live prelude, extern-free so the cordis driver can actually run it (the
#: `H` handle would need a host stub at the boundary).
_LIVE = (
    "service Res {\n"
    "  fn open_res() -> Int\n"
    "  fn release_res(h: Int) -> Int\n"
    "  fn other(h: Int) -> Int\n"
    "}\n"
    "service S { fn go(n: Int) -> Int }\n"
    "component Backing provides r: Res {\n"
    "  provide r {\n"
    "    fn open_res() = 1\n"
    "    fn release_res(h) = 0\n"
    "    fn other(h) = 0\n"
    "  }\n"
    "}\n"
    "component Holder requires r: Res provides s: S {\n"
    "@@BODY@@"
    "  provide s { fn go(n) = n }\n"
    "}\n"
)

_LIFECYCLE = """
lifecycle test "the activation bracket is named, not proved" {
  load Backing
  load Holder
  call s.go(1)
  unload Holder
  unload Backing
  assert no_residue
}
"""

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the teardown report and `revl test` are produced by the cordis-py "
           "driver; install it with `sh backends/python/setup.sh`")


def _live(body: str) -> str:
    return _LIVE.replace("@@BODY@@", body)


def _revl_test(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    path = tmp_path / "holder.rvl"
    path.write_text(source, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


@needs_cordis
def test_the_clean_verdict_now_names_the_inverse_it_could_not_prove():
    """The issue's exact program, end to end.

    Before: `noResidue: True`, `checks.effects: True`, and NO entry saying the
    reversal was unverified — a clean verdict over an `undo` that released
    nothing. After: the verdict is unchanged, because it judges what the runtime
    can observe and `r.other(h)` releases nothing observable, but the report now
    names the bracket it had to trust the author for."""
    from revl.mcp.session import Session  # noqa: PLC0415

    session = Session()
    session.load(compile_source(
        _live("  let h = effect r.open_res() undo r.other(h)\n"), "t.rvl"))
    session.call("s", "go", [1])
    report = session.unload()
    assert report["noResidue"] is True
    assert report["checks"]["effects"] is True
    assert report["trustTheAuthor"] == [
        {"component": "Holder", "method": "<activation>", "inverse": "asserted"}]


@needs_cordis
def test_a_proved_activation_bracket_adds_nothing_to_the_report():
    """The other half of the same coin: a proved family is not listed, so
    `trustTheAuthor` stays a signal about UNVERIFIED reversals only and does not
    degrade into a list of every effect."""
    from revl.mcp.session import Session  # noqa: PLC0415

    session = Session()
    session.load(compile_source(_live(_MAP), "t.rvl"))
    session.call("s", "go", [1])
    report = session.unload()
    assert report["noResidue"] is True
    assert "trustTheAuthor" not in report


@needs_cordis
def test_the_issues_program_passes_its_own_lifecycle_test(tmp_path):
    """No over-rejection: the issue's program is admitted and its lifecycle test
    passes, because the fix NAMES the unproved inverse rather than refusing a
    service reversal the language supports."""
    result = _revl_test(
        tmp_path,
        _live("  let h = effect r.open_res() undo r.other(h)\n") + _LIFECYCLE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS the activation bracket is named, not proved" in result.stdout


@needs_cordis
def test_a_wrong_activation_host_write_is_refused_before_it_runs(tmp_path):
    """The fail-closed half, seen from `revl test`.

    On base this program already FAILS, but at RUNTIME: the host stub notices
    the table was not restored ("an undo did not reverse its write", R4), which
    only happens once the program is actually RUN. The fix moves the verdict to
    admission, so the compile-time refusal is what this pins — the program is
    rejected before any host state exists."""
    result = _revl_test(
        tmp_path,
        _live(_MAP + _WRITE.format(undo='m.remove("x")')) + _LIFECYCLE)
    assert result.returncode != 0
    assert "PASS" not in result.stdout
    assert NEEDS_REMOVE in result.stdout + result.stderr
