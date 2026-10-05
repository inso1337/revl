"""Issue #1920: two rust-tier build failures, both held here under real cargo.

1. An `extern acquire` handle bound in a component's activation and captured
   by a provide block was typed `Arc<Value>`, and the extern's undo was handed
   the `Arc` where the handle was declared. A call's result is now held by
   value, as the extern's declared handle type.
2. A service named `Box` was emitted as `pub trait Box`, which shadows
   `std::boxed::Box` in `Box<dyn Box>`. A service is a Rust trait, so it takes
   the type-name reservation (`Box_`), as a user record already did, and the
   reservation now covers the rest of the prelude and std names the module
   spells as bare tokens (`Result`, `Arc`, `Clone`, `Default`, ...).

The shape is tests/fixtures/emit_rust_corpus/prelude_names.rvl, which the
self-host port also holds to byte agreement.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import compile_files  # noqa: E402

import test_emit_rust as rust_tests  # noqa: E402

_spec = importlib.util.spec_from_file_location("rust_emit_1920", ROOT / "backends" / "rust" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "prelude_names.rvl"

LIFECYCLE = '''
lifecycle test "a captured extern handle behind a service named Box" {
  load BoxKit
  let v = call box.get()
  assert v == 42
}
'''


def _emit(path: Path) -> str:
    return emit.emit(compile_files([str(path)]))


def test_a_service_named_box_does_not_shadow_std_box():
    src = _emit(DOC)
    assert "pub trait Box_: Send + Sync {" in src
    assert "let box_box: Box<dyn Box_> = Box::new(BoxKitBox {" in src
    assert "impl Box_ for BoxKitBox {" in src
    assert "store: Arc<Box<dyn Box_>>," in src
    assert "pub trait Box:" not in src and "dyn Box>" not in src
    # the service's NAME is the surface spelling in every string
    assert '"box" => Some("Box"),' in src


def test_a_captured_extern_acquire_handle_has_its_declared_type():
    src = _emit(DOC)
    assert "    h: Value," in src
    assert "Arc<Value>" not in src
    assert "let h = box_open();" in src
    assert "box_close(h_undo)" in src
    assert "peek(self.h.clone())" in src


@pytest.mark.parametrize("name", ["Box", "Vec", "Arc", "Result", "Option", "Clone", "Default"])
def test_a_service_named_after_a_prelude_type_takes_the_reservation(tmp_path, name):
    path = tmp_path / "svc.rvl"
    path.write_text(
        f"service {name} {{ fn get() -> Int }}\n"
        f"component K provides k: {name} {{\n  provide k {{ fn get() = 1 }}\n}}\n",
        encoding="utf-8")
    src = _emit(path)
    assert f"pub trait {name}_: Send + Sync {{" in src
    assert f"Box<dyn {name}_>" in src


@rust_tests.needs_cargo
@pytest.mark.parametrize("name", ["Box", "Vec", "Arc", "Result", "Option", "Clone", "Default"])
def test_a_service_named_after_a_prelude_type_builds(tmp_path, name):
    path = tmp_path / "svc.rvl"
    path.write_text(
        f"service {name} {{ fn get() -> Int }}\n"
        f"component K provides k: {name} {{\n  provide k {{ fn get() = 1 }}\n}}\n",
        encoding="utf-8")
    crate = tmp_path / "crate"
    crate.mkdir()
    result = rust_tests._cargo_check(crate, _emit(path))
    assert result.returncode == 0, result.stderr


@rust_tests.needs_cargo
def test_the_captured_handle_and_the_box_service_build_and_run(tmp_path):
    """The whole shape under `cargo test`: the activation acquires the handle,
    the provide method reads it through `peek`, and the in-file lifecycle test
    calls the `Box` service and checks the answer."""
    path = tmp_path / "prelude_names.rvl"
    path.write_text(DOC.read_text(encoding="utf-8") + LIFECYCLE, encoding="utf-8")
    crate = tmp_path / "crate"
    crate.mkdir()
    result = rust_tests._cargo_test(crate, _emit(path), "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a_captured_extern_handle_behind_a_service_named_box ... ok" in result.stdout
