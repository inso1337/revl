"""Issue #2700: `revl bundle` carries a spliced body file and a user host
module, and `revl verify` re-hashes them against the pins in the IR.

A composition with `= @py file "helpers.py"` or `= @py ref sym from "path"`
used to be refused at bundle time. These tests pin the cases that replaced the
two refusals:

- the bundle verifies from a copy, with the original tree gone;
- one changed byte in a carried file fails verify and names the file;
- a path that leaves the module's directory is refused at bundle time, as
  written or through a link, and a refused bundle leaves an existing one alone;
- a ref whose pin resolves from a deeper bundled root disagrees with the
  module-relative path and is refused with the path it would have carried;
- a file shared by two externs, a file whose module is not bundled, a file
  changed since the compile and paths that cannot share the flat `source/`
  tree are refused or recorded once per pin;
- each file's digest is bound into the bundle record, and a record that was
  dropped or edited is not a pass;
- a hostile path in a bundle's own text is not read;
- a packed one-file bundle keeps the exact bytes it carries.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from revl import bundle as B
from revl.compiler import compile_files
from revl.errors import RevlError

HELPER = "x = a + b\nreturn str(x)\n"
ENGINE = "def do_engine(x):\n    return x\n"

FILE_RVL = ("extern pure fn addstr(a: Int, b: Int) -> Str\n"
            '    = @py file "helpers.py"\n')
REF_RVL = ("extern pure fn engine(x: Str) -> Str\n"
           '    = @py ref do_engine from "host/engine.py"\n')


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_app(root: Path) -> Path:
    _write(root / "helpers.py", HELPER)
    return _write(root / "app.rvl", FILE_RVL)


def _ref_app(root: Path) -> Path:
    _write(root / "host" / "engine.py", ENGINE)
    return _write(root / "app.rvl", REF_RVL)


def _build(app: Path, out: Path, **kw) -> str:
    return B.build_bundle([str(app)], str(out), env={}, **kw)


def _tier(report, name):
    return next(c for c in report.checks if c.tier == name)


def _manifest(bundle: str) -> dict:
    return json.loads((Path(bundle) / B.RUNTIME_MANIFEST).read_text("utf-8"))


# -- acceptance 1: bundles, and verifies with no access to the original tree --

@pytest.mark.parametrize("make,carried,body", [
    (_file_app, "helpers.py", HELPER),
    (_ref_app, "host/engine.py", ENGINE),
], ids=["file-body", "user-module-ref"])
def test_bundle_verifies_without_the_original_tree(tmp_path, make, carried, body):
    src = tmp_path / "tree"
    app = make(src)
    out = _build(app, tmp_path / "built.revlbundle")
    moved = tmp_path / "elsewhere" / "copy.revlbundle"
    shutil.copytree(out, moved)
    shutil.rmtree(src)
    shutil.rmtree(out)

    assert (moved / "source" / carried).read_text("utf-8") == body
    report = B.verify_bundle(str(moved))
    assert report.ok, B.render(report)
    assert _tier(report, f"host file {carried}").status == B.OK
    assert _tier(report, "reproducible").status == B.OK


def test_file_body_in_a_subdirectory_keeps_its_path(tmp_path):
    src = tmp_path / "tree"
    _write(src / "bodies" / "add.py", HELPER)
    app = _write(src / "app.rvl",
                 FILE_RVL.replace("helpers.py", "bodies/add.py"))
    out = _build(app, tmp_path / "b.revlbundle")
    assert (Path(out) / "source" / "bodies" / "add.py").is_file()
    shutil.rmtree(src)
    assert B.verify_bundle(out).ok


def test_one_file_bundle_keeps_carried_bytes(tmp_path):
    """A CRLF helper is pinned by its exact bytes; packing must not rewrite
    the newlines."""
    src = tmp_path / "tree"
    app = _file_app(src)
    data = HELPER.replace("\n", "\r\n").encode("utf-8")
    (src / "helpers.py").write_bytes(data)
    out = _build(app, tmp_path / "b.revlbundle")
    assert (Path(out) / "source" / "helpers.py").read_bytes() == data
    packed = B.pack_bundle(out, str(tmp_path / "b.revlbundle1"))
    shutil.rmtree(src)
    shutil.rmtree(out)
    report = B.verify_bundle(packed)
    assert report.ok, B.render(report)
    unpacked = B.unpack_bundle(packed, str(tmp_path / "again"))
    assert (Path(unpacked) / "source" / "helpers.py").read_bytes() == data


# -- acceptance 2: one changed byte fails verify and names the file ------------

@pytest.mark.parametrize("make,carried", [
    (_file_app, "helpers.py"),
    (_ref_app, "host/engine.py"),
], ids=["file-body", "user-module-ref"])
def test_changed_byte_fails_verify_naming_the_file(tmp_path, make, carried):
    out = _build(make(tmp_path / "tree"), tmp_path / "b.revlbundle")
    target = Path(out) / "source" / carried
    raw = bytearray(target.read_bytes())
    raw[-2] ^= 0x01
    target.write_bytes(bytes(raw))

    report = B.verify_bundle(out)
    assert not report.ok
    check = _tier(report, f"host file {carried}")
    assert check.status == B.MISMATCH
    assert carried in check.detail
    assert check.recorded != check.rebuilt


def test_missing_carried_file_fails_verify(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    (Path(out) / "source" / "helpers.py").unlink()
    report = B.verify_bundle(out)
    assert not report.ok
    check = _tier(report, "host file helpers.py")
    assert check.status == B.MISMATCH and "missing" in check.detail


# -- acceptance 3: a path that leaves the root is refused at bundle time -------
#
# The compiler refuses `../outside.py` and a link out of the tree before the
# bundle step runs, so the end-to-end tests below only show the whole command
# refuses. The bundle step's own jail is exercised directly, with the link made
# after the compile and before the read.

def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def test_path_escaping_the_root_is_refused_at_bundle_time(tmp_path):
    src = tmp_path / "tree"
    _write(tmp_path / "outside.py", HELPER)
    app = _write(src / "app.rvl", FILE_RVL.replace("helpers.py", "../outside.py"))
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError):
        _build(app, out)
    assert not out.exists()


@pytest.mark.parametrize("written", ["../outside.py", "/etc/hosts", "a/../../x.py",
                                     "", "bad\x00name.py"])
def test_read_host_file_refuses_a_path_that_leaves_the_directory(tmp_path, written):
    src = tmp_path / "tree"
    src.mkdir()
    _write(tmp_path / "outside.py", HELPER)
    with pytest.raises(RevlError, match="empty, absolute, or leaves its module"):
        B._read_host_file(str(src), written, "e")


def test_read_host_file_refuses_a_link_out_of_the_directory(tmp_path):
    src = tmp_path / "tree"
    _write(tmp_path / "outside.py", HELPER)
    src.mkdir()
    _symlink(tmp_path / "outside.py", src / "helpers.py")
    with pytest.raises(RevlError, match="resolves outside its module's directory"):
        B._read_host_file(str(src), "helpers.py", "e")


def test_link_made_after_the_compile_is_refused_by_the_bundle_step(tmp_path):
    src = tmp_path / "tree"
    app = _file_app(src)
    ir = compile_files([str(app)])
    _write(tmp_path / "outside.py", HELPER)          # same bytes as the pin
    (src / "helpers.py").unlink()
    _symlink(tmp_path / "outside.py", src / "helpers.py")
    with pytest.raises(RevlError, match="resolves outside its module's directory"):
        B._collect_host_files(ir, [str(app)])


def test_named_pipe_is_refused_without_blocking(tmp_path):
    """A blocking open of a pipe with no writer never returns. The open runs in
    a thread with a bound, so a regression fails the test instead of hanging
    the run (CI has no pytest-timeout)."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes unavailable")
    src = tmp_path / "tree"
    src.mkdir()
    fifo = src / "helpers.py"
    os.mkfifo(fifo)
    outcome: list = []

    def _open() -> None:
        try:
            B._read_host_file(str(src), "helpers.py", "e")
            outcome.append("read")
        except RevlError as error:
            outcome.append(error)

    worker = threading.Thread(target=_open, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        # release the stuck open so the thread ends, then fail the test
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(5)
        pytest.fail("opening a named pipe blocked")
    assert len(outcome) == 1 and isinstance(outcome[0], RevlError)
    assert "not a regular file" in str(outcome[0])


def _ref_above_inputs(src: Path) -> list[str]:
    """Inside the compile root, but not under the directory of the module that
    names it: the compiler allows it, the bundle step does not."""
    _write(src / "host" / "engine.py", ENGINE)
    main = _write(src / "main.rvl", "extern pure fn other(x: Str) -> Str\n"
                                    '    = @py { return x }\n')
    nested = _write(src / "plug" / "m.rvl",
                    REF_RVL.replace("host/engine.py", "../host/engine.py"))
    return [str(main), str(nested)]


def test_ref_above_its_module_directory_is_refused(tmp_path):
    inputs = _ref_above_inputs(tmp_path / "tree")
    compile_files(inputs)                      # the compiler accepts it
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="empty, absolute, or leaves its module"):
        B.build_bundle(inputs, str(out), env={})
    assert not out.exists()


def test_refused_bundle_leaves_an_existing_bundle_alone(tmp_path):
    out = tmp_path / "b.revlbundle"
    good = _write(tmp_path / "ok" / "app.rvl", "component C { }\n")
    B.build_bundle([str(good)], str(out), env={})
    before = {p.relative_to(out).as_posix(): p.read_bytes()
              for p in sorted(out.rglob("*")) if p.is_file()}
    inputs = _ref_above_inputs(tmp_path / "tree")
    with pytest.raises(RevlError, match="empty, absolute, or leaves its module"):
        B.build_bundle(inputs, str(out), env={}, force=True)
    after = {p.relative_to(out).as_posix(): p.read_bytes()
             for p in sorted(out.rglob("*")) if p.is_file()}
    assert after == before


# -- what the bundle step refuses before it writes anything --------------------

def test_file_of_an_unbundled_module_is_refused(tmp_path):
    src = tmp_path / "tree"
    _write(src / "lib_body.py", HELPER)
    _write(src / "lib.rvl", 'pub extern pure fn libadd(a: Int, b: Int) -> Str\n'
                            '    = @py file "lib_body.py"\n')
    root = _write(src / "root.rvl", 'use "lib.rvl" { libadd }\ncomponent C { }\n')
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="not among the bundled sources"):
        _build(root, out)
    assert not out.exists()


def test_ref_pinned_from_a_deeper_bundled_root_is_refused(tmp_path):
    """A ref pin records the path relative to the deepest root that contains the
    target. When a second bundled source sits under `host/`, that root is
    `host/`, so the pin says `engine.py` while the module-relative path the
    bundle would carry is `host/engine.py`. The two disagree and the bundle is
    refused instead of writing the file at a path its own import cannot use."""
    src = tmp_path / "tree"
    app = _write(src / "app.rvl", REF_RVL)
    _write(src / "host" / "engine.py", ENGINE)
    # a bundled source under `host/` makes that directory a compile root too
    util = _write(src / "host" / "util.rvl",
                  "extern pure fn util(x: Str) -> Str\n"
                  '    = @py { return x }\n')
    inputs = [str(app), str(util)]
    ir = compile_files(inputs)                 # the compiler accepts it
    assert ir["externs"][0]["refs"]["py"]["path"] == "engine.py"
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="would resolve as"):
        B.build_bundle(inputs, str(out), env={})
    assert not out.exists()


def test_file_changed_since_the_compile_is_refused(tmp_path):
    app = _file_app(tmp_path / "tree")
    ir = compile_files([str(app)])
    (tmp_path / "tree" / "helpers.py").write_text(HELPER + "# edited\n", "utf-8")
    with pytest.raises(RevlError, match="changed since it was compiled"):
        B._collect_host_files(ir, [str(app)])


def test_file_named_like_a_bundled_source_is_refused(tmp_path):
    _write(tmp_path / "tree" / "helpers.py", HELPER)
    app = _write(tmp_path / "tree" / "app.rvl", FILE_RVL)
    ir = compile_files([str(app)])
    # a second bundled source that already owns the file's name in `source/`
    clash = _write(tmp_path / "other" / "helpers.py", "component C { }\n")
    with pytest.raises(RevlError, match="name of a bundled source"):
        B._collect_host_files(ir, [str(app), str(clash)])


def _two_modules(root: Path, first: str, second: str) -> list[str]:
    """Two modules in different directories, each splicing a body file."""
    out = []
    for sub, name, written in (("a", "one", first), ("b", "two", second)):
        _write(root / sub / (name + ".py"), f"# {name}\nreturn str(a)\n")
        out.append(str(_write(
            root / sub / f"{name}.rvl",
            f"extern pure fn {name}(a: Int) -> Str\n"
            f'    = @py file "{name}.py"\n'.replace(f"{name}.py\"", written + '"'))))
    return out


def test_same_path_with_different_bytes_is_refused(tmp_path):
    root = tmp_path / "tree"
    inputs = _two_modules(root, "shared.py", "shared.py")
    _write(root / "a" / "shared.py", "return 'a'\n")
    _write(root / "b" / "shared.py", "return 'b'\n")
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="different contents"):
        B.build_bundle(inputs, str(out), env={})
    assert not out.exists()


def test_two_spellings_of_one_path_are_refused(tmp_path):
    root = tmp_path / "tree"
    inputs = _two_modules(root, "Shared.py", "shared.py")
    _write(root / "a" / "Shared.py", "return 'a'\n")
    _write(root / "b" / "shared.py", "return 'a'\n")
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="case-folding or normalizing"):
        B.build_bundle(inputs, str(out), env={})
    assert not out.exists()


def test_a_file_that_is_also_a_directory_is_refused(tmp_path):
    root = tmp_path / "tree"
    inputs = _two_modules(root, "shared", "shared/inner.py")
    _write(root / "a" / "shared", "return 'a'\n")
    _write(root / "b" / "shared" / "inner.py", "return 'b'\n")
    out = tmp_path / "b.revlbundle"
    with pytest.raises(RevlError, match="also the directory"):
        B.build_bundle(inputs, str(out), env={})
    assert not out.exists()


def test_same_path_with_same_bytes_from_two_modules_bundles(tmp_path):
    root = tmp_path / "tree"
    inputs = _two_modules(root, "shared.py", "shared.py")
    for sub in ("a", "b"):
        _write(root / sub / "shared.py", "return 'same'\n")
    out = B.build_bundle(inputs, str(tmp_path / "b.revlbundle"), env={})
    assert len(_manifest(out)[B.HOST_FILES_KEY]) == 2
    shutil.rmtree(root)
    assert B.verify_bundle(out).ok


# -- one file shared by several externs ----------------------------------------

def test_module_shared_by_two_externs_verifies(tmp_path):
    """The common case: one host module, two externs each ref'ing a symbol."""
    src = tmp_path / "tree"
    _write(src / "host" / "engine.py", ENGINE + "\ndef other(x):\n    return x\n")
    app = _write(src / "app.rvl", REF_RVL + "extern pure fn second(x: Str) -> Str\n"
                                 '    = @py ref other from "host/engine.py"\n')
    out = _build(app, tmp_path / "b.revlbundle")
    records = _manifest(out)[B.HOST_FILES_KEY]
    assert len(records) == 2
    assert {r["path"] for r in records} == {"source/host/engine.py"}
    shutil.rmtree(src)
    report = B.verify_bundle(out)
    assert report.ok, B.render(report)
    assert _tier(report, "host file host/engine.py").status == B.OK


# -- the digest is bound into the bundle record --------------------------------

@pytest.mark.parametrize("make,carried,body,kind", [
    (_file_app, "helpers.py", HELPER, "body"),
    (_ref_app, "host/engine.py", ENGINE, "ref"),
], ids=["file-body", "user-module-ref"])
def test_digest_is_recorded_in_the_manifest_and_the_ir(tmp_path, make, carried,
                                                        body, kind):
    out = _build(make(tmp_path / "tree"), tmp_path / "b.revlbundle")
    digest = _sha(body.encode("utf-8"))
    records = _manifest(out)[B.HOST_FILES_KEY]
    assert [(r["path"], r["sha256"], r["kind"]) for r in records] == [
        (f"source/{carried}", digest, kind)]
    ir_text = (Path(out) / "ir" / "ir.json").read_text("utf-8")
    assert digest in ir_text


def test_dropped_manifest_record_is_not_a_pass(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    path = Path(out) / B.RUNTIME_MANIFEST
    doc = json.loads(path.read_text("utf-8"))
    del doc[B.HOST_FILES_KEY]
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", "utf-8")
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


def test_edited_manifest_digest_is_not_a_pass(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    path = Path(out) / B.RUNTIME_MANIFEST
    doc = json.loads(path.read_text("utf-8"))
    doc[B.HOST_FILES_KEY][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", "utf-8")
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


def test_bundle_without_host_files_is_unchanged(tmp_path):
    app = _write(tmp_path / "plain" / "app.rvl", "component C { }\n")
    out = _build(app, tmp_path / "b.revlbundle")
    assert B.HOST_FILES_KEY not in _manifest(out)
    report = B.verify_bundle(out)
    assert report.ok
    assert not [c for c in report.checks if c.tier.startswith("host file")]


# -- a hostile path in the bundle's own text is never read ----------------------

def test_pin_path_that_escapes_the_bundle_is_not_read(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    secret = _write(tmp_path / "secret.py", "TOP = 1\n")
    ir_path = Path(out) / "ir" / "ir.json"
    doc = json.loads(ir_path.read_text("utf-8"))
    pin = doc["externs"][0]["body_files"]["py"]
    pin["path"] = "../../secret.py"
    pin["sha256"] = _sha(secret.read_bytes())
    ir_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", "utf-8")

    report = B.verify_bundle(out)
    assert not report.ok
    escaped = [c for c in report.checks
               if c.tier.startswith("host file") and c.status == B.MISMATCH]
    assert any("escapes" in c.detail for c in escaped)
    assert not any(c.status == B.OK and c.tier.startswith("host file ")
                   for c in report.checks)


def test_link_inside_the_bundle_source_tree_is_not_followed_out(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    carried = Path(out) / "source" / "helpers.py"
    outside = _write(tmp_path / "outside.py", HELPER)    # same bytes as the pin
    carried.unlink()
    _symlink(outside, carried)
    report = B.verify_bundle(out)
    assert not report.ok
    check = _tier(report, "host file helpers.py")
    assert check.status == B.MISMATCH and "escapes" in check.detail


# -- a manifest record that does not match the pins ----------------------------

def _edit_manifest(out: str, edit) -> None:
    path = Path(out) / B.RUNTIME_MANIFEST
    doc = json.loads(path.read_text("utf-8"))
    edit(doc)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", "utf-8")


def test_duplicated_manifest_record_is_not_a_pass(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    _edit_manifest(out, lambda d: d[B.HOST_FILES_KEY].append(
        dict(d[B.HOST_FILES_KEY][0])))
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


def test_extra_manifest_record_for_an_unpinned_path_is_not_a_pass(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    extra = _write(Path(out) / "source" / "extra.py", "x = 1\n")
    _edit_manifest(out, lambda d: d[B.HOST_FILES_KEY].append(
        {"path": "source/extra.py", "sha256": _sha(extra.read_bytes()),
         "extern": "addstr", "tier": "py", "kind": "body"}))
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


# -- a hostile IR shape is a MISMATCH, not an exception -------------------------

def _shape_externs(doc):
    doc["externs"] = [1]


def _shape_externs_not_list(doc):
    doc["externs"] = {"a": 1}


def _shape_body_files_not_object(doc):
    doc["externs"][0]["body_files"] = ["py"]


def _shape_body_files_entry(doc):
    doc["externs"][0]["body_files"] = {"py": "helpers.py"}


def _shape_refs_entry(doc):
    doc["externs"][0]["refs"] = {"py": 5}


def _shape_refs_not_object(doc):
    doc["externs"][0]["refs"] = "py"


@pytest.mark.parametrize("edit", [
    _shape_externs, _shape_externs_not_list, _shape_body_files_not_object,
    _shape_body_files_entry, _shape_refs_entry, _shape_refs_not_object,
], ids=lambda f: f.__name__.removeprefix("_shape_"))
def test_malformed_ir_shape_is_a_mismatch_not_an_exception(tmp_path, edit):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    ir_path = Path(out) / "ir" / "ir.json"
    doc = json.loads(ir_path.read_text("utf-8"))
    edit(doc)
    ir_path.write_text(json.dumps(doc), "utf-8")
    with pytest.raises(RevlError, match="malformed host-file pins"):
        B._host_pins_of(doc)
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


def test_ir_that_is_not_an_object_is_a_mismatch(tmp_path):
    out = _build(_file_app(tmp_path / "tree"), tmp_path / "b.revlbundle")
    (Path(out) / "ir" / "ir.json").write_text("[1, 2]", "utf-8")
    report = B.verify_bundle(out)
    assert not report.ok
    assert _tier(report, "host files").status == B.MISMATCH


# -- a pub or imported extern keeps its declaring module -----------------------

def test_pub_extern_of_an_imported_module_bundles(tmp_path):
    src = tmp_path / "tree"
    _write(src / "lib_body.py", HELPER)
    lib = _write(src / "lib.rvl", "pub extern pure fn libadd(a: Int, b: Int) -> Str\n"
                                  '    = @py file "lib_body.py"\n')
    root = _write(src / "root.rvl", 'use "lib.rvl" { libadd }\ncomponent C { }\n')
    out = B.build_bundle([str(root), str(lib)], str(tmp_path / "b.revlbundle"),
                         env={})
    assert [(r["extern"], r["path"]) for r in _manifest(out)[B.HOST_FILES_KEY]] == [
        ("libadd", "source/lib_body.py")]
    shutil.rmtree(src)
    report = B.verify_bundle(out)
    assert report.ok, B.render(report)
    assert _tier(report, "host file lib_body.py").status == B.OK
