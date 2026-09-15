"""The PRODUCTION half of the webui asset pair (roadmap item 459, issue #722).

`webui.add_entry` names two external files: the dev-mode source, which item 459
F1 made a resolved, jailed, sha256-pinned asset handle, and the built Vite
manifest a production Cordis WebUI host reads to find the chunk it should serve
(design note `docs/design/526-webui-asset-alignment.md`, `base/entry.ts:37`).

Only the first of those was ever opened. `DevWebUI.add_entry` confined the
handle, checked the file existed and re-hashed its bytes, then stored
`prod_manifest` verbatim and never looked at it -- not jailed, not parsed, not
checked to describe the same frontend. The asymmetry was invisible because both
arguments arrive in one call, and this suite's sibling
`tests/test_app_notes_725.py` passed the literal string `"m"` for it in six
places without anything noticing.

It is not a cosmetic gap, because the two halves are keyed DIFFERENTLY and
nothing said so. The handle carries a path relative to the compile-tree root
(`frontend/entry.client.ts`); a Vite manifest is keyed relative to the VITE root
(`entry.client.ts`). A production host that joined them by string looks up a key
the manifest does not have, and 459's exit clause "462's UI is built from
external assets" fails in the one mode that ships.

So the join is made by FILE IDENTITY: the Vite root is declared nowhere, so each
directory between the manifest and the app root is tried as one, and an entry
matches when its `src` names the same file on disk as the pinned handle. That
assumes nothing about `outDir` or `.vite/`, which are Vite's to change.

Every check below drives the refusal rather than describing it. The two controls
at the end exercise only pre-existing surface and hold on both sides of the
change, so a green run is not explained by the harness refusing everything.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.dev import DevWebUI  # noqa: E402

ENTRY_REL = "frontend/entry.client.ts"
MANIFEST_REL = "frontend/dist/.vite/manifest.json"
ENTRY_SOURCE = "export default {}\n"


def _app(tmp_path: Path, manifest: object | str | None = ...,
         chunk: str | None = "bundle.js") -> Path:
    """A miniature app tree with the real shape: an entry asset, a built
    `dist/` and the Vite manifest at the path `examples/app/notes.rvl` names.

    `manifest` defaults to the manifest a real `vite build` of that entry
    writes; pass `None` for an unbuilt frontend, a `str` for a file that is not
    JSON, or any object to plant a different one.
    """
    app = tmp_path / "app"
    (app / "frontend" / "dist" / ".vite").mkdir(parents=True)
    (app / "frontend" / "entry.client.ts").write_text(ENTRY_SOURCE, "utf-8")
    if chunk is not None:
        (app / "frontend" / "dist" / chunk).write_text("// built\n", "utf-8")
    if manifest is ...:
        manifest = {"entry.client.ts": {"file": "bundle.js",
                                        "src": "entry.client.ts",
                                        "isEntry": True}}
    target = app / Path(MANIFEST_REL)
    if manifest is None:
        pass
    elif isinstance(manifest, str):
        target.write_text(manifest, "utf-8")
    else:
        target.write_text(json.dumps(manifest), "utf-8")
    return app


def _handle(app: Path) -> dict:
    """The asset handle `asset "./frontend/entry.client.ts"` lowers to, computed
    from the real bytes so the dev host's own pin passes and the production half
    is what is under test."""
    source = app / Path(ENTRY_REL)
    return {"path": ENTRY_REL,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def _add(app: Path, manifest: str = MANIFEST_REL) -> DevWebUI:
    host = DevWebUI(app)
    host.add_entry(_handle(app), manifest, ["/notes"])
    return host


# -- the resolution ----------------------------------------------------------

def test_the_built_manifest_resolves_the_pinned_asset_to_its_chunk(tmp_path):
    """The whole point: the dev source and the built manifest are two names for
    one frontend, and the host now says which chunk the pinned asset becomes."""
    host = _add(_app(tmp_path))
    assert host.prod_entries == ["frontend/dist/bundle.js"]


def test_the_key_and_the_handle_path_do_not_have_to_be_spelled_the_same(tmp_path):
    """The defect this closes, stated as a check. The manifest key is
    `entry.client.ts` (Vite-root relative) and the handle is
    `frontend/entry.client.ts` (compile-root relative). A string join finds
    nothing; matching the file they both name finds the chunk."""
    app = _app(tmp_path)
    keys = list(json.loads((app / Path(MANIFEST_REL)).read_text("utf-8")))
    assert keys == ["entry.client.ts"] != [ENTRY_REL]
    assert _add(app).prod_entries == ["frontend/dist/bundle.js"]


def test_an_unbuilt_frontend_is_recorded_not_refused(tmp_path):
    """`revl dev` runs Vite over the SOURCE, so "no manifest yet" is the
    command's ordinary state and must not refuse. It is recorded as unbuilt,
    which is a different answer from "resolved"."""
    host = _add(_app(tmp_path, manifest=None, chunk=None))
    assert host.prod_entries == [None]
    assert len(host.entries) == 1


def test_a_second_entry_key_in_the_manifest_does_not_confuse_the_match(tmp_path):
    """A manifest carries every entry of the project. The one that matches the
    pinned asset is chosen by the file it names, not by position."""
    app = _app(tmp_path, manifest={
        "other.client.ts": {"file": "other.js", "src": "other.client.ts",
                            "isEntry": True},
        "entry.client.ts": {"file": "bundle.js", "src": "entry.client.ts",
                            "isEntry": True},
    })
    (app / "frontend" / "dist" / "other.js").write_text("// other\n", "utf-8")
    assert _add(app).prod_entries == ["frontend/dist/bundle.js"]


# -- the refusals ------------------------------------------------------------

def test_a_manifest_naming_no_entry_for_this_asset_is_refused(tmp_path):
    """The load-bearing refusal. A frontend built from a DIFFERENT entry leaves
    the declared asset unreachable in production, and before this the dev run
    said nothing at all."""
    app = _app(tmp_path, manifest={
        "other.client.ts": {"file": "bundle.js", "src": "other.client.ts",
                            "isEntry": True},
    })
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "names no entry for the pinned asset" in str(excinfo.value)
    assert "other.client.ts" in str(excinfo.value)


def test_a_chunk_that_is_not_a_declared_entry_is_not_served(tmp_path):
    """`isEntry` is what separates the module the shell loads from the chunks it
    pulls in. A manifest whose only record for this file is a non-entry chunk
    names no page, so it is refused rather than served."""
    app = _app(tmp_path, manifest={
        "entry.client.ts": {"file": "bundle.js", "src": "entry.client.ts"},
    })
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "names no entry for the pinned asset" in str(excinfo.value)


def test_a_manifest_that_is_not_readable_json_is_refused(tmp_path):
    app = _app(tmp_path, manifest="{ not json")
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "is not readable JSON" in str(excinfo.value)


def test_a_manifest_that_is_not_an_object_is_refused(tmp_path):
    app = _app(tmp_path, manifest=["entry.client.ts"])
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "is not a Vite manifest object" in str(excinfo.value)


def test_a_matching_entry_with_no_chunk_is_refused(tmp_path):
    app = _app(tmp_path, manifest={
        "entry.client.ts": {"src": "entry.client.ts", "isEntry": True},
    })
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "with no `file` chunk" in str(excinfo.value)


def test_a_chunk_that_is_not_on_disk_is_refused(tmp_path):
    """A manifest left over from a build whose output was cleaned points at a
    file that is not there; serving it is a 404 the dev run can see now."""
    app = _app(tmp_path, chunk=None)
    with pytest.raises(RuntimeError) as excinfo:
        _add(app)
    assert "not on disk under" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["/etc/hosts", "../outside.json",
                                 "frontend/../../outside.json"])
def test_a_manifest_path_outside_the_app_root_is_refused(tmp_path, bad):
    """Confinement is a property of the DECLARATION, so it is checked whether or
    not the file exists -- the same rule the dev source has always had, applied
    to the half that never had it."""
    with pytest.raises(RuntimeError) as excinfo:
        _add(_app(tmp_path), manifest=bad)
    assert "must be a relative path under" in str(excinfo.value)


def test_a_manifest_symlinked_out_of_the_app_root_is_refused(tmp_path):
    """`..` in the written path is not the only way out: the jail re-checks
    containment AFTER resolution, so a symlink planted inside the tree is
    refused too."""
    app = _app(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", "utf-8")
    link = app / "frontend" / "escape.json"
    link.symlink_to(outside)
    with pytest.raises(RuntimeError) as excinfo:
        _add(app, manifest="frontend/escape.json")
    assert "escapes the app root" in str(excinfo.value)


@pytest.mark.parametrize("bad", [None, "", 7, {"path": "x"}])
def test_a_manifest_that_is_not_a_path_is_refused(tmp_path, bad):
    host = DevWebUI(_app(tmp_path))
    with pytest.raises(RuntimeError) as excinfo:
        host.add_entry(_handle(host.app_root), bad, ["/notes"])
    assert "declares no production manifest path" in str(excinfo.value)


def test_nothing_is_recorded_when_the_production_half_is_refused(tmp_path):
    """A refusal leaves no half-registered entry behind: the production check
    runs before anything is appended, so `entries`, `channels`, `digests` and
    `prod_entries` stay in step."""
    app = _app(tmp_path, manifest={
        "other.client.ts": {"file": "bundle.js", "src": "other.client.ts",
                            "isEntry": True},
    })
    host = DevWebUI(app)
    with pytest.raises(RuntimeError):
        host.add_entry(_handle(app), MANIFEST_REL, ["/notes"])
    assert (host.entries, host.channels, host.digests, host.prod_entries) == \
        ([], [], [], [])


def test_the_dev_run_prints_the_production_entry(tmp_path, capsys):
    """A dev run states which chunk the production host would serve, next to the
    source it is serving now, so the two halves are visible together."""
    _add(_app(tmp_path))
    assert "prod entry frontend/dist/bundle.js" in capsys.readouterr().out


# -- controls: pre-existing surface, unchanged by this slice -----------------

def test_control_a_bare_path_string_is_still_refused_as_a_handle(tmp_path):
    """Item 459 F1's refusal, untouched here. Holds before and after."""
    host = DevWebUI(_app(tmp_path))
    with pytest.raises(RuntimeError) as excinfo:
        host.add_entry("./" + ENTRY_REL, MANIFEST_REL, ["/notes"])
    assert "not an asset handle" in str(excinfo.value)


def test_control_an_edited_asset_still_fails_its_pin(tmp_path):
    """The dev source's own deploy-time check, untouched here. Holds before and
    after, and proves the harness is registering real entries rather than
    refusing everything."""
    app = _app(tmp_path)
    host = DevWebUI(app)
    with pytest.raises(RuntimeError) as excinfo:
        host.add_entry({"path": ENTRY_REL, "sha256": "0" * 64},
                       MANIFEST_REL, ["/notes"])
    assert "does not match the sha256" in str(excinfo.value)
