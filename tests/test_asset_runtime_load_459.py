"""Roadmap item 459 F6 (issue #722): reading a pinned asset from disk at RUN time.

Until now item 459's asset work was entirely a COMPILE-time act. `asset "<path>"`
(F1) resolves a path relative to the declaring `.rvl`, jails the realpath to the
root compile tree and pins the file's sha256 into the handle `{ path, sha256 }`;
`stdlib/template.rvl` (stage 1) takes a template as a `Str` and says in its own
docstring that reading one FROM DISK is not here. Nothing turned a handle back
into bytes while a program was running, which is what a component rendering its
own external template needs — and it is the one piece of item 459 that genuinely
reaches a per-tier HOST BODY rather than pure revl or the toolchain.

`stdlib/asset.rvl`'s `load(handle)` is that door, and this suite is what makes
its three claims checkable rather than asserted.

# 1. The pin survives the runtime read

A runtime read of a file the compiler already hashed is the obvious place to
LOSE the pin: the artifact would claim one set of bytes and the process would
render another. `load` does the opposite — the handle's digest is handed to the
host body and the text comes back only when the file still hashes to it. An
edited file is `Err(EDIGEST)` and yields NOTHING: not the bytes, not their
length, not a prefix. `test_an_edited_file_is_refused_and_its_bytes_never_appear`
drives that, including the negative half (the new content is absent from the
refusal).

# 2. Confinement, driven rather than described

A template path resolved at run time is a file read, so a hole here is a
file-read primitive. There is NO second jail: `load` routes through
`stdlib/fs.rvl`'s `resolve_within` — the same family-1 guard every witnessed
mutation and every inverse passes, realpath BEFORE the membership check — and
reads through `read_pinned_confined`, which re-establishes containment on the
root-anchored `O_NOFOLLOW` directory walk. Each escape below is EXECUTED: a
symlinked leaf, a symlinked directory component, `..` out of the root, an
absolute path, a NUL, a directory, an unconfigured root. And the property that
makes the jail the realpath's rather than the text's is pinned in both
directions: `..` that stays inside RESOLVES (it is never refused as text), while
a symlink whose bytes match the pin exactly is still refused.

# 3. A tier that cannot do it refuses BY NAME

`rs`, `go`, `java` and `wasm` carry no filesystem bodies anywhere in the stdlib.
A composition calling `load` and targeting one of them is refused at compile
time, and the refusal names the extern and the tiers that do have a body. wasm
used to answer "callee 'load_pinned' is not a lowerable function" — the same
sentence it gives for a misspelled name — so a portability limit and a typo were
indistinguishable; it now says what the other five emitters say.

# The gate can fail, measured

`test_every_mutation_is_caught` rebuilds the shipped guard module with one
defect introduced at a time (twenty of them: a dropped digest check, a
`startswith` containment test, a digest compared by prefix, a symlink followed,
the root read from the working directory when unset, the content returned on the
Err path, ...) and requires each mutant to fail at least one check in
`_CONFINEMENT_CHECKS`. `test_the_shipped_guard_passes_every_check` is the
baseline that keeps that from being vacuous: the real module fails none of them.

Two controls (`test_control_*`) exercise only pre-existing surface and hold both
before and after this change, so a green run is not explained by the harness
compiling nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402

_BACKEND_PY = ROOT / "backends" / "python"
_BACKEND_TS = ROOT / "backends" / "typescript"
if str(_BACKEND_PY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PY))
import revl_fs_workspace as ws  # noqa: E402

_ASSET_RVL = ROOT / "stdlib" / "asset.rvl"
_GUARD_SRC = _BACKEND_PY / "revl_fs_workspace.py"

_TEMPLATE = "<h1>{{html:title}}</h1>\n"
_TEMPLATE_SHA = hashlib.sha256(_TEMPLATE.encode("utf-8")).hexdigest()

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(
    not _HAS_NODE, reason="node is required to run the emitted ts ref thunks")


# ===========================================================================
# harness
# ===========================================================================

_APP = '''use "stdlib/asset.rvl" { load, locate, AssetRef }

pub fn page() -> Result[Str, Str] {
  return match load(asset "%(written)s") {
    Ok(t) => Ok(t),
    Err(e) => Err(e.code),
  }
}

pub fn page_message() -> Str {
  return match load(asset "%(written)s") {
    Ok(t) => t,
    Err(e) => e.message,
  }
}

pub fn where_is_it() -> Str {
  return match locate(asset "%(written)s") {
    Ok(p) => p,
    Err(e) => e.code,
  }
}
'''


def _app(tmp_path: Path, *, written: str = "./frontend/page.tpl",
         content: str = _TEMPLATE, at: str = "frontend/page.tpl") -> Path:
    """A composition whose only function loads one asset at run time.

    `use "stdlib/asset.rvl"` resolves through the installed stdlib rather than a
    copy beside the app, deliberately: `load_pinned`'s `@ts ref` reaches the
    install tree, which only an INSTALL-ORIGIN module may do (item 410's second
    root). A copied stdlib is user-origin and is refused — which is itself the
    door staying shut, so the test app exercises the shipped one.
    """
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    target = app / at
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    main = app / "main.rvl"
    main.write_text(_APP % {"written": written}, encoding="utf-8")
    return main


def _py_module(main: Path, name: str):
    """The composition emitted to py and loaded. The bodies run against the
    SHIPPED guard, not a stub."""
    src = backend_emitter("python").emit(compile_files([str(main)]))
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    return mod


def _outcome(result) -> tuple[str, object]:
    return type(result).__name__, result.value


def _load(mod) -> tuple[str, object]:
    return _outcome(mod.page())


# ===========================================================================
# 1. the door exists, and it is typed
# ===========================================================================

def test_the_module_declares_the_handle_type_and_one_public_door():
    """`AssetRef` is the canonical name F1 left open for the handle's record
    shape, `load`/`locate` are the doors, and `load_pinned` — which takes a bare
    path and a bare digest — is PRIVATE. Keeping it unexported is what makes the
    only presentable digests the ones `resolve_assets` computed."""
    ir = compile_files([str(_ASSET_RVL)])
    fns = {fn["name"]: fn for fn in ir["functions"]}
    assert fns["load"]["public"] and fns["locate"]["public"]
    assert _ASSET_RVL.read_text(encoding="utf-8").count(
        "extern pure fn load_pinned") == 1
    assert "pub extern pure fn load_pinned" not in _ASSET_RVL.read_text(
        encoding="utf-8"), "the raw path+digest extern must not be public"
    assert ir["types"]["AssetRef"]


def test_a_bare_string_does_not_typecheck_where_a_handle_belongs(tmp_path):
    """The typed boundary, in the failing direction. F1 made a resolved path and
    an unresolved one different values; `load` is where that difference is
    load-bearing at run time, so a `Str` must not reach it."""
    main = _app(tmp_path)
    main.write_text(
        'use "stdlib/asset.rvl" { load }\n'
        'pub fn page() -> Result[Str, Str] {\n'
        '  return match load("./frontend/page.tpl") {\n'
        '    Ok(t) => Ok(t),\n'
        '    Err(e) => Err(e.code),\n'
        '  }\n'
        '}\n', encoding="utf-8")
    with pytest.raises(RevlError) as exc:
        compile_files([str(main)])
    assert "AssetRef" in str(exc.value) or "Str" in str(exc.value)


# ===========================================================================
# 2. the happy path, executed
# ===========================================================================

def test_a_running_program_reads_its_template_from_disk(tmp_path, monkeypatch):
    """The whole point of F6, run rather than described: the artifact carries a
    path and a digest, the bytes stay on disk, and the process gets the text."""
    main = _app(tmp_path)
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(main.parent))
    mod = _py_module(main, "revl_asset_f6_happy")
    try:
        assert _load(mod) == ("Ok", _TEMPLATE)
    finally:
        sys.modules.pop("revl_asset_f6_happy", None)


def test_the_artifact_carries_the_digest_and_not_the_bytes(tmp_path):
    """A frontend asset is a file the composition ships, not a string literal
    the artifact carries. The emitted module must therefore contain the pin and
    the root-relative path, and NOT the template text."""
    main = _app(tmp_path)
    src = backend_emitter("python").emit(compile_files([str(main)]))
    assert _TEMPLATE_SHA in src
    assert "frontend/page.tpl" in src
    assert "{{html:title}}" not in src


def test_the_loaded_text_renders_through_the_stage_one_template_module(
        tmp_path, monkeypatch):
    """F6 closes the loop stage 1 named: `stdlib/template.rvl` takes the
    template as a `Str` and says the text will come from "a resolved asset
    reference". Here it does, and the rendered page escapes its hole."""
    app = tmp_path / "app"
    (app / "frontend").mkdir(parents=True, exist_ok=True)
    (app / "frontend" / "page.tpl").write_text(_TEMPLATE, encoding="utf-8")
    main = app / "main.rvl"
    main.write_text(
        'use "stdlib/asset.rvl" { load }\n'
        'use "stdlib/template.rvl" { render, Binding }\n'
        'pub fn page(title: Str) -> Result[Str, Str] {\n'
        '  return match load(asset "./frontend/page.tpl") {\n'
        '    Ok(tpl) => match render(tpl, [{ name: "title", value: title }]) {\n'
        '      Ok(text) => Ok(text),\n'
        '      Err(e) => Err(e.code),\n'
        '    },\n'
        '    Err(e) => Err(e.code),\n'
        '  }\n'
        '}\n', encoding="utf-8")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(app))
    mod = _py_module(main, "revl_asset_f6_render")
    try:
        kind, value = _outcome(mod.page("<script>"))
        assert kind == "Ok"
        assert value == "<h1>&lt;script&gt;</h1>\n"
    finally:
        sys.modules.pop("revl_asset_f6_render", None)


# ===========================================================================
# 3. the pin is ENFORCED by the runtime read
# ===========================================================================

def test_an_edited_file_is_refused_and_its_bytes_never_appear(
        tmp_path, monkeypatch):
    """The reconciliation between F1's compile-time pin and F6's runtime read.

    The file is edited AFTER the compile, so the handle's digest is the old
    bytes' and the disk holds the new ones. The read refuses, and the refusal
    carries no part of the content: a digest mismatch that leaked a prefix would
    be a content oracle over anything inside the root."""
    main = _app(tmp_path)
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(main.parent))
    mod = _py_module(main, "revl_asset_f6_edit")
    try:
        assert _load(mod) == ("Ok", _TEMPLATE)
        secret = "<h1>REPLACED AFTER THE BUILD</h1>\n"
        (main.parent / "frontend" / "page.tpl").write_text(
            secret, encoding="utf-8")
        kind, code = _load(mod)
        assert (kind, code) == ("Err", "EDIGEST")
        message = mod.page_message()
        assert "REPLACED" not in message and secret not in message
    finally:
        sys.modules.pop("revl_asset_f6_edit", None)


def test_the_digest_cannot_be_opted_out_of(tmp_path, monkeypatch):
    """A hand-built handle carrying no digest is refused BEFORE the open, so
    "no pin" is not spellable as an empty string. The guard is driven directly
    because no `asset` expression can produce such a handle — which is the
    point: the refusal is the backstop for a forged record."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(tmp_path))
    (tmp_path / "t.tpl").write_text(_TEMPLATE, encoding="utf-8")
    real = ws.resolve_within("t.tpl")
    for bogus in ("", "deadbeef", _TEMPLATE_SHA.upper(), _TEMPLATE_SHA[:63],
                  _TEMPLATE_SHA + "0"):
        with pytest.raises(ws.FsOpError) as exc:
            ws.read_pinned_confined(real, bogus)
        assert exc.value.code == "EINVAL", bogus


# ===========================================================================
# 4. confinement, every arm driven
# ===========================================================================

def test_no_configured_root_is_a_refusal_not_a_fallback(tmp_path, monkeypatch):
    """With no root, the load must refuse — never fall back to the working
    directory, which would make the jail the CWD of whoever started the
    process."""
    main = _app(tmp_path)
    mod = _py_module(main, "revl_asset_f6_noroot")
    try:
        monkeypatch.delenv(ws.WORKSPACE_ENV, raising=False)
        monkeypatch.chdir(main.parent)
        assert _load(mod) == ("Err", "EWORKSPACE")
    finally:
        sys.modules.pop("revl_asset_f6_noroot", None)


def test_a_symlinked_leaf_out_of_the_root_is_refused_even_when_it_matches(
        tmp_path, monkeypatch):
    """The load-bearing one. The link's target holds EXACTLY the pinned bytes,
    so the digest check would pass; containment of the realpath refuses first.
    A jail that ran after the pin would be a read primitive for any file whose
    content an attacker can predict."""
    main = _app(tmp_path)
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(main.parent))
    mod = _py_module(main, "revl_asset_f6_symlink")
    try:
        assert _load(mod) == ("Ok", _TEMPLATE)
        # the swap happens AFTER the build, which is the only way this shape can
        # arise: the COMPILE-time jail realpaths too, and refuses the same link
        # outright (`test_a_handle_path_is_never_absolute_and_never_leaves_the_root`).
        # The runtime jail is what stands between a post-build swap and a read.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "elsewhere.tpl").write_text(_TEMPLATE, encoding="utf-8")
        target = main.parent / "frontend" / "page.tpl"
        target.unlink()
        target.symlink_to(outside / "elsewhere.tpl")
        assert _load(mod) == ("Err", "EOUTSIDE")
    finally:
        sys.modules.pop("revl_asset_f6_symlink", None)


def test_a_symlinked_directory_component_is_refused(tmp_path, monkeypatch):
    """realpath resolves EVERY component, not only the leaf, so a directory
    swapped for a link out of the tree is caught by the same check."""
    main = _app(tmp_path)
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(main.parent))
    mod = _py_module(main, "revl_asset_f6_dirlink")
    try:
        assert _load(mod) == ("Ok", _TEMPLATE)
        outside = tmp_path / "outside"
        (outside / "frontend").mkdir(parents=True)
        (outside / "frontend" / "page.tpl").write_text(_TEMPLATE, encoding="utf-8")
        shutil.rmtree(main.parent / "frontend")
        (main.parent / "frontend").symlink_to(outside / "frontend")
        assert _load(mod) == ("Err", "EOUTSIDE")
    finally:
        sys.modules.pop("revl_asset_f6_dirlink", None)


def test_dot_dot_that_stays_inside_the_root_resolves(tmp_path, monkeypatch):
    """The other direction of the same rule, and the one a textual `..` ban
    would break. Containment of the REALPATH is the jail, so a path that walks
    out and back in is one handle and loads normally."""
    main = _app(tmp_path, written="./frontend/../frontend/page.tpl")
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(main.parent))
    mod = _py_module(main, "revl_asset_f6_dotdot")
    try:
        assert _load(mod) == ("Ok", _TEMPLATE)
    finally:
        sys.modules.pop("revl_asset_f6_dotdot", None)


def test_a_handle_path_is_never_absolute_and_never_leaves_the_root(
        tmp_path, monkeypatch):
    """The compile-time half already refuses an absolute path and a `..` that
    leaves the compile tree. Driven here because the RUNTIME jail must not be
    the only thing standing between a handle and an arbitrary file: both halves
    hold, and neither is load-bearing alone."""
    with pytest.raises(RevlError, match="absolute"):
        compile_files([str(_app(tmp_path / "abs", written="/etc/passwd"))])

    esc = tmp_path / "esc"
    esc.mkdir()
    (esc / "secret.tpl").write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(RevlError, match="OUTSIDE"):
        compile_files([str(_app(esc, written="../secret.tpl"))])

    # and the same file reached through a symlink INSIDE the tree: the realpath
    # is what is jailed, so the written text being innocent changes nothing.
    link = tmp_path / "lnk"
    link.mkdir()
    (link / "secret.tpl").write_text(_TEMPLATE, encoding="utf-8")
    main = _app(link, at="frontend/page.tpl")
    (main.parent / "frontend" / "page.tpl").unlink()
    (main.parent / "frontend" / "page.tpl").symlink_to(link / "secret.tpl")
    with pytest.raises(RevlError, match="OUTSIDE"):
        compile_files([str(main)])


def test_the_runtime_guard_refuses_the_escapes_a_forged_handle_could_carry(
        tmp_path, monkeypatch):
    """The handle is an ordinary record, so a program CAN hand-build one. The
    runtime jail is therefore driven directly with the shapes no `asset`
    expression produces: an absolute path outside the root, a `..` escape, and a
    NUL. None of them is refused by reading the text — each is refused because
    of where its realpath lands (or because no name can hold it)."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    for path, code in (("/etc/passwd", "EOUTSIDE"),
                       ("../secret.txt", "EOUTSIDE"),
                       ("a\x00b", "EINVAL")):
        with pytest.raises(ws.FsOpError) as exc:
            ws.read_pinned_confined(ws.resolve_within(path), _TEMPLATE_SHA)
        assert exc.value.code == code, path


def test_a_directory_is_not_a_template(tmp_path, monkeypatch):
    """A non-regular file is refused before a byte is read, so a directory
    cannot answer and a fifo cannot hang the reader."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(tmp_path))
    (tmp_path / "sub").mkdir()
    with pytest.raises(ws.FsOpError) as exc:
        ws.read_pinned_confined(ws.resolve_within("sub"), _TEMPLATE_SHA)
    assert exc.value.code in ("ENOTFILE", "EISDIR")


def test_the_module_writes_no_second_jail(tmp_path):
    """`stdlib/asset.rvl`'s `@py` body may import the guard and nothing else,
    and may call only a listed family guard or read helper — the same scan
    `tests/test_fs_confinement_families.py` applies to `stdlib/fs.rvl`. A path
    -confinement bug in a compiler or a runtime that reads files is a file-read
    primitive, and two jails would drift."""
    import ast
    import re
    text = _ASSET_RVL.read_text(encoding="utf-8")
    bodies = re.findall(
        r"^(?:pub )?extern [^\n]*?\bfn (\w+)\(.*?= @py \{\n(.*?)\n\}",
        text, re.S | re.M)
    assert bodies, "no @py body found in stdlib/asset.rvl"
    listed = {e for entries in ws.PATH_FAMILIES.values() for e in entries}
    listed |= set(ws.READ_HELPERS)
    guards = set(ws.PATH_FAMILIES["named-endpoint"])
    for name, body in bodies:
        tree = ast.parse("def _body():\n" + body)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert imported <= {"revl_fs_workspace"}, \
            f"`{name}`'s @py body imports {sorted(imported)}"
        bound: set[str] = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == "_ws"
                    and node.value.func.attr in guards):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        bound.add(tgt.id)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "_ws"):
                assert node.func.attr in listed, \
                    f"`{name}` calls unlisted guard helper `_ws.{node.func.attr}`"
                if node.func.attr == "read_pinned_confined":
                    arg = node.args[0]
                    assert isinstance(arg, ast.Name) and arg.id in bound, \
                        (f"`{name}` hands `read_pinned_confined` a path that did "
                         f"not come from {sorted(guards)}")


# ===========================================================================
# 5. tiers: two reach it, four refuse BY NAME
# ===========================================================================

_NO_FS_TIERS = ("rust", "go", "java", "wasm")
_TIER_TAG = {"rust": "@rs", "go": "@go", "java": "@java", "wasm": "@wasm"}


@pytest.mark.parametrize("backend", _NO_FS_TIERS)
def test_a_tier_with_no_filesystem_body_refuses_by_name(backend, tmp_path):
    """Not "this does nothing here" and not "unknown callee": the refusal names
    the extern and the tiers that DO carry a body, so an author retargeting a
    composition learns what is missing and where it exists."""
    ir = compile_files([str(_app(tmp_path / backend))])
    with pytest.raises(Exception) as exc:
        backend_emitter(backend).emit(ir)
    message = str(exc.value)
    assert "load_pinned" in message, message
    assert f"{_TIER_TAG[backend]} body" in message, message
    assert "py" in message and "not portable" in message, message


@pytest.mark.parametrize("backend", ("python", "typescript"))
def test_the_two_filesystem_tiers_emit(backend, tmp_path):
    """The positive half of the same claim: py and ts do carry a body, so the
    refusal above is about those four tiers and not about the feature."""
    src = backend_emitter(backend).emit(
        compile_files([str(_app(tmp_path / backend))]))
    assert "load_pinned" in src


# ===========================================================================
# 6. the two tiers answer one corpus identically
# ===========================================================================

#: `@ROOT@` is folded to the workspace root by each runner. The corpus is the
#: confinement decision plus the pin, written once and run twice.
_CASES = [
    ("frontend/page.tpl", _TEMPLATE_SHA),
    ("frontend/../frontend/page.tpl", _TEMPLATE_SHA),
    ("frontend/page.tpl", "0" * 64),
    ("frontend/missing.tpl", _TEMPLATE_SHA),
    ("frontend", _TEMPLATE_SHA),
    ("link_out.tpl", _TEMPLATE_SHA),
    ("../outside/elsewhere.tpl", _TEMPLATE_SHA),
    ("/etc/passwd", _TEMPLATE_SHA),
    ("frontend/page.tpl", ""),
    ("frontend/page.tpl", _TEMPLATE_SHA.upper()),
]


def _corpus_workspace(tmp_path: Path) -> Path:
    app = tmp_path / "ws"
    (app / "frontend").mkdir(parents=True)
    (app / "frontend" / "page.tpl").write_text(_TEMPLATE, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.tpl").write_text(_TEMPLATE, encoding="utf-8")
    (app / "link_out.tpl").symlink_to(outside / "elsewhere.tpl")
    return app


def _py_corpus(root: Path, monkeypatch) -> list[dict]:
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(root))
    out = []
    for path, digest in _CASES:
        try:
            value = ws.read_pinned_confined(ws.resolve_within(path), digest)
            out.append({"case": [path, digest], "kind": "Ok", "value": value})
        except ws.FsOpError as exc:
            out.append({"case": [path, digest], "kind": "Err",
                        "value": exc.code})
    return out


def _ts_corpus(root: Path) -> list[dict]:
    """The same corpus through the SHIPPED ts entry point, under node."""
    generated = _BACKEND_TS / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    harness = generated / "_asset_runtime_459_harness.ts"
    harness.write_text(
        "const cases = JSON.parse(process.argv[2]) as [string, string][]\n"
        "const mod = await import('../../revl_fs_ts.ts')\n"
        "const out = cases.map(([p, d]) => {\n"
        "  const r = mod.fsReadPinned(p, d) as { kind: string; value: any }\n"
        "  return { case: [p, d], kind: r.kind,\n"
        "           value: r.kind === 'Ok' ? r.value : r.value.code }\n"
        "})\n"
        "process.stdout.write(JSON.stringify(out))\n",
        encoding="utf-8")
    env = dict(os.environ, **{ws.WORKSPACE_ENV: os.path.realpath(str(root))})
    proc = subprocess.run(
        ["node", str(harness), json.dumps(_CASES)],
        capture_output=True, text=True, cwd=str(_BACKEND_TS), env=env)
    if proc.returncode != 0:
        raise AssertionError(f"ts asset harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_the_py_tier_answers_the_corpus_as_specified(tmp_path, monkeypatch):
    """The oracle the ts tier is diffed against, written out rather than
    derived, so a shared regression on both tiers cannot pass as agreement."""
    answers = {tuple(row["case"]): (row["kind"], row["value"])
               for row in _py_corpus(_corpus_workspace(tmp_path), monkeypatch)}
    assert answers[("frontend/page.tpl", _TEMPLATE_SHA)] == ("Ok", _TEMPLATE)
    assert answers[("frontend/../frontend/page.tpl", _TEMPLATE_SHA)] == \
        ("Ok", _TEMPLATE)
    assert answers[("frontend/page.tpl", "0" * 64)] == ("Err", "EDIGEST")
    assert answers[("frontend/missing.tpl", _TEMPLATE_SHA)] == ("Err", "ENOENT")
    assert answers[("frontend", _TEMPLATE_SHA)][0] == "Err"
    assert answers[("link_out.tpl", _TEMPLATE_SHA)] == ("Err", "EOUTSIDE")
    assert answers[("../outside/elsewhere.tpl", _TEMPLATE_SHA)] == \
        ("Err", "EOUTSIDE")
    assert answers[("/etc/passwd", _TEMPLATE_SHA)] == ("Err", "EOUTSIDE")
    assert answers[("frontend/page.tpl", "")] == ("Err", "EINVAL")
    assert answers[("frontend/page.tpl", _TEMPLATE_SHA.upper())] == \
        ("Err", "EINVAL")


@_needs_node
def test_the_ts_tier_answers_the_same_corpus(tmp_path, monkeypatch):
    """Executed agreement, not a claimed one: one corpus, one workspace on disk,
    the real py guard and the real ts entry point, diffed case for case."""
    root = _corpus_workspace(tmp_path)
    py = {tuple(r["case"]): (r["kind"], r["value"])
          for r in _py_corpus(root, monkeypatch)}
    ts = {tuple(r["case"]): (r["kind"], r["value"]) for r in _ts_corpus(root)}
    assert ts == py


# ===========================================================================
# 7. the gate can fail, measured
# ===========================================================================

def _guard_module(source: str):
    """A private copy of the shipped guard module, built from `source`."""
    mod = types.ModuleType("revl_fs_workspace_mutant")
    mod.__file__ = str(_GUARD_SRC)
    exec(compile(source, "<guard mutant>", "exec"), mod.__dict__)
    mod.__source__ = source
    return mod


def _check_pin_enforced(guard, root: Path) -> None:
    real = guard.resolve_within("frontend/page.tpl")
    assert guard.read_pinned_confined(real, _TEMPLATE_SHA) == _TEMPLATE
    try:
        guard.read_pinned_confined(real, "0" * 64)
    except guard.FsOpError as exc:
        assert exc.code == "EDIGEST"
    else:
        raise AssertionError("a wrong digest was accepted")


def _check_no_content_on_refusal(guard, root: Path) -> None:
    real = guard.resolve_within("frontend/page.tpl")
    try:
        guard.read_pinned_confined(real, "0" * 64)
    except guard.FsOpError as exc:
        assert _TEMPLATE.strip() not in str(exc)
        assert _TEMPLATE.strip() not in exc.message
    else:
        raise AssertionError("a wrong digest was accepted")


def _check_empty_digest_refused(guard, root: Path) -> None:
    real = guard.resolve_within("frontend/page.tpl")
    for bogus in ("", _TEMPLATE_SHA[:8], _TEMPLATE_SHA.upper()):
        try:
            guard.read_pinned_confined(real, bogus)
        except guard.FsOpError as exc:
            assert exc.code == "EINVAL"
        else:
            raise AssertionError(f"digest {bogus!r} was accepted")


def _check_symlink_leaf_refused(guard, root: Path) -> None:
    try:
        real = guard.resolve_within("link_out.tpl")
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
        return
    try:
        guard.read_pinned_confined(real, _TEMPLATE_SHA)
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
    else:
        raise AssertionError("a symlink out of the root was read")


def _check_dotdot_escape_refused(guard, root: Path) -> None:
    try:
        real = guard.resolve_within("../outside/elsewhere.tpl")
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
        return
    try:
        guard.read_pinned_confined(real, _TEMPLATE_SHA)
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE", (
            "a `..` escape must refuse as a BOUNDARY refusal; an ENOENT here "
            "means the path was sanitized textually rather than resolved")
    else:
        raise AssertionError("a `..` escape was read")


def _check_sibling_root_refused(guard, root: Path) -> None:
    """`/ws` must not contain `/ws-evil`: the containment test is a path
    comparison, never a string prefix."""
    sibling = root.parent / (root.name + "-evil")
    sibling.mkdir(exist_ok=True)
    (sibling / "page.tpl").write_text(_TEMPLATE, encoding="utf-8")
    try:
        real = guard.resolve_within(str(sibling / "page.tpl"))
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
        return
    try:
        guard.read_pinned_confined(real, _TEMPLATE_SHA)
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
    else:
        raise AssertionError("a sibling of the root was read")


def _check_absolute_outside_refused(guard, root: Path) -> None:
    try:
        real = guard.resolve_within("/etc/hosts")
    except guard.FsOpError as exc:
        assert exc.code == "EOUTSIDE"
        return
    raise AssertionError(f"an absolute path outside the root resolved to {real}")


def _check_no_root_is_a_refusal(guard, root: Path, monkeypatch=None) -> None:
    saved = os.environ.pop(guard.WORKSPACE_ENV, None)
    try:
        guard.resolve_within("frontend/page.tpl")
    except guard.FsOpError as exc:
        assert exc.code == "EWORKSPACE"
    else:
        raise AssertionError("an unconfigured root did not refuse")
    finally:
        if saved is not None:
            os.environ[guard.WORKSPACE_ENV] = saved


def _check_directory_refused(guard, root: Path) -> None:
    real = guard.resolve_within("frontend")
    try:
        guard.read_pinned_confined(real, _TEMPLATE_SHA)
    except guard.FsOpError as exc:
        assert exc.code == "ENOTFILE"
    else:
        raise AssertionError("a directory was read as a template")


def _check_a_near_miss_digest_is_refused(guard, root: Path) -> None:
    """A digest that agrees with the real one on a PREFIX and differs later must
    still refuse. A comparison that looks at part of the value is a comparison
    that can be met by guessing part of the value."""
    real = guard.resolve_within("frontend/page.tpl")
    near = _TEMPLATE_SHA[:32] + ("0" * 32 if _TEMPLATE_SHA[32] != "0"
                                 else "1" * 32)
    try:
        guard.read_pinned_confined(real, near)
    except guard.FsOpError as exc:
        assert exc.code == "EDIGEST"
    else:
        raise AssertionError("a digest matching only a prefix was accepted")


def _check_the_read_goes_through_the_walk(guard, root: Path) -> None:
    """The read must reach the file through the root-anchored directory-fd walk
    and nothing else: no builtin `open`, and every `os.open` carried by the
    helper takes a `dir_fd=` and refuses a symlink leaf with `O_NOFOLLOW`.

    Stated over the SOURCE because the property is about which syscall is made,
    not about what it answers on a tree with no attacker in it: a second read by
    name returns the same bytes on a quiet filesystem and a different file on a
    busy one."""
    import ast
    tree = ast.parse(guard.__source__)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "read_pinned_confined"), None)
    assert fn is not None, "the guard has no `read_pinned_confined`"
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "exec", "eval", "__import__"), \
                f"the pinned read calls the builtin `{node.func.id}`"
        if (isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os" and node.func.attr == "open"):
            assert any(kw.arg == "dir_fd" for kw in node.keywords), \
                "the pinned read opens by NAME rather than through the walk"
            flags = ast.dump(node.args[1]) if len(node.args) > 1 else ""
            assert "_O_NOFOLLOW" in flags, \
                "the pinned read opens its leaf without O_NOFOLLOW"


def _check_read_helper_is_listed_and_total(guard, root: Path) -> None:
    assert "read_pinned_confined" in guard.READ_HELPERS
    assert getattr(guard.read_pinned_confined, "is_total_guard", False)


#: Every property the runtime read must have, as a runnable check. A mutant that
#: passes all of them is a defect this suite cannot see, which is why each one
#: is executed against the mutated module rather than asserted about the source.
_CONFINEMENT_CHECKS = {
    "pin-enforced": _check_pin_enforced,
    "no-content-on-refusal": _check_no_content_on_refusal,
    "empty-digest-refused": _check_empty_digest_refused,
    "symlink-leaf-refused": _check_symlink_leaf_refused,
    "dotdot-escape-refused": _check_dotdot_escape_refused,
    "sibling-root-refused": _check_sibling_root_refused,
    "absolute-outside-refused": _check_absolute_outside_refused,
    "no-root-is-a-refusal": _check_no_root_is_a_refusal,
    "directory-refused": _check_directory_refused,
    "near-miss-digest-refused": _check_a_near_miss_digest_is_refused,
    "read-goes-through-the-walk": _check_the_read_goes_through_the_walk,
    "listed-and-total": _check_read_helper_is_listed_and_total,
}

#: One defect each, textual, against the SHIPPED guard source. Every one of them
#: is a plausible edit: a check deleted, a comparison loosened, a fallback
#: introduced, an error path that leaks. The point of the table is not the
#: mutants, it is that `test_every_mutation_is_caught` proves the checks above
#: can fail.
_MUTATIONS = {
    "digest-check-dropped": (
        'if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):',
        'if False:'),
    "digest-compared-by-prefix": (
        'if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):',
        'if not hashlib.sha256(data).hexdigest().startswith(expected_sha256[:4]):'),
    "digest-shape-unchecked": (
        'if not isinstance(expected_sha256, str) or not _SHA256_HEX.match(expected_sha256):',
        'if False:'),
    "digest-shape-case-insensitive": (
        r'_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")',
        r'_SHA256_HEX = re.compile(r"[0-9a-fA-F]{8}")'),
    "content-leaked-into-the-refusal": (
        '            "file content does not match the pinned sha256 recorded when the "\n'
        '            "asset was resolved at compile time",',
        '            "file content does not match the pinned sha256: " + data.decode("utf-8", "replace"),'),
    "regular-file-check-dropped": (
        '        if not stat.S_ISREG(st.st_mode):\n'
        '            raise FsOpError(\n'
        '                "ENOTFILE",\n'
        '                "pinned read target is not a regular file",',
        '        if False:\n'
        '            raise FsOpError(\n'
        '                "ENOTFILE",\n'
        '                "pinned read target is not a regular file",'),
    "symlink-followed-at-the-leaf": (
        '        fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK,\n'
        '                     dir_fd=dirfd)',
        '        fd = os.open(real, os.O_RDONLY)'),
    "read-by-name-not-through-the-walk": (
        '    parent, leaf = _split(real)\n'
        '    dirfd = _open_dirfd(parent)',
        '    parent, leaf = os.path.dirname(real), os.path.basename(real)\n'
        '    dirfd = os.open(parent, os.O_RDONLY)'),
    "containment-by-string-prefix": (
        '    return real == root or real.startswith(root + os.sep)',
        '    return real.startswith(root)'),
    "containment-dropped": (
        '    if not _is_within(root, real):\n'
        '        raise ConfinementError(\n'
        '            "EOUTSIDE",\n'
        '            "path escapes the session workspace root",\n'
        '            real,\n'
        '        )',
        '    if False:\n'
        '        raise ConfinementError("EOUTSIDE", "x", real)'),
    "dotdot-stripped-textually": (
        '    target = path if os.path.isabs(path) else os.path.join(root, path)',
        '    target = os.path.join(root, path.replace("../", ""))'),
    "realpath-skipped": (
        '    real = os.path.realpath(target)',
        '    real = os.path.normpath(target)'),
    "dotdot-refused-textually-instead": (
        '    real = os.path.realpath(target)',
        '    real = target if ".." not in path else os.path.realpath(target)'),
    "unset-root-falls-back-to-cwd": (
        '    if not root:\n'
        '        raise ConfinementError(\n'
        '            "EWORKSPACE",',
        '    if not root:\n'
        '        root = os.getcwd()\n'
        '    if False:\n'
        '        raise ConfinementError(\n'
        '            "EWORKSPACE",'),
    "helper-not-listed": (
        '                                 "read_pinned_confined")',
        '                                 )'),
    "helper-not-total": (
        'for _entry in READ_HELPERS:\n'
        '    globals()[_entry] = _make_total(_entry, globals()[_entry])',
        'for _entry in READ_HELPERS:\n'
        '    pass'),
    "leaf-opened-without-nofollow": (
        '        fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK,\n'
        '                     dir_fd=dirfd)',
        '        fd = os.open(leaf, os.O_RDONLY, dir_fd=dirfd)'),
    "digest-over-a-second-read": (
        '    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):',
        '    if not hmac.compare_digest(\n'
        '            hashlib.sha256(open(real, "rb").read()).hexdigest(),\n'
        '            expected_sha256):'),
    "text-returned-before-the-check": (
        '    data = b"".join(chunks)\n'
        '    if not hmac.compare_digest',
        '    data = b"".join(chunks)\n'
        '    return data.decode("utf-8", "replace")\n'
        '    if not hmac.compare_digest'),
    "empty-expected-digest-passes": (
        '    if not isinstance(expected_sha256, str) or not _SHA256_HEX.match(expected_sha256):',
        '    if expected_sha256 is None:'),
    "digest-of-the-expected-value": (
        'hashlib.sha256(data).hexdigest(), expected_sha256):',
        'expected_sha256, expected_sha256):'),
}


@pytest.fixture
def mutation_workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "frontend").mkdir(parents=True)
    (root / "frontend" / "page.tpl").write_text(_TEMPLATE, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.tpl").write_text(_TEMPLATE, encoding="utf-8")
    (root / "link_out.tpl").symlink_to(outside / "elsewhere.tpl")
    return root


def _run_checks(guard, root: Path) -> set[str]:
    """The names of the checks this guard FAILS."""
    failed = set()
    for name, check in _CONFINEMENT_CHECKS.items():
        try:
            check(guard, root)
        except AssertionError:
            failed.add(name)
        except Exception:  # a mutant that explodes is also caught
            failed.add(name)
    return failed


def test_the_shipped_guard_passes_every_check(mutation_workspace, monkeypatch):
    """The baseline that keeps the mutation proof from being vacuous: run the
    battery against the module as shipped and it must fail nothing. Without this
    a check that always fails would 'catch' every mutant."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(mutation_workspace))
    guard = _guard_module(_GUARD_SRC.read_text(encoding="utf-8"))
    assert _run_checks(guard, mutation_workspace) == set()


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_every_mutation_is_caught(name, mutation_workspace, monkeypatch):
    """Each defect must fail at least one check. This is what makes "the gate
    bites" a measured fact: a check nothing can break is a check that proves
    nothing."""
    monkeypatch.setenv(ws.WORKSPACE_ENV, str(mutation_workspace))
    old, new = _MUTATIONS[name]
    source = _GUARD_SRC.read_text(encoding="utf-8")
    assert old in source, f"mutation `{name}` no longer applies to the guard"
    guard = _guard_module(source.replace(old, new, 1))
    failed = _run_checks(guard, mutation_workspace)
    assert failed, f"mutation `{name}` passed every confinement check"


# ===========================================================================
# 8. non-vacuity controls (hold BEFORE and AFTER this change)
# ===========================================================================

def test_control_the_compile_time_asset_handle_is_unchanged(tmp_path):
    """Pre-existing F1 surface: `asset "<path>"` still resolves to the
    root-relative path and the real digest. If this ever fails, the harness is
    broken rather than the feature."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "x.tpl").write_text(_TEMPLATE, encoding="utf-8")
    (app / "a.rvl").write_text(
        "type Asset = { path: Str, sha256: Str }\n"
        'pub fn entry() -> Asset { return asset "./x.tpl" }\n', encoding="utf-8")
    src = backend_emitter("python").emit(compile_files([str(app / "a.rvl")]))
    assert _TEMPLATE_SHA in src and "x.tpl" in src


def test_control_the_template_module_still_runs_on_every_tier(tmp_path):
    """Pre-existing stage-1 surface: `stdlib/template.rvl` is pure revl, so it
    emits on all six tiers. F6 must not have dragged a host body into it."""
    ir = compile_files([str(ROOT / "stdlib" / "template.rvl")])
    for backend in ("python", "typescript", "rust", "go", "java"):
        assert backend_emitter(backend).emit(ir)
