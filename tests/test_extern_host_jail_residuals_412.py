"""Roadmap item 412: the second-pass adversarial residuals in the item-396
host-file jails (hostref.py option B + hostfile.py option A). The jails are
SOUND (no escape, no load-time execution); these are hardening fixes:

- R1 (silent-stale): `plug_refs` evicted only a ref's OWN dotted chain, so a
  ref'd module's TRANSITIVE dep (`import shared`, `from pkg import dep`) was
  served STALE from `sys.modules` on an in-process replug even though the ref
  itself re-imported fresh — the item-380 class one level deeper. The fix sweeps
  the WHOLE in-root closure under the user compile roots.
- R2 (fail-open): a USER ref whose pinned file is DEPLOYED in a user root but
  does NOT resolve to it at plug used to skip the hash check and defer to the
  thunk's first-call import — a module that becomes resolvable between plug and
  first call would run UNHASHED, where a stdlib ref fails CLOSED. The fix refuses
  loudly, matching the stdlib backstop, while a legitimately-not-deployed user
  ref still surfaces the first-call ImportError as before.
- R3 (platform gap): `_real_path_of_fd` gained a Windows branch, and the
  docstrings name macOS/Linux/Windows as where the handle-realpath guarantee
  holds rather than claiming it universally.
- R4 (cosmetic): a NUL byte in a body-file path now raises a clean RevlError
  instead of an uncaught ValueError from `os.open`.
"""

from __future__ import annotations

import sys
import types

import pytest

from revl import hostfile, hostref
from revl.compiler import compile_files
from revl.errors import RevlError


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load_artifact(ir, gen=1):
    from _backend_import import backend_emitter
    emit_py = backend_emitter("python")
    source = emit_py.emit(ir)
    mod = types.ModuleType(f"revl_run_gen412_{gen}")
    sys.modules[mod.__name__] = mod
    exec(compile(source, f"<412 artifact gen{gen}>", "exec"), mod.__dict__)
    return mod


# ---------------------------------------------------------------------------
# R1: a replug after editing a ref'd module's TRANSITIVE DEP runs the NEW dep.
# The dep is a SEPARATE top-level module (`import shared`), the hardest case: it
# sits under the user root but OUTSIDE the ref's own package, so only a whole
# in-root-closure sweep (not a per-package one) re-imports it fresh.
# ---------------------------------------------------------------------------

def test_replug_reimports_transitive_dep_fresh(tmp_path):
    root = tmp_path
    _write(root / "pkgR1" / "__init__.py", "")
    _write(root / "pkgR1" / "engine.py",
           "import sharedR1\n"
           "def do_engine(x):\n    return sharedR1.VALUE + ':' + x\n")
    shared = _write(root / "sharedR1.py", "VALUE = 'v1'\n")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkgR1/engine.py"\n')
    names = ("pkgR1", "pkgR1.engine", "sharedR1")
    for name in names:
        sys.modules.pop(name, None)
    try:
        ir1 = compile_files([str(m)])
        mod1 = _load_artifact(ir1, gen=1)
        hostref.plug_refs(ir1, [str(root)])
        assert mod1.engine("x") == "v1:x"
        assert "sharedR1" in sys.modules, "the transitive dep loaded on first call"

        # edit ONLY the transitive dep; the ref'd engine.py is byte-identical, so
        # its pinned hash does not change — the staleness is one level deeper.
        _write(shared, "VALUE = 'v2'\n")
        ir2 = compile_files([str(m)])
        assert ir2["externs"][0]["refs"]["py"]["sha256"] == \
            ir1["externs"][0]["refs"]["py"]["sha256"], \
            "engine.py unchanged, so the ref hash is unchanged (dep-only edit)"
        mod2 = _load_artifact(ir2, gen=2)
        hostref.plug_refs(ir2, [str(root)])  # sweeps the in-root closure
        assert mod2.engine("x") == "v2:x", \
            "replug served the STALE transitive dep (R1 not fixed)"
    finally:
        for name in names:
            sys.modules.pop(name, None)
        for g in ("revl_run_gen412_1", "revl_run_gen412_2"):
            sys.modules.pop(g, None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


def test_replug_reimports_same_package_dep_fresh(tmp_path):
    """The simpler same-package transitive dep (`from pkg import dep`) is also
    re-imported fresh (the R1 example verbatim)."""
    root = tmp_path
    _write(root / "pkgR1b" / "__init__.py", "")
    _write(root / "pkgR1b" / "dep.py", "VALUE = 'd1'\n")
    _write(root / "pkgR1b" / "engine.py",
           "from pkgR1b import dep\n"
           "def do_engine(x):\n    return dep.VALUE + ':' + x\n")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkgR1b/engine.py"\n')
    names = ("pkgR1b", "pkgR1b.engine", "pkgR1b.dep")
    for name in names:
        sys.modules.pop(name, None)
    try:
        ir1 = compile_files([str(m)])
        mod1 = _load_artifact(ir1, gen=1)
        hostref.plug_refs(ir1, [str(root)])
        assert mod1.engine("x") == "d1:x"

        _write(root / "pkgR1b" / "dep.py", "VALUE = 'd2'\n")
        ir2 = compile_files([str(m)])
        mod2 = _load_artifact(ir2, gen=2)
        hostref.plug_refs(ir2, [str(root)])
        assert mod2.engine("x") == "d2:x", "replug served the stale package dep"
    finally:
        for name in names:
            sys.modules.pop(name, None)
        for g in ("revl_run_gen412_1", "revl_run_gen412_2"):
            sys.modules.pop(g, None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# ---------------------------------------------------------------------------
# R2: a USER ref whose pinned file is DEPLOYED in-root but does not RESOLVE to
# it at plug (a partial same-top-level shadow on an earlier sys.path entry) now
# REFUSES loudly, instead of deferring to an unhashed first-call import.
# ---------------------------------------------------------------------------

def test_user_ref_deployed_but_unresolvable_fails_closed(tmp_path):
    root = tmp_path / "proj"
    _write(root / "pkgR2" / "__init__.py", "")
    _write(root / "pkgR2" / "engine.py", "def do_engine(x):\n    return x\n")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkgR2/engine.py"\n')
    ir = compile_files([str(m)])  # user ref: no "root" key
    assert "root" not in ir["externs"][0]["refs"]["py"]

    # a FOREIGN regular `pkgR2` package (has __init__.py) on an EARLIER sys.path
    # entry WITHOUT the `engine` submodule: the static walk descends into it,
    # finds no leaf, returns None — the deployed in-root engine.py never resolves.
    foreign = tmp_path / "foreign"
    _write(foreign / "pkgR2" / "__init__.py", "RAN = True\n")

    before = list(sys.path)
    for name in ("pkgR2", "pkgR2.engine"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(foreign))
    try:
        with pytest.raises(RevlError) as exc:
            hostref.plug_refs(ir, [str(root)])
        msg = str(exc.value)
        # names the DEPLOYED in-root file and the dotted name that did not resolve
        assert str(root / "pkgR2" / "engine.py") in msg
        assert "pkgR2.engine" in msg
        assert "does not resolve to it" in msg
    finally:
        sys.path[:] = before
        for name in ("pkgR2", "pkgR2.engine"):
            sys.modules.pop(name, None)


def test_user_ref_not_deployed_still_defers(tmp_path):
    """A user ref whose pinned file is legitimately NOT deployed at plug (absent
    from every user root, no shadow) must NOT be over-refused — it still surfaces
    the thunk's first-call ImportError, so `plug_refs` returns cleanly."""
    root = tmp_path / "proj"
    engine = _write(root / "pkgR2b" / "engine.py", "def do_engine(x):\n    return x\n")
    _write(root / "pkgR2b" / "__init__.py", "")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkgR2b/engine.py"\n')
    ir = compile_files([str(m)])
    # remove the deployed leaf so it is genuinely absent at plug (no shadow)
    engine.unlink()

    before = list(sys.path)
    for name in ("pkgR2b", "pkgR2b.engine"):
        sys.modules.pop(name, None)
    try:
        hostref.plug_refs(ir, [str(root)])  # must NOT raise
    finally:
        sys.path[:] = before
        for name in ("pkgR2b", "pkgR2b.engine"):
            sys.modules.pop(name, None)


def test_legit_user_ref_still_plugs_and_runs(tmp_path):
    """The happy path is unchanged: a deployed, resolvable, hash-matching user
    ref plugs and runs (the R2 fix is additive)."""
    root = tmp_path
    _write(root / "pkgR2c" / "__init__.py", "")
    _write(root / "pkgR2c" / "engine.py",
           "def do_engine(x):\n    return 'ok:' + x\n")
    m = _write(root / "m.rvl",
               "extern pure fn engine(x: Str) -> Str\n"
               '    = @py ref do_engine from "pkgR2c/engine.py"\n')
    names = ("pkgR2c", "pkgR2c.engine")
    for name in names:
        sys.modules.pop(name, None)
    try:
        ir = compile_files([str(m)])
        mod = _load_artifact(ir, gen=1)
        hostref.plug_refs(ir, [str(root)])
        assert mod.engine("hi") == "ok:hi"
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.pop("revl_run_gen412_1", None)
        if str(root) in sys.path:
            sys.path.remove(str(root))


# ---------------------------------------------------------------------------
# R3: the docstring matches the code (platforms named, Windows branch present).
# ---------------------------------------------------------------------------

def test_r3_windows_branch_present_and_docstring_matches():
    assert hasattr(hostfile, "_win_final_path_of_fd")
    # the universal claim is downgraded: the module docstring names the three
    # platforms and states the fallback race, not a blanket guarantee.
    doc = hostfile.__doc__ or ""
    assert "GetFinalPathNameByHandle" in doc
    assert "macOS/Linux/Windows" in doc
    fn_doc = hostfile._real_path_of_fd.__doc__ or ""
    assert "Windows" in fn_doc


# ---------------------------------------------------------------------------
# R4: a NUL byte in a body-file path is a clean RevlError, not a crash.
# ---------------------------------------------------------------------------

def test_r4_nul_byte_in_body_path_is_clean_error(tmp_path):
    with pytest.raises(RevlError) as exc:
        hostfile.read_body_file_disk(
            str(tmp_path), "bad\x00name.py", "py", "e", "m.rvl", 1)
    assert "invalid path" in str(exc.value)
