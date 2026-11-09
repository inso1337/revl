"""A file only a used module's provider needs does not break the importer.

`tests/test_consumer_declarations_only.py` pins that `use "svc.rvl" { Name }`
never imports components. Before this suite's change the used module was still
resolved whole at load, so a consumer that named one service was refused for a
file it could never reach: an `asset` a provider body names, or the host-body
file or host-module ref of an extern only that provider calls.

The rule now follows what the compile emits:

- a module reached only for a service (and the types that service names) emits
  no extern and lowers no body, so none of its files is read;
- a module one of whose pure declarations (a `pub type`, a `pub fn`, a `pub
  extern`) is imported emits every extern it declares, so their host-body files
  and refs are resolved as before, and so is every asset outside a component.
  The assets written inside its components are not: components are never
  imported;
- a root module is resolved whole, exactly as before. Each case below pins the
  refusal of the same module as a root, with the same message.

Still a refusal for the importer, pinned here too: a syntax error anywhere in
the used module, and a backend import in an inline `@py` body there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from revl import compile_files
from revl.errors import RevlError

_SERVICE = """\
pub type Note = { text: Str }

service Notes {
  emission fn save(note: Note) -> Int
}
"""

_PROVIDER = """\
component NotesProvider provides notes: Notes {
  provide notes {
    fn save(note) -> Int {
      %s
      return 0
    }
  }
}
"""

_HELPER = "extern pure fn helper(x: Str) -> Str\n    = %s\n\n"

_CONSUMER = """\
use "svc.rvl" { %s }

service Front {
  emission fn go(text: Str) -> Int
}

component NotesUser requires notes: Notes provides front: Front {
  provide front {
    fn go(text) -> Int {
      emit notes.save({ text: text })
      return 0
    }
  }
}
"""

_CLEAN = _SERVICE + _PROVIDER % "let seen = note.text"

# case id -> (svc.rvl with a provider that needs a file nobody supplies,
#             the refusal when svc.rvl is a root: in memory, then from disk)
_CASES = {
    "asset": (
        _SERVICE + _PROVIDER % 'let handle = asset "./missing.txt"',
        r"`asset` file './missing\.txt' is not in the in-memory sources map",
        r"`asset` file './missing\.txt' not found"),
    "body-file": (
        _SERVICE + _HELPER % '@py file "missing_body.py"'
        + _PROVIDER % "let seen = helper(note.text)",
        r"@py host-body file 'missing_body\.py' for extern `helper` is not in "
        r"the in-memory sources map",
        r"@py host-body file 'missing_body\.py' for extern `helper` not "
        r"found"),
    "ref": (
        _SERVICE + _HELPER % '@py ref do_it from "missing_mod.py"'
        + _PROVIDER % "let seen = helper(note.text)",
        r"@py host-module ref 'missing_mod\.py' for extern `helper` is not in "
        r"the in-memory sources map",
        r"@py host-module ref 'missing_mod\.py' for extern `helper` not "
        r"found"),
}


def _compile(tmp_path: Path, roots: list[str], modules: dict[str, str],
             *, on_disk: bool = False) -> dict:
    paths = [str(tmp_path / name) for name in roots]
    if on_disk:
        for name, text in modules.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        return compile_files(paths)
    sources = {str(tmp_path / name): text for name, text in modules.items()}
    return compile_files(paths, sources=sources)


def _consumer(tmp_path: Path, svc: str, names: str = "Notes", **kw) -> dict:
    modules = {"svc.rvl": svc, "consumer.rvl": _CONSUMER % names}
    return _compile(tmp_path, ["consumer.rvl"], modules, **kw)


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True)


@pytest.mark.parametrize("on_disk", [False, True], ids=["memory", "disk"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_a_file_only_the_provider_needs_leaves_the_service_importer_green(
        tmp_path, case, on_disk):
    svc, _, _ = _CASES[case]
    ir = _consumer(tmp_path, svc, on_disk=on_disk)
    assert [c["name"] for c in ir["components"]] == ["NotesUser"]
    # Nothing of the provider reached the document: it is the one a clean
    # provider beside the same service gives.
    assert _dump(ir) == _dump(_consumer(tmp_path, _CLEAN, on_disk=on_disk))
    assert not ir.get("externs")


@pytest.mark.parametrize("on_disk", [False, True], ids=["memory", "disk"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_the_same_module_as_a_root_is_still_refused(tmp_path, case, on_disk):
    svc, in_memory, from_disk = _CASES[case]
    modules = {"svc.rvl": svc, "consumer.rvl": _CONSUMER % "Notes"}
    for roots in (["svc.rvl"], ["consumer.rvl", "svc.rvl"]):
        with pytest.raises(RevlError, match=from_disk if on_disk else in_memory):
            _compile(tmp_path, roots, modules, on_disk=on_disk)


def test_an_asset_in_a_component_of_a_module_used_for_a_type_is_not_read(tmp_path):
    # `Note` is a pure declaration, so svc.rvl joins the declaration closure;
    # its components still do not.
    svc, _, _ = _CASES["asset"]
    ir = _consumer(tmp_path, svc, names="Notes, Note")
    assert [c["name"] for c in ir["components"]] == ["NotesUser"]
    assert _dump(ir) == _dump(_consumer(tmp_path, _CLEAN, names="Notes, Note"))


@pytest.mark.parametrize("case", ["body-file", "ref"])
def test_an_extern_of_a_module_used_for_a_type_still_needs_its_file(tmp_path, case):
    # The closure emits every extern the module declares, `helper` included,
    # so its host file is part of the compile whoever calls it.
    svc, in_memory, _ = _CASES[case]
    with pytest.raises(RevlError, match=in_memory):
        _consumer(tmp_path, svc, names="Notes, Note")


_ASSET_FN = """\
pub type Banner = { path: Str, sha256: Str }

pub fn banner() -> Banner { return asset "./banner.txt" }

"""


def _consumer_with_banner(tmp_path: Path, files: dict[str, str]) -> dict:
    svc, _, _ = _CASES["asset"]
    modules = {"svc.rvl": _ASSET_FN + svc,
               "consumer.rvl": _CONSUMER % "Notes, banner", **files}
    return _compile(tmp_path, ["consumer.rvl"], modules)


def test_an_asset_outside_a_component_of_a_used_module_is_still_resolved(tmp_path):
    ir = _consumer_with_banner(tmp_path, {"banner.txt": "hello\n"})
    [banner] = [f for f in ir["functions"] if f["name"] == "banner"]
    assert '"banner.txt"' in _dump(banner)

    with pytest.raises(RevlError, match=r"`asset` file './banner\.txt' is not "
                                        r"in the in-memory sources map"):
        _consumer_with_banner(tmp_path, {})


def test_an_imported_extern_of_a_used_module_still_gets_its_body_file(tmp_path):
    svc = ("pub " + _HELPER % '@py file "body.py"') + _CLEAN
    modules = {"svc.rvl": svc, "consumer.rvl": _CONSUMER % "Notes, helper",
               "body.py": "return x.upper()\n"}
    ir = _compile(tmp_path, ["consumer.rvl"], modules)
    [helper] = ir["externs"]
    assert helper["name"] == "helper"
    assert "x.upper()" in helper["bodies"]["py"]


def test_a_syntax_error_in_a_used_module_still_breaks_the_importer(tmp_path):
    broken = _SERVICE + _PROVIDER % "let seen = ("
    with pytest.raises(RevlError) as refused:
        _consumer(tmp_path, broken)
    assert refused.value.filename.endswith("svc.rvl")


def test_a_backend_import_in_an_inline_py_body_of_a_used_module_is_still_refused(
        tmp_path):
    svc = (_SERVICE + _HELPER % "@py {\n      import runtime\n      return x\n    }"
           + _PROVIDER % "let seen = helper(note.text)")
    with pytest.raises(RevlError, match="user-origin @py body for extern "
                                        "`helper` imports backend module "
                                        "`runtime`"):
        _consumer(tmp_path, svc)
