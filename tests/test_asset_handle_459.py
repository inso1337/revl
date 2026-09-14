"""Roadmap item 459 F1 (issue #722): typed, jailed, content-pinned asset handles.

Before this, a revl source named a frontend asset with a bare `Str`:
`examples/webui-entry/console.rvl` handed `"./frontend/entry.client.ts"` to the
WebUI coeffect and the toolchain never opened it. Three things were therefore
unstated — that the file exists, that it lives inside the composition, and that
it is still the bytes that were reviewed — and a path that was resolved looked
exactly like one that was not.

`asset "<path>"` states all three at compile time. It is not sugar for a string:

* the path resolves relative to the DECLARING `.rvl` file, exactly like a
  host-module `ref` (item 396 option B);
* the resolved REALPATH is jailed to the root compile tree — the same
  `_pick_root`/`_contained` rule option B uses, deliberately reused rather than
  re-implemented, because a second jail is a second thing to get wrong;
* the file's bytes are hashed and the sha256 is pinned into the handle.

The value is the record `{ path: Str, sha256: Str }` — `path` ROOT-RELATIVE, so
a host joins it to the app root rather than to the declaring module. Because the
handle is an ordinary record, it lowers, types and emits on every tier with no
new backend case, and a bare `Str` no longer type-checks where an asset is
expected.

CONFINEMENT. Every arm below can only REFUSE; none repairs, defaults or falls
back. The failure direction of each is stated in its own test: an absolute path,
a missing file, a non-regular file, a path whose realpath leaves the jail
(`..` or a symlink), a `${...}` path the compiler cannot resolve, an in-memory
compile asked to hash a file it was not given, and an untrusted author naming a
host file at all. A path-confinement hole here would be a file-read primitive,
so the tests below drive the escapes rather than describe them.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

ASSET_TYPE = "type Asset = { path: Str, sha256: Str }\n"


def _app(tmp_path: Path, written: str, *, body: str | None = None) -> Path:
    """A one-file composition whose single fn returns an `asset` handle."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    (app / "a.rvl").write_text(
        body if body is not None else
        ASSET_TYPE + f'pub fn entry() -> Asset {{ return asset "{written}" }}\n',
        encoding="utf-8")
    return app / "a.rvl"


def _entry(tmp_path: Path, text: str = "export default 1\n") -> Path:
    front = tmp_path / "app" / "frontend"
    front.mkdir(parents=True, exist_ok=True)
    target = front / "entry.client.ts"
    target.write_text(text, encoding="utf-8")
    return target


def _handle_of(ir: dict, fn: str = "entry") -> dict:
    """The `{path, sha256}` record literal the fn returns, as a plain dict."""
    decl = next(f for f in ir["functions"] if f["name"] == fn)
    record = decl["body"][0]["expr"]
    assert record["kind"] == "record"
    return {name: node["value"] for name, node in record["fields"]}


# ---------------------------------------------------------------------------
# what the handle IS
# ---------------------------------------------------------------------------

def test_an_asset_resolves_to_the_root_relative_path_and_the_real_digest(tmp_path):
    """The handle carries the RESOLVED path (relative to the root compile tree,
    not the path as written) and the sha256 of the file's actual bytes,
    recomputed here from disk so the pin is checked against the truth."""
    target = _entry(tmp_path, "export default 'notes'\n")
    ir = compile_files([str(_app(tmp_path, "./frontend/entry.client.ts"))])

    handle = _handle_of(ir)
    assert handle["path"] == "frontend/entry.client.ts"
    assert handle["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_the_digest_follows_the_bytes(tmp_path):
    """Editing the asset changes the pin. Without this the digest could be any
    constant and every other assertion here would still pass."""
    _entry(tmp_path, "export default 1\n")
    first = _handle_of(compile_files([str(_app(tmp_path, "./frontend/entry.client.ts"))]))
    _entry(tmp_path, "export default 2\n")
    second = _handle_of(compile_files([str(_app(tmp_path, "./frontend/entry.client.ts"))]))
    assert first["sha256"] != second["sha256"]
    assert first["path"] == second["path"]


def test_a_traversing_but_contained_path_normalises_to_one_handle(tmp_path):
    """`..` is allowed while the RESOLVED realpath stays inside the jail — the
    same rule option B's `ref` uses — and two spellings of one file give one
    handle, because the value is the resolved path and not the written one."""
    _entry(tmp_path)
    plain = _handle_of(compile_files([str(_app(tmp_path, "./frontend/entry.client.ts"))]))
    noisy = _handle_of(compile_files(
        [str(_app(tmp_path, "./frontend/../frontend/entry.client.ts"))]))
    assert plain == noisy


# ---------------------------------------------------------------------------
# the refusals — each names its failure direction
# ---------------------------------------------------------------------------

def test_an_absolute_path_is_refused_before_any_filesystem_access(tmp_path):
    """Direction: REFUSE an absolute path. It is rejected textually, before any
    stat, so the refusal is identical whether or not the file exists and is
    therefore not an existence oracle for absolute paths."""
    _entry(tmp_path)
    for written in ("/etc/hosts", "/definitely/not/here/at/all.ts"):
        with pytest.raises(RevlError) as excinfo:
            compile_files([str(_app(tmp_path, written))])
        assert "is absolute" in str(excinfo.value)


def test_a_missing_file_is_refused(tmp_path):
    """Direction: REFUSE a path that names nothing. The old `Str` accepted it
    silently and the failure surfaced, if at all, in a browser."""
    _entry(tmp_path)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "./frontend/gone.ts"))])
    assert "not found" in str(excinfo.value)


def test_a_directory_is_refused(tmp_path):
    """Direction: REFUSE a non-regular file. An asset is one file whose bytes
    are hashed; a directory has no content to pin."""
    _entry(tmp_path)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "./frontend"))])
    assert "not a regular file" in str(excinfo.value)


def test_a_path_escaping_the_root_tree_is_refused(tmp_path):
    """Direction: REFUSE a resolved path outside the root compile tree. This is
    the confinement arm: `..` is not rejected textually, so containment of the
    resolved realpath is the whole jail."""
    _entry(tmp_path)
    (tmp_path / "secret.txt").write_text("TOPSECRET\n", encoding="utf-8")
    for written in ("../secret.txt", "./frontend/../../secret.txt"):
        with pytest.raises(RevlError) as excinfo:
            compile_files([str(_app(tmp_path, written))])
        assert "resolves OUTSIDE the root compile tree" in str(excinfo.value)


def test_a_symlink_out_of_the_root_tree_is_refused(tmp_path):
    """Direction: REFUSE a link whose TARGET leaves the jail. The written path
    is contained and innocent; only the realpath is not, which is exactly why
    containment is checked on the realpath and not on the text."""
    _entry(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n", encoding="utf-8")
    link = tmp_path / "app" / "frontend" / "innocent.ts"
    link.symlink_to(secret)

    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "./frontend/innocent.ts"))])
    message = str(excinfo.value)
    assert "resolves OUTSIDE the root compile tree" in message
    # and the refusal names the file it actually reached, not the alias.
    assert "secret.txt" in message


def test_an_empty_path_is_refused(tmp_path):
    """Direction: REFUSE the empty path rather than resolve it to the module's
    own directory."""
    _entry(tmp_path)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, ""))])
    assert "empty `asset` path" in str(excinfo.value)


def test_a_computed_path_is_refused_at_parse(tmp_path):
    """Direction: REFUSE a `${...}` path. Resolution, jailing and hashing all
    happen at COMPILE time, so a path that depends on a runtime value is not an
    asset — and admitting one would make the handle unpinnable by construction."""
    _entry(tmp_path)
    body = 'pub fn entry(x: Str) -> Str { return asset `${x}` }\n'
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "", body=body))])
    assert "must be a plain string literal" in str(excinfo.value)


def test_an_in_memory_compile_will_not_read_the_asset_from_disk(tmp_path):
    """Direction: REFUSE an asset an in-memory compile was not given.

    `compile_source(..., modules=...)` reads nothing from disk, which is what
    keeps a compile of foreign source from becoming a file-existence and
    file-digest oracle over the host. The asset arm keeps that promise: the file
    is really there, and the compile still refuses because it was not supplied.
    """
    target = _entry(tmp_path)
    assert target.is_file()
    app = _app(tmp_path, "./frontend/entry.client.ts")
    source = app.read_text(encoding="utf-8")

    with pytest.raises(RevlError) as excinfo:
        compile_source(source, str(app), modules={str(app): source})
    assert "not in the in-memory sources map" in str(excinfo.value)

    # supplied through the same map, it resolves — the refusal is about where
    # the bytes come from, not about in-memory compiles as such.
    ir = compile_source(source, str(app),
                        modules={str(app): source,
                                 str(target): "export default 1\n"})
    assert _handle_of(ir)["sha256"] == hashlib.sha256(
        b"export default 1\n").hexdigest()


def test_a_bare_source_string_refuses_an_asset(tmp_path):
    """Direction: REFUSE. A bare `compile_source` has no module directory to
    resolve against and no root tree to jail against, so there is no safe
    reading of the path at all."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(ASSET_TYPE +
                       'pub fn entry() -> Asset { return asset "./x.ts" }\n')
    assert "needs `modules=`" in str(excinfo.value)


def test_an_untrusted_author_may_not_name_a_host_file(tmp_path):
    """Direction: REFUSE under the untrusted-author profile.

    An `asset` is a compile-time file read whose path the admitted source
    chooses. The jail bounds the reach to the compile tree, but "which files
    exist in the tree I am compiled in, and what are their digests" is still not
    an answer the gate should hand an untrusted author, and the same profile
    already forbids `extern` for the stronger version of that reason. The
    refusal is structural — it fires before the path is resolved or stat'd, so
    it is not itself an oracle."""
    _entry(tmp_path)
    app = _app(tmp_path, "./frontend/entry.client.ts")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(app)],
                      profile=AdmissionProfile.untrusted_author(()))
    message = str(excinfo.value)
    assert "forbids reading host files" in message
    assert "./frontend/entry.client.ts" in message


def test_the_untrusted_refusal_does_not_depend_on_the_file_existing(tmp_path):
    """The untrusted refusal is byte-identical for a path that exists and one
    that does not, so it cannot be used to probe the tree."""
    _entry(tmp_path)
    profile = AdmissionProfile.untrusted_author(())

    def refusal(written: str) -> str:
        with pytest.raises(RevlError) as excinfo:
            compile_files([str(_app(tmp_path, written))], profile=profile)
        return str(excinfo.value).replace(written, "<path>")

    assert refusal("./frontend/entry.client.ts") == refusal("./frontend/nope.ts")


# ---------------------------------------------------------------------------
# the handle is TYPED
# ---------------------------------------------------------------------------

def test_a_bare_string_no_longer_type_checks_where_an_asset_is_expected(tmp_path):
    """The point of the handle: `Str` and `Asset` are different types, so a path
    that was never resolved cannot stand in for one that was."""
    _entry(tmp_path)
    body = ASSET_TYPE + \
        'pub fn entry() -> Asset { return "./frontend/entry.client.ts" }\n'
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "", body=body))])
    assert "expects `Asset`" in str(excinfo.value)


def test_an_asset_is_not_a_string(tmp_path):
    """The converse: the handle does not silently decay to its path."""
    _entry(tmp_path)
    body = 'pub fn entry() -> Str { return asset "./frontend/entry.client.ts" }\n'
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_app(tmp_path, "", body=body))])
    assert "expects `Str`" in str(excinfo.value)


def test_asset_is_not_a_reserved_word(tmp_path):
    """`asset` is intercepted only when a string literal is juxtaposed after it,
    so a parameter, field or binding named `asset` still reads as a variable.
    The lexer is untouched."""
    _entry(tmp_path)
    body = 'pub fn pick(asset: Str) -> Str { return asset }\n'
    ir = compile_files([str(_app(tmp_path, "", body=body))])
    assert any(f["name"] == "pick" for f in ir["functions"])


# ---------------------------------------------------------------------------
# the shipped references use it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source,entry",
    [("examples/webui-entry/console.rvl",
      "examples/webui-entry/frontend/entry.client.ts"),
     ("examples/app/notes.rvl",
      "examples/app/frontend/entry.client.ts")],
)
def test_the_shipped_webui_entries_are_typed_handles(source, entry):
    """Both references hand the WebUI coeffect a handle, not a path string, and
    the digest in the artifact is the digest of the file on disk."""
    ir = compile_files([str(ROOT / source)])
    component = next(c for c in ir["components"]
                     if any(step.get("step") == "emit" for step in c["body"]))
    emit = next(step for step in component["body"] if step["step"] == "emit")
    handle = emit["expr"]["args"][0]
    assert handle["kind"] == "record"
    fields = {name: node["value"] for name, node in handle["fields"]}
    assert fields["path"] == "frontend/entry.client.ts"
    assert fields["sha256"] == hashlib.sha256(
        (ROOT / entry).read_bytes()).hexdigest()

    # `dev_source` is declared as the asset record; `prod_manifest` is still a
    # `Str`, and deliberately so — a Vite manifest is a build output that does
    # not exist when the composition is compiled.
    add_entry = ir["services"]["WebUI"]["methods"]["add_entry"]
    params = {p["name"]: p["type"] for p in add_entry["params"]}
    assert params["dev_source"] == "Asset"
    assert params["prod_manifest"] == "Str"


# ---------------------------------------------------------------------------
# non-vacuity control: these hold on main too
# ---------------------------------------------------------------------------

def test_control_an_ordinary_record_literal_is_untouched(tmp_path):
    """A hand-written `{ path: ..., sha256: ... }` still compiles and is still an
    ordinary record. The asset work adds a refusal path through every record
    literal's lowering, and this is the control that says that path did not
    start refusing records generally.

    It passes before this change and after it, so a green run of the suite above
    is not explained by the harness compiling nothing.
    """
    (tmp_path / "app").mkdir(exist_ok=True)
    app = tmp_path / "app" / "plain.rvl"
    app.write_text(
        ASSET_TYPE +
        'pub fn entry() -> Asset { return { path: "p", sha256: "d" } }\n',
        encoding="utf-8")
    ir = compile_files([str(app)])
    assert _handle_of(ir) == {"path": "p", "sha256": "d"}


def test_control_a_plain_string_path_still_compiles(tmp_path):
    """Naming a file with a bare `Str` is still legal revl — `asset` is an
    additional, stronger way to name one, not a ban on strings. `prod_manifest`
    depends on this staying true."""
    (tmp_path / "app").mkdir(exist_ok=True)
    app = tmp_path / "app" / "plain.rvl"
    app.write_text(
        'pub fn manifest() -> Str { return "./dist/.vite/manifest.json" }\n',
        encoding="utf-8")
    ir = compile_files([str(app)])
    decl = next(f for f in ir["functions"] if f["name"] == "manifest")
    assert decl["body"][0]["expr"]["value"] == "./dist/.vite/manifest.json"


# ---------------------------------------------------------------------------
# `revl fmt` keeps its headline proof on asset-bearing sources
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source", ["examples/webui-entry/console.rvl", "examples/app/notes.rvl"])
def test_the_format_gate_still_proves_ir_identity_on_an_asset_source(source):
    """`revl fmt` compiles the original and the candidate in memory and admits
    the rewrite only when their IR is byte-identical. An in-memory compile
    resolves an asset through the sources map only (it reads nothing from disk,
    which is what keeps it from being an oracle), so the formatter supplies the
    asset bytes itself. Without that the gate would silently drop to the weaker
    token-identity proof on exactly the files this item added assets to.
    """
    from revl.formatter import format_source, ir_equivalent

    path = str(ROOT / source)
    original = Path(path).read_text(encoding="utf-8")
    candidate = format_source(original, path)
    result = ir_equivalent(original, candidate, path)
    assert result.admitted
    assert result.proof in ("unchanged", "IR byte-identical")
    assert result.warning is None
