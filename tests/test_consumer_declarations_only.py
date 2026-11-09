"""A consumer compiles against the declarations it imports, not a provider's body.

The shape is `examples/mediator.rvl` split into modules: `svc.rvl` holds the
services, `consumer.rvl` holds `NotesConsumer` (requires `writer:
AppendWriter`), and `mediator.rvl` holds `FsMediator`, the provider, in
variants. Every module is an in-memory source, and `_compile` makes any read
from disk raise, so a module left out of `sources` is absent.

What holds today, pinned as regressions (they pass on the base they were
written on, so none of them was red first):

- (a) the consumer plus the service module compiles with no provider at all;
- (b) the consumer component's IR is byte-equal whether the provider is
  absent, valid, or has a different valid body;
- (c) a provider with a broken body in a module the consumer does not `use`
  leaves the consumer compile green: that module is never loaded;
- (d) a provider whose body fails to check (a requirement it does not declare)
  in the SAME module as the services the consumer `use`s also leaves it green:
  components are never imported, and so never lowered or checked (the
  root-module merge in `compiler._compile_files`), so `use { names }` does not
  carry `FsMediator` in. The `nope` refusal appears only when that module is
  itself a root of the compile, where its components are the composition.

Not pinned here: the used module is still parsed whole, so a syntax error in
a component body there breaks the importer. The files only its components need
(`asset` files, host body files, refs) are not read:
`tests/test_used_module_resources.py`. Compiling against service declarations
alone, with the dependency's source gone, is an open design question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from revl import compile_files
from revl import compiler as _compiler
from revl.errors import RevlError

_SERVICES = """\
service FileStore {
  emission[fs] fn write(path: Str, data: Str) -> Int
}

service AppendWriter {
  emission fn append(path_under_dir: Str, data: Str) -> Int
}

service Notes {
  emission fn save(data: Str) -> Int
}
"""

_CONSUMER = """\
use "svc.rvl" { AppendWriter, Notes }

component NotesConsumer requires writer: AppendWriter provides notes: Notes {
  provide notes {
    fn save(data) -> Int {
      emit writer.append("notes/daily.log", data)
      return 0
    }
  }
}
"""

_MEDIATOR = """\
component FsMediator requires fs: FileStore provides writer: AppendWriter {
  provide writer {
    fn append(path_under_dir, data) -> Int {
      %s
    }
  }
}
"""

_VALID = "emit fs.write(path_under_dir, data)\n      return 0"
_OTHER_VALID = 'emit fs.write("other/", data)\n      return 1'
_BROKEN = "emit nope.write(path_under_dir, data)\n      return 0"
_NOPE = "`nope` is not a declared requirement of FsMediator"


def _provider(body: str) -> str:
    return 'use "svc.rvl" { FileStore, AppendWriter }\n\n' + _MEDIATOR % body


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    def _boom(abs_path: str) -> str:
        raise AssertionError(f"read {abs_path} from disk")
    monkeypatch.setattr(_compiler, "_read_module_text", _boom)


def _compile(tmp_path: Path, roots: list[str], modules: dict[str, str]) -> dict:
    sources = {str(tmp_path / name): text for name, text in modules.items()}
    return compile_files([str(tmp_path / name) for name in roots], sources=sources)


def _component(ir: dict, name: str) -> str:
    [comp] = [c for c in ir["components"] if c["name"] == name]
    return json.dumps(comp, sort_keys=True)


def _consumer_only(tmp_path: Path, **extra: str) -> dict:
    modules = {"svc.rvl": _SERVICES, "consumer.rvl": _CONSUMER, **extra}
    return _compile(tmp_path, ["consumer.rvl"], modules)


def _with_provider(tmp_path: Path, body: str) -> dict:
    modules = {"svc.rvl": _SERVICES, "consumer.rvl": _CONSUMER,
               "mediator.rvl": _provider(body)}
    return _compile(tmp_path, ["consumer.rvl", "mediator.rvl"], modules)


def test_a_consumer_compiles_with_no_provider(tmp_path):
    ir = _consumer_only(tmp_path)
    assert [c["name"] for c in ir["components"]] == ["NotesConsumer"]
    assert {"AppendWriter", "Notes"} <= set(ir["services"])


def test_b_consumer_ir_is_identical_across_provider_bodies(tmp_path):
    absent = _consumer_only(tmp_path)
    valid = _with_provider(tmp_path, _VALID)
    other = _with_provider(tmp_path, _OTHER_VALID)

    # The two provider bodies really differ, so the equality below is not vacuous.
    assert _component(valid, "FsMediator") != _component(other, "FsMediator")

    consumer = _component(absent, "NotesConsumer")
    assert _component(valid, "NotesConsumer") == consumer
    assert _component(other, "NotesConsumer") == consumer

    # A provider body never reaches the services section.
    assert json.dumps(valid["services"], sort_keys=True) == \
        json.dumps(other["services"], sort_keys=True)
    # With the provider absent, the services the consumer reaches are the same
    # bytes; `FileStore` joins only because the provider module imports it.
    for name in ("AppendWriter", "Notes"):
        assert json.dumps(absent["services"][name], sort_keys=True) == \
            json.dumps(valid["services"][name], sort_keys=True)
    assert set(valid["services"]) - set(absent["services"]) == {"FileStore"}


def test_c_broken_provider_in_a_module_never_used_is_never_loaded(tmp_path):
    # The body is really broken: as a root, the provider module is refused.
    with pytest.raises(RevlError, match=_NOPE):
        _compile(tmp_path, ["mediator.rvl"],
                 {"svc.rvl": _SERVICES, "mediator.rvl": _provider(_BROKEN)})

    ir = _consumer_only(tmp_path, **{"mediator.rvl": _provider(_BROKEN)})
    assert _component(ir, "NotesConsumer") == \
        _component(_consumer_only(tmp_path), "NotesConsumer")


_SERVICES_NO_NOTES = _SERVICES.split("service Notes")[0]
_CONSUMER_OWN_NOTES = _CONSUMER.replace(
    'use "svc.rvl" { AppendWriter, Notes }\n',
    'use "svc.rvl" { AppendWriter }\n\n'
    "service Notes {\n  emission fn save(data: Str) -> Int\n}\n")


@pytest.mark.parametrize("services, consumer", [
    pytest.param(_SERVICES, _CONSUMER, id="use-AppendWriter-and-Notes"),
    pytest.param(_SERVICES_NO_NOTES, _CONSUMER_OWN_NOTES, id="use-AppendWriter"),
])
def test_d_broken_provider_beside_the_used_services_leaves_the_consumer_green(
        tmp_path, services, consumer):
    same_module = services + "\n" + _MEDIATOR % _BROKEN
    modules = {"svc.rvl": same_module, "consumer.rvl": consumer}

    ir = _compile(tmp_path, ["consumer.rvl"], modules)
    assert [c["name"] for c in ir["components"]] == ["NotesConsumer"]

    # The refusal belongs to a compile that makes the module a root.
    with pytest.raises(RevlError, match=_NOPE):
        _compile(tmp_path, ["consumer.rvl", "svc.rvl"], modules)
