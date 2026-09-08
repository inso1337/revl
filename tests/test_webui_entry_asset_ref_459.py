"""Roadmap item 459 (issue #722): first-class frontend asset integration, the
enabling slice.

The legacy console carries ~4,800 lines of HTML/CSS/JS inside revl source
strings. 459's direction is the opposite: a revl component contributes a frontend
by naming EXTERNAL asset files and handing them to Cordis WebUI
(`ctx.webui.addEntry`, docs/design/526-webui-asset-alignment.md), with the
binding living in a real, source-mapped TypeScript module rather than a revl
string.

This suite pins that property on the shipped reference `examples/webui-entry/`,
using ONLY reviewed language surface (the item-396/410 host-ref door). It asserts
the emitted TS artifact:

- reaches the webui binding by IMPORTING an external host module (the ref is
  content-hash-pinned in the IR, the integrity/source-map anchor to the
  original file), never as an inline host body;
- passes the frontend entry and Vite manifest through as external asset PATHS;
- contains ZERO inline HTML/CSS/JS blob (the anti-pattern 459 removes);

and that the referenced frontend entry is itself a real asset file on disk.

The full close (a first-class ambient `ctx.webui` coeffect with a typed
reactive-state/RPC contract, and the exemplary frontend) is architect- and
462-gated; see docs/design/530-webui-entry-surface.md. This slice guards the
mechanism that close builds on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402

EXAMPLE_DIR = ROOT / "examples" / "webui-entry"
CONSOLE = EXAMPLE_DIR / "console.rvl"
SHIM = EXAMPLE_DIR / "webui_host.ts"
ENTRY = EXAMPLE_DIR / "frontend" / "entry.client.ts"


@pytest.fixture(scope="module")
def ir():
    return compile_files([str(CONSOLE)])


@pytest.fixture(scope="module")
def ts(ir):
    return backend_emitter("typescript").emit(ir)


# -- the reference ships as real files --------------------------------------

def test_reference_files_exist():
    """The binding shim and the frontend entry are real, checked-in asset files,
    not paths to nowhere."""
    assert CONSOLE.is_file()
    assert SHIM.is_file()
    assert ENTRY.is_file()


# -- the binding is an external module reference, hash-pinned ----------------

def test_ir_records_ts_ref_with_hash(ir):
    """The webui_add_entry extern binds to the shim through the host-ref door, so
    the IR carries a ts ref with the imported symbol, the root-relative path, and
    a content hash of the file's bytes (the source-map / integrity anchor)."""
    ext = next(e for e in ir["externs"] if e["name"] == "webui_add_entry")
    ref = ext["refs"]["ts"]
    assert ref["symbol"] == "webuiAddEntry"
    assert ref["path"] == "webui_host.ts"
    assert len(ref["sha256"]) == 64
    # a ref-only extern carries no inline ts body: the JS is not in the IR.
    assert "ts" not in (ext.get("bodies") or {})


def test_emitted_ts_imports_shim_not_inline(ts):
    """The artifact reaches the binding by importing the external module (the
    lazy host-ref thunk), never by pasting a host body."""
    assert '_revl_ref_path("webui_host.ts")' in ts
    assert '_m["webuiAddEntry"]' in ts


# -- assets flow through as external paths -----------------------------------

def test_asset_paths_passed_as_external_files(ts):
    """The frontend entry and the built Vite manifest are handed to the binding
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
