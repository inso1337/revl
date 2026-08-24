"""Derived semantic versioning (roadmap item 64).

The version number is computed from the drift classification, never chosen:

  * an additive interface change (a method/service added, a parameter widened,
    a return narrowed) computes MINOR;
  * a removed or narrowed operation computes MAJOR;
  * an operation that GAINS an emission computes MAJOR even when its shape is
    compatible (a consumer's G8 audit changes meaning);
  * an operation that LOSES an emission computes MINOR (strictly purer);
  * an identical interface computes PATCH (no bump).

The classification is the real drift predicate's, not a second copy: the
cross-check test proves `version.derive`'s verdict for every shared operation
matches what `admission._service_compatible` reports directly (the same probe
item 49 used to bind admission to the predicate).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.lower import _service_compatible, _service_from_ir  # noqa: E402
from revl.parser import ServiceDecl  # noqa: E402
from revl.version import derive, diff_services  # noqa: E402


# A provider whose service is `Store`; every variant keeps a valid provider so
# the composition compiles — only the service *declaration* is what the version
# diff reads.
def _composition(service_body: str, extra_method: str = "") -> str:
    return f"""
service Store {{
{service_body}
}}
component Impl provides s: Store {{
  let store = effect Map.new() undo store.drop()
  provide s {{
    fn get(key) = store.get(key)
    fn write(key, value) {{
      effect store.insert(key, value)
      undo   store.remove(key)
    }}
{extra_method}
  }}
}}
"""


BASE = _composition(
    "  fn get(key: Str) -> Str\n"
    "  emission fn write(key: Str, value: Str)")


def _bump(current_src: str, previous_src: str = BASE,
          version: str | None = None) -> dict:
    return derive(compile_source(previous_src), compile_source(current_src),
                  previous_version=version)


# ------------------------------------------------------ the five bump rules

def test_additive_method_is_minor():
    current = _composition(
        "  fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)\n"
        "  fn size() -> Int",
        extra_method="    fn size() = 0")
    result = _bump(current)
    assert result["bump"] == "minor"
    added = [c for c in result["changes"] if c["method"] == "size"]
    assert added and added[0]["kind"] == "added" and added[0]["bump"] == "minor"


def test_new_service_is_minor():
    current = BASE + """
service Extra { fn ping() -> Int }
component Pinger provides e: Extra {
  provide e { fn ping() = 1 }
}
"""
    result = _bump(current)
    assert result["bump"] == "minor"
    assert any(c["kind"] == "service-added" and c["service"] == "Extra"
               for c in result["changes"])


def test_removed_op_is_major():
    current = _composition("  emission fn write(key: Str, value: Str)")
    # a provider must still implement only what the service declares
    current = current.replace(
        "    fn get(key) = store.get(key)\n", "")
    result = _bump(current)
    assert result["bump"] == "major"
    removed = [c for c in result["changes"] if c["method"] == "get"]
    assert removed and removed[0]["kind"] == "removed"


def test_narrowed_param_is_major():
    # `get` parameter narrows Str -> Nat: a value a consumer passed no longer
    # fits (contravariant), so the drift predicate flags it, so it is major.
    current = _composition(
        "  fn get(key: Nat) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    result = _bump(current)
    assert result["bump"] == "major"
    change = next(c for c in result["changes"] if c["method"] == "get")
    assert change["kind"] == "signature" and change["bump"] == "major"


def test_gains_emission_is_major_even_when_shape_compatible():
    # `get` keeps the exact same signature but becomes an `emission`: the shape
    # is compatible, yet a consumer's unmarked call site would silently cross
    # the boundary, so the capability change alone forces major.
    current = _composition(
        "  emission fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    result = _bump(current)
    assert result["bump"] == "major"
    change = next(c for c in result["changes"] if c["method"] == "get")
    assert change["kind"] == "emission" and change["bump"] == "major"


def test_loses_emission_is_minor():
    # `write` drops its `emission`: strictly purer. The drift predicate does
    # not flag it as a break (safe for a consumer), but versioning records the
    # narrowed authority as a minor bump.
    current = _composition(
        "  fn get(key: Str) -> Str\n"
        "  fn write(key: Str, value: Str)")
    result = _bump(current)
    assert result["bump"] == "minor"
    change = next(c for c in result["changes"] if c["method"] == "write")
    assert change["kind"] == "emission-loss" and change["bump"] == "minor"


def test_identical_is_patch_no_changes():
    result = _bump(BASE, version="1.4.2")
    assert result["bump"] == "patch"
    assert result["changes"] == []
    assert result["nextVersion"] == "1.4.3"


# ------------------------------------------------- computed next version

@pytest.mark.parametrize("current, previous_version, expected", [
    (_composition("  fn get(key: Str) -> Str\n"
                  "  emission fn write(key: Str, value: Str)\n"
                  "  fn size() -> Int", "    fn size() = 0"), "1.4.2", "1.5.0"),
    (_composition("  emission fn get(key: Str) -> Str\n"
                  "  emission fn write(key: Str, value: Str)"), "1.4.2", "2.0.0"),
])
def test_next_version_is_computed(current, previous_version, expected):
    result = _bump(current, version=previous_version)
    assert result["nextVersion"] == expected


# ------------------------------------- cross-check against the real predicate

def test_derive_agrees_with_the_real_drift_predicate():
    """For every operation shared by two service tables, `derive`'s verdict is
    exactly the real predicate's: a reported drift is major, its absence is
    compatible (minor or, if identical, no change). This is the item-49 probe,
    pointed at versioning: the classification is reused, not re-implemented."""
    current = _composition(
        "  emission fn get(key: Str) -> Str\n"       # gains emission (major)
        "  fn write(key: Str, value: Str)\n"          # loses emission (minor)
        "  fn size() -> Int",                          # added (minor)
        extra_method="    fn size() = 0")
    prev_ir = compile_source(BASE)
    cur_ir = compile_source(current)
    changes = {(c.service, c.method): c
               for c in diff_services(prev_ir["services"], cur_ir["services"])}

    old = prev_ir["services"]["Store"]
    new = cur_ir["services"]["Store"]
    old_svc = _service_from_ir("Store", old)
    new_svc = _service_from_ir("Store", new)
    shared = set(old_svc.methods) & set(new_svc.methods)
    for method in shared:
        one_old = ServiceDecl("Store", {method: old_svc.methods[method]}, 0)
        one_new = ServiceDecl("Store", {method: new_svc.methods[method]}, 0)
        drift = _service_compatible(one_new, one_old, providers_retained=False)
        change = changes.get(("Store", method))
        if drift is not None:
            # a break the predicate reports must surface as a major change
            assert change is not None and change.bump == "major"
        else:
            # no break: either an emission-loss / compatible minor, or no change
            if change is not None:
                assert change.bump == "minor"


# ------------------------------------------------------------ the CLI

def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def test_cli_emit_manifest_round_trips(tmp_path, capsys):
    prev = _write(tmp_path, "prev.rvl", BASE)
    assert main(["version", str(prev), "--emit-manifest"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "services" in doc and "Store" in doc["services"]


def test_cli_major_human_and_json(tmp_path, capsys):
    prev = _write(tmp_path, "prev.rvl", BASE)
    assert main(["version", str(prev), "--emit-manifest"]) == 0
    prev_json = tmp_path / "prev.json"
    prev_json.write_text(capsys.readouterr().out)

    current = _write(tmp_path, "cur.rvl", _composition(
        "  emission fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)"))

    assert main(["version", str(current), "--against", str(prev_json),
                 "--current-version", "2.3.1"]) == 0
    human = capsys.readouterr().out
    assert "MAJOR" in human and "2.3.1 -> 3.0.0" in human
    assert "becomes an `emission`" in human

    assert main(["version", str(current), "--against", str(prev_json),
                 "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["bump"] == "major"


def test_cli_minor_additive_and_emission_loss(tmp_path, capsys):
    prev = _write(tmp_path, "prev.rvl", BASE)
    assert main(["version", str(prev), "--emit-manifest"]) == 0
    prev_json = tmp_path / "prev.json"
    prev_json.write_text(capsys.readouterr().out)

    current = _write(tmp_path, "cur.rvl", _composition(
        "  fn get(key: Str) -> Str\n"
        "  fn write(key: Str, value: Str)\n"           # loses emission
        "  fn size() -> Int",                           # additive
        extra_method="    fn size() = 0"))

    assert main(["version", str(current), "--against", str(prev_json),
                 "--current-version", "1.4.2"]) == 0
    human = capsys.readouterr().out
    assert "MINOR" in human and "1.4.2 -> 1.5.0" in human


def test_cli_audit_doc_is_rejected_clearly(tmp_path, capsys):
    prev = _write(tmp_path, "prev.rvl", BASE)
    assert main(["audit", str(prev), "--json"]) == 0
    audit_json = tmp_path / "audit.json"
    audit_json.write_text(capsys.readouterr().out)

    current = _write(tmp_path, "cur.rvl", BASE)
    assert main(["version", str(current), "--against", str(audit_json)]) == 1
    err = capsys.readouterr().err
    assert "no `services` table" in err


def test_cli_needs_against_or_emit_manifest(tmp_path, capsys):
    prev = _write(tmp_path, "prev.rvl", BASE)
    assert main(["version", str(prev)]) == 2
    assert "--against" in capsys.readouterr().err
