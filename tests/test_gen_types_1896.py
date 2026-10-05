"""`revl gen-types`: a typed model document becomes revl types and a service
signature, with the model's sha256 in the header, checked by the compiler
(issue #1896, docs/gen-types.md).

The decision's exit tests, each below:
  * the same model gives identical bytes (a golden test);
  * one wrong field type in the fixture is refused, naming the field;
  * a stale header digest is refused;
  * a dependent module admitted against v1 fails against v2 with a type
    diagnostic.
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

from revl.audit_diff import audit_report  # noqa: E402
from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.gen_types import gen_types_file, load_model  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "gen_types"
MODEL = FIXTURES / "rentals.model.json"
# not `.rvl`, so the golden is not itself a corpus document
GOLDEN = FIXTURES / "rentals.types.golden"


@pytest.fixture
def project(tmp_path):
    """The fixture model in a scratch directory, generated beside it."""
    shutil.copy(MODEL, tmp_path / "rentals.model.json")
    return tmp_path


def _generate(project: Path) -> Path:
    out = project / "types.rvl"
    out.write_text(gen_types_file(str(project / "rentals.model.json"), str(out)),
                   encoding="utf-8")
    return out


def _edit_model(project: Path, edit) -> None:
    path = project / "rentals.model.json"
    model = json.loads(path.read_text(encoding="utf-8"))
    edit(model)
    path.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")


def _field(model: dict, entity: str, field: str) -> dict:
    ent = next(e for e in model["entities"] if e["name"] == entity)
    return next(f for f in ent["fields"] if f["name"] == field)


def _refusal(fn, *args, **kwargs) -> RevlError:
    with pytest.raises(RevlError) as caught:
        fn(*args, **kwargs)
    return caught.value


# ------------------------------------------------ generation


def test_the_same_model_gives_the_golden_bytes(project):
    first = _generate(project).read_text(encoding="utf-8")
    second = _generate(project).read_text(encoding="utf-8")
    assert first == second == GOLDEN.read_text(encoding="utf-8")


def test_the_generated_file_compiles_and_carries_its_digest(project):
    ir = compile_files([str(_generate(project))])
    assert set(ir["types"]) == {"CustomerKey", "Customer", "OrderKey", "Order",
                                "OrderLineKey", "OrderLine"}
    assert set(ir["services"]["RentalsData"]["methods"]) >= {
        "get_customer", "list_customer", "put_customer", "delete_customer"}
    import hashlib
    digest = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    row = {"file": "types.rvl", "model": "rentals.model.json", "sha256": digest}
    assert ir["generated_from"] == [row]
    assert audit_report(ir)["generated_from"] == [row]


def test_a_wrong_field_type_is_refused_naming_the_field(project):
    _edit_model(project, lambda m: _field(m, "Order", "total").update(type="Decimal"))
    error = _refusal(gen_types_file, str(project / "rentals.model.json"))
    assert "entity `Order`, field `total`: type 'Decimal' is not a model field type" \
        in error.message
    assert "/entities/1/fields/1/type" in error.message
    assert error.line > 0


@pytest.mark.parametrize("edit, says", [
    (lambda m: _field(m, "Order", "paid").update(optinal=True),
     "has an unknown key `optinal`"),
    (lambda m: m["entities"][1]["relations"][0].update(to="Shop"),
     "`Shop` is not an entity of this model"),
    (lambda m: m["entities"][0].update(key=["email"]),
     "key field `email` may not be optional or a list"),
    (lambda m: m["entities"][0]["fields"][1].update(name="match"),
     "`match` is a revl keyword"),
    (lambda m: m.update(revl_model=2), "`revl_model` must be 1"),
    (lambda m: m["entities"][2].update(name="OrderKey"),
     "which another name in the model already takes"),
])
def test_an_ill_formed_model_is_refused_by_name(project, edit, says):
    _edit_model(project, edit)
    assert says in _refusal(gen_types_file, str(project / "rentals.model.json")).message


def test_a_model_outside_the_output_directory_is_refused(project):
    sub = project / "out"
    sub.mkdir()
    error = _refusal(gen_types_file, str(project / "rentals.model.json"),
                     str(sub / "types.rvl"))
    assert "outside the directory the generated file is written to" in error.message


# ------------------------------------------------ the compiler's check


def test_a_stale_header_digest_is_refused(project):
    types = _generate(project)
    _edit_model(project, lambda m: _field(m, "Order", "total").update(type="Int"))
    error = _refusal(compile_files, [str(types)])
    assert "was generated from `rentals.model.json`" in error.message
    assert "has changed since" in error.message
    assert error.category == "admission"


def test_a_missing_model_is_refused(project):
    types = _generate(project)
    (project / "rentals.model.json").unlink()
    assert "cannot be read" in _refusal(compile_files, [str(types)]).message


@pytest.mark.parametrize("change, says", [
    (lambda text: text.replace("// Model document: rentals.model.json",
                               "// Model document: ../rentals.model.json"),
     "outside its own directory"),
    (lambda text: text.replace("// Model sha256: ", "// Model digest: "),
     "does not carry its `Model document:` and `Model sha256:` lines"),
])
def test_a_tampered_header_is_refused(project, change, says):
    types = _generate(project)
    types.write_text(change(types.read_text(encoding="utf-8")), encoding="utf-8")
    assert says in _refusal(compile_files, [str(types)]).message


def test_an_in_memory_compile_reads_the_model_only_from_the_sources_map(project):
    types = _generate(project)
    text = types.read_text(encoding="utf-8")
    model = (project / "rentals.model.json").read_text(encoding="utf-8")
    ir = compile_source(text, str(types), modules={
        str(project / "rentals.model.json"): model})
    assert ir["generated_from"][0]["model"] == "rentals.model.json"
    error = _refusal(compile_source, text, str(types), modules={str(types): text})
    assert "not in the in-memory sources map" in error.message
    assert "needs `modules=`" in _refusal(compile_source, text, "types.rvl").message


def test_a_file_without_the_header_is_untouched(project):
    plain = project / "plain.rvl"
    plain.write_text("pub type T = { a: Int }\n", encoding="utf-8")
    assert "generated_from" not in compile_files([str(plain)])


# ------------------------------------------------ a dependent module


DEPENDENT = ('use "types.rvl" { Order }\n'
             "pub fn settled(o: Order) -> Bool { return o.paid }\n")


def test_a_dependent_module_admitted_on_v1_fails_on_v2_with_a_type_diagnostic(project):
    _generate(project)
    dependent = project / "billing.rvl"
    dependent.write_text(DEPENDENT, encoding="utf-8")
    compile_files([str(dependent)])                     # v1: admitted
    _edit_model(project, lambda m: _field(m, "Order", "paid").update(type="Str"))
    _generate(project)                                  # v2, regenerated
    error = _refusal(compile_files, [str(dependent)])
    assert classify(error)["code"] == "T1", classify(error)
    assert "Bool" in error.message and "Str" in error.message


# ------------------------------------------------ the command


def test_the_command_writes_the_file_and_names_a_refusal(project):
    run = subprocess.run([sys.executable, "-m", "revl", "gen-types",
                          "rentals.model.json", "-o", "types.rvl"],
                         cwd=project, capture_output=True, text=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})
    assert run.returncode == 0, run.stderr
    assert (project / "types.rvl").read_text(encoding="utf-8") \
        == GOLDEN.read_text(encoding="utf-8")
    _edit_model(project, lambda m: _field(m, "Order", "paid").update(type=3))
    run = subprocess.run([sys.executable, "-m", "revl", "gen-types",
                          "rentals.model.json", "--json-diagnostics"],
                         cwd=project, capture_output=True, text=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})
    assert run.returncode == 1
    diagnostic = json.loads(run.stdout)["diagnostics"][0]
    assert "entity `Order`, field `paid`" in diagnostic["message"]


def test_the_loader_reads_the_fixture():
    model = load_model(MODEL.read_bytes(), filename=str(MODEL))
    assert [e["name"] for e in model.entities] == ["Customer", "Order", "OrderLine"]
    assert model.locale == "fr-FR" and model.service == "RentalsData"
