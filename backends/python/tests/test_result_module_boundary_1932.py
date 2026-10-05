"""Issue #1932: a ``Result`` crossing a MODULE boundary must still ``match``.

The emitter used to write ``class Ok`` / ``class Err`` into EVERY emitted
module.  Each module is its own :class:`types.ModuleType`, so the consumer's
``match`` tested the value against *its own* ``Ok`` — no arm matched and the
lowered match raised ``TypeError: non-exhaustive match``.  The classes now have
one definition, in this backend's ``runtime.py``, and every module imports them
(``from runtime import Err, …, Ok``), so the producer's value IS the consumer's
class.

This is the execution-level proof: two independently emitted modules, the
second admitted against the first one's signature and run against the first
one's live service object.
"""

from __future__ import annotations

import pathlib
import sys

import emit
import runtime as runtime_mod
from conftest import load_module

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

BOOT = """
service Answers { fn get() -> Result[Str, Str] }

component AnswersKit provides answers: Answers {
  provide answers {
    fn get() = Ok("forty-two")
  }
}
"""

BOOT_ERR = """
service Answers { fn get() -> Result[Str, Str] }

component AnswersKit provides answers: Answers {
  provide answers {
    fn get() = Err("no answer")
  }
}
"""

SHOW = """
service Answers { fn get() -> Result[Str, Str] }

service Show { fn panel() -> Str }

component ShowKit requires answers: Answers provides show: Show {
  provide show {
    fn panel() {
      return match answers.get() {
        Ok(v) => "<p>" + v + "</p>",
        Err(e) => "<p>error " + e + "</p>",
      }
    }
  }
}
"""


class _Ctx:
    """The sliver of a cordis ``Context`` an emitted body uses: ``effect`` to
    run the activation generator, ``provide``/``set`` for the provided key,
    and attribute access for the injected services.  (The real ``cordis`` is
    not a dependency of this backend's test env.)"""

    def __init__(self, **services) -> None:
        self._keys: dict = {}
        for name, value in services.items():
            setattr(self, name, value)

    def effect(self, fn, label=None):
        return list(fn())

    def provide(self, key):
        return None

    def set(self, key, value):
        self._keys[key] = value

    def get(self, key):
        return self._keys[key]


def _modules(producer_source: str = BOOT):
    """Compile, emit and exec both modules; return the two module objects and
    the producer's live service object."""
    producer_ir = compile_source(producer_source, "boot.rvl")
    consumer_ir = compile_source(SHOW, "show.rvl", manifest=producer_ir)

    producer = load_module(emit.emit(producer_ir), "boot_module")
    consumer = load_module(emit.emit(consumer_ir), "show_module")

    ctx = _Ctx()
    producer.AnswersKit["apply"](ctx, {})
    return producer, consumer, ctx.get("answers")


def _panel(consumer, answers) -> str:
    ctx = _Ctx(answers=answers)
    consumer.ShowKit["apply"](ctx, {})
    return ctx.get("show").panel()


def test_ok_crosses_the_module_boundary_and_matches_in_the_consumer():
    producer, consumer, answers = _modules()

    # asserted first, so the failure mode without the fix IS the reported one:
    # TypeError: non-exhaustive match
    assert _panel(consumer, answers) == "<p>forty-two</p>"

    value = answers.get()
    assert type(value) is runtime_mod.Ok
    assert isinstance(value, consumer.Ok)
    assert consumer.Ok is producer.Ok is runtime_mod.Ok


def test_err_crosses_the_module_boundary_and_matches_in_the_consumer():
    producer, consumer, answers = _modules(BOOT_ERR)

    assert _panel(consumer, answers) == "<p>error no answer</p>"

    value = answers.get()
    assert type(value) is runtime_mod.Err
    assert isinstance(value, consumer.Err)
    assert consumer.Err is producer.Err is runtime_mod.Err


def test_module_imports_the_shared_cases_instead_of_defining_them():
    """The unit-level form of the same rule: no module re-defines the builtin
    sum cases, and every module that mentions them imports them from the one
    runtime."""
    for source in (BOOT, BOOT_ERR, SHOW):
        text = emit.emit(compile_source(source, "module.rvl"))
        assert "class Ok:" not in text
        assert "class Err:" not in text
        runtime_import = next(line for line in text.splitlines()
                              if line.startswith("from runtime import"))
        assert {"Ok", "Err"} <= set(runtime_import.split(" import ", 1)[1].split(", "))
