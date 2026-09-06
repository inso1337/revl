"""Issue #460: a user-origin component reaches the confined backend surface
through the sanctioned revl stdlib doors, on BOTH tiers.

# The gap this closes

The user @py backend-import rule (`src/revl/compiler.py`) refuses a user-origin
`@py` body that ambiently imports a `backends/python` module, and the old hint
sent the reader to `@py ref` — which cannot reach it, because a user-origin ref
is jailed to the compile root (item 396 option B) and backend modules live in
the install tree. So for a user-origin component both doors were shut and the
diagnostic was non-actionable.

Measured downstream (revl-harness): the toolbox that reaches
`revl_shell_classify` for the item-252 shell verdict (its terminal toolbox), and
the two that reach `revl_fs_workspace` for the confined filesystem guard (its
witnessed-shell / measured toolboxes), stopped compiling — a compile refusal, so
it regressed 35/35 -> 34/35 on BOTH tiers at once.

The fix follows #264: the trusted surface a backend module exposes is reached
through the revl stdlib, whose install-origin modules hold the sole sanctioned
door to the install tree.

  * `revl_shell_classify.classify` is `stdlib/shell.rvl`'s `classify`. Its ts
    body now reaches the classifier through the item-410 stdlib host-ref door
    (`= @ts ref`), like `stdlib/fs.rvl`, so a user-origin consumer reaches it on
    ts WITHOUT the fragile `globalThis.__revlShell` seam (which nothing installs
    for such a consumer).
  * the `revl_fs_workspace` confinement guard is `stdlib/fs.rvl`'s observation
    surface (`resolve_within` / `lexists` / `is_dir`, #264), already reachable on
    both tiers; the consumer reads the confined path with its own plain host
    body, importing no revl module.

This suite pins that a user-origin component mirroring the harness's reach now
ADMITS and behaves, on py and (with node) on ts, and that the previously-refused
ambient import is now served by a hint that names the working door.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from revl.compiler import compile_files, compile_source
from revl.errors import RevlError

from _backend_import import backend_emitter  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PY = _ROOT / "backends" / "python"
_BACKEND_TS = _ROOT / "backends" / "typescript"
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))
import revl_fs_workspace as ws  # noqa: E402

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(
    not _HAS_NODE, reason="node is required to run the emitted ts consumer")


# The user-origin component, mirroring the harness's reach: the shell verdict
# through `stdlib/shell.rvl`'s `classify`, and the confined fs guard through
# `stdlib/fs.rvl`'s observation surface. Its OWN body (`read_confined`) imports
# no revl module on either tier — it is handed an already-confined path.
_CONSUMER = """use "stdlib/shell.rvl" { classify, plan_verdict, plan_op_count }
use "stdlib/fs.rvl" { resolve_within, lexists, is_dir }

pub fn shell_verdict(cmd: Str) -> Str {
  return plan_verdict(classify(cmd))
}

pub fn shell_ops(cmd: Str) -> Int {
  return plan_op_count(classify(cmd))
}

extern pure fn read_confined(real: Str) -> Str
  = @py {
    try:
        with open(real, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "error: " + str(e)
  }
  = @ts {
    try {
      return process.getBuiltinModule("node:fs").readFileSync(real, "utf8")
    } catch (e: any) {
      return "error: " + (e && e.message ? e.message : String(e))
    }
  }

pub fn read_workspace_file(path: Str) -> Str {
  return match resolve_within(path) {
    Ok(real) => read_confined(real),
    Err(e) => "refused: " + e.code
  }
}

pub fn guard_lexists(path: Str) -> Str {
  return match lexists(path) {
    Ok(b) => "ok",
    Err(e) => "refused: " + e.code
  }
}
"""

#: (kind, arg, expected). Shell verdicts + confined reads + guard refusals.
_EXPECTED = [
    ["verdict", "mv a b", "witnessed"],
    ["verdict", "rm -rf x", "emission"],
    ["verdict", "echo hi | cat", "emission"],
    ["read", "a.txt", "alpha\n"],
    ["read", "link_out", "refused: EOUTSIDE"],
    ["read", "../outside/secret.txt", "refused: EOUTSIDE"],
    ["guard", "sub", "ok"],
    ["guard", "..", "refused: EOUTSIDE"],
]


def _build_workspace(base: Path) -> Path:
    root = base / "ws"
    outside = base / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(outside / "secret.txt", root / "link_out")
    return root


@pytest.fixture
def workspace(tmp_path):
    return _build_workspace(tmp_path)


def _write_consumer(tmp_path: Path) -> Path:
    proj = tmp_path / "consumer"
    proj.mkdir()
    app = proj / "app.rvl"
    app.write_text(_CONSUMER, encoding="utf-8")
    return app


# ===========================================================================
# 1. the ambient import is refused, and the hint now names the WORKING door
# ===========================================================================

@pytest.mark.parametrize("module, door", [
    ("revl_shell_classify", 'use "stdlib/shell.rvl" { classify }'),
    ("revl_fs_workspace", 'use "stdlib/fs.rvl" { resolve_within, lexists, is_dir }'),
])
def test_ambient_import_refused_hint_points_at_stdlib(module, door):
    body = (
        "extern pure fn reach(x: Str) -> Str = @py {\n"
        f"    import {module}\n"
        "    return x\n"
        "}\n"
    )
    with pytest.raises(RevlError) as exc:
        compile_source(body)
    hint = exc.value.hint or ""
    assert door in hint, hint
    assert "`@py ref` cannot reach" in hint, hint


# ===========================================================================
# 2. the user-origin consumer ADMITS, inheriting the stdlib-origin refs
# ===========================================================================

def test_consumer_admits_and_inherits_stdlib_refs(tmp_path):
    app = _write_consumer(tmp_path)
    ir = compile_files([str(app)])
    externs = {e["name"]: e for e in ir["externs"]}

    # classify reaches the classifier through the stdlib host-ref door on ts.
    assert externs["classify"]["refs"]["ts"]["symbol"] == "classify"
    assert externs["classify"]["refs"]["ts"]["root"] == "stdlib"
    assert externs["classify"]["refs"]["ts"]["path"] == \
        "backends/typescript/revl_shell_classify_ts.ts"

    # the fs observation externs arrive with their #264 stdlib refs intact.
    for name, symbol in {"resolve_within": "fsResolveWithin",
                         "lexists": "fsLexists", "is_dir": "fsIsDir"}.items():
        assert externs[name]["refs"]["ts"]["symbol"] == symbol
        assert externs[name]["refs"]["ts"]["root"] == "stdlib"

    # the consumer's OWN body needs — and gains — no ref of its own.
    assert "refs" not in externs["read_confined"]


def test_consumer_body_imports_no_revl_module():
    """The property #264 established, restated for this consumer: it reaches the
    confined surfaces through revl, so its own body carries no import/require of
    a revl module and no path into the install tree."""
    for forbidden in ("revl_shell_classify", "revl_fs_workspace", "revl_fs_ts",
                      "__revlShell", "__REVL_STDLIB_REF_ROOT__", "../.."):
        assert forbidden not in _CONSUMER, forbidden


# ===========================================================================
# 3. it behaves, on py and (with node) on ts
# ===========================================================================

def _py_answers(app: Path, root: Path, monkeypatch) -> list[list[str]]:
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    src = backend_emitter("python").emit(compile_files([str(app)]))
    mod = types.ModuleType("revl_460_consumer_gen")
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(src, "<460 consumer artifact>", "exec"), mod.__dict__)
        call = {
            "verdict": mod.shell_verdict,
            "read": mod.read_workspace_file,
            "guard": mod.guard_lexists,
        }
        return [[kind, arg, call[kind](arg)] for kind, arg, _ in _EXPECTED]
    finally:
        sys.modules.pop("revl_460_consumer_gen", None)


def _ts_answers(app: Path, root: Path) -> list[list[str]]:
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "issue460_consumer.ts"
    module.write_text(
        backend_emitter("typescript").emit(compile_files([str(app)]),
                                           runtime_import="../../runtime.ts"),
        encoding="utf-8")
    harness = generated / "_issue460_harness.ts"
    harness.write_text(
        ";(globalThis as any).__REVL_STDLIB_REF_ROOT__ = process.argv[2]\n"
        "const calls = JSON.parse(process.argv[3]) as string[][]\n"
        "const mod = await import('./issue460_consumer.ts')\n"
        "const fn: Record<string, (p: string) => string> = {\n"
        "  verdict: mod.shell_verdict, read: mod.read_workspace_file,\n"
        "  guard: mod.guard_lexists }\n"
        "process.stdout.write(JSON.stringify(\n"
        "  calls.map(([kind, arg]) => [kind, arg, fn[kind](arg)])))\n",
        encoding="utf-8")
    env = dict(os.environ, **{ws.WORKSPACE_ENV: str(root)})
    try:
        proc = subprocess.run(
            ["node", str(harness), str(_ROOT),
             json.dumps([[k, a] for k, a, _ in _EXPECTED])],
            capture_output=True, text=True, cwd=str(_BACKEND_TS), env=env)
    finally:
        module.unlink(missing_ok=True)
        harness.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"ts consumer harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_consumer_behaves_on_py(workspace, tmp_path, monkeypatch):
    app = _write_consumer(tmp_path)
    assert _py_answers(app, workspace, monkeypatch) == _EXPECTED


@_needs_node
def test_consumer_behaves_on_ts(workspace, tmp_path):
    """The tier that regressed on the fragile global seam: the emitted `classify`
    now loads the classifier through the stdlib host-ref door, so this runs with
    only the runner-provided install root set — nothing pre-installs
    `globalThis.__revlShell`."""
    app = _write_consumer(tmp_path)
    assert _ts_answers(app, workspace) == _EXPECTED
