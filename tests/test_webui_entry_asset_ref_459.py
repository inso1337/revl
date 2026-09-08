"""Roadmap item 459 (issue #722): first-class frontend asset integration.

The legacy console carries ~4,800 lines of HTML/CSS/JS inside revl source
strings. 459's direction is the opposite: a revl component contributes a frontend
by naming EXTERNAL asset files and handing them to Cordis WebUI
(`ctx.webui.addEntry`, docs/design/526-webui-asset-alignment.md), with the
frontend entry living in a real, source-mapped TypeScript file rather than a
revl string.

The enabling slice (#761) reached the Cordis `Context` through the untyped
embedder bridge `globalThis.__revlWebui`, a `@ts ref` host-ref door onto an
ambient service. Design note 530 flagged that as a slice compromise; the
architect approved making the binding a FIRST-CLASS ambient-service coeffect.
This suite pins that surface on the reference `examples/webui-entry/`, using only
reviewed language surface (`service` + `requires`). It asserts:

- the component declares the coeffect (`requires webui: WebUI`) and the WebUI
  service surface, so the binding is a typed `requires` boundary, not a global;
- the emitted TS resolves `webui` through Cordis `inject` (`inject: ["webui"]`,
  the body reads `ctx.webui.add_entry`) — no `globalThis.__revlWebui` bridge and
  no `@ts ref` host-ref door remain, anywhere;
- the frontend entry and Vite manifest still pass through as external asset
  PATHS, and the artifact carries ZERO inline HTML/CSS/JS blob;
- `revl audit` reports the crossing on the component's boundary: the `webui`
  requirement (G1) and the `webui.add_entry` emission (the trusted host
  boundary, G8), with no untyped extern door in the surface.

The full close (the typed reactive-state/RPC `data` contract, folded into item
457, and the exemplary frontend, item 462) is still app-gated; see design note
530. This slice guards the coeffect boundary that close builds on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402

EXAMPLE_DIR = ROOT / "examples" / "webui-entry"
CONSOLE = EXAMPLE_DIR / "console.rvl"
HOST = EXAMPLE_DIR / "webui_host.ts"
ENTRY = EXAMPLE_DIR / "frontend" / "entry.client.ts"


@pytest.fixture(scope="module")
def ir():
    return compile_files([str(CONSOLE)])


@pytest.fixture(scope="module")
def ts(ir):
    return backend_emitter("typescript").emit(ir)


# -- the reference ships as real files --------------------------------------

def test_reference_files_exist():
    """The host setup module and the frontend entry are real, checked-in asset
    files, not paths to nowhere."""
    assert CONSOLE.is_file()
    assert HOST.is_file()
    assert ENTRY.is_file()


# -- the binding is a first-class coeffect, not a globalThis bridge ----------

def test_component_declares_webui_coeffect(ir):
    """`ConsoleUI` declares a first-class coeffect on the ambient WebUI service
    (`requires webui: WebUI`), and `WebUI` is a declared service — the typed
    boundary the whole slice is about, expressed on reviewed surface only."""
    console = next(c for c in ir["components"] if c["name"] == "ConsoleUI")
    assert (console.get("requires") or {}) == {"webui": "WebUI"}
    # the component provides nothing: the service is host-provided (ambient),
    # not re-provided by a revl component (that would be the parallel-framework
    # anti-pattern 526 argues against).
    assert not (console.get("provides") or {})
    services = ir.get("services") or []
    names = [s["name"] if isinstance(s, dict) else s for s in services] \
        if isinstance(services, list) else list(services)
    assert "WebUI" in names


def test_no_ts_ref_extern_door(ir):
    """The coeffect replaces the enabling slice's `@ts ref` host-ref door: there
    is no `webui_add_entry` extern, and no extern reaches `webui_host.ts` at
    all. The binding is resolved by the composition, not imported through a
    hash-pinned host-ref."""
    for ext in ir.get("externs") or []:
        assert ext["name"] != "webui_add_entry"
        refs = ext.get("refs") or {}
        for ref in refs.values():
            assert "webui_host" not in (ref.get("path") or "")


def test_emitted_ts_resolves_webui_through_inject(ts):
    """The artifact resolves `webui` through Cordis' own `inject` (the ambient
    host-provided service), and the activation body reaches it as `ctx.webui`."""
    assert 'inject: ["webui"]' in ts
    assert "ctx.webui.add_entry(" in ts


def _code_lines(path: Path) -> str:
    """The executable lines of a source file — comment lines dropped — so an
    assertion about what the CODE does is not tripped by a doc comment that
    names the old mechanism to explain the migration away from it."""
    out = []
    in_block = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if line.startswith("//"):
            continue
        if line.startswith("/*"):
            in_block = "*/" not in line
            continue
        out.append(raw)
    return "\n".join(out)


def test_no_globalthis_bridge_anywhere(ts):
    """The `globalThis.__revlWebui` embedder bridge the enabling slice used is
    gone. The emitted artifact carries no trace of it, and the reference's host
    CODE reaches nothing through a global — an ambient coeffect has no global
    reach. (Doc comments still name the bridge to explain the migration; the
    check is on code, not prose.)"""
    assert "__revlWebui" not in ts
    assert "globalThis" not in ts
    host_code = _code_lines(HOST)
    assert "__revlWebui" not in host_code
    assert "globalThis" not in host_code
    # the host setup module installs the ambient service through the provide
    # seam, not a global assignment.
    assert ".provide('webui'" in host_code


# -- assets still flow through as external paths -----------------------------

def test_asset_paths_passed_as_external_files(ts):
    """The frontend entry and the built Vite manifest are handed to the coeffect
    as PATHS to external files, exactly Cordis WebUI's `addEntry` shape."""
    assert "./frontend/entry.client.ts" in ts
    assert "./dist/.vite/manifest.json" in ts


# -- the anti-pattern is absent ----------------------------------------------

@pytest.mark.parametrize(
    "blob",
    ["<!doctype", "<!DOCTYPE", "<html", "<script", "<style", "<div"],
)
def test_no_inline_frontend_blob(ts, blob):
    """The emitted artifact carries no HTML/CSS/JS document string. This is the
    property the string-carried console cannot state about itself."""
    assert blob not in ts


def test_frontend_entry_is_a_real_ts_asset():
    """The referenced entry is a normal TypeScript module (bundle-able,
    source-mappable), not revl source masquerading as an asset."""
    text = ENTRY.read_text(encoding="utf-8")
    assert "export default" in text
    # it is not a revl component carried as a string.
    assert "component " not in text
    assert "extern " not in text


# -- the coeffect is on the audit / G1 boundary ------------------------------

def test_audit_surfaces_the_coeffect_boundary(capsys):
    """`revl audit` reports the coeffect as a first-class crossing: the `webui`
    requirement is on `ConsoleUI`'s injected boundary (G1 — declared access),
    the `webui.add_entry` emission is on its emission boundary (the trusted host
    boundary, G8), and the WebUI service surface is enumerated. No untyped extern
    door appears in the surface."""
    from revl.__main__ import main as revl_main

    assert revl_main(["audit", "--json", str(CONSOLE)]) == 0
    audit = json.loads(capsys.readouterr().out)
    comps = {c["name"]: c for c in audit["manifest"]["components"]}
    assert "ConsoleUI" in comps
    assert comps["ConsoleUI"]["inject"] == ["webui"]
    assert comps["ConsoleUI"]["provides"] == []

    boundary = audit["boundary"]["ConsoleUI"]
    assert "webui.add_entry" in boundary["emissions"]
    # the binding is not reached through an untyped host-ref extern.
    assert boundary.get("externs") == []
    assert audit.get("externs") == []

    # the ambient service's surface is enumerable next to the crossing.
    assert "WebUI" in audit["distributability"]
