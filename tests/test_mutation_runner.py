"""The mutation runner (tools/mutation_runner.py, issue #2761 slice 7).

What is worth testing is that it measures SENSITIVITY and can fail. A runner
that reported "no survivors" on every module would be the sixth check in this
repository that cannot fire, so the demonstration is two-sided and constructed:
a small module whose two refusal arms are each pinned by a test has no
survivors, and the same module with one test deleted has exactly that arm's
drop-refusal mutant surviving and none of the other arms'. Nothing in the tree
is weakened; the module and its tests live in a temporary directory the test
owns.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]

_MODULE = '''\
class RevlError(Exception):
    pass


def check(n, name):
    if n < 0:
        raise RevlError("negative")
    if not name:
        raise RevlError("empty")
    if n > 100:
        print("not a refusal arm: no raise")
    return n


class Box:
    def put(self, v):
        if v is None:
            raise RevlError("none")
        return v
'''

_TESTS = '''\
import pytest
from guard import Box, RevlError, check


def test_negative_is_refused():
    with pytest.raises(RevlError):
        check(-1, "x")


def test_empty_name_is_refused():
    with pytest.raises(RevlError):
        check(1, "")


def test_none_is_refused():
    with pytest.raises(RevlError):
        Box().put(None)


def test_the_good_path():
    assert check(1, "x") == 1
    assert Box().put(3) == 3
'''


def _tool():
    return load_by_path("mutation_runner", ROOT / "tools" / "mutation_runner.py")


def _stage(tmp_path, tests=_TESTS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "guard.py").write_text(_MODULE, encoding="utf-8")
    (tmp_path / "test_guard.py").write_text(tests, encoding="utf-8")
    return tmp_path


def test_arms_are_the_refusal_ifs_numbered_per_function_in_source_order():
    tool = _tool()
    found = tool.arms(_MODULE)
    assert [a.id for a in found] == ["check#1", "check#2", "Box.put#1"]
    assert len({a.digest for a in found}) == 3, "two arms share a digest"


def test_a_mutation_changes_exactly_the_one_arm_it_names():
    tool = _tool()
    dropped = tool.mutate(_MODULE, "check#1", "drop-refusal")
    # `ast.unparse` spells strings with single quotes.
    assert "raise RevlError('negative')" not in dropped
    assert "raise RevlError('empty')" in dropped
    negated = tool.mutate(_MODULE, "check#2", "negate-guard")
    assert "if not not name:" in negated
    assert "raise RevlError('negative')" in negated
    with pytest.raises(KeyError):
        tool.mutate(_MODULE, "check#9", "drop-refusal")
    with pytest.raises(ValueError):
        tool.mutate(_MODULE, "check#1", "no-such-operator")


def test_a_pinned_module_has_no_survivors(tmp_path):
    """Half one of the acceptance: the unweakened module produces none."""
    tool = _tool()
    report = tool.survey(_stage(tmp_path), "guard.py", ("test_guard.py",))
    assert len(report["mutants"]) == 6
    assert report["survivors"] == []
    assert {m["status"] for m in report["mutants"]} == {"killed"}


def test_a_weakened_arm_leaves_exactly_its_own_mutants_alive(tmp_path):
    """Half two: delete the one test that pins the `empty` arm, and its
    drop-refusal mutant survives while every other arm's stays killed. The
    negate-guard mutant of the same arm is still killed, and rightly: the
    happy-path test calls it with a name, the negated guard refuses it, and that
    test fails. Only a removed refusal is invisible to a suite that never feeds
    the arm a bad input. The runner can fail."""
    tool = _tool()
    weak = _TESTS.replace(
        '''def test_empty_name_is_refused():
    with pytest.raises(RevlError):
        check(1, "")


''', "")
    assert "test_empty_name_is_refused" not in weak
    report = tool.survey(_stage(tmp_path, weak), "guard.py", ("test_guard.py",))
    survivors = {(m["arm"], m["operator"]) for m in report["survivors"]}
    assert survivors == {("check#2", "drop-refusal")}


def test_a_red_baseline_is_refused_not_measured(tmp_path):
    tool = _tool()
    broken = _TESTS + "\n\ndef test_always_red():\n    assert False\n"
    with pytest.raises(RuntimeError, match="red baseline"):
        tool.survey(_stage(tmp_path, broken), "guard.py", ("test_guard.py",))


def test_check_is_shrink_only_in_both_directions(tmp_path):
    tool = _tool()
    clean = tool.survey(_stage(tmp_path / "clean"), "guard.py",
                        ("test_guard.py",))
    ledger = tool.ledger_of(clean)
    assert tool.check(clean, ledger) == []

    # `test_none_is_refused` renamed away so pytest no longer collects it.
    weak = tool.survey(
        _stage(tmp_path / "weak", _TESTS.replace(
            "def test_none_is_refused", "def _none_is_refused")),
        "guard.py", ("test_guard.py",))
    new = tool.check(weak, ledger)
    assert any(p.startswith("NEW SURVIVOR Box.put#1") for p in new), new

    stale = tool.check(clean, tool.ledger_of(weak))
    assert any(p.startswith("no longer survives Box.put#1") for p in stale), stale


def test_the_committed_ledger_names_real_arms_with_their_current_digests():
    """Cheap and independent of any run: every recorded survivor is an arm that
    is in the target now, with the same source, in a direction the runner has."""
    tool = _tool()
    ledger = tool.load_ledger()
    assert (ROOT / ledger["target"]).is_file()
    assert all((ROOT / t).is_file() for t in ledger["tests"])
    current = {a.id: a for a in tool.arms(
        (ROOT / ledger["target"]).read_text(encoding="utf-8"))}
    assert current, "the target has no refusal arm; the runner would be vacuous"
    for survivor in ledger["survivors"]:
        assert survivor["operator"] in tool.OPERATORS
        assert current[survivor["arm"]].digest == survivor["digest"], survivor
    assert ledger["target"] == tool.TARGET and tuple(ledger["tests"]) == tool.TESTS


def test_the_arms_cli_lists_the_target_without_running_anything():
    tool = ROOT / "tools" / "mutation_runner.py"
    done = subprocess.run([sys.executable, str(tool), "--arms"],
                          capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-1000:]
    assert "_validated_digests#1" in done.stdout


def test_the_ledger_is_labelled_as_readable_by_a_candidate():
    ledger = json.loads(
        (ROOT / "tests" / "fixtures" / "mutation_survivors.json")
        .read_text(encoding="utf-8"))
    about = " ".join(ledger["_about"])
    assert "a candidate can read it" in about and "NOT hidden" in about
