"""Item 296, slice 1: proposed-not-silent synthesis. `render_adapter` produces
ordinary `.rvl` source (the section-4 artifact / `revl adapt --emit`); the
committed declaration re-admits through the UNMODIFIED gate (carry-over is a
general wiring feature, not an adapter special case).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.parser import MethodDecl, ServiceDecl  # noqa: E402
from revl.adapt import bridge_plan, render_adapter, derivation_hash  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

_TYPES_AND_SERVICES = """
type Options = { seed: Opt[Str] }
type Error = { code: Str }
service VendorCache { emission[cache] fn get(key: Str, options: Options) -> Result[Str, Error] }
service Cache { emission[cache] fn get(key: Str) -> Opt[Str] }
"""


def _decls():
    req = ServiceDecl("Cache", {"get": MethodDecl(
        "get", [("key", "Str")], "Opt[Str]", True, 0, capabilities=("cache",))},
        0)
    prov = ServiceDecl("VendorCache", {"get": MethodDecl(
        "get", [("key", "Str"), ("options", "Options")], "Result[Str, Error]",
        True, 0, capabilities=("cache",))}, 0)
    return req, prov


def test_flagship_synthesized_source_readmits():
    req, prov = _decls()
    prov_types = {"Options": {"kind": "record",
                              "fields": {"seed": "Opt[Str]"}},
                  "Error": {"kind": "record", "fields": {"code": "Str"}}}
    opt = {"get": {"return": {"merge": "total"}}}
    plan = bridge_plan(req, prov, opt, prov_types=prov_types)
    assert plan.ok, plan.refusals

    src = render_adapter("CacheAdapter", req, prov, opt,
                         provide_key="cache", require_key="backing",
                         carried_tokens=("cache",), prov_types=prov_types)
    # the rendered source shows the bridge (auditable): the merge and the
    # carrying(...) alias are visible.
    assert "carrying(cache)" in src
    assert "Err(_) => None" in src

    # committed -> re-admits through the ordinary gate
    ir = compile_source(_TYPES_AND_SERVICES + src, "committed.rvl")
    assert "CacheAdapter" in {c["name"] for c in ir["components"]}


def test_cli_adapt_check_and_refuse(tmp_path, capsys):
    from revl.__main__ import main
    need = tmp_path / "need.rvl"
    need.write_text(
        "service Cache { emission[cache] fn get(key: Str) -> Opt[Str] }\n")
    cand = tmp_path / "cand.rvl"
    cand.write_text(
        "type Options = { seed: Opt[Str] }\ntype Error = { code: Str }\n"
        "service VendorCache { emission[cache] fn get(key: Str, options: "
        "Options) -> Result[Str, Error] }\n")
    d = tmp_path / "d.json"
    d.write_text('{"get": {"return": {"merge": "total"}}}')

    # compatible-with-adapter, with the rendered artifact
    rc = main(["adapt", str(need), str(cand), "--adapt", str(d), "--emit",
               "--name", "CacheAdapter"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"verdict": "compatible-with-adapter"' in out
    assert "carrying(cache)" in out

    # without the opt-in: a named refusal, nonzero exit
    rc2 = main(["adapt", str(need), str(cand)])
    out2 = capsys.readouterr().out
    assert rc2 == 1
    assert "outcome-merge" in out2


def test_derivation_hash_is_stable_and_sensitive():
    a = derivation_hash("R", "P", "sha1", "adapt-x")
    assert a == derivation_hash("R", "P", "sha1", "adapt-x")
    assert a != derivation_hash("R", "P", "sha2", "adapt-x")   # candidate moved
    assert a != derivation_hash("R", "P", "sha1", "adapt-y")   # decl changed
