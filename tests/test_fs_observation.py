"""The stdlib FS module's OBSERVATION half — `resolve_within`, `lexists`,
`is_dir` (stdlib/fs.rvl, backends/typescript/revl_fs_ts.ts).

# Why this surface exists

A consumer's own host body needs the confinement decision. Not the mutations —
those are the witnessed catalog and they are reachable — but the plain question
"may I look at this path, and what is there?", so a `@py`/`@ts` body can read a
file, or verify a write took, without deciding for itself where the workspace
boundary is.

Every door to it was shut, and each one for a good reason:

  * item 396(B) jails a USER-origin `= @ts ref` to the user COMPILE-ROOT tree,
    and `backends/typescript/revl_fs_ts.ts` lives in the revl install tree;
  * item 410's second root (`__REVL_STDLIB_REF_ROOT__`) is reserved for
    INSTALL-origin modules, which a consumer's file is not — origin decides,
    and an escaping `use` cannot forge it (the 8de55eb fix);
  * item 422 F1 removed the unconfined primitives the deprecated
    `globalThis.__revlFs` seam published, correctly: an exported unconfined
    rename IS that finding.

What was left for a user-origin `@ts` body was a relative path GUESS into the
install tree — `require("../../revl_fs_ts.ts")`, three candidates deep — which
breaks whenever revl moves. revl-harness hit exactly that three times (F-H47.5,
F-H58.11, F-H71.2).

The door is now a revl one rather than a host one: `use "stdlib/fs.rvl"
{ resolve_within, lexists, is_dir }`, ask revl for the confined path, then read
it with plain `os` / `node:fs` in your own body. No host import on either tier,
so there is nothing to guess and nothing to break when the install moves.

# What this suite pins

1. THE PREMISE. The three doors above are still shut. If one of them ever opens,
   this surface is the more expensive answer and the tests below say so out loud
   rather than leaving a stale blocker in a docstring.
2. THE JAIL IS UNCHANGED. The observations route through the SAME family-1
   `resolve_within` guard every mutation and every inverse uses, and refuse with
   the same `ConfinementError` vocabulary. A widened jail here would recreate
   what 422 F1 removed.
3. OBSERVATION ONLY. No write primitive is exposed, on either tier.
4. IDENTICAL SEMANTICS ON BOTH TIERS, executed rather than claimed: one case
   corpus, run through the REAL emitted py bodies and the REAL emitted ts `ref`
   thunks over the SAME workspace on disk, diffed record for record.
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

from revl.compiler import compile_files
from revl.errors import RevlError

from _backend_import import backend_emitter  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PY = _ROOT / "backends" / "python"
_BACKEND_TS = _ROOT / "backends" / "typescript"
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))
import revl_fs_workspace as ws  # noqa: E402

_FS_RVL = _ROOT / "stdlib" / "fs.rvl"
_TS_HOST_REL = "../backends/typescript/revl_fs_ts.ts"

#: The three observation externs, and the guard entry point each is built from.
_OBSERVERS = {
    "resolve_within": "fsResolveWithin",
    "lexists": "fsLexists",
    "is_dir": "fsIsDir",
}

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(
    not _HAS_NODE, reason="node is required to run the emitted ts observation thunks")


# ===========================================================================
# 1. THE PREMISE: the three doors are still shut
# ===========================================================================

def test_a_user_origin_ts_ref_still_cannot_reach_the_install_tree(tmp_path):
    """Door 1, item 396(B). A user module's `= @ts ref` into the real
    `backends/typescript/revl_fs_ts.ts` is refused: origin selects the user root
    set, so the install tree is simply not a jail a user ref may reach. The day
    this stops being true, this whole surface has a cheaper alternative."""
    proj = tmp_path / "proj"
    proj.mkdir()
    target = _BACKEND_TS / "revl_fs_ts.ts"
    rel = os.path.relpath(target, proj)
    app = proj / "app.rvl"
    app.write_text(
        "pub extern pure fn peek(p: Str) -> Str\n"
        "  = @py { return p }\n"
        f'  = @ts ref resolveWithin from "{rel}"\n',
        encoding="utf-8")
    with pytest.raises(RevlError) as exc:
        compile_files([str(app)])
    msg = str(exc.value)
    assert "OUTSIDE the root compile tree" in msg
    assert "revl_fs_ts.ts" in msg


def test_a_user_module_never_becomes_install_origin(tmp_path):
    """Door 2, item 410. The second ref root is selected by the DECLARING
    module's origin, not by the ref's target, so a user file cannot be stamped
    `root: stdlib` and inherit the install-tree jail. Proven positively (the
    real stdlib/fs.rvl IS stdlib-origin) and negatively (a user module's own ref
    carries no root kind)."""
    stdlib_ir = compile_files([str(_FS_RVL)])
    for ext in stdlib_ir["externs"]:
        assert ext["refs"]["ts"]["root"] == "stdlib", ext["name"]

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "helper.ts").write_text(
        "export function peek(p: string): string { return p }\n", encoding="utf-8")
    app = proj / "app.rvl"
    app.write_text(
        "pub extern pure fn peek(p: Str) -> Str\n"
        "  = @py { return p }\n"
        '  = @ts ref peek from "helper.ts"\n',
        encoding="utf-8")
    ext = compile_files([str(app)])["externs"][0]
    assert "root" not in ext["refs"]["ts"], \
        "a user-origin ref gained a root kind; the 410 two-root split has moved"


def test_the_deprecated_seam_publishes_no_observation_shortcut():
    """Door 3, item 422 F1. The `globalThis.__revlFs` seam is not an answer
    either: it publishes the guard entry points only (its `exists`/`isFile`/
    `isDir` conveniences and every write primitive were removed), and reaching
    even that much still requires IMPORTING the install-tree module, which is
    the guess this surface exists to delete. Pinned on the py peer's shape here;
    the ts seam's own contents are pinned in
    backends/typescript/tests/fs_confinement_families.test.ts."""
    for gone in ("exists", "is_file", "is_dir", "replace", "remove",
                 "write_file", "mkdir_one", "rmdir", "snapshot"):
        assert not hasattr(ws, gone), \
            f"the guard module exposes `{gone}`; item 422 F1 removed it"


# ===========================================================================
# 2. THE SHAPE: observation only, same jail, no host import
# ===========================================================================

def test_the_observers_are_pure_and_carry_no_capability():
    """Observation costs nothing at either gate: `revl audit` enumerates no
    effect and the ClassMap gives these no action class. The mutations keep
    their `witnessed[fs]`, which is what this must not quietly become."""
    ir = compile_files([str(_FS_RVL)])
    classes = {e["name"]: (e["class"], tuple(e.get("capabilities") or ()))
               for e in ir["externs"]}
    for name in _OBSERVERS:
        assert classes[name] == ("pure", ()), \
            f"{name} changed classification; observation must stay pure"
    for mutation in ("write", "rm", "move", "mkdir"):
        assert classes[mutation] == ("witnessed", ("fs",))


def test_the_module_exposes_no_write_primitive():
    """The absence IS the item 422 F1 finding, and the downstream consumer
    explicitly does not want them back. An `unrm`-shaped or `replace`-shaped
    export here would be that finding with a new name. The catalog's four
    mutations are witnessed and revertible; nothing else may mutate."""
    ir = compile_files([str(_FS_RVL)])
    mutating = {e["name"] for e in ir["externs"] if e["class"] == "witnessed"}
    assert mutating == {"write", "rm", "move", "mkdir"}
    inverses = {e["name"] for e in ir["externs"]
                if e["class"] == "pure"} - set(_OBSERVERS)
    assert inverses == {"restore", "unrm", "unmove", "rmdir_if_empty"}, \
        "a new `pure` extern appeared on stdlib/fs.rvl; if it mutates, it is " \
        "item 422 F1 again"


def test_each_observer_refs_its_own_entry_point_in_the_shipped_host():
    """The ts tier goes through the SAME module the mutations do, by `ref`
    rather than by guess: no `globalThis`, no relative specifier in the body."""
    ir = compile_files([str(_FS_RVL)])
    refs = {e["name"]: e["refs"]["ts"] for e in ir["externs"]}
    for name, symbol in _OBSERVERS.items():
        assert refs[name]["symbol"] == symbol
        assert refs[name]["path"] == "backends/typescript/revl_fs_ts.ts"
    source = (_BACKEND_TS / "revl_fs_ts.ts").read_text(encoding="utf-8")
    for symbol in _OBSERVERS.values():
        assert f"export function {symbol}(" in source


def test_the_observations_route_through_the_family_1_guard():
    """The jail is unchanged: every observation body resolves through
    `resolve_within`, the same `named-endpoint` guard the four mutations and
    every inverse use, and reads only through a listed READ helper. The generic
    version of this scan (over every extern in the module) is
    tests/test_fs_confinement_families.py; this is the observation-specific
    half, stated where the surface is."""
    assert ws.PATH_FAMILIES["named-endpoint"] == ("resolve_within",)
    assert set(ws.READ_HELPERS) == {"lexists_confined", "is_dir_confined"}
    text = _FS_RVL.read_text(encoding="utf-8")
    for name, reads in (("resolve_within", ()),
                        ("lexists", ("_ws.lexists_confined(",)),
                        ("is_dir", ("_ws.is_dir_confined(",))):
        body = text.split(f"pub extern pure fn {name}(")[1].split("= @ts ref")[0]
        assert "target = _ws.resolve_within(path)" in body, name
        for read in reads:
            assert read in body, name


# ===========================================================================
# 3. THE BEHAVIOUR, and the cross-tier diff
# ===========================================================================
#
# One corpus, run on both tiers over the SAME workspace on disk. `@ROOT@` is
# substituted with the workspace root by each runner, so an absolute-path case
# can be written machine-independently.

_CASES: list[str] = [
    "a.txt",                    # an ordinary file
    "sub",                      # a directory
    "sub/b.txt",                # a file below it
    "sub/../a.txt",             # normalised back inside
    "missing.txt",              # a name that does not exist yet (a write target)
    "deep/missing.txt",         # ...with a missing parent too
    "",                         # empty: relative to the root, so the root
    ".",                        # the root itself
    ".revl-fs-garbage",         # a sidecar name, absent here
    "link_in",                  # a symlink INSIDE the root
    "a.txt/nested",             # a non-directory used as a directory
    "@ROOT@/a.txt",             # absolute, inside
    "..",                       # the parent: outside
    "../outside/secret.txt",    # a traversal, outside
    "link_out",                 # a symlink pointing OUT of the root (must #1)
    "sub/../../outside/secret.txt",   # normalisation cannot launder it
    "/etc/hosts",               # absolute, outside
    "nul\x00byte",              # a name no filesystem can hold
]


def _build_workspace(base: Path) -> Path:
    """`base/ws` (the root) beside `base/outside` (what must stay unreachable)."""
    root = base / "ws"
    outside = base / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("beta\n", encoding="utf-8")
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(root / "a.txt", root / "link_in")
    os.symlink(outside / "secret.txt", root / "link_out")
    return root


def _err_record(value) -> dict:
    """Normalise an `Err` payload to the `FsError` fields, whatever the tier's
    record type is (a py dict, a ts object)."""
    if isinstance(value, dict):
        return {k: value[k] for k in ("code", "message", "path")}
    return {k: getattr(value, k) for k in ("code", "message", "path")}


def _normalise(result, root: str) -> dict:
    """One tier's answer as comparable JSON, with the machine-specific root
    folded back to `@ROOT@` so a failure diff is readable."""
    def fold(v):
        return v.replace(root, "@ROOT@") if isinstance(v, str) else v

    if type(result).__name__ == "Ok":
        return {"kind": "Ok", "value": fold(result.value)}
    return {"kind": "Err", "value": {k: fold(v)
                                     for k, v in _err_record(result.value).items()}}


def _py_module(root: str):
    """The REAL stdlib/fs.rvl emitted to py and loaded, with the workspace root
    configured. Bodies run against the shipped guard, not a stub."""
    ir = compile_files([str(_FS_RVL)])
    src = backend_emitter("python").emit(ir)
    mod = types.ModuleType("revl_fs_observation_gen")
    sys.modules[mod.__name__] = mod
    exec(compile(src, "<fs observation artifact>", "exec"), mod.__dict__)
    return mod


def _py_answers(root: Path, monkeypatch) -> list[dict]:
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    real_root = os.path.realpath(str(root))
    mod = _py_module(real_root)
    try:
        out = []
        for case in _CASES:
            arg = case.replace("@ROOT@", real_root)
            out.append({
                "input": case,
                "resolve_within": _normalise(mod.resolve_within(arg), real_root),
                "lexists": _normalise(mod.lexists(arg), real_root),
                "is_dir": _normalise(mod.is_dir(arg), real_root),
            })
        return out
    finally:
        sys.modules.pop("revl_fs_observation_gen", None)


def _ts_answers(root: Path) -> list[dict]:
    """The same corpus through the REAL emitted ts `ref` thunks, under node.

    The thunk joins the recorded relative path against the runner-provided
    `globalThis.__REVL_STDLIB_REF_ROOT__` (item 410's second root), set here to
    the repo root exactly as the node placement runner self-derives it — so this
    exercises the shipped door, not a hand-written import."""
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "fs_observation.ts"
    ir = compile_files([str(_FS_RVL)])
    module.write_text(
        backend_emitter("typescript").emit(ir, runtime_import="../../runtime.ts"),
        encoding="utf-8")

    harness = generated / "_fs_observation_harness.ts"
    harness.write_text(
        ";(globalThis as any).__REVL_STDLIB_REF_ROOT__ = process.argv[2]\n"
        "const root = process.argv[3]\n"
        "const cases = JSON.parse(process.argv[4]) as string[]\n"
        "const mod = await import('./fs_observation.ts')\n"
        "const out = cases.map((c: string) => {\n"
        "  const arg = c.split('@ROOT@').join(root)\n"
        "  return {\n"
        "    input: c,\n"
        "    resolve_within: mod.resolve_within(arg),\n"
        "    lexists: mod.lexists(arg),\n"
        "    is_dir: mod.is_dir(arg),\n"
        "  }\n"
        "})\n"
        "process.stdout.write(JSON.stringify(out))\n",
        encoding="utf-8")

    env = dict(os.environ, **{ws.WORKSPACE_ENV: str(root)})
    proc = subprocess.run(
        ["node", str(harness), str(_ROOT), os.path.realpath(str(root)),
         json.dumps(_CASES)],
        capture_output=True, text=True, cwd=str(_BACKEND_TS), env=env)
    if proc.returncode != 0:
        raise AssertionError(f"ts observation harness failed:\n{proc.stderr}")

    real_root = os.path.realpath(str(root))

    def fold(v):
        return v.replace(real_root, "@ROOT@") if isinstance(v, str) else v

    out = []
    for row in json.loads(proc.stdout):
        folded = {"input": row["input"]}
        for name in _OBSERVERS:
            r = row[name]
            value = r["value"]
            folded[name] = {
                "kind": r["kind"],
                "value": ({k: fold(value[k]) for k in ("code", "message", "path")}
                          if r["kind"] == "Err" else fold(value)),
            }
        out.append(folded)
    return out


@pytest.fixture
def workspace(tmp_path):
    return _build_workspace(tmp_path)


def test_py_answers_the_corpus_as_specified(workspace, monkeypatch):
    """The py tier's table, written out. This is the oracle the ts tier is
    diffed against, so it is asserted explicitly rather than derived."""
    answers = {row["input"]: row for row in _py_answers(workspace, monkeypatch)}

    def ok(case, name, value):
        assert answers[case][name] == {"kind": "Ok", "value": value}, \
            (case, name, answers[case][name])

    def refused(case, code):
        for name in _OBSERVERS:
            got = answers[case][name]
            assert got["kind"] == "Err", (case, name, got)
            assert got["value"]["code"] == code, (case, name, got)

    ok("a.txt", "resolve_within", "@ROOT@/a.txt")
    ok("a.txt", "lexists", True)
    ok("a.txt", "is_dir", False)
    ok("sub", "is_dir", True)
    ok("sub/b.txt", "resolve_within", "@ROOT@/sub/b.txt")
    ok("sub/../a.txt", "resolve_within", "@ROOT@/a.txt")
    # a target that does not exist yet RESOLVES (a `write`/`mkdir` target must),
    # and answers the existence question honestly.
    ok("missing.txt", "resolve_within", "@ROOT@/missing.txt")
    ok("missing.txt", "lexists", False)
    ok("deep/missing.txt", "resolve_within", "@ROOT@/deep/missing.txt")
    # the root itself, spelled three ways
    for spelling in ("", ".", "@ROOT@/a.txt"):
        assert answers[spelling]["resolve_within"]["kind"] == "Ok"
    ok("", "resolve_within", "@ROOT@")
    ok(".", "is_dir", True)
    ok("@ROOT@/a.txt", "resolve_within", "@ROOT@/a.txt")
    # must #1: the symlink is resolved BEFORE the membership test, so one
    # pointing inside is admitted at its TARGET...
    ok("link_in", "resolve_within", "@ROOT@/a.txt")
    ok("link_in", "lexists", True)
    # ...and one pointing out is refused, not followed.
    refused("link_out", "EOUTSIDE")
    refused("..", "EOUTSIDE")
    refused("../outside/secret.txt", "EOUTSIDE")
    refused("sub/../../outside/secret.txt", "EOUTSIDE")
    refused("/etc/hosts", "EOUTSIDE")
    refused("nul\x00byte", "EINVAL")
    # the refusal names the resolved offender, never leaks a fact about it
    assert answers["link_out"]["lexists"]["value"]["path"].endswith("secret.txt")


def test_no_workspace_root_is_an_operator_refusal_not_a_boundary_one(
        workspace, monkeypatch):
    """`EWORKSPACE` stays distinguishable from `EOUTSIDE`: an unconfigured root
    is an operator mistake, and a consumer that cannot tell the two apart
    reports a security refusal for a missing env var (which is what the
    downstream relay's two distinct messages are for)."""
    monkeypatch.delenv(ws.WORKSPACE_ENV, raising=False)
    mod = _py_module(os.path.realpath(str(workspace)))
    try:
        for name in _OBSERVERS:
            result = getattr(mod, name)("a.txt")
            assert type(result).__name__ == "Err"
            assert _err_record(result.value)["code"] == "EWORKSPACE"
    finally:
        sys.modules.pop("revl_fs_observation_gen", None)


def test_observation_never_mutates_the_workspace(workspace, monkeypatch):
    """Observation is observation: not one of the three creates the sidecar
    directories, a snapshot, or anything else. (`write` creates the preimage
    dir on its first call; these must not.)"""
    before = sorted(p.name for p in workspace.iterdir())
    _py_answers(workspace, monkeypatch)
    assert sorted(p.name for p in workspace.iterdir()) == before


# ===========================================================================
# 4. THE DOOR ITSELF: a consumer reaches the jail importing nothing
# ===========================================================================

_CONSUMER = """use "stdlib/fs.rvl" { resolve_within, lexists, is_dir }

// The consumer's OWN host body. It is handed an already-CONFINED absolute path
// and reads it with the plain host filesystem module — importing nothing from
// revl on either tier, which is the whole point: there is no specifier to guess
// and nothing to break when the install moves.
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
    Ok(real) => match lexists(path) {
      Ok(found) => read_confined(real),
      Err(e) => "refused: " + e.code
    },
    Err(e) => "refused: " + e.code
  }
}

pub fn verify_dir(path: Str) -> Str {
  return match is_dir(path) {
    Ok(d) => "ok",
    Err(e) => "refused: " + e.code
  }
}
"""

#: What the consumer must answer on BOTH tiers. `a.txt` is read through the
#: confined path; the two escapes are refused with the boundary code, never
#: followed.
_CONSUMER_EXPECTED = [
    ["read", "a.txt", "alpha\n"],
    ["read", "link_out", "refused: EOUTSIDE"],
    ["read", "../outside/secret.txt", "refused: EOUTSIDE"],
    ["dir", "sub", "ok"],
    ["dir", "..", "refused: EOUTSIDE"],
]


def _write_consumer(tmp_path: Path) -> Path:
    proj = tmp_path / "consumer"
    proj.mkdir()
    app = proj / "app.rvl"
    app.write_text(_CONSUMER, encoding="utf-8")
    return app


def test_the_consumer_body_imports_nothing_from_revl():
    """The gap, stated as a property. The body the consumer writes carries no
    `require`/`import` of a revl module and no path into the install tree — it
    asks revl for the confinement decision instead. A regression here means the
    guess is back."""
    for forbidden in ("revl_fs_ts", "revl_fs_workspace", "__revlFs",
                      "__REVL_STDLIB_REF_ROOT__", "../.."):
        assert forbidden not in _CONSUMER, forbidden


def test_a_consumer_compiles_against_the_observers(tmp_path):
    """User-origin, no ref of its own, and the three observers arrive with their
    stdlib-origin refs intact — the consumer inherits the install-tree door
    without being able to open it itself."""
    app = _write_consumer(tmp_path)
    ir = compile_files([str(app)])
    externs = {e["name"]: e for e in ir["externs"]}
    for name, symbol in _OBSERVERS.items():
        assert externs[name]["refs"]["ts"]["symbol"] == symbol
        assert externs[name]["refs"]["ts"]["root"] == "stdlib"
    assert "refs" not in externs["read_confined"],         "the consumer's own body gained a ref; it must need none"


def _consumer_answers_py(app: Path, root: Path, monkeypatch) -> list[list[str]]:
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    src = backend_emitter("python").emit(compile_files([str(app)]))
    mod = types.ModuleType("revl_fs_consumer_gen")
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(src, "<fs consumer artifact>", "exec"), mod.__dict__)
        call = {"read": mod.read_workspace_file, "dir": mod.verify_dir}
        return [[kind, arg, call[kind](arg)]
                for kind, arg, _ in _CONSUMER_EXPECTED]
    finally:
        sys.modules.pop("revl_fs_consumer_gen", None)


def _consumer_answers_ts(app: Path, root: Path) -> list[list[str]]:
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    module = generated / "fs_consumer.ts"
    module.write_text(
        backend_emitter("typescript").emit(compile_files([str(app)]),
                                           runtime_import="../../runtime.ts"),
        encoding="utf-8")
    harness = generated / "_fs_consumer_harness.ts"
    harness.write_text(
        ";(globalThis as any).__REVL_STDLIB_REF_ROOT__ = process.argv[2]\n"
        "const calls = JSON.parse(process.argv[3]) as string[][]\n"
        "const mod = await import('./fs_consumer.ts')\n"
        "const fn: Record<string, (p: string) => string> = {\n"
        "  read: mod.read_workspace_file, dir: mod.verify_dir }\n"
        "process.stdout.write(JSON.stringify(\n"
        "  calls.map(([kind, arg]) => [kind, arg, fn[kind](arg)])))\n",
        encoding="utf-8")
    env = dict(os.environ, **{ws.WORKSPACE_ENV: str(root)})
    proc = subprocess.run(
        ["node", str(harness), str(_ROOT),
         json.dumps([[k, a] for k, a, _ in _CONSUMER_EXPECTED])],
        capture_output=True, text=True, cwd=str(_BACKEND_TS), env=env)
    if proc.returncode != 0:
        raise AssertionError(f"ts consumer harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_a_consumer_reaches_the_jail_on_py(workspace, tmp_path, monkeypatch):
    """End to end on py: the consumer reads a workspace file through the
    confined path and is refused, with the boundary code, on both escapes — the
    symlink pointing out included, which is the case a consumer rolling its own
    `os.path.exists` check gets wrong."""
    app = _write_consumer(tmp_path)
    assert _consumer_answers_py(app, workspace, monkeypatch) == _CONSUMER_EXPECTED


@_needs_node
def test_a_consumer_reaches_the_jail_on_ts(workspace, tmp_path):
    """...and the same on ts, through the emitted `ref` thunk against the
    runner-provided install root. This is the gap closed: before it, a
    user-origin `@ts` body's only route here was a relative path-guess into the
    install tree."""
    app = _write_consumer(tmp_path)
    assert _consumer_answers_ts(app, workspace) == _CONSUMER_EXPECTED


@_needs_node
def test_py_and_ts_answer_the_corpus_identically(workspace, monkeypatch):
    """The tier-parity claim, executed. Same workspace, same corpus, same
    `Ok`/`Err` kind, same resolved path, same `code`/`message`/`path` on every
    refusal. A divergence here is a real one — the consumer's whole reason for
    asking revl rather than the host is that the answer does not depend on which
    tier it lands on."""
    py = _py_answers(workspace, monkeypatch)
    ts = _ts_answers(workspace)
    assert [r["input"] for r in ts] == [r["input"] for r in py]
    mismatches = [(p["input"], p, t) for p, t in zip(py, ts) if p != t]
    assert not mismatches, "py<->ts observation divergence:\n" + "\n".join(
        f"  {c!r}\n    py={json.dumps(p, sort_keys=True)}"
        f"\n    ts={json.dumps(t, sort_keys=True)}"
        for c, p, t in mismatches)
