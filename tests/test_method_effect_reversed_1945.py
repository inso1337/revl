"""`no_residue` is not green when a provide-method effect's `undo` did not
reverse its write (issue #1945, part 2).

WHAT WAS WRONG. A method-body effect with an `undo` that is not its inverse,
`effect store.insert(k, v) undo store.remove("not-the-key")`, passed `revl test`'s
`assert no_residue`, and the session teardown report said `noResidue: true`.
The four R4 counters see the disposer RUN, and the R1 host-resource pairing
sees the enclosing bracket's `store.drop()` release the map, which is exactly
what masks the lost inverse: the inserted key never left.

WHAT HOLDS NOW. Every effect bracket, in an activation body or a provide
method, journals the host Map keys its forward write and its `undo` touch
(`Frame._journal_begin`, `runtime._journal_note`). The journal is a stack of
the frames whose brackets are running, so a provider's write inside a
consumer's bracket belongs to both. Two judgments, both net (two brackets of
one frame that together restore a key are clean):

- per frame, when its teardown completes: every key its brackets touched, in a
  map still open, back to its value before the frame's first bracketed write.
  This sees a consumer unloaded while the provider's map lives on;
- per map, when it is released: every bracketed key back to its value before
  the first bracketed write to it.

A miss is `bracket-fault` residue (`NotReversed`) naming the map and the key, so
`assert no_residue` fails and the session's `noResidue` is false.

What it does NOT judge (decision D6 on the issue): a write no bracket opened a
journal for. An operator call into a provider method that writes its own
handle, `fn put(k, v) = store.insert(k, v)`, is the intended form; the caller
brackets the crossing, and the handle's release at unload is its end.

The overwrite case is decision D1: `insert(k, v)` undone by `remove(k)` is the
table inverse only when `k` was absent. Over an existing key the `remove`
destroys the value the insert replaced, and teardown says so, naming the key.

Every executed test drives a live cordis-py composition; without the pinned
`cordis` fork they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

ROOT = Path(__file__).resolve().parents[1]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the teardown runs against a live cordis-py composition; install "
           "the pinned fork with `sh backends/python/setup.sh`")

STORE = """service Kv {
  fn get(k: Str) -> Str
  fn set(k: Str, v: Str) -> Str
  fn put(k: Str, v: Str) -> Str
}

component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
      return v
    }
    fn put(k, v) = store.insert(k, v)
  }
}
"""


def _ir(undo_key=None) -> dict:
    """STORE's IR. With `undo_key`, the `set` effect's `undo` removes that
    literal key instead: the issue's wrong-undo row, built as IR so the
    runtime half is tested whatever the static check admits."""
    ir = compile_source(STORE, "store.rvl")
    if undo_key is None:
        return ir
    ir = copy.deepcopy(ir)
    [store] = ir["components"]
    [provide] = [s for s in store["body"] if s.get("step") == "provide"]
    [set_] = [m for m in provide["methods"] if m["name"] == "set"]
    set_["body"][0]["undo"]["args"] = [{"kind": "lit", "value": undo_key}]
    return ir


def _teardown(ir: dict, calls) -> dict:
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(ir)
    for method, args in calls:
        session.call("kv", method, args)
    return session.unload()


def _messages(report: dict) -> list:
    return [r.get("error", {}).get("message", "")
            for r in report.get("compensationResidue") or []]


@needs_cordis
def test_the_issues_wrong_undo_row_is_residue_naming_the_key():
    report = _teardown(_ir(undo_key="not-the-key"), [("set", ["alpha", "one"])])
    assert report["noResidue"] is False
    assert report["checks"]["compensations"] is False
    [message] = _messages(report)
    assert ("key 'alpha' held absent before the first bracketed `insert` of it "
            "and 'one' when the map was released") in message


@needs_cordis
def test_an_undo_that_is_the_inverse_is_clean():
    report = _teardown(_ir(), [("set", ["alpha", "one"])])
    assert report["noResidue"] is True
    assert _messages(report) == []


@needs_cordis
def test_an_insert_over_an_existing_key_fails_loudly_naming_it():
    """D1: `remove(k)` cannot restore the value `insert` replaced."""
    report = _teardown(_ir(), [("put", ["alpha", "zero"]), ("set", ["alpha", "one"])])
    assert report["noResidue"] is False
    [message] = _messages(report)
    assert ("key 'alpha' held 'zero' before the first bracketed `insert` of it "
            "and absent when the map was released") in message


@needs_cordis
def test_two_bracketed_writes_to_one_key_that_restore_it_are_clean():
    """The judgment is the net one: the second `insert` overwrites the first,
    and its `remove` drops the first value, but the first bracket's own
    `remove` takes that value out anyway, so the world is restored."""
    report = _teardown(_ir(), [("set", ["alpha", "one"]), ("set", ["alpha", "two"])])
    assert report["noResidue"] is True
    assert _messages(report) == []


@needs_cordis
def test_an_operator_call_write_is_not_residue():
    """D6: an unbracketed write by a provider into its own handle is not
    judged; the handle's release at unload is its end."""
    report = _teardown(_ir(), [("put", ["beta", "zero"]), ("set", ["alpha", "one"])])
    assert report["noResidue"] is True
    assert _messages(report) == []


_SEEDER = """service Store {
  fn seed(k: Str) -> Int
  fn unseed(k: Str) -> Int
}
service Ready { fn go(k: Str) -> Str }

component Backing provides s: Store {
  let data = effect Map.new() undo data.drop()
  provide s {
    fn seed(k)   = data.insert(k, "1")
    fn unseed(k) = data.remove(k)
  }
}

component Seeder requires s: Store provides r: Ready {
  provide r {
    fn go(k) {
      effect s.seed(k)
      undo   s.unseed(UNSEED)
      return k
    }
  }
}
"""


@needs_cordis
@pytest.mark.parametrize("unseed, clean", [("k", True), ('"nope"', False)],
                         ids=["the inverse", "the wrong key"])
def test_a_service_reversal_is_judged_by_the_state_it_left(unseed, clean):
    """The round trip's `_WRONG_PARTIAL` case, which `verified effect` cannot
    reach in a method: the service effect's undo is the author's word
    statically (`asserted`), and the provider's own map says whether it held."""
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(compile_source(_SEEDER.replace("UNSEED", unseed), "seed.rvl"))
    session.call("r", "go", ["alpha"])
    report = session.unload()
    assert report["noResidue"] is clean
    if not clean:
        [message] = _messages(report)
        # Seeder's frame judges its own bracket when it unloads, while
        # Backing's map is still open
        assert ("key 'alpha' held absent before the first bracketed `insert` of "
                "it and '1' when it unloaded") in message


# ---------------------------------------------------------- `revl test`

_LIFECYCLE = """
lifecycle test "a method-body write is reversed" {
  load Store
  CALLS
  unload Store
  assert no_residue
}
"""


def _revl_test(tmp_path: Path, calls: str) -> subprocess.CompletedProcess:
    path = tmp_path / "store.rvl"
    path.write_text(STORE + _LIFECYCLE.replace("CALLS", calls), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


@needs_cordis
def test_revl_test_fails_an_overwrite_naming_the_key(tmp_path):
    result = _revl_test(tmp_path, 'call kv.put("alpha", "zero")\n'
                                  '  call kv.set("alpha", "one")')
    assert "FAIL a method-body write is reversed" in result.stdout
    assert "NotReversed" in result.stdout
    assert "key 'alpha' held 'zero' before the first bracketed" in result.stdout


@needs_cordis
@pytest.mark.parametrize("calls", [
    'call kv.set("alpha", "one")',
    'call kv.put("beta", "zero")\n  call kv.set("alpha", "one")',
], ids=["the inverse", "an operator-call write beside it"])
def test_revl_test_passes_what_was_reversed(tmp_path, calls):
    result = _revl_test(tmp_path, calls)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS a method-body write is reversed" in result.stdout


def test_every_effect_bracket_opens_a_journal():
    """The activation body's `Map.new()` bracket and the method's `insert`
    bracket each open one; the unbracketed `put` opens none (D6)."""
    from revl._paths import python_backend_emitter  # noqa: PLC0415
    src = python_backend_emitter().emit(compile_source(STORE, "store.rvl"))
    assert src.count("_revl_frame._journal_begin()") == 2


# ------------------------------------------------- net per activation frame

_APP = """
component App requires kv: Kv {
  effect kv.set("who", "alice") undo UNDO
}
"""

_LIFECYCLE_APP = """
lifecycle test "the consumer's bracket is reversed when it unloads" {
  load Store
  load App
  unload App
  unload Store
  assert no_residue
}
"""

_UNSET = STORE.replace(
    "  fn put(k: Str, v: Str) -> Str\n",
    "  fn put(k: Str, v: Str) -> Str\n  fn unset(k: Str) -> Str\n").replace(
    "    fn put(k, v) = store.insert(k, v)\n",
    "    fn put(k, v) = store.insert(k, v)\n"
    "    fn unset(k) {\n      return store.remove(k)\n    }\n")


def _revl_test_app(tmp_path: Path, undo: str) -> subprocess.CompletedProcess:
    path = tmp_path / "app.rvl"
    path.write_text(_UNSET + _APP.replace("UNDO", undo) + _LIFECYCLE_APP,
                    encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


@needs_cordis
def test_a_consumers_non_inverse_undo_is_named_when_the_consumer_unloads(tmp_path):
    """The case a map-release check misses: the consumer unloads while the
    provider's map lives on. `kv.set("who", "")` leaves the key present and
    empty where it was absent, and nothing would ever release it. The consumer's
    own frame judges its bracket when IT unloads, naming the map and the key."""
    result = _revl_test_app(tmp_path, 'kv.set("who", "")')
    assert "FAIL the consumer's bracket is reversed when it unloads" in result.stdout
    assert ("App.insert: NotReversed: map#1 key 'who' held absent before the "
            "first bracketed `insert` of it and '' when it unloaded") in result.stdout


@needs_cordis
def test_a_consumers_inverse_undo_is_clean(tmp_path):
    """The same consumer, undone by removing the key it set (through an
    unbracketed provider write, D6), restores the world."""
    result = _revl_test_app(tmp_path, 'kv.unset("who")')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS the consumer's bracket is reversed when it unloads" in result.stdout


# ---------------------- restoring the value the body read (issue #1980)

#: The same store, but `set` reads the entry before it overwrites it and its
#: `undo` restores what it read. `store.get(k)` is `Opt`, so the `None` arm is
#: the `remove(k)` the old form used and the `Some` arm puts the old value back
#: — which is the arm `remove(k)` alone could not do (the D1 case above).
RESTORE_STORE = """service Kv {
  fn get(k: Str) -> Str
  fn set(k: Str, v: Str) -> Str
  fn put(k: Str, v: Str) -> Str
}

component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      let prev = store.get(k)
      effect store.insert(k, v)
      undo   match prev { Some(x) => store.insert(k, x), None => store.remove(k) }
      return v
    }
    fn put(k, v) = store.insert(k, v)
  }
}
"""


@needs_cordis
@pytest.mark.parametrize("calls", [
    [("set", ["alpha", "one"])],
    [("put", ["alpha", "zero"]), ("set", ["alpha", "one"])],
], ids=["an absent key", "an existing key"])
def test_the_restored_read_reverses_the_key(calls):
    """The #1966 net-per-frame check, on the form #1980 admits: the `None` arm
    takes an absent key back out, and the `Some` arm puts an existing key's old
    value back. Both frames end where they started, so neither is residue."""
    report = _teardown(compile_source(RESTORE_STORE, "store.rvl"), calls)
    assert report["noResidue"] is True
    assert _messages(report) == []


_RESTORE_LIFECYCLE = """
lifecycle test "a restored write is reversed" {
  load Store
  CALLS
  unload Store
  assert no_residue
}
"""


def _restore_revl_test(tmp_path: Path, calls: str) -> subprocess.CompletedProcess:
    path = tmp_path / "restore.rvl"
    path.write_text(RESTORE_STORE + _RESTORE_LIFECYCLE.replace("CALLS", calls),
                    encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


@needs_cordis
def test_revl_test_passes_the_restored_write_over_an_existing_key(tmp_path):
    """The very call `test_revl_test_fails_an_overwrite_naming_the_key` fails on
    with `remove(k)` as the undo — same `put`, same `set`, same key. Restoring
    the read value is what makes the loud path green."""
    result = _restore_revl_test(tmp_path, 'call kv.put("alpha", "zero")\n'
                                          '  call kv.set("alpha", "one")')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS a restored write is reversed" in result.stdout
