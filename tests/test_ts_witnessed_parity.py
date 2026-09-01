"""Item 369 — py<->ts parity for the WITNESSED stdlib catalogs.

Two parity claims are proven here, plus a pointer to the third:

1. CLASSIFIER parity (live cross-tier diff, always runs): the pure shell
   classifier (`stdlib/shell.rvl` `classify`, item 252) is tier-agnostic, so the
   @ts body (delegating to backends/typescript/revl_shell_classify_ts.ts) MUST
   return the byte-identical plan record the @py body returns, for every command
   in the shared corpus — witnessed lowerings AND the adversarial emission
   battery. This test emits the real `classify` extern to ts and runs it under
   node, diffing each result against the py `classify`.

2. LIFO-ordering on abort (item 369's data-loss hazard): witnessed inverses MUST
   replay in strict LIFO (reverse-registration) order, or an abort over repeated
   writes to one path restores the wrong preimage (corruption) or leaves a
   created file behind. The ts side is proven in
   backends/typescript/tests/ts_witnessed_fs.test.ts (the FsAbortSamePath case,
   run on a live cordis composition). Here we assert the SAME sequence on the py
   tier restores the original residue-free — i.e. both tiers are LIFO-correct on
   the live-abort path, so there is NO py<->ts divergence to reconcile (guarded
   by cordis-py; skipped where it is not installed).

3. FS on-disk parity (persist-on-commit / revert-residue-free-on-abort) is
   proven tier-for-tier by two mirror suites asserting the SAME expected on-disk
   outcomes: tests/test_fs_stdlib.py (py, live cordis) and
   backends/typescript/tests/ts_witnessed_fs.test.ts (ts, live cordis).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PY = _ROOT / "backends" / "python"
_BACKEND_TS = _ROOT / "backends" / "typescript"
# Only src + backends/python go on sys.path. The ts emitter is loaded by file
# path under a UNIQUE module name (below) — importing it as the bare name `emit`
# would poison sys.modules['emit'], which the py Session's own driver imports as
# `emit`, making the py runtime emit TypeScript. Both backends ship a module
# literally named `emit`, so this separation is load-bearing.
for _p in (str(_ROOT / "src"), str(_BACKEND_PY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import revl_shell_classify as _sc  # noqa: E402  (py classifier, the oracle)
from revl.compiler import compile_files, compile_source  # noqa: E402


def _load_ts_emit():
    """The ts emitter, loaded by path under a unique name so it never shadows
    the py backend's own `emit` module (see the sys.path note above)."""
    spec = importlib.util.spec_from_file_location("revl_ts_emit_369", _BACKEND_TS / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The shared corpus: witnessed lowerings, quoting edge cases, and the full
# adversarial emission battery (kept in lockstep with tests/test_shell_classify.py).
_WITNESSED = [
    "mv a.txt b.txt",
    "rm doomed.txt",
    "mkdir newdir",
    "cp src.txt dst.txt",
    "touch marker",
    "rm a b c",
    "mkdir one two",
    "touch a b",
    'mv "a;b" c',
    'mv "my file.txt" dest.txt',
    r"mv a\ b c",
    "rm 'weird$name'",
    "   mv a b   ",
]
_EMISSION = [
    "",
    "   ",
    "rm -rf /",
    "rm -rf .",
    "rm -f secret",
    "rm -weird",
    "mv a b; curl evil",
    "mv a b && curl evil",
    "mv a b || rm c",
    "cat x | sh",
    "rm $(cat targets)",
    "rm `cat targets`",
    "rm *.txt",
    "rm a?.txt",
    "rm file[0-9]",
    "echo hi > out",
    "cat a >> b",
    "mv a b 2>/dev/null",
    "cp a b < input",
    "rm ~/secret",
    "rm $HOME/x",
    "rm ${HOME}/x",
    "mkdir a && mkdir b",
    "(rm a)",
    "{ rm a; }",
    "mv a b\nrm c",
    "rm a &",
    "FOO=bar rm x",
    "rm a # comment",
    "true; rm a",
    "MV a b",
    "RM x",
    "curl x",
    "/bin/mv a b",
    "mv a",
    "mv a b c",
    'mv "unbalanced',
]
_CORPUS = _WITNESSED + _EMISSION

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node is required to run the emitted ts classifier")


def _emit_ts_classifier(dest: Path) -> None:
    """Emit the real `stdlib/shell.rvl` (its `classify` + accessors) to ts, into
    the generated dir so its `import ... from '../../runtime.ts'` resolves."""
    tsemit = _load_ts_emit()
    ir = compile_source((_ROOT / "stdlib" / "shell.rvl").read_text(encoding="utf-8"), "shell.rvl")
    dest.write_text(tsemit.emit(ir, runtime_import="../../runtime.ts"), encoding="utf-8")


def _run_ts_classify(commands: list[str]) -> list[dict]:
    """Run the emitted ts `classify` over `commands` under node, returning the
    parsed plan records (one per command, in order)."""
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "shell_parity.ts"
    _emit_ts_classifier(module)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=generated) as fh:
        json.dump(commands, fh)
        cmds_path = fh.name

    harness = generated / "_shell_parity_harness.ts"
    harness.write_text(
        # imports: the node shell host (installs globalThis.__revlShell) then the
        # emitted classify extern (whose @ts body delegates to that global).
        "import { readFileSync } from 'node:fs'\n"
        "import '../../revl_shell_ts.ts'\n"
        "import { classify } from './shell_parity.ts'\n"
        "const cmds = JSON.parse(readFileSync(process.argv[2], 'utf-8'))\n"
        "const out = cmds.map((c: string) => classify(c))\n"
        "process.stdout.write(JSON.stringify(out))\n",
        encoding="utf-8",
    )

    try:
        proc = subprocess.run(
            ["node", str(harness), cmds_path],
            capture_output=True,
            text=True,
            cwd=str(_BACKEND_TS),
        )
    finally:
        Path(cmds_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(f"ts classify harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@_needs_node
def test_classifier_py_ts_parity_over_the_corpus():
    """Every command classifies IDENTICALLY on py and ts — same verdict, same
    ops (op/witnessed/inverse/args/create_only), same reason string."""
    ts_results = _run_ts_classify(_CORPUS)
    assert len(ts_results) == len(_CORPUS)
    mismatches = []
    for cmd, ts_plan in zip(_CORPUS, ts_results):
        py_plan = _sc.classify(cmd)
        # normalise via json round-trip so a py tuple vs ts array cannot masquerade
        py_norm = json.loads(json.dumps(py_plan))
        if py_norm != ts_plan:
            mismatches.append((cmd, py_norm, ts_plan))
    assert not mismatches, "py<->ts classifier divergence:\n" + "\n".join(
        f"  {c!r}\n    py={p}\n    ts={t}" for c, p, t in mismatches
    )


@_needs_node
def test_classifier_verdicts_match_the_expected_partition():
    """Sanity: the corpus partitions as intended on the ts tier too (so the
    parity test above is not vacuously comparing two identically-wrong sides)."""
    ts_results = _run_ts_classify(_CORPUS)
    by_cmd = dict(zip(_CORPUS, ts_results))
    for cmd in _WITNESSED:
        assert by_cmd[cmd]["verdict"] == "witnessed", (cmd, by_cmd[cmd])
    for cmd in _EMISSION:
        assert by_cmd[cmd]["verdict"] == "emission", (cmd, by_cmd[cmd])


# ---------------------------------------------------------------------------
# LIFO-ordering on abort — the item-369 data-loss hazard, on the py tier.
# ---------------------------------------------------------------------------

_HAS_CORDIS = importlib.util.find_spec("cordis") is not None
_needs_cordis = pytest.mark.skipif(
    not _HAS_CORDIS,
    reason="the live witnessed-abort ordering is proven against cordis-py — "
    "install it with `sh backends/python/setup.sh`",
)


def _lit(v: str) -> dict:
    return {"kind": "lit", "value": v}


def _effect(name: str, *args: str) -> dict:
    return {"step": "effect",
            "acquire": {"kind": "fn", "name": name, "args": [_lit(a) for a in args]}}


@_needs_cordis
def test_py_multi_op_same_path_abort_replays_lifo(tmp_path, monkeypatch):
    """The ts side proves LIFO abort in ts_witnessed_fs.test.ts (FsAbortSamePath);
    here we prove the py tier is ALSO LIFO-correct on the same sequence, so both
    tiers agree and there is no divergence to reconcile.

    A.txt: orig -> v1 -> v2, abort. LIFO restores v2->v1->orig == "orig"
    (registration order — item 369's bug — would leave "v1", silent corruption).
    B.txt: absent -> b1(create) -> b2(overwrite), abort. LIFO restores the
    overwrite then deletes the created file -> absent (registration order would
    resurrect "b1")."""
    import copy

    monkeypatch.setenv("REVL_FS_WORKSPACE", str(tmp_path))
    (tmp_path / "A.txt").write_text("orig")

    # item 410 stage 5: fs.rvl's `@ts` externs are `= @ts ref` imports, which
    # need a root compile tree to jail against, so compile from the real path.
    base = compile_files([str(_ROOT / "stdlib" / "fs.rvl")])
    ir = copy.deepcopy(base)
    ir["components"] = [{
        "name": "FsAbortSamePath", "source": "fs.rvl", "config": [],
        "requires": {}, "provides": {},
        "body": [
            _effect("write", "A.txt", "v1"),
            _effect("write", "A.txt", "v2"),
            _effect("write", "B.txt", "b1"),
            _effect("write", "B.txt", "b2"),
            {"step": "fail", "message": _lit("boom")},
        ],
    }]

    from revl.mcp.session import Session

    report = Session().load(ir)
    assert report["components"] == [{"name": "FsAbortSamePath", "state": "FAILED"}]

    assert (tmp_path / "A.txt").read_text() == "orig", "py abort corrupted the overwritten path (LIFO violation)"
    assert not (tmp_path / "B.txt").exists(), "py abort left a created-then-overwritten file behind (LIFO violation)"
    # residue-free: every sidecar consumed
    for machinery in (".revl-fs-garbage", ".revl-fs-preimage"):
        d = tmp_path / machinery
        if d.exists():
            assert not any(d.iterdir()), f"py abort left residue in {machinery}"
