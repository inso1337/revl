"""Roadmap item 396 option A: an extern host body that references an external
host-code file, `= @backend file "path"`, spliced at COMPILE time.

The feature reads a body-shaped host file relative to the declaring `.rvl`
file's directory and splices it as the extern body, byte-identical to an inline
`= @backend { ... }` on the py/ts tiers. This suite pins the design's exit
tests: splice-equals-inline (py and ts), the JAIL (absolute path, `..` escape,
symlink escape, prefix-sibling containment, the stated hardlink residual), the
in-memory / loaderless no-disk contract, content-hash reproducibility, and the
no-extern-before-resolution ordering for an untrusted author.

Design: docs/design/396-host-code-file-reference.md.
"""

from __future__ import annotations

import os

import pytest

from revl import hostfile
from revl.admit_profile import AdmissionProfile
from revl.compiler import compile_files, compile_source
from revl.errors import RevlError

from _backend_import import backend_emitter  # noqa: E402

emit_py = backend_emitter("python")
emit_ts = backend_emitter("typescript")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- splice equivalence (scoped to py and ts, per the design) --------------

_INNER_PY = "x = a + b\nreturn str(x)"
_INNER_TS = "const x = a + b;\nreturn String(x);"


def _py_inline(inner):
    return (f"extern pure fn addstr(a: Int, b: Int) -> Str\n"
            f"    = @py {{\n{inner}\n}}\n")


def _py_file(name):
    return (f"extern pure fn addstr(a: Int, b: Int) -> Str\n"
            f'    = @py file "{name}"\n')


def test_splice_equals_inline_py(tmp_path):
    """`= @py { X }` and `= @py file "f.py"` (file = exactly X) emit
    byte-identical py output."""
    inline = _write(tmp_path / "inline.rvl", _py_inline(_INNER_PY))
    _write(tmp_path / "body.py", _INNER_PY + "\n")
    filed = _write(tmp_path / "filed.rvl", _py_file("body.py"))

    out_inline = emit_py.emit(compile_files([str(inline)]))
    out_filed = emit_py.emit(compile_files([str(filed)]))
    assert out_inline == out_filed
    assert "def addstr(a, b):\n    x = a + b\n    return str(x)" in out_filed


def test_splice_equals_inline_ts(tmp_path):
    """Same byte-identity on the ts tier."""
    inline = _write(
        tmp_path / "inline.rvl",
        f"extern pure fn addstr(a: Int, b: Int) -> Str\n    = @ts {{\n{_INNER_TS}\n}}\n")
    _write(tmp_path / "body.ts", _INNER_TS + "\n")
    filed = _write(
        tmp_path / "filed.rvl",
        'extern pure fn addstr(a: Int, b: Int) -> Str\n    = @ts file "body.ts"\n')

    out_inline = emit_ts.emit(compile_files([str(inline)]))
    out_filed = emit_ts.emit(compile_files([str(filed)]))
    assert out_inline == out_filed


def test_ir_records_path_and_hash(tmp_path):
    _write(tmp_path / "body.py", _INNER_PY + "\n")
    filed = _write(tmp_path / "m.rvl", _py_file("body.py"))
    ir = compile_files([str(filed)])
    ext = ir["externs"][0]
    assert ext["body_files"]["py"]["path"] == "body.py"
    assert len(ext["body_files"]["py"]["sha256"]) == 64


def test_additivity_no_file_form_has_no_body_files_key(tmp_path):
    inline = _write(tmp_path / "m.rvl", _py_inline(_INNER_PY))
    ir = compile_files([str(inline)])
    assert "body_files" not in ir["externs"][0]


# -- the jail ---------------------------------------------------------------

def _refuse(tmp_path, body_line, extra=None):
    """Compile a one-extern module whose @py body is `body_line`; return the
    raised RevlError (or fail)."""
    if extra:
        for rel, text in extra.items():
            _write(tmp_path / rel, text)
    src = f"extern pure fn f() -> Str\n    = {body_line}\n"
    m = _write(tmp_path / "m.rvl", src)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(m)])
    return excinfo.value


def test_jail_refuses_absolute_path(tmp_path):
    err = _refuse(tmp_path, '@py file "/etc/passwd"')
    assert "absolute" in str(err)


def test_jail_refuses_dotdot_escape(tmp_path):
    _write(tmp_path / "outside.py", "return '1'")   # sibling of tmp_path root
    # place the module in a subdir so `..` climbs into tmp_path (still refused
    # as written, before any resolution).
    m = _write(tmp_path / "sub" / "m.rvl",
               'extern pure fn f() -> Str\n    = @py file "../outside.py"\n')
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(m)])
    assert ".." in str(excinfo.value)


def test_jail_refuses_symlink_escape(tmp_path):
    outside = _write(tmp_path.parent / f"outside-{tmp_path.name}.py", "secret = 1")
    moddir = tmp_path / "mod"
    moddir.mkdir()
    link = moddir / "escape.py"
    os.symlink(outside, link)
    m = _write(moddir / "m.rvl",
               'extern pure fn f() -> Str\n    = @py file "escape.py"\n')
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(m)])
    assert "OUTSIDE" in str(excinfo.value) or "not inside" in str(excinfo.value)


def test_jail_prefix_sibling_does_not_pass_containment():
    """`/foo` vs `/foobar`: a commonpath check must not conflate them (the bug a
    `str.startswith` jail would have)."""
    assert hostfile._contained("/foo/body.py", "/foo") is True
    assert hostfile._contained("/foobar/body.py", "/foo") is False
    assert hostfile._contained("/foo", "/foo") is True


def test_jail_hardlink_residual_is_accepted_and_stated(tmp_path):
    """The one accepted residual (design §jail): an inode reachable both INSIDE
    the tree and outside it (a hardlink straddling the jail boundary) realpaths
    to the inside name when opened there and passes. This documents the residual
    as a test rather than claiming it away.

    Ordering matters for determinism: the inside name is created FIRST, then a
    second hardlink is placed outside the module dir. A hardlink has no
    direction (both names are equal entries to one inode), so this is the same
    boundary-straddling inode either way, but the opened handle's real path
    (macOS `F_GETPATH`, Linux `/proc/self/fd`) reports whichever name the
    kernel's vnode name cache holds. With the inside name created first that is
    the inside name deterministically; creating the outside name first let
    `F_GETPATH` return the OUTSIDE link under concurrent filesystem churn from
    neighbouring tests (~2% of the time), flipping the jail verdict and making
    this test order dependent (item 415). Everything stays under `tmp_path` so
    nothing leaks into the shared session basetemp."""
    moddir = tmp_path / "mod"
    moddir.mkdir()
    inside = _write(moddir / "body.py", _INNER_PY + "\n")   # created inside FIRST
    outside = tmp_path / "hl-outside.py"                    # a sibling of moddir
    try:
        os.link(inside, outside)   # second hardlink, OUTSIDE the module dir
    except OSError:
        pytest.skip("hardlink not permitted on this filesystem")
    m = _write(moddir / "m.rvl", _py_file("body.py"))
    ir = compile_files([str(m)])   # accepted, by design
    assert ir["externs"][0]["body_files"]["py"]["path"] == "body.py"


def test_missing_file_refused_naming_resolved_path(tmp_path):
    err = _refuse(tmp_path, '@py file "nope.py"')
    assert "not found" in str(err)
    assert "nope.py" in str(err)


def test_import_path_env_changes_nothing(tmp_path, monkeypatch):
    """A body file never consults the `use` search path — setting
    REVL_IMPORT_PATH does not let a missing file resolve from elsewhere."""
    other = tmp_path / "elsewhere"
    _write(other / "body.py", _INNER_PY + "\n")
    monkeypatch.setenv("REVL_IMPORT_PATH", str(other))
    err = _refuse(tmp_path, '@py file "body.py"')   # not next to m.rvl
    assert "not found" in str(err)


# -- in-memory / loaderless (re-review F5) ----------------------------------

def test_in_memory_resolves_through_sources_map_not_disk(tmp_path):
    """A virtual module's body file resolves through the `sources` map; a decoy
    file on disk at the equivalent path is never read."""
    # decoy on disk that would emit different bytes if it were read.
    _write(tmp_path / "body.py", "return 'DECOY'")
    src = _py_file("body.py")
    modules = {str(tmp_path / "body.py"): "return 'FROM_MEMORY'"}
    ir = compile_source(src, str(tmp_path / "m.rvl"), modules=modules)
    out = emit_py.emit(ir)
    assert "FROM_MEMORY" in out
    assert "DECOY" not in out


def test_in_memory_no_disk_fallback(tmp_path):
    """A body file present ONLY on disk (not in the sources map) does NOT
    resolve for a virtual module — nothing is read from disk."""
    _write(tmp_path / "body.py", "return '1'")
    src = _py_file("body.py")
    # supply the module in-memory but NOT the body file.
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, str(tmp_path / "m.rvl"),
                       modules={str(tmp_path / "other.rvl"): "// unused\n"})
    assert "in-memory sources map" in str(excinfo.value)


def test_loaderless_source_refuses_body_file():
    """A bare `compile_source` (no modules, no manifest) has no module directory
    and reads nothing from disk, so a body file is refused structurally."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_py_file("body.py"), "m.rvl")
    assert "modules=" in str(excinfo.value)


# -- reproducibility / content hash (re-review F10) -------------------------

def test_reproducible_hash_and_changes_on_edit(tmp_path):
    body = _write(tmp_path / "body.py", _INNER_PY + "\n")
    m = _write(tmp_path / "m.rvl", _py_file("body.py"))
    h1 = compile_files([str(m)])["externs"][0]["body_files"]["py"]["sha256"]
    h2 = compile_files([str(m)])["externs"][0]["body_files"]["py"]["sha256"]
    assert h1 == h2   # two compiles of the same tree agree
    body.write_text(_INNER_PY + "\nreturn '2'\n", encoding="utf-8")
    h3 = compile_files([str(m)])["externs"][0]["body_files"]["py"]["sha256"]
    assert h3 != h1   # touching the file changes the recorded hash


# -- no-extern ordering for an untrusted author (re-review F4) ---------------

def test_no_extern_refuses_before_body_file_resolution(tmp_path, monkeypatch):
    """Under the untrusted-author profile a module declaring `= @py file ...`
    gets the no-extern refusal, the refusal is byte-identical whether or not the
    named file exists, and NO body file is resolved, read, or stat'd (no
    existence oracle)."""
    profile = AdmissionProfile.untrusted_author([])

    # sentinel: the disk resolver must never be called under the profile.
    def _boom(*a, **k):
        raise AssertionError("body-file resolver ran under a no-extern profile")

    monkeypatch.setattr(hostfile, "read_body_file_disk", _boom)

    m_exists = _write(tmp_path / "a" / "m.rvl", _py_file("body.py"))
    _write(tmp_path / "a" / "body.py", _INNER_PY + "\n")   # file EXISTS
    m_absent = _write(tmp_path / "b" / "m.rvl", _py_file("body.py"))  # file ABSENT

    with pytest.raises(RevlError) as e_exists:
        compile_files([str(m_exists)], profile=profile)
    with pytest.raises(RevlError) as e_absent:
        compile_files([str(m_absent)], profile=profile)

    def _norm(err):
        return str(err).split(":", 2)[-1]   # drop the file:line prefix
    assert "forbids new" in str(e_exists.value)
    assert _norm(e_exists.value) == _norm(e_absent.value)   # no existence oracle


def test_imported_module_body_file_resolves_under_profile(tmp_path):
    """Root-scoped: a pre-granted `use`d module may declare externs and its body
    files resolve normally even under a no-extern profile (re-review F4)."""
    _write(tmp_path / "lib.rvl",
           'pub extern pure fn libadd(a: Int, b: Int) -> Str\n'
           '    = @py file "lib_body.py"\n')
    _write(tmp_path / "lib_body.py", _INNER_PY + "\n")
    root = _write(
        tmp_path / "root.rvl",
        'use "lib.rvl" { libadd }\n'
        "component C { }\n")
    # the root itself declares no extern; the imported lib does, with a file
    # body — it must resolve rather than being refused.
    profile = AdmissionProfile.untrusted_author([])
    ir = compile_files([str(root)], profile=profile)
    # lib's extern lowered with its file body resolved.
    names = {e["name"] for e in ir.get("externs") or []}
    assert "libadd" in names


# -- bundle interim refusal (stage 4 follow-up) -----------------------------

def test_bundle_refuses_file_form(tmp_path):
    from revl.bundle import build_bundle
    _write(tmp_path / "body.py", _INNER_PY + "\n")
    m = _write(tmp_path / "m.rvl",
               _py_file("body.py") + "component C { }\n")
    with pytest.raises(RevlError) as excinfo:
        build_bundle([str(m)], str(tmp_path / "out.revlbundle"))
    assert "host-body file" in str(excinfo.value)
