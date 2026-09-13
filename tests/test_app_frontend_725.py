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
#: can be built or typechecked, so the toolchain legs skip rather than fail. Same
#: gating shape as the cordis-py legs of tests/test_app_notes_725.py.
_npm = shutil.which("npm")
needs_frontend_toolchain = pytest.mark.skipif(
    _npm is None or not (FRONTEND / "node_modules" / "vite").is_dir(),
    reason="needs the frontend node toolchain: run "
           "`npm ci` in examples/app/frontend",
)

#: The ONE diagnostic the checked-in frontend still carries, recorded as gap G4 in
#: docs/webapp-competitiveness-report.md: `revl export client` emits
#: `private readonly transport` on a fully routed client, which the strict
#: tsconfig (`noUnusedLocals`) reports as unread. It is a generator gap owned by
#: item 457, not something this app can fix in an asset it regenerates, so it is
#: filtered by NAME here — if the generator stops emitting it, this test keeps
#: passing and G4 closes.
_G4 = "notes.client.ts(66,63): error TS6138"


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
    as PATHS to external files, exactly Cordis WebUI's `addEntry` shape."""
    assert "./frontend/entry.client.ts" in ts
    assert "./frontend/dist/.vite/manifest.json" in ts


@pytest.mark.parametrize(
    "blob",
    ["<!doctype", "<!DOCTYPE", "<html", "<script", "<style", "<div", "<template"],
)
def test_no_inline_frontend_blob(ts, blob):
    """The emitted artifact carries no HTML/CSS/JS/Vue-template document string.
    This is the property the string-carried console cannot state about itself."""
    assert blob not in ts


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
    over the strict tsconfig reports nothing in the hand-written assets; the one
    remaining diagnostic is the recorded generator gap G4 in the artifact
    `revl export client` produces."""
    result = subprocess.run(
        [_npm, "exec", "--", "vue-tsc", "--noEmit", "-p", "tsconfig.json"],
        cwd=FRONTEND, capture_output=True, text=True, timeout=900,
    )
    out = result.stdout + result.stderr
    # third-party `.ts` shipped inside node_modules is not this app's contract.
    ours = [
        line for line in out.splitlines()
        if line and not line.startswith(("node_modules", " ", "\t"))
        and "error TS" in line and not line.startswith(_G4)
    ]
    assert ours == [], "\n".join(ours)
    for asset in HAND_WRITTEN:
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
