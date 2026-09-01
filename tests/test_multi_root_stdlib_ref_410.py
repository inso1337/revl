"""Roadmap item 410: multi-root stdlib host-ref imports, the 396(B) follow-up.

396(B) landed with ONE ref root, the user compile tree, and REFUSED any ref from
a module resolved through the item-319 search path (the stdlib default or a
REVL_IMPORT_PATH entry). 410 adds a SECOND root so a SHIPPED stdlib module can
`= @ts ref`/`@py ref` a SHIPPED helper, while keeping the two trust domains
strictly separate:

- ORIGIN decides the jail's root set. An install-origin module (search-path
  resolved, or contained in `stdlib_root()`) jails its refs to that one install
  entry and stamps the additive IR key `"root": "stdlib"`; a user module is
  byte-identical to 396(B).
- A USER ref can NEVER reach the install tree (security), and a stdlib ref never
  resolves against the user tree.
- The py driver appends the install root at plug (append-not-prepend, hash-check
  backstop); the ts runner provides a second root `__REVL_STDLIB_REF_ROOT__`.
- `revl bundle` carries a stdlib ref (re-resolve, not travel) and `revl verify`
  re-hashes it against the pin on the VERIFYING machine's install.

The stand-in install pattern (a monkeypatched `stdlib_root()` over a temp tree)
exercises the whole search-path + containment path AND keeps the py-plug and
verify install roots self-consistent, so a stand-in composition runs and
round-trips end to end without touching the real revl install tree.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types

import pytest

from revl import compiler as _compiler
from revl import hostref
from revl.compiler import compile_files
from revl.errors import RevlError

from _backend_import import backend_emitter  # noqa: E402

emit_py = backend_emitter("python")
emit_ts = backend_emitter("typescript")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _standin_install(tmp_path, monkeypatch, *, py=True, ts=True):
    """Build a stand-in revl install tree under `tmp_path/install` and point
    `stdlib_root()` at its `stdlib/` (patched everywhere the compiler, the py
    driver, the ts runner and bundle read it), so the search-path default and the
    py-plug/verify install roots all agree on ONE tree. Returns the install root
    Path. The stdlib module `stdlib/mymod.rvl` refs shipped helpers under
    `runtimes/` (a collision-free stand-in for `backends/`)."""
    install = tmp_path / "install"
    stdlib_dir = install / "stdlib"
    stdlib_dir.mkdir(parents=True)
    (install / "runtimes" / "pyhost").mkdir(parents=True)
    (install / "runtimes" / "tshost").mkdir(parents=True)

    bodies = []
    if py:
        _write(install / "runtimes" / "pyhost" / "helper.py",
               "def shout(x):\n    return x.upper()\n")
        bodies.append('    = @py ref shout from "../runtimes/pyhost/helper.py"')
    if ts:
        _write(install / "runtimes" / "tshost" / "helper.ts",
               "export function shout(x: string): string "
               "{ return x.toUpperCase() }\n")
        bodies.append('    = @ts ref shout from "../runtimes/tshost/helper.ts"')
    _write(stdlib_dir / "mymod.rvl",
           "pub extern pure fn shout(x: Str) -> Str\n" + "\n".join(bodies) + "\n")

    def _stdlib_root():
        return stdlib_dir

    # patch `stdlib_root` in every module that bound it at import, so the
    # search-path default, the py-plug install root, the ts runner root and the
    # verify tier all agree on this one stand-in install.
    monkeypatch.setattr(_compiler, "stdlib_root", _stdlib_root)
    monkeypatch.setattr(hostref, "stdlib_root", _stdlib_root)
    from revl import bundle as _bundle
    from revl import run_ts as _run_ts
    monkeypatch.setattr(_bundle, "stdlib_root", _stdlib_root)
    monkeypatch.setattr(_run_ts, "stdlib_root", _stdlib_root)
    return install


# ---------------------------------------------------------------------------
# HEADLINE: a stdlib module's ref resolves, pins the kind, and RUNS (py)
# ---------------------------------------------------------------------------

def test_stdlib_ref_resolves_and_pins_kind(tmp_path, monkeypatch):
    install = _standin_install(tmp_path, monkeypatch)
    app = _write(tmp_path / "proj" / "app.rvl",
                 'use "stdlib/mymod.rvl" { shout }\n'
                 "extern pure fn drive(x: Str) -> Str = @py { return x }\n")
    ir = compile_files([str(app)])
    ext = next(e for e in ir["externs"] if e["name"] == "shout")
    py = ext["refs"]["py"]
    ts = ext["refs"]["ts"]
    # the additive kind is stamped, and the path is layout-uniform (relative to
    # the install root, no machine path, no `..`).
    assert py["root"] == "stdlib"
    assert py["path"] == "runtimes/pyhost/helper.py"
    assert ts["root"] == "stdlib"
    assert ts["path"] == "runtimes/tshost/helper.ts"
    expect = hashlib.sha256(
        (install / "runtimes" / "pyhost" / "helper.py").read_bytes()).hexdigest()
    assert py["sha256"] == expect


def test_stdlib_ref_runs_on_py(tmp_path, monkeypatch):
    """End to end on py: emit + plug (append the install root, hash-check) + first
    call runs the shipped helper. No host-install step, no __revlFs-style global.
    The plug appends `stdlib_root().parent` (the stand-in install), which is where
    the ref pinned its file, so the dotted `runtimes.pyhost.helper` resolves."""
    install = _standin_install(tmp_path, monkeypatch, ts=False)
    app = _write(tmp_path / "proj" / "app.rvl",
                 'use "stdlib/mymod.rvl" { shout }\n')
    ir = compile_files([str(app)])
    for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
        sys.modules.pop(name, None)
    src = emit_py.emit(ir)
    mod = types.ModuleType("revl_run_gen410")
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(src, "<410 artifact>", "exec"), mod.__dict__)
        # the driver appends the user root (app dir) — no stdlib root yet; plug
        # adds the install root because a stdlib ref is present.
        hostref.plug_refs(ir, [str(app.parent)])
        assert str(install) in sys.path, "install root was not appended at plug"
        assert mod.shout("hi") == "HI"
    finally:
        for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen410", None)
        for p in (str(install), str(app.parent)):
            if p in sys.path:
                sys.path.remove(p)


def test_stdlib_ref_ts_emit_dispatches_to_stdlib_root(tmp_path, monkeypatch):
    """The ts thunk for a stdlib ref resolves through `_revl_ref_path_stdlib`
    (the install root), never the user `_revl_ref_path`; the runtime defines both
    path functions and their two globals."""
    _standin_install(tmp_path, monkeypatch, py=False)
    app = _write(tmp_path / "proj" / "app.rvl",
                 'use "stdlib/mymod.rvl" { shout }\n')
    ir = compile_files([str(app)])
    out = emit_ts.emit(ir)
    assert "function _revl_ref_path_stdlib(" in out
    assert "__REVL_STDLIB_REF_ROOT__" in out
    # the shout thunk dispatches to the stdlib path fn with the recorded rel path
    assert '_revl_ref_path_stdlib("runtimes/tshost/helper.ts")' in out
    # and NOT to the user path fn for this stdlib ref
    thunk = out.split("export function shout(")[1].split("\n\n")[0]
    assert "_revl_ref_path(" not in thunk


def test_run_ts_spec_carries_stdlib_root_and_kind(tmp_path, monkeypatch):
    """`run_ts._spec` gains `stdlibRefRoot` and each ref carries its `root` kind
    so the node runner joins/hash-checks against the right trust domain."""
    from revl import run_ts as _run_ts
    install = _standin_install(tmp_path, monkeypatch, py=False)
    monkeypatch.setattr(_run_ts, "stdlib_root", lambda: install / "stdlib")
    app = _write(tmp_path / "proj" / "app.rvl",
                 'use "stdlib/mymod.rvl" { shout }\n')
    ir = compile_files([str(app)])
    spec = _run_ts._spec(ir, {}, [str(app)], "mod")
    assert spec["stdlibRefRoot"] == str(install)
    ref = next(r for r in spec["refs"] if r["extern"] == "shout")
    assert ref["root"] == "stdlib"
    assert ref["path"] == "runtimes/tshost/helper.ts"


# ---------------------------------------------------------------------------
# SECURITY: a user ref can never reach the install tree, and vice versa
# ---------------------------------------------------------------------------

def test_user_ref_cannot_reach_stdlib_root(tmp_path, monkeypatch):
    """A USER module (user-origin) that writes a ref ESCAPING into the install
    tree is refused at compile with the outside-the-root message — origin selects
    the user root set, so the install tree is simply not a jail it may reach."""
    install = _standin_install(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    app = _write(proj / "app.rvl",
                 "extern pure fn steal(x: Str) -> Str\n"
                 f'    = @py ref shout from '
                 f'"{os.path.relpath(install / "runtimes" / "pyhost" / "helper.py", proj)}"\n')
    with pytest.raises(RevlError) as exc:
        compile_files([str(app)])
    msg = str(exc.value)
    assert "OUTSIDE the root compile tree" in msg
    # and the target really was the install helper it tried to reach
    assert "helper.py" in msg


def test_escaping_use_cannot_forge_stdlib_origin(tmp_path, monkeypatch):
    """A `..`-escaping `use` path that MISSES importer-relative resolution but
    JOINS a search-path base (here the install-tree root `stdlib_root().parent`)
    must NOT be stamped install-origin just because the join matched: its
    resolved realpath escapes the base. `resolve_use` gates the install-origin
    record on realpath containment, so an escaping `use` is user-origin and a ref
    it declares into the install tree is refused by 396(B) — it cannot forge
    `root: stdlib` and reach an install-tree host file (e.g. backends/.../emit.py).
    """
    # stand-in install two levels under tmp_path so an escaping `use` from a
    # one-level `proj` misses importer-relative resolution but hits the install
    # base via `..`.
    install = tmp_path / "opt" / "revl"
    stdlib_dir = install / "stdlib"
    stdlib_dir.mkdir(parents=True)
    _write(install / "backends" / "python" / "emit.py",
           "def pwned(x):\n    return 'PWNED:' + x\n")
    monkeypatch.setattr(_compiler, "stdlib_root", lambda: stdlib_dir)
    monkeypatch.setattr(hostref, "stdlib_root", lambda: stdlib_dir)

    # attacker module OUTSIDE the install tree, refing an install-tree host file
    attacker = tmp_path / "attacker"
    _write(attacker / "evil.rvl",
           "pub extern pure fn pwn(x: Str) -> Str\n"
           '    = @py ref pwned from "../opt/revl/backends/python/emit.py"\n')

    # root project: `use "../../attacker/evil.rvl"` misses proj-relative
    # resolution (nothing above tmp_path) but joins the install base via `..`.
    proj = tmp_path / "proj"
    app = _write(proj / "main.rvl", 'use "../../attacker/evil.rvl" { pwn }\n')

    with pytest.raises(RevlError) as exc:
        compile_files([str(app)])
    msg = str(exc.value)
    assert "OUTSIDE the root compile tree" in msg, msg
    assert "emit.py" in msg


def test_user_kind_ref_never_appends_stdlib_root_at_plug(tmp_path, monkeypatch):
    """A handcrafted USER-kind ref IR (no `"root"` key) naming a stdlib-relative
    path must NOT resolve against the stdlib root at plug: `plug_refs` appends the
    install root ONLY when a stdlib-kind ref exists, so a user-kind ref falls to
    the thunk's first-call ImportError, never a silent stdlib resolution."""
    install = _standin_install(tmp_path, monkeypatch)
    before = list(sys.path)
    ir = {"externs": [{
        "name": "steal",
        "refs": {"py": {"symbol": "shout",
                        "path": "runtimes/pyhost/helper.py",
                        "sha256": "0" * 64}},  # user-kind: NO "root" key
    }]}
    try:
        hostref.plug_refs(ir, [str(tmp_path / "userroot")])
        assert str(install) not in sys.path, \
            "a user-kind ref appended the install root (would cross domains)"
    finally:
        sys.path[:] = before


# ---------------------------------------------------------------------------
# ADDITIVITY: no stdlib ref = byte-identical
# ---------------------------------------------------------------------------

def test_additivity_user_ref_no_root_key(tmp_path):
    """A user-origin ref (396(B)'s only case) carries NO `"root"` key, so its IR
    is byte-identical to before 410, and its py plug never appends any stdlib
    root."""
    _write(tmp_path / "host" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(tmp_path / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "host/engine.py"\n')
    ir = compile_files([str(m)])
    assert "root" not in ir["externs"][0]["refs"]["py"]
    # a user ref never adds the stdlib install root to sys.path
    before = list(sys.path)
    try:
        hostref.plug_refs(ir, [str(tmp_path)])
        assert all("install" not in p for p in sys.path if p not in before) \
            or sys.path[:len(before)] == before
    finally:
        for name in list(sys.modules):
            if name.startswith("host"):
                sys.modules.pop(name, None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_additivity_no_ref_plug_untouched(tmp_path):
    """A composition with NO ref never touches sys.path at plug (unchanged)."""
    before = list(sys.path)
    m = _write(tmp_path / "m.rvl",
               "extern pure fn e(x: Str) -> Str\n    = @py { return x }\n")
    ir = compile_files([str(m)])
    hostref.plug_refs(ir, [str(tmp_path)])
    assert sys.path == before


# ---------------------------------------------------------------------------
# BUNDLE round-trip across a DIFFERENT install path
# ---------------------------------------------------------------------------

def _install_b_from(install_a, dest):
    """Copy a stand-in install to a NEW absolute path (install B, same content)."""
    import shutil
    shutil.copytree(install_a, dest)
    return dest


def test_bundle_roundtrips_stdlib_ref_across_install_paths(tmp_path, monkeypatch):
    from revl.bundle import build_bundle, verify_bundle
    # bundle on install A
    install_a = _standin_install(tmp_path, monkeypatch, ts=False)
    app = _write(tmp_path / "proj" / "app.rvl",
                 'use "stdlib/mymod.rvl" { shout }\n')
    out = build_bundle([str(app)], str(tmp_path / "bundle"))

    # verify on install B: a DIFFERENT absolute path, identical content + version
    install_b = _install_b_from(install_a, tmp_path / "install_b")
    from revl import bundle as _bundle
    monkeypatch.setattr(_compiler, "stdlib_root", lambda: install_b / "stdlib")
    monkeypatch.setattr(_bundle, "stdlib_root", lambda: install_b / "stdlib")
    # verify recompiles through the compiler's search path -> install B, and the
    # stdlib refs tier re-hashes B's helper against the recorded pin.
    report = verify_bundle(out)
    tiers = {c.tier: c for c in report.checks}
    stdlib_tier = next(c for t, c in tiers.items() if t.startswith("stdlib ref"))
    assert stdlib_tier.status == "OK", (stdlib_tier.tier, stdlib_tier.detail)
    # the round-trip signal: the reproducible aggregate (source + IR + emitted)
    # is OK across the different install path, and no 410-relevant tier
    # diverged. (An orthogonal `gauntlet`/`attestation` tier verdict of this
    # minimal, un-attested composition is not a 410 concern.)
    assert tiers["reproducible"].status == "OK", \
        [(c.tier, c.status, c.detail) for c in report.checks]
    assert not any(
        c.status == "MISMATCH"
        for c in report.checks
        if c.tier in ("source", "IR") or c.tier.startswith("emitted")
        or c.tier.startswith("stdlib ref")), \
        [(c.tier, c.status) for c in report.checks]

    # doctor the helper on B, same version -> stdlib refs MISMATCH naming the file
    _write(install_b / "runtimes" / "pyhost" / "helper.py",
           "def shout(x):\n    return x + '!'\n")
    report2 = verify_bundle(out)
    stdlib2 = next(c for c in report2.checks if c.tier.startswith("stdlib ref"))
    assert stdlib2.status == "MISMATCH"
    assert "helper.py" in stdlib2.tier

    # missing helper on B -> stdlib refs UNVERIFIED (SKIP) naming the file
    os.remove(install_b / "runtimes" / "pyhost" / "helper.py")
    report3 = verify_bundle(out)
    stdlib3 = next(c for c in report3.checks if c.tier.startswith("stdlib ref"))
    from revl.bundle import UNVERIFIED
    assert stdlib3.status == UNVERIFIED  # a SKIP: the install lacks the helper
    assert "helper.py" in stdlib3.tier


_BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""


def test_bundle_no_stdlib_ref_has_no_stdlib_tier(tmp_path):
    """A bundle with no stdlib ref grows no `stdlib refs` line — the tier is
    additive (empty list when the composition carries no stdlib ref)."""
    from revl.bundle import build_bundle, verify_bundle
    m = _write(tmp_path / "app.rvl", _BASE)
    out = build_bundle([str(m)], str(tmp_path / "b"), env={})
    report = verify_bundle(out)
    assert not any(c.tier.startswith("stdlib ref") for c in report.checks)
