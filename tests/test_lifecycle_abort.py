"""`abort` in a `lifecycle test` body — roadmap item 377 (F-H1.7).

H1's flagship proof — perform witnessed mutations (write, overwrite, delete),
ABORT, then assert the workspace is byte-identical to before the session — could
previously ONLY be written in host Python (tests/test_session_commit.py::
test_abort_reverts_witnessed_and_drops_deferred), because a `lifecycle test`
body had no `abort` statement. For a language whose pitch is "the guarantees
live in the language," the differentiator's own proof being inexpressible in its
own test surface is a real gap. This adds the `abort` lifecycle statement, which
drives the enclosing session frame's 245 abort — mark every live frame aborting,
replay the witnessed inverses LIFO, drop the deferral queue — so the H1 proof is
a pure `.rvl` lifecycle test.

`abort` mirrors `revl.mcp.session.Session.abort` (item 245, docs/design/
245-session-commit.md): begin_abort -> dispose LIFO (inverses replay) ->
finalize_abort. Like a session abort it tears the live composition down.

Split:
  * parse / lower / reject (no runtime needed);
  * the H1 flagship proof executed on the live cordis-py runtime — both
    directions: a clean revert passes, a leaky `undo` is caught by the
    in-language digest assertion (an assertion that can only pass is not one).
"""

import os
import subprocess
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# 1. parse + lower (no runtime)
# ---------------------------------------------------------------------------

_SRC_MIN = (
    "service Cache {\n"
    "  fn get(key: Str) -> Opt[Str]\n"
    "  emission fn put(key: Str, value: Str)\n"
    "}\n"
    "component UserCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache {\n"
    "    fn get(key) = store.get(key)\n"
    "    fn put(key, value) { effect store.insert(key, value) undo store.remove(key) }\n"
    "  }\n"
    "}\n"
    "lifecycle test \"abort tears the composition down\" {\n"
    "  load UserCache\n"
    "  call cache.put(\"k\", \"v\")\n"
    "  abort\n"
    "  assert no_residue\n"
    "}\n"
)


def test_abort_parses_and_lowers_in_a_lifecycle_body():
    ir = compile_source(_SRC_MIN, "lifecycle_abort_min.rvl")
    (lc,) = [t for t in (ir.get("tests") or []) if t.get("lifecycle")]
    steps = [s.get("step") for s in lc["body"]]
    assert steps == ["load", "call", "abort", "assert_no_residue"], steps


def test_abort_is_rejected_outside_a_lifecycle_body():
    """`abort` is a lifecycle statement; a plain `test` block is pure."""
    src = "test \"pure\" {\n  abort\n  assert 1 == 1\n}\n"
    with pytest.raises(RevlError, match=r"`abort` is only allowed in a `lifecycle test` body"):
        compile_source(src, "abort_in_pure.rvl")


def test_abort_with_nothing_loaded_is_refused():
    """An `abort` with no live composition has no session frame to abort."""
    src = (
        "lifecycle test \"vacuous\" {\n"
        "  abort\n"
        "  assert no_residue\n"
        "}\n"
    )
    with pytest.raises(RevlError, match=r"`abort` has nothing to abort"):
        compile_source(src, "abort_vacuous.rvl")


def test_abort_used_as_an_identifier_still_works():
    """`abort` is contextual: it heads a lifecycle statement only when it stands
    alone, so a program using the name elsewhere keeps compiling."""
    src = (
        "fn f(abort: Int) -> Int { return abort + 1 }\n"
        "test \"names\" { assert f(2) == 3 }\n"
    )
    compile_source(src, "abort_ident.rvl")   # no raise


# ---------------------------------------------------------------------------
# 2. the H1 flagship proof, executed on the live cordis-py runtime
# ---------------------------------------------------------------------------

needs_runtime = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


# The witnessed write / overwrite / delete + a pure workspace-digest reader, all
# inline (cross-module `use` of a witnessed extern is a separate follow-on). The
# digest is a content hash over the workspace directory — the in-language stand
# -in for "the workspace is byte-identical". {ws} is filled with a real temp dir.
_H1_TEMPLATE = r'''
type WWit = {{ path: Str, existed: Bool, prior: Str }}
type FsError = {{ code: Str }}

extern pure fn w_restore(w: WWit) -> Unit = @py {{
    import os
    if w['existed']:
        with open(w['path'], 'w') as _f:
            _f.write(w['prior'])
    else:
        if os.path.exists(w['path']):
            os.remove(w['path'])
    return
}}

extern witnessed[fs] fn w_write(path: Str, contents: Str) -> Result[WWit, FsError]
  undo w_restore(result) = @py {{
    import os
    existed = os.path.exists(path)
    prior = ''
    if existed:
        with open(path) as _f:
            prior = _f.read()
    with open(path, 'w') as _f:
        _f.write(contents)
    return Ok({{'path': path, 'existed': existed, 'prior': prior}})
}}

extern witnessed[fs] fn w_delete(path: Str) -> Result[WWit, FsError]
  undo w_restore(result) = @py {{
    import os
    with open(path) as _f:
        prior = _f.read()
    os.remove(path)
    return Ok({{'path': path, 'existed': True, 'prior': prior}})
}}

extern pure fn dir_digest(dir: Str) -> Str = @py {{
    import os, hashlib
    h = hashlib.sha256()
    for name in sorted(os.listdir(dir)):
        p = os.path.join(dir, name)
        if os.path.isfile(p):
            h.update(name.encode())
            h.update(b'\x00')
            with open(p, 'rb') as _f:
                h.update(_f.read())
    return h.hexdigest()
}}

service Fs {{
  emission fn write(path: Str, contents: Str)
  emission fn delete(path: Str)
}}
service Ws {{ fn digest() -> Str }}

component Mutator provides fs: Fs {{
  provide fs {{
    fn write(path, contents) {{ effect w_write(path, contents) }}
    fn delete(path) {{ effect w_delete(path) }}
  }}
}}

component Probe provides ws: Ws {{
  config {{ dir: Str }}
  provide ws {{
    fn digest() = dir_digest(config.dir)
  }}
}}

lifecycle test "abort restores the workspace byte-for-byte" {{
  load Probe with {{ dir: "{ws}" }}
  let pre = call ws.digest()
  unload Probe

  load Mutator
  call fs.write("{ws}/new.txt", "created")
  call fs.write("{ws}/a.txt", "overwritten")
  call fs.delete("{ws}/b.txt")
  abort

  load Probe with {{ dir: "{ws}" }}
  let post = call ws.digest()
  assert post == pre
  unload Probe
  assert no_residue
}}
'''


def _revl_test(path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl", "test", str(path)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


def _seed_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("original-a", encoding="utf-8")
    (ws / "b.txt").write_text("original-b", encoding="utf-8")
    return ws


@needs_runtime
def test_h1_flagship_abort_restores_workspace(tmp_path):
    """The H1 differentiator, entirely in-language: witnessed write/overwrite/
    delete -> abort -> assert the workspace digest equals the pre-session one.
    No host Python drives the composition — the proof lives in the `.rvl`."""
    ws = _seed_workspace(tmp_path)
    src = _H1_TEMPLATE.format(ws=ws.as_posix())
    path = tmp_path / "h1.rvl"
    path.write_text(src, encoding="utf-8")

    result = _revl_test(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS abort restores the workspace byte-for-byte" in result.stdout
    assert "[py] pass: 1 test(s) passed" in result.stdout

    # belt-and-suspenders: the on-disk workspace really is pristine after the run
    assert (ws / "a.txt").read_text(encoding="utf-8") == "original-a"
    assert (ws / "b.txt").read_text(encoding="utf-8") == "original-b"
    assert not (ws / "new.txt").exists(), "abort left a created file behind"


@needs_runtime
def test_h1_a_leaky_undo_is_caught_by_the_in_language_assert(tmp_path):
    """A component whose `undo` does not truly revert leaves the workspace
    changed, so the in-language `assert post == pre` FAILS. An assertion that can
    only pass is not an assertion — this proves the digest check has teeth."""
    ws = _seed_workspace(tmp_path)
    # break the inverse: never restore the prior/existed state
    leaky = _H1_TEMPLATE.format(ws=ws.as_posix()).replace(
        "    if w['existed']:", "    if False:")
    path = tmp_path / "h1_leak.rvl"
    path.write_text(leaky, encoding="utf-8")

    result = _revl_test(path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL abort restores the workspace byte-for-byte" in result.stdout
    assert "assertion failed" in result.stdout
