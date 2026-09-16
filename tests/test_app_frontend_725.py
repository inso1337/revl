"""The exemplary revl web application — the TS FRONTEND (item 462, issue #725).

Slice 4 of the 462 milestone (design docs/design/525-exemplary-web-app.md and
docs/design/525-webapp-slice4-frontend.md): 525 requires "a UI built from
external assets + a TS frontend behind a typed boundary (item 459)", NOT the
HTML/CSS/JS-carried-in-revl-strings console this milestone replaces. The frontend
lives under `examples/app/frontend/` as a normal Vite/Vue project and is bound to
the app through the merged webui coeffect (#772, design notes 526 and 530): the
`NotesConsole` component declares `requires webui: WebUI` and registers the
frontend by naming EXTERNAL asset files through `webui.add_entry`.

This suite pins the boundary properties the string console cannot state about
itself, mirroring tests/test_webui_entry_asset_ref_459.py but on the app:

* the component declares the coeffect (`requires webui: WebUI`) and provides
  nothing (the WebUI service is host-ambient, not re-provided);
* the emitted TS resolves `webui` through Cordis `inject` and reaches it as
  `ctx.webui.add_entry`, with no `globalThis` bridge and no extern door;
* the frontend entry and Vite manifest pass through as external asset PATHS, and
  the emitted artifact carries ZERO inline HTML/CSS/JS blob;
* the frontend assets are real, source-mapped files that CONSUME the notes app's
  typed routes through the `revl export client` client (item 457 artifact 5);
* `revl audit` reports the crossing on `NotesConsole`'s boundary (G1/G8).

The reactive half of the boundary is typed too, and this suite guards it: the
`data` parameter of `webui.add_entry` carries a declared record of reactive state,
the RPC surface is the console's declared provision, and
`revl export client --lang ts --face webui --component NotesConsole` PROJECTS both
into `contract.ts` (design note 530 Decision B, item 457 slice S4) — so the
server's published fields and the browser's `useRpc<T>()` type are one
declaration, the way `notes.client.ts` is one declaration with `NotesApi`. That
closes the filed gap G3 of docs/design/525-webapp-slice4-frontend.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402

APP = ROOT / "examples" / "app" / "notes.rvl"
FRONTEND = ROOT / "examples" / "app" / "frontend"
ENTRY = FRONTEND / "entry.client.ts"
SCREEN = FRONTEND / "NotesConsole.vue"
CONTRACT = FRONTEND / "contract.ts"
CLIENT = FRONTEND / "notes.client.ts"
VITE = FRONTEND / "vite.config.ts"
PKG = FRONTEND / "package.json"
LOCK = FRONTEND / "package-lock.json"
HAND_WRITTEN = ("entry.client.ts", "NotesConsole.vue", "contract.ts")

#: `npm ci` in `examples/app/frontend` is what makes the real Vite/Vue toolchain
#: available; without it the asset-shape assertions above still hold but nothing
#: can be built or typechecked, so the toolchain legs skip rather than fail on a
#: contributor's machine. Same gating shape as the cordis-py legs of
#: tests/test_app_notes_725.py.
#:
#: A SKIP is the right answer locally and the wrong one in the job that exists to
#: run these legs: gap G5 (docs/webapp-competitiveness-report.md) is exactly "no
#: CI leg builds or typechecks the frontend", and a leg that quietly skips there
#: reopens it without anyone seeing a red. `REVL_REQUIRE_FRONTEND_TOOLCHAIN=1`
#: turns the skip off, so in the `frontend-assets` job the legs always execute
#: and a missing toolchain is a FAILURE naming what to install.
_npm = shutil.which("npm")
_REQUIRE_TOOLCHAIN = os.environ.get("REVL_REQUIRE_FRONTEND_TOOLCHAIN") == "1"
_HAVE_TOOLCHAIN = _npm is not None and (FRONTEND / "node_modules" / "vite").is_dir()
needs_frontend_toolchain = pytest.mark.skipif(
    not _HAVE_TOOLCHAIN and not _REQUIRE_TOOLCHAIN,
    reason="needs the frontend node toolchain: run "
           "`npm ci` in examples/app/frontend",
)

#: Gap G4 (docs/webapp-competitiveness-report.md) was the one diagnostic the
#: checked-in frontend still carried: `revl export client` declared a
#: `private readonly transport` on a FULLY routed client, which nothing reads and
#: the strict tsconfig (`noUnusedLocals`) reported as `notes.client.ts error
#: TS6138`. The generator no longer emits it (`src/revl/export_client.py`
#: `_service_block`), so there is no filtered-by-name exemption here any more and
#: the typecheck leg below asserts ZERO first-party diagnostics.


@pytest.fixture(scope="module")
def ir():
    return compile_files([str(APP)])


@pytest.fixture(scope="module")
def ts(ir):
    return backend_emitter("typescript").emit(ir)


# -- the frontend ships as real Vite/Vue asset files ------------------------

def test_frontend_asset_files_exist():
    """The frontend is a real checked-in Vite/Vue project, not paths to
    nowhere: the client entry, the screen, the shared contract, the generated
    REST client, and the Vite build config."""
    for f in (ENTRY, SCREEN, CONTRACT, CLIENT, VITE, FRONTEND / "package.json"):
        assert f.is_file(), f


# -- the binding is a first-class coeffect, not a globalThis bridge ----------

def test_console_declares_webui_coeffect(ir):
    """`NotesConsole` declares the coeffect on the ambient WebUI service
    (`requires webui: WebUI`) plus the `ranking` service its channel reports, and
    provides the console's RPC surface — the typed boundary, on reviewed surface
    only (`service` + `requires` + `provides`)."""
    console = next(c for c in ir["components"] if c["name"] == "NotesConsole")
    assert (console.get("requires") or {}) == {
        "webui": "WebUI", "ranking": "Ranker"}
    assert (console.get("provides") or {}) == {"console": "NotesConsoleRpc"}
    services = ir.get("services") or []
    names = list(services) if isinstance(services, dict) else [
        s["name"] if isinstance(s, dict) else s for s in services
    ]
    assert "WebUI" in names
    assert "NotesConsoleRpc" in names


def test_add_entry_declares_a_typed_data_channel(ir):
    """`WebUI.add_entry` takes a `data` parameter whose type is a DECLARED record:
    the reactive state the entry publishes is typed in revl, not an untyped `T`
    handed to Cordis (the property design note 526 argues revl adds)."""
    add_entry = ir["services"]["WebUI"]["methods"]["add_entry"]
    data = next(p for p in add_entry["params"] if p["name"] == "data")
    assert data["type"] == "NotesConsoleState"
    state = ir["types"]["NotesConsoleState"]
    assert state["kind"] == "record"
    assert state["fields"] == {"strategy": "Str", "signals": "Int"}


def test_the_console_publishes_the_typed_state_it_declares(ir):
    """The `emit` hands `add_entry` a record built from the ranking service the
    console requires, so the published fields are the declared ones — the value
    and the type are the same declaration, not two."""
    console = next(c for c in ir["components"] if c["name"] == "NotesConsole")
    emit = next(s for s in console["body"] if s.get("step") == "emit")
    assert emit["expr"]["method"] == "add_entry"
    data = emit["expr"]["args"][3]
    assert data["kind"] == "record"
    published = {name: value for name, value in data["fields"]}
    assert set(published) == {"strategy", "signals"}
    for name, value in published.items():
        assert value["kind"] == "call"
        assert value["target"] == {"kind": "req", "name": "ranking"}
        assert value["method"] == name


def test_no_extern_door_for_the_binding(ir):
    """The coeffect is not reached through a `@ts ref` host-ref extern: no
    extern reaches the webui binding at all."""
    for ext in ir.get("externs") or []:
        assert ext["name"] != "webui_add_entry"
        for ref in (ext.get("refs") or {}).values():
            assert "webui" not in (ref.get("path") or "").lower()


def test_emitted_ts_resolves_webui_through_inject(ts):
    """The artifact resolves `webui` through Cordis' own `inject` (the ambient
    host-provided service), and the activation body reaches it as `ctx.webui`,
    passing the typed reactive state as the `data` argument."""
    assert 'inject: ["webui", "ranking"]' in ts
    assert "ctx.webui.add_entry(" in ts
    assert "{strategy: ctx.ranking.strategy(), signals: ctx.ranking.signals()}" in ts
    # the RPC half is the declared provision, registered on the same Context
    assert 'provide: ["console"]' in ts
    assert 'ctx.provide("console"' in ts


def test_no_globalthis_bridge_in_the_artifact(ts):
    """The emitted artifact carries no `globalThis.__revlWebui` embedder bridge:
    an ambient coeffect has no global reach."""
    assert "__revlWebui" not in ts
    assert "globalThis" not in ts


# -- assets flow through as external paths, no inline blob -------------------

def test_asset_paths_passed_as_external_files(ts):
    """The frontend entry and the built Vite manifest are handed to the coeffect
    as external files, exactly Cordis WebUI's `addEntry` shape.

    The dev source is an item-459-F1 asset HANDLE: the emitted artifact carries
    the root-relative resolved path (not the path as written) next to the sha256
    the compiler pinned, so the artifact states which file AND which bytes. The
    production manifest is still a plain path — it is a build output that does
    not exist when the composition is compiled, so there is nothing to pin.
    """
    import hashlib

    entry = ROOT / "examples" / "app" / "frontend" / "entry.client.ts"
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    assert '"frontend/entry.client.ts"' in ts
    assert digest in ts
    assert "./frontend/dist/.vite/manifest.json" in ts


@pytest.mark.parametrize(
    "blob",
    ["<!doctype", "<!DOCTYPE", "<html", "<script", "<style", "<div", "<template"],
)
def test_no_inline_frontend_blob(ts, blob):
    """The emitted artifact carries no HTML/CSS/JS/Vue-template document string.
    This is the property the string-carried console cannot state about itself.

    Kept as written, and no longer the only guard: this list is seven forms
    somebody thought of, and `<span`, `<a href=` and `document.createElement`
    all pass it. `test_no_embedded_frontend_document_in_the_artifact` below
    states the same property by shape, and
    `tests/test_no_embedded_frontend_document_1120.py` states it over every
    `.rvl` in the repository rather than over this one artifact.
    """
    assert blob not in ts


def test_no_embedded_frontend_document_in_the_artifact(ts):
    """The same artifact, by structure instead of by substring: no markup tree,
    no document declaration, no value interpolated into markup. Widens the
    parametrized list above — it does not replace it, because the two can fail
    for different reasons and a substring hit is a clearer error message."""
    from test_no_embedded_frontend_document_1120 import scan_text

    found = scan_text(ts, "the emitted typescript artifact")
    assert found is None, str(found)


# -- the assets are real and consume the typed routes ------------------------

def test_entry_is_a_real_cordis_client_extension():
    """The entry is a normal Cordis WebUI client extension (`defineExtension`)
    that registers the `/notes` page and reads the typed channel, not revl
    source masquerading as an asset."""
    text = ENTRY.read_text(encoding="utf-8")
    assert "defineExtension" in text
    assert "useRpc" in text
    assert "NotesConsole" in text
    # a real ES module, not revl source: it imports its screen and default-exports
    # a client extension.
    assert "export default" in text
    assert "import " in text


def test_screen_is_a_real_vue_sfc():
    """`NotesConsole.vue` is a real Vue single-file component (bundle-able,
    source-mappable), with a script and a template."""
    text = SCREEN.read_text(encoding="utf-8")
    assert "<script setup" in text
    assert "<template>" in text


def test_frontend_consumes_the_notes_typed_routes():
    """The checked-in client is the artifact `revl export client` derives from
    the `NotesApi` declaration, and the screen imports it — so the browser call
    surface is one declaration with the server's router (item 457 artifact 5).
    Regenerating the client reproduces the checked-in file byte-for-byte."""
    from revl.export_client import export_client

    client_text = CLIENT.read_text(encoding="utf-8")
    assert "class NotesApiClient" in client_text
    for method in ("get_note", "list_notes", "create_note"):
        assert f"async {method}(" in client_text
    assert "export interface Note" in client_text
    assert "export interface NewNote" in client_text

    screen = SCREEN.read_text(encoding="utf-8")
    assert "NotesApiClient" in screen
    assert "./notes.client" in screen

    # the asset is the projection of the declaration, not a hand-drift.
    regenerated = export_client(
        compile_files([str(APP)]), lang="ts", service="NotesApi"
    )
    assert regenerated.strip() == client_text.strip()


def test_contract_is_projected_from_the_declaration():
    """`contract.ts` is the typed reactive-state/RPC channel `useRpc<T>()` reads,
    and it is a GENERATED artifact: `revl export client --lang ts --face webui
    --component NotesConsole` reproduces the checked-in file byte-for-byte. So the
    server's published fields and the browser's type are one declaration (design
    note 530 Decision B, item 457 slice S4), not two hand-kept ones."""
    from revl.export_client import export_client

    text = CONTRACT.read_text(encoding="utf-8")
    assert "--face webui" in text.splitlines()[0]
    assert "readonly strategy: string;" in text
    assert "readonly signals: number;" in text
    assert "score(id: string): Promise<number>;" in text
    assert "bump(id: string): Promise<void>;" in text
    assert ("export type NotesConsoleChannel = NotesConsoleState & "
            "NotesConsoleRpc;") in text

    regenerated = export_client(
        compile_files([str(APP)]), lang="ts", face="webui",
        component="NotesConsole",
    )
    assert regenerated.strip() == text.strip()


def test_the_projected_channel_is_what_the_frontend_consumes():
    """The client extension reads the projected channel with `useRpc<T>()` and the
    screen calls only its RPC half, so the browser's reachable server surface is
    exactly the console's declared provision."""
    entry = ENTRY.read_text(encoding="utf-8")
    assert "useRpc<NotesConsoleChannel>()" in entry
    screen = SCREEN.read_text(encoding="utf-8")
    assert "channel.bump(" in screen
    assert "channel.score(" in screen


def test_vite_build_emits_source_maps_and_manifest():
    """459's "source maps pointing at the original files": the Vite build emits
    source maps and the manifest the coeffect names as the production entry."""
    text = VITE.read_text(encoding="utf-8")
    assert "sourcemap: true" in text
    assert "manifest: true" in text


# -- the coeffect is on the audit / G1 boundary ------------------------------

def test_audit_surfaces_the_coeffect_boundary(capsys):
    """`revl audit` reports the coeffect as a first-class crossing: the `webui`
    requirement on `NotesConsole` (G1), the `webui.add_entry` emission (the
    trusted host boundary, G8), and no untyped extern door in the surface."""
    from revl.__main__ import main as revl_main

    assert revl_main(["audit", "--json", str(APP)]) == 0
    audit = json.loads(capsys.readouterr().out)
    comps = {c["name"]: c for c in audit["manifest"]["components"]}
    assert "NotesConsole" in comps
    assert sorted(comps["NotesConsole"]["inject"]) == ["ranking", "webui"]
    assert comps["NotesConsole"]["provides"] == ["console"]

    boundary = audit["boundary"]["NotesConsole"]
    assert "webui.add_entry" in boundary["emissions"]
    assert boundary.get("externs") == []
    assert "WebUI" in audit["distributability"]


# -- the toolchain really runs: build, source maps, typecheck ----------------

@needs_frontend_toolchain
def test_the_frontend_toolchain_is_really_installed():
    """Anti-vacuity for the two legs below, and the whole of gap G5: they are the
    only things in this repo that COMPILE the frontend, and a skip reads exactly
    like a pass in a job summary. Under `REVL_REQUIRE_FRONTEND_TOOLCHAIN=1` — the
    `frontend-assets` CI job — the skip is off, so this fails and names the
    missing half instead of the legs silently not running."""
    assert _npm is not None, (
        "npm is not on PATH, so the frontend cannot be built or typechecked. "
        "The `frontend-assets` job pins node with actions/setup-node."
    )
    assert (FRONTEND / "node_modules" / "vite").is_dir(), (
        "examples/app/frontend/node_modules is cold: run `npm ci` there. "
        "Without it vue-tsc and vite are absent and the two legs below measure "
        "nothing (docs/webapp-competitiveness-report.md gap G5)."
    )


@needs_frontend_toolchain
def test_the_frontend_really_builds_and_maps_to_the_originals(tmp_path):
    """459's "source maps pointing at the original files", proven by building
    rather than by reading `vite.config.ts`: the real Vite build emits a bundle
    whose map names `entry.client.ts`, `NotesConsole.vue` and `notes.client.ts`,
    and writes the `.vite/manifest.json` the `webui` coeffect declares as the
    production entry."""
    out = tmp_path / "dist"
    result = subprocess.run(
        [_npm, "run", "build", "--", "--outDir", str(out)],
        cwd=FRONTEND, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    manifest = json.loads((out / ".vite" / "manifest.json").read_text("utf-8"))
    # the manifest is keyed by the SAME path the component names through
    # `webui.add_entry`, which is how a production host resolves the dev source
    # to its built asset.
    assert "entry.client.ts" in manifest
    assert manifest["entry.client.ts"]["isEntry"] is True
    bundle = out / manifest["entry.client.ts"]["file"]
    assert bundle.is_file()

    sources = json.loads((bundle.with_suffix(".js.map")).read_text("utf-8"))["sources"]
    named = {Path(s).name for s in sources}
    assert {"entry.client.ts", "NotesConsole.vue", "notes.client.ts"} <= named, sources


@needs_frontend_toolchain
def test_the_frontend_typechecks_against_the_real_cordis_client():
    """The typed boundary is only typed if it COMPILES against the real
    `@cordisjs/client` surface, not against a loose local stand-in. `vue-tsc`
    over the strict tsconfig reports NOTHING in any first-party file of this
    project — the hand-written assets and the generated ones alike. Gap G4 was
    the single exemption this assertion used to carry; it is closed, so the
    assertion is now unconditional and a reintroduced diagnostic in a generated
    artifact reds here."""
    result = subprocess.run(
        [_npm, "exec", "--", "vue-tsc", "--noEmit", "-p", "tsconfig.json"],
        cwd=FRONTEND, capture_output=True, text=True, timeout=900,
    )
    out = result.stdout + result.stderr
    # third-party `.ts` shipped inside node_modules is not this app's contract:
    # `@cordisjs/client` resolves its `.` export to raw source (`client/index.ts`),
    # which `skipLibCheck` cannot skip because it is not a `.d.ts`, and this
    # project does not own that package's strictness.
    ours = [
        line for line in out.splitlines()
        if line and not line.startswith(("node_modules", " ", "\t"))
        and "error TS" in line
    ]
    assert ours == [], "\n".join(ours)
    for asset in (*HAND_WRITTEN, "notes.client.ts"):
        assert not any(line.startswith(asset) for line in out.splitlines()), out


def test_the_frontend_tree_is_pinned_and_the_lockfile_does_not_drift():
    """Item 461's reproducibility clause for the frontend half of `revl dev`:
    the command spawns `npm run dev`, so an unpinned tree means the one dev
    command can break on an upstream release. `package-lock.json` is committed
    and its root entry declares the SAME ranges as `package.json`, which is what
    `npm ci` refuses to install past."""
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    root = lock["packages"][""]
    for field in ("dependencies", "devDependencies"):
        assert root.get(field, {}) == pkg.get(field, {}), field
    # `npm ci` needs a v2+ lockfile with a resolved tree, not a bare v1 shim.
    assert lock["lockfileVersion"] >= 2
    assert lock["packages"]["node_modules/vite"]["version"].startswith("7.")


def test_element_plus_is_pinned_above_the_transitive_floor():
    """`element-plus` reaches this tree only through `@cordisjs/client`, which
    declares it at an EXACT `2.7.7`, so nothing in `package.json`'s own ranges can
    move it and a range bump is not available. The floor is an npm `overrides`
    entry instead, and the resolved tree has to agree with it: a lockfile
    regenerated by an npm that dropped the override falls straight back to the
    transitive pin, and no other assertion in this suite reads that version.

    The floor is asserted as a lower bound rather than one literal so a later
    bump does not have to edit this test, only raise the override.
    """
    floor = (2, 11, 1)

    def parts(v):
        return tuple(int(n) for n in v.split("."))

    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    pinned = (pkg.get("overrides") or {}).get("element-plus")
    assert pinned is not None, (
        "examples/app/frontend/package.json declares no `overrides` entry for "
        "element-plus, so the installed version is whatever @cordisjs/client "
        "pins."
    )
    assert parts(pinned) >= floor, pinned

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    resolved = lock["packages"]["node_modules/element-plus"]["version"]
    assert resolved == pinned, (resolved, pinned)

    # The declaration the override exists to outrank. If `@cordisjs/client`
    # stops pinning exactly, this reds and the override can be dropped rather
    # than left in place unread.
    upstream = lock["packages"]["node_modules/@cordisjs/client"]["dependencies"]
    assert upstream["element-plus"] == "2.7.7", upstream["element-plus"]


def test_the_pinned_vite_major_is_one_the_vue_plugin_peers():
    """The `ERESOLVE` this project used to need `--legacy-peer-deps` for was its
    own: `@vitejs/plugin-vue@5` peers `vite ^5 || ^6` against a pinned `vite ^7`.
    Pinning the plugin to a major that peers the pinned Vite is what makes plain
    `npm ci` resolve, so the pairing is asserted rather than left to a comment."""
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    dev = pkg["devDependencies"]
    assert dev["vite"].startswith("^7"), dev["vite"]
    assert dev["@vitejs/plugin-vue"].startswith("^6"), dev["@vitejs/plugin-vue"]
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    peers = lock["packages"]["node_modules/@vitejs/plugin-vue"]["peerDependencies"]
    assert "^7.0.0" in peers["vite"], peers


@needs_frontend_toolchain
def test_the_built_manifest_resolves_the_asset_the_component_pinned(tmp_path):
    """The two halves of `webui.add_entry` describe ONE frontend, proven by
    building it (item 459, issue #722).

    The component names a dev source and a production Vite manifest in one call,
    and until now only the first was ever opened: `DevWebUI` confined, existence-
    checked and re-hashed the handle, then stored the manifest path verbatim.
    That mattered because the two are keyed differently — the handle carries the
    compile-root-relative `frontend/entry.client.ts`, a Vite manifest is keyed by
    the Vite-root-relative `entry.client.ts` — so nothing in the repo could say
    whether the declared asset was reachable in the mode that ships. The build
    above asserts the manifest exists; this asserts it names THIS asset.

    Everything here is the real article: the handle comes from compiling
    `notes.rvl`, the manifest from a real `vite build`, and the resolution from
    the `DevWebUI` `revl dev` installs. The tree is copied to `tmp_path` first so
    the build writes no `dist/` into the checkout, with `node_modules` linked
    rather than reinstalled.
    """
    from revl.dev import DevWebUI

    app = tmp_path / "app"
    shutil.copytree(FRONTEND.parent, app,
                    ignore=shutil.ignore_patterns("node_modules", "dist"))
    (app / "frontend" / "node_modules").symlink_to(FRONTEND / "node_modules")

    built = subprocess.run([_npm, "run", "build"], cwd=app / "frontend",
                           capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stdout + built.stderr

    ir = compile_files([str(app / "notes.rvl")])
    console = next(c for c in ir["components"] if c["name"] == "NotesConsole")
    emit = next(s for s in console["body"] if s.get("step") == "emit")
    handle = {name: node["value"]
              for name, node in emit["expr"]["args"][0]["fields"]}
    manifest = emit["expr"]["args"][1]["value"]

    host = DevWebUI(app)
    host.add_entry(handle, manifest, ["/notes"])

    # the chunk a production Cordis WebUI host would serve for this entry, read
    # out of the manifest the component itself names.
    resolved = host.prod_entries[0]
    assert resolved is not None, (
        f"{manifest} names no entry for {handle['path']}")
    assert (app / resolved).is_file()
    records = json.loads((app / Path(manifest)).read_text(encoding="utf-8"))
    assert Path(resolved).name == records["entry.client.ts"]["file"]
