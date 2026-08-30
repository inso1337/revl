"""Roadmap item 396 option B: an extern host body that REFERENCES an external
host MODULE, `= @backend ref sym from "path"`, emitted as a LAZY import THUNK.

Option B wraps existing host code as-is: the referenced file stays a normal host
module (importable, formattable, type-checked, unit-tested) and the emitter
generates a thunk that imports the symbol at the extern's FIRST CALL, inside the
extern frame. This suite pins the design's B exit tests AND every re-review
must-fix (docs/design/396-host-code-file-reference.md §"Re-review corrections"):

- the lazy thunk runs host code only at first call — a load-time sentinel, AND a
  PARENT package `__init__.py` sentinel, stay UNSET through artifact load and
  plug (no `importlib.util.find_spec` parent execution);
- the plug-time content-hash check refuses a swapped file;
- `sys.modules` eviction makes a replug in one process run the NEW code;
- `ref` is refused on go/rust/java/wasm (unsolved/unimportable tiers);
- additivity (no ref = byte-identical);
- the softened coroutine assert catches the plain `async def` shape without
  false-refusing a valid awaitable callable;
- `sys.path` is APPENDED (never prepended) and only when refs are present.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types

import pytest

from revl import hostref
from revl.compiler import compile_files, compile_source
from revl.errors import RevlError

from _backend_import import backend_emitter  # noqa: E402

emit_py = backend_emitter("python")
emit_ts = backend_emitter("typescript")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ref_rvl(sym="do_engine", path="host/engine.py", cls="pure",
             async_=False, name="engine"):
    colour = "async " if async_ else ""
    return (f"extern {cls} {colour}fn {name}(x: Str) -> Str\n"
            f'    = @py ref {sym} from "{path}"\n')


# -- additivity: no ref = byte-identical -----------------------------------

def test_additivity_no_ref_byte_identical(tmp_path):
    """A program using no ref lowers and emits byte-identically to before: the
    `refs` key, `_REVL_REFS`, and the inspect import are all absent."""
    src = ("extern pure fn addstr(a: Int, b: Int) -> Str\n"
           "    = @py { return str(a + b) }\n")
    m = _write(tmp_path / "m.rvl", src)
    ir = compile_files([str(m)])
    assert "refs" not in ir["externs"][0]
    out = emit_py.emit(ir)
    assert "_REVL_REFS" not in out
    assert "import inspect as _inspect" not in out


# -- IR shape --------------------------------------------------------------

def test_ir_records_symbol_path_hash(tmp_path):
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl", _ref_rvl())
    ir = compile_files([str(m)])
    ref = ir["externs"][0]["refs"]["py"]
    assert ref["symbol"] == "do_engine"
    assert ref["path"] == "host/engine.py"
    assert len(ref["sha256"]) == 64
    # a ref-only extern carries no `bodies` entry for that tier
    assert "py" not in (ir["externs"][0].get("bodies") or {})


def test_reproducible_hash_changes_on_edit(tmp_path):
    hostf = _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl", _ref_rvl())
    h1 = compile_files([str(m)])["externs"][0]["refs"]["py"]["sha256"]
    h1b = compile_files([str(m)])["externs"][0]["refs"]["py"]["sha256"]
    assert h1 == h1b
    _write(hostf, "def do_engine(x):\n    return x + '!'\n")
    h2 = compile_files([str(m)])["externs"][0]["refs"]["py"]["sha256"]
    assert h2 != h1


# -- the tier gate: only py and ts ----------------------------------------

@pytest.mark.parametrize("tier", ["go", "rs", "java", "wasm"])
def test_ref_refused_on_unsolved_tiers(tmp_path, tier):
    """go/rust/java lack a file-addressable import primitive and wasm cannot
    import a file; a ref on any of the four is refused at compile, naming the
    tier and redirecting, never a broken artifact."""
    _write(tmp_path / "host" / "engine.py", "x = 1\n")
    m = _write(tmp_path / "m.rvl",
               f"extern pure fn e(x: Str) -> Str\n"
               f'    = @{tier} ref foo from "host/engine.py"\n')
    with pytest.raises(RevlError) as exc:
        compile_files([str(m)])
    msg = str(exc.value)
    assert f"@{tier}" in msg
    assert "not supported" in msg


def test_py_ref_paired_with_wasm_body_compiles(tmp_path):
    """A program pairing a @py ref with an inline @wasm body compiles; each tier
    emits its own form (the ref tier a thunk, the other its body)."""
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "host/engine.py"\n'
               "    = @wasm { (local.get $x) }\n")
    ir = compile_files([str(m)])
    ext = ir["externs"][0]
    assert ext["refs"]["py"]["symbol"] == "do_engine"
    assert "wasm" in ext["bodies"]


# -- the jail --------------------------------------------------------------

def test_jail_absolute_refused(tmp_path):
    hostf = _write(tmp_path / "host" / "engine.py", "x = 1\n")
    m = _write(tmp_path / "m.rvl",
               f"extern pure fn e(x: Str) -> Str\n"
               f'    = @py ref foo from "{hostf}"\n')
    with pytest.raises(RevlError, match="absolute"):
        compile_files([str(m)])


def test_jail_outside_root_tree_refused(tmp_path):
    outside = _write(tmp_path.parent / "outside_396b" / "ext.py", "def foo(x):\n    return x\n")
    m = _write(tmp_path / "proj" / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n"
               f'    = @py ref foo from "../../{outside.parent.name}/ext.py"\n')
    with pytest.raises(RevlError, match="OUTSIDE the root compile tree"):
        compile_files([str(m)])


def test_jail_symlink_escape_refused(tmp_path):
    outside = _write(tmp_path.parent / "outside_396b_sym" / "ext.py", "def foo(x):\n    return x\n")
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    link = proj / "engine.py"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    m = _write(proj / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n"
               '    = @py ref foo from "engine.py"\n')
    with pytest.raises(RevlError, match="OUTSIDE the root compile tree"):
        compile_files([str(m)])


def test_dotdot_within_tree_allowed(tmp_path):
    """`..` is ALLOWED when the resolved realpath stays inside the ROOT tree: an
    imported `plugins/p.rvl` may ref `../host/engine.py`. The root compile file
    sits at the tree root, so the resolved file stays contained, and the recorded
    path is derived RELATIVE TO THE ROOT (no `..`)."""
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    _write(tmp_path / "plugins" / "p.rvl",
           "pub extern pure fn engine(x: Str) -> Str\n"
           '    = @py ref do_engine from "../host/engine.py"\n')
    app = _write(tmp_path / "app.rvl", 'use "plugins/p.rvl" { engine }\n')
    ir = compile_files([str(app)])
    ext = next(e for e in ir["externs"] if e["name"] == "engine")
    assert ext["refs"]["py"]["path"] == "host/engine.py"


# -- in-memory / loaderless contract ---------------------------------------

def test_loaderless_source_refuses_ref():
    with pytest.raises(RevlError, match="needs `modules="):
        compile_source(_ref_rvl())


def test_in_memory_ref_resolves_through_sources(tmp_path):
    root = str(tmp_path)
    app = os.path.join(root, "m.rvl")
    hostf = os.path.join(root, "host", "engine.py")
    sources = {
        app: _ref_rvl(),
        hostf: "def do_engine(x):\n    return x\n",
    }
    ir = compile_files([app], sources=sources)
    ref = ir["externs"][0]["refs"]["py"]
    assert ref["path"] == "host/engine.py"
    body = sources[hostf].encode("utf-8")
    assert ref["sha256"] == hashlib.sha256(body).hexdigest()


# -- py emit: the lazy thunk ----------------------------------------------

def test_py_thunk_shape_sync(tmp_path):
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl", _ref_rvl())
    out = emit_py.emit(compile_files([str(m)]))
    assert "def engine(x):" in out
    assert "from host.engine import do_engine as _f" in out
    assert "_inspect.iscoroutinefunction(_f)" in out
    assert "_inspect.isawaitable(_r)" in out
    # the import is INSIDE the function, not at module top
    top = out.split("def engine(x):")[0]
    assert "from host.engine import" not in top


def test_py_thunk_shape_async(tmp_path):
    _write(tmp_path / "host" / "engine.py",
           "async def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl", _ref_rvl(async_=True, cls="emission"))
    out = emit_py.emit(compile_files([str(m)]))
    assert "async def engine(x):" in out
    assert "return await _f(x)" in out
    # the async direction is NOT hard-refused (no iscoroutinefunction gate)
    thunk = out.split("async def engine(x):")[1].split("\n\n")[0]
    assert "iscoroutinefunction" not in thunk


# -- driver simulation helpers (mirror src/revl/run.py _emit_module) -------

def _load_artifact(ir, root_dir, gen=1):
    """Emit + exec the IR as the py run driver does: exec into a fresh module.
    Does NOT plug (no host execution yet)."""
    source = emit_py.emit(ir)
    mod = types.ModuleType(f"revl_run_gen{gen}")
    sys.modules[mod.__name__] = mod
    exec(compile(source, f"<artifact gen{gen}>", "exec"), mod.__dict__)
    return mod


# -- the headline: no host execution at load/plug, only at first call ------

def test_no_load_execution_leaf_and_parent_sentinel(tmp_path, monkeypatch):
    """The lazy thunk runs host code ONLY at first call. A leaf-module sentinel
    AND a PARENT package `__init__.py` sentinel both stay UNSET through artifact
    load and plug (proving the plug-time spec walk executes no parent __init__),
    and are set only after the extern's first call."""
    leaf = f"LEAF_{os.getpid()}"
    parent = f"PARENT_{os.getpid()}"
    monkeypatch.delenv(leaf, raising=False)
    monkeypatch.delenv(parent, raising=False)
    root = tmp_path
    _write(root / "pkg" / "__init__.py",
           f"import os\nos.environ[{parent!r}] = '1'\n")
    _write(root / "pkg" / "engine.py",
           f"import os\nos.environ[{leaf!r}] = '1'\n"
           "def do_engine(x):\n    return 'engine:' + x\n")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg/engine.py"\n')
    ir = compile_files([str(m)])

    for name in ("pkg", "pkg.engine"):
        sys.modules.pop(name, None)
    try:
        mod = _load_artifact(ir, str(root))
        assert os.environ.get(leaf) is None, "leaf ran at artifact LOAD"
        assert os.environ.get(parent) is None, "parent ran at artifact LOAD"

        # plug: append sys.path, hash-check, evict — must NOT import anything
        hostref.plug_refs(ir, [str(root)])
        assert os.environ.get(leaf) is None, "leaf ran at PLUG"
        assert os.environ.get(parent) is None, "parent __init__ ran at PLUG"

        # first call: NOW the host module (and its parent) execute
        assert mod.engine("hi") == "engine:hi"
        assert os.environ.get(leaf) == "1"
        assert os.environ.get(parent) == "1"
    finally:
        for name in ("pkg", "pkg.engine"):
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen1", None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# -- replug in one process runs the NEW code -------------------------------

def test_replug_runs_new_code(tmp_path):
    root = tmp_path
    hostf = _write(root / "pkg2" / "engine.py",
                   "def do_engine(x):\n    return 'v1'\n")
    _write(root / "pkg2" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg2/engine.py"\n')
    for name in ("pkg2", "pkg2.engine"):
        sys.modules.pop(name, None)
    try:
        ir1 = compile_files([str(m)])
        mod1 = _load_artifact(ir1, str(root), gen=1)
        hostref.plug_refs(ir1, [str(root)])
        assert mod1.engine("x") == "v1"

        # edit the ref'd file and REPLUG in the same process
        _write(hostf, "def do_engine(x):\n    return 'v2'\n")
        ir2 = compile_files([str(m)])
        assert ir2["externs"][0]["refs"]["py"]["sha256"] != \
            ir1["externs"][0]["refs"]["py"]["sha256"]
        mod2 = _load_artifact(ir2, str(root), gen=2)
        hostref.plug_refs(ir2, [str(root)])  # evicts the stale pkg2.engine
        assert mod2.engine("x") == "v2", "replug ran STALE code"
    finally:
        for name in ("pkg2", "pkg2.engine"):
            sys.modules.pop(name, None)
        for g in ("revl_run_gen1", "revl_run_gen2"):
            sys.modules.pop(g, None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# -- the plug-time hash check refuses a swapped file -----------------------

def test_plug_hash_check_refuses_swap(tmp_path):
    root = tmp_path
    hostf = _write(root / "pkg3" / "engine.py",
                   "def do_engine(x):\n    return 'orig'\n")
    _write(root / "pkg3" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg3/engine.py"\n')
    for name in ("pkg3", "pkg3.engine"):
        sys.modules.pop(name, None)
    try:
        ir = compile_files([str(m)])
        # swap the file AFTER compile (the compile/deploy TOCTOU)
        _write(hostf, "def do_engine(x):\n    return 'SWAPPED'\n")
        with pytest.raises(RevlError) as exc:
            hostref.plug_refs(ir, [str(root)])
        msg = str(exc.value)
        assert "does not match" in msg
        assert "expected sha256" in msg
        assert str(hostf) in msg or "engine.py" in msg
    finally:
        for name in ("pkg3", "pkg3.engine"):
            sys.modules.pop(name, None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# -- sys.path is APPENDED, not prepended -----------------------------------

def test_ref_free_program_never_touches_sys_path(tmp_path):
    before = list(sys.path)
    m = _write(tmp_path / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n    = @py { return x }\n")
    ir = compile_files([str(m)])
    hostref.plug_refs(ir, [str(tmp_path)])
    assert sys.path == before


def test_plug_appends_not_prepends(tmp_path):
    root = tmp_path
    _write(root / "pkg4" / "engine.py", "def do_engine(x):\n    return x\n")
    _write(root / "pkg4" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg4/engine.py"\n')
    try:
        ir = compile_files([str(m)])
        first = sys.path[0]
        hostref.plug_refs(ir, [str(root)])
        assert sys.path[0] == first, "root was PREPENDED (would shadow trusted modules)"
        assert str(root) in sys.path
    finally:
        for name in ("pkg4", "pkg4.engine"):
            sys.modules.pop(name, None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# -- the softened coroutine assert -----------------------------------------

def test_sync_ref_to_async_def_raises_at_first_call(tmp_path):
    """A declared-SYNC ref whose symbol is a plain `async def` raises the thunk's
    TypeError at first call (iscoroutinefunction catches the plain shape)."""
    root = tmp_path
    _write(root / "pkg5" / "engine.py",
           "async def do_engine(x):\n    return x\n")
    _write(root / "pkg5" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg5/engine.py"\n')
    for name in ("pkg5", "pkg5.engine"):
        sys.modules.pop(name, None)
    try:
        ir = compile_files([str(m)])
        mod = _load_artifact(ir, str(root))
        hostref.plug_refs(ir, [str(root)])
        with pytest.raises(TypeError, match="declared sync but"):
            mod.engine("x")
    finally:
        for name in ("pkg5", "pkg5.engine"):
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen1", None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_sync_ref_returning_coroutine_caught_by_isawaitable(tmp_path):
    """A plain `def` that RETURNS a coroutine (a sync wrapper) is not an
    `iscoroutinefunction`, so the value-level `isawaitable` guard is what catches
    it — the softened claim, not "impossible by construction". The un-awaited
    coroutine we deliberately provoke here is expected (RuntimeWarning filtered)."""
    root = tmp_path
    _write(root / "pkg6" / "engine.py",
           "async def _inner(x):\n    return x\n"
           "def do_engine(x):\n    return _inner(x)\n")
    _write(root / "pkg6" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg6/engine.py"\n')
    for name in ("pkg6", "pkg6.engine"):
        sys.modules.pop(name, None)
    try:
        ir = compile_files([str(m)])
        mod = _load_artifact(ir, str(root))
        hostref.plug_refs(ir, [str(root)])
        with pytest.raises(TypeError, match="returned"):
            mod.engine("x")
    finally:
        for name in ("pkg6", "pkg6.engine"):
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen1", None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


def test_async_ref_to_valid_awaitable_callable_not_refused(tmp_path):
    """A declared-async ref whose symbol is a CALLABLE INSTANCE with an async
    `__call__` (not an `iscoroutinefunction`) is NOT false-refused — the async
    direction stays awaited-by-name (must-fix #3)."""
    import asyncio
    root = tmp_path
    _write(root / "pkg7" / "engine.py",
           "class _Eng:\n"
           "    async def __call__(self, x):\n"
           "        return 'ok:' + x\n"
           "do_engine = _Eng()\n")
    _write(root / "pkg7" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern emission async fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkg7/engine.py"\n')
    for name in ("pkg7", "pkg7.engine"):
        sys.modules.pop(name, None)
    try:
        ir = compile_files([str(m)])
        mod = _load_artifact(ir, str(root))
        hostref.plug_refs(ir, [str(root)])
        assert asyncio.run(mod.engine("hi")) == "ok:hi"
    finally:
        for name in ("pkg7", "pkg7.engine"):
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen1", None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# -- ts emit: the thunk with an extension-ful specifier --------------------

def test_ts_thunk_extensionful_specifier(tmp_path):
    _write(tmp_path / "host" / "engine.ts",
           "export function doEngine(x: string): string { return x }\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @ts ref doEngine from "host/engine.ts"\n')
    out = emit_ts.emit(compile_files([str(m)]))
    assert "_revl_ref_path(\"host/engine.ts\")" in out
    assert "_REVL_REFS" in out
    # a module-top static import of the ref'd module must NOT appear
    assert "from \"host/engine\"" not in out


def test_ts_async_ref_dynamic_import(tmp_path):
    _write(tmp_path / "host" / "engine.ts",
           "export async function doEngine(x: string) { return x }\n")
    m = _write(tmp_path / "m.rvl",
               "extern emission async fn engine(x: Str) -> Str\n"
               '    = @ts ref doEngine from "host/engine.ts"\n')
    out = emit_ts.emit(compile_files([str(m)]))
    assert "await import(" in out
    assert "await _f(x)" in out


def test_ts_extensionless_refused(tmp_path):
    _write(tmp_path / "host" / "engine", "export function foo(x){return x}\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @ts ref foo from "host/engine"\n')
    with pytest.raises(RevlError, match="no file extension"):
        compile_files([str(m)])


# -- collision, config, poly ----------------------------------------------

def test_collision_body_plus_ref_refused(tmp_path):
    _write(tmp_path / "host" / "engine.py", "def foo(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n"
               "    = @py { return x }\n"
               '    = @py ref foo from "host/engine.py"\n')
    with pytest.raises(RevlError, match="duplicate @py body"):
        compile_files([str(m)])


def test_config_plus_ref_refused(tmp_path):
    _write(tmp_path / "host" / "engine.py", "def foo(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n"
               '    config { provider: Str = "a" }\n'
               '    = @py ref foo from "host/engine.py"\n')
    with pytest.raises(RevlError, match="config extern cannot use a host-module ref"):
        compile_files([str(m)])


def test_poly_plus_ref_refused(tmp_path):
    _write(tmp_path / "host" / "engine.py", "def foo(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern emission fn|async e(x: Str) -> Str\n"
               '    = @py ref foo from "host/engine.py"\n')
    with pytest.raises(RevlError, match="caller-decided-colour"):
        compile_files([str(m)])


# -- bundle interim refusal + audit provenance -----------------------------

def test_bundle_ref_program_clean_refusal(tmp_path):
    """Until stage 4 lands, bundling a ref program is a CLEAN refusal naming the
    gap, never a bundle that cannot verify (the flat basename copy would not
    carry the root-relative ref'd file)."""
    from revl.bundle import build_bundle
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl", _ref_rvl())
    with pytest.raises(RevlError, match="references an external host module"):
        build_bundle([str(m)], str(tmp_path / "out"))


def test_audit_surfaces_ref_provenance(tmp_path):
    """`revl audit` shows the ref path and symbol for a ref extern, and a
    ref-only extern's backend (not "none")."""
    from revl.__main__ import _boundary
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern emission fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "host/engine.py"\n')
    ir = compile_files([str(m)])
    # the --json declared_externs surface (mirrors __main__.audit)
    ext = ir["externs"][0]
    backends = sorted(set(ext.get("bodies") or {}) | set(ext.get("refs") or {}))
    assert backends == ["py"]
    prov = {t: f"{r['path']}#{r['symbol']}" for t, r in ext["refs"].items()}
    assert prov == {"py": "host/engine.py#do_engine"}
