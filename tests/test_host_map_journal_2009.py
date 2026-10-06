"""#2009: the #1945 host-map write journal, mirrored into the ts/go/rust
lifecycle harnesses (py is the reference and already had it).

#1945 part 2 made `assert no_residue` observe *host-map state*: just before a
host `Map` is released, every key a bracketed effect inserted must be gone, and
no `remove` undo may have destroyed an entry an `insert` overwrote.  That fold
lives only in ``backends/python/runtime.py`` (``_JOURNAL``, ``_journal_note``,
``_judge_journal``, ``NotReversed``), so "an ``undo`` did not reverse its write"
was simply not observable on the other tiers.

The program below is the issue's D1 program, adapted to the one host verb whose
result type is declared (``Map.insert_if_absent`` -> ``Bool``,
``src/revl/typecheck.py``): the literal D1 spells ``put`` with ``store.insert``,
whose host signature returns nothing, and the frontend refuses that on every
tier.  The frontend also refuses the literal program outright, so these tests
drive the *runtime* path (``revl test --backend <tier>``), exactly the way
``tests/test_method_effect_reversed_1945.py`` does for py -- never
``revl compile``.

What the program does, and why it is the right probe:

* ``put("alpha", "zero")`` is a bare ``insert_if_absent`` return expression --
  no effect bracket, so it is *not* journalled (on py either).
* ``set("alpha", "one")`` opens a bracket, so ``insert("alpha", "one")``
  records the prior value ``Some("zero")`` -- first write wins.
* at teardown the inverse ``remove("alpha")`` runs and deletes the entry, so
  when the map is released ``alpha`` is absent while the journal says it held
  ``"zero"``.  An undo destroyed an entry an ``insert`` overwrote: residue.

Before this change every non-py tier was SILENT on that program -- it printed
``[<tier>] pass:`` and proved nothing.  Each test below therefore asserts the
*caught* direction; the base behaviour (a silent pass) is recorded in the
docstring of each tier test, and the per-tier divergence table lives in the PR
body.

The java tier is measurable, but not via ``PATH``: ``/usr/bin/javac`` is a
macOS shim that fails with "Unable to locate a Java Runtime", so the gate here
asks ``revl.run_java`` for the JDK the runner itself will use (the same
resolution ``src/revl/test.py:_java_toolchain`` performs) rather than trusting
``shutil.which``.  Gating on ``which`` would have reported the tier as
unmeasured while the runner happily ran it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The issue's D1 program, with `put` spelled against the one result-typed host
# verb (`Map.insert_if_absent -> Bool`).  `set` is the bracketed write whose
# inverse (`remove`) is what fails to reverse it.
D1 = """\
service Kv {
  fn set(k: Str, v: Str) -> Str
  fn put(k: Str, v: Str) -> Bool
}

component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
      return v
    }
    fn put(k, v) = store.insert_if_absent(k, v)
  }
}

lifecycle test "a method-body write is reversed" {
  load Store
  call kv.put("alpha", "zero")
  call kv.set("alpha", "one")
  unload Store
  assert no_residue
}
"""

# The py fold's exact message (backends/python/runtime.py:_not_reversed), minus
# the per-tier quoting of the key.  Every tier that mirrors the fold must say
# this, so the fold is pinned to the reference wording rather than to a tier's
# own idea of what went wrong.
PY_MESSAGE = "an undo did not reverse its write"
PY_PREFIX = "before the first bracketed"

needs_vitest = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists(),
    reason="vitest not installed in backends/typescript")
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
needs_rust = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")


def _java_jdk_available() -> bool:
    """Whether `revl test --backend java` will really run.

    ``shutil.which("javac")`` is the wrong question on macOS: ``/usr/bin/javac``
    exists but is a shim that prints "Unable to locate a Java Runtime". The
    runner does not use PATH either -- ``src/revl/test.py:_java_toolchain``
    (730) resolves a JDK that RESPONDS and accepts ``--release 21`` -- so this
    asks that same resolver. Gating on ``which`` would have reported the tier as
    unmeasured while the runner happily ran it. If this returns False the java
    test is UNMEASURED, not passing.
    """
    try:
        from revl.test import _java_toolchain
    except Exception:  # pragma: no cover - import-time environment problem
        return False
    return isinstance(_java_toolchain(), tuple)


needs_java = pytest.mark.skipif(
    not _java_jdk_available(),
    reason="no JDK >= 21 reachable by revl.run_java: the java tier is UNMEASURED for #2009")


def _revl_test(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=900)


def _d1_caught_on_tier(backend: str, tmp_path: Path):
    """D1 must FAIL on `backend`, and fail for the journal's reason.

    Base (before #2009) this printed `[<backend>] pass:` -- the non-reversing
    undo was invisible on the tier.
    """
    doc = tmp_path / "d1.rvl"
    doc.write_text(D1, encoding="utf-8")
    ran = _revl_test("--backend", backend, str(doc))
    out = ran.stdout + ran.stderr
    assert f"[{backend}] fail:" in out, out
    assert ran.returncode == 1, out
    assert PY_MESSAGE in out, out
    assert PY_PREFIX in out, out
    assert "alpha" in out, out
    assert "zero" in out, out


@needs_vitest
def test_ts_tier_catches_a_non_reversing_host_map_undo(tmp_path):
    """ts, base: `[ts] pass:` (silent). With the fold: `[ts] fail:` naming the
    un-reversed write. The ts journal is frame-owned -- opened at the bracket,
    closed by the disposer (`Frame.guard`/`Frame.bracket` in
    backends/typescript/runtime.ts), mirroring py `Frame._guard`."""
    _d1_caught_on_tier("ts", tmp_path)


@needs_go
def test_go_tier_catches_a_non_reversing_host_map_undo(tmp_path):
    """go, base: `[go] pass:` (silent). With the fold: `[go] fail:` with the
    byte-exact py message. The go journal is a `revlJournalFrame` on the
    provider, armed by the bracketed method and judged at teardown
    (backends/go/emit.py, `revlJournalNew`/`Begin`/`Guard`/`Judge`), mirroring
    py `_journal_note`/`_judge_frame`/`_judge_journal`."""
    _d1_caught_on_tier("go", tmp_path)


@needs_rust
def test_rust_tier_catches_a_non_reversing_host_map_undo(tmp_path):
    """rust, base: `[rust] pass:` (silent). With the fold: `[rust] fail:` with
    the byte-exact py message. The rust journal lives ON the host `Map`
    (`RevlMapJournal<V>` field, `journal_note`/`journal_judge` in
    backends/rust/emit.py) with a thread-local bracket depth standing in for
    py's `_JOURNAL` stack, and `drop_` judges -- mirroring py
    `_journal_note`/`_judge_journal` at `Map` release."""
    _d1_caught_on_tier("rust", tmp_path)


@needs_java
def test_java_tier_catches_a_non_reversing_host_map_undo(tmp_path):
    """java, base: `[java] pass: JVM: all REVL_TESTS ran, including 1 lifecycle
    test(s)` (silent). With the fold: an `AssertionError` naming the un-reversed
    write, thrown from `revlLifecycleNoResidue`. The journal is a
    `RevlMapJournal<V>` field on the host `Map`, noted from
    `insert`/`insert_if_absent`/`remove` under a thread-local depth gate and
    judged at the top of `drop()` (backends/java/emit.py) -- mirroring py
    `_journal_note`/`_judge_journal` at `Map` release."""
    _d1_caught_on_tier("java", tmp_path)
