"""A grant on a confined stack-layer row is key-precise (issue #1926).

Follow-up to issue #1921 (PR #1925), which let the operator `grant` a confined
stack-layer row its reach.

WHAT WAS WRONG. The confinement profile map is per SOURCE FILE:
`composition._row_profiles` mapped a row's source path to
`AdmissionProfile.untrusted_author(<granted services>)`, and
`admit_profile.check_allowlist` tested every component compiled from that file
against that SERVICE-name set. Measured on #1925's head (af01be94e):

- A second component in the granted row's file, requiring the same service,
  was admitted under the row's grant, whether it required it under the granted
  key or under another key. The key-subset check (`_require_granted`) looks
  only at the row's own component header, so the sibling borrowed the grant.
- Two confined rows reading one file (`component <Name>` picks each one) each
  wrote that file's single profile, so the second row's grant replaced the
  first: `KitA`, granted `approvals`, was refused under `KitB`'s grant.

WHAT HOLDS NOW. Each confined row contributes the `(component, requires key)`
pairs its grant names (`AdmissionProfile.granted_requires`), and a granted
service covers a requirement only for those pairs. A component no grant names
is refused, naming its key, unless the turn provides that key itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_composition_confinement as conf  # noqa: E402
import test_truc_apply_stack_check as stack  # noqa: E402
from revl import RevlError  # noqa: E402
from revl.admit_profile import AdmissionProfile, check_allowlist  # noqa: E402
from revl.composition import _row_profiles, admit_composition, resolve_file  # noqa: E402

GRANT_BOTH = "  grant @records with { approvals, writer }\n"

#: the row picks its component out of a file that holds more than one.
NAMED_LAYER = conf._RECORDS_LAYER.replace(
    "provides records", "provides records component RecordsKit")


def _sibling(key: str) -> str:
    """A second component in the row's source file, requiring the granted
    service under `key`. It is in no row, so no grant names it."""
    return (f"component Sneak requires {key}: ApprovalGate {{\n"
            "}\n")


def _project(tmp_path: Path, *, sibling: str = "", site: str = GRANT_BOTH) -> Path:
    return conf._grant_project(tmp_path, kit=conf._RECORDS_KIT + sibling,
                               layer=NAMED_LAYER, site=site)


def _admit(doc: Path, root: Path, full: bool) -> dict:
    return admit_composition(str(doc), str(root), confine=True, full=full)


# ------------------------------------------------------------ the exit tests


@pytest.mark.parametrize("full", [False, True], ids=["delta", "full"])
@pytest.mark.parametrize("key", ["approvals", "gate"])
def test_a_sibling_component_is_refused_naming_the_service_and_its_key(tmp_path, key, full):
    """Exit test 1. Two components in one confined file, only the row's one
    granted: the other is refused, naming the service and its own key, whether
    it requires the service under the granted key or another one."""
    doc = _project(tmp_path, sibling=_sibling(key))
    with pytest.raises(RevlError) as exc:
        _admit(doc, tmp_path, full)
    msg = str(exc.value)
    assert "component `Sneak` reaches service `ApprovalGate`" in msg
    assert f"via `requires {key}`" in msg and f"`Sneak.{key}`" in msg
    assert "`RecordsKit.approvals`" in msg   # who the service IS granted to
    assert getattr(exc.value, "code", None) == "R2"


@pytest.mark.parametrize("full", [False, True], ids=["delta", "full"])
def test_the_granted_component_still_admits_beside_an_inert_sibling(tmp_path, full):
    """Exit test 2. The granted row's own component admits, and so does a
    sibling that reaches nothing."""
    doc = _project(tmp_path, sibling="component Idle {\n}\n")
    names = {c["name"] for c in _admit(doc, tmp_path, full)["components"]}
    assert {"RecordsKit", "Idle"} <= names


def test_the_profile_grants_the_rows_own_component_and_keys(tmp_path):
    doc = _project(tmp_path)
    profile = _row_profiles(resolve_file(str(doc), str(tmp_path)),
                            str(tmp_path))[str(tmp_path / "records_kit.rvl")]
    assert profile.granted == frozenset({"ApprovalGate", "SourceWriter"})
    assert profile.granted_requires == frozenset(
        {("RecordsKit", "approvals"), ("RecordsKit", "writer")})
    assert profile == AdmissionProfile.untrusted_author(
        profile.granted, requires=profile.granted_requires)


def test_a_partial_grant_is_still_refused(tmp_path):
    """Exit test 3. Only `approvals` granted: the row is refused naming
    `writer`, as #1925 does, with or without a sibling in the file."""
    for sibling in ("", _sibling("approvals")):
        doc = _project(tmp_path / str(len(sibling)), sibling=sibling,
                       site="  grant @records with { approvals }\n")
        with pytest.raises(RevlError) as exc:
            _admit(doc, tmp_path / str(len(sibling)), False)
        msg = str(exc.value)
        assert "requires `writer`" in msg and "its grant does not list" in msg


# ------------------------------------------------- two rows, one source file

_TWO_KITS = """
use "services.rvl" { }
component KitA requires approvals: ApprovalGate provides ra: Records {
  provide ra { fn put(x) = x }
}
component KitB requires writer: SourceWriter provides rb: Records {
  provide rb { fn put(x) = writer.write(x) }
}
"""

_TWO_ROWS = """
layer RecordsKitLayer for Demo {
  add row @a from "../records_kit.rvl" provides ra component KitA
  add row @b from "../records_kit.rvl" provides rb component KitB
}
"""


@pytest.mark.parametrize("full", [False, True], ids=["delta", "full"])
def test_two_rows_of_one_file_each_keep_their_own_grant(tmp_path, full):
    """The per-file map held one profile, so the second row's grant replaced
    the first and `KitA` was refused under `KitB`'s grant."""
    doc = conf._grant_project(
        tmp_path, kit=_TWO_KITS, layer=_TWO_ROWS,
        site="  grant @a with { approvals }\n  grant @b with { writer }\n")
    names = {c["name"] for c in _admit(doc, tmp_path, full)["components"]}
    assert {"KitA", "KitB"} <= names


def test_two_rows_of_one_file_do_not_share_their_grants(tmp_path):
    """Each row's grant still covers only its own component: `KitB` requiring
    `ApprovalGate` is not covered by `KitA`'s grant of it."""
    kits = _TWO_KITS.replace(
        "component KitB requires writer: SourceWriter",
        "component KitB requires writer: SourceWriter, approvals: ApprovalGate")
    doc = conf._grant_project(
        tmp_path, kit=kits, layer=_TWO_ROWS,
        site="  grant @a with { approvals }\n  grant @b with { writer }\n")
    with pytest.raises(RevlError) as exc:
        _admit(doc, tmp_path, False)
    # `_require_granted` refuses it first: the row's own header lists the key
    assert "requires `approvals`" in str(exc.value)


# ------------------------------------------------------------- truc apply


def test_truc_apply_refuses_the_sibling_naming_its_key(tmp_path):
    """The issue's own test, through `truc apply`: the operator grants the
    stack row both keys, and a sibling in the vendored source that requires
    `ApprovalGate` under another key is refused, naming that key."""
    proj = stack._records_project(
        tmp_path, site="  grant records_kit::@records with { approvals, writer }\n")
    kit = proj / "trucs" / "records_kit"
    (kit / "component.rvl").write_text(
        (kit / "component.rvl").read_text() + _sibling("gate"))
    (kit / "layer.rvl").write_text((kit / "layer.rvl").read_text().replace(
        "provides records", "provides records component RecordsKit"))
    report = json.loads(stack._host.apply(str(proj), False))
    assert report["code"] == 1
    assert "`Sneak.gate`" in report["message"]
    assert "component `Sneak` reaches service `ApprovalGate`" in report["message"]


# --------------------------------------------------- the allowlist, directly


def _document(*components: dict) -> dict:
    return {"filename": "c.rvl", "components": list(components)}


def test_a_service_name_grant_is_unchanged_when_no_keys_are_given():
    """Every other door builds `untrusted_author(granted)` with no keys and
    keeps the per-service allowlist."""
    profile = AdmissionProfile.untrusted_author({"ApprovalGate"})
    assert profile.granted_requires is None
    check_allowlist(_document(
        {"name": "A", "requires": {"x": "ApprovalGate"}},
        {"name": "B", "requires": {"y": "ApprovalGate"}}), profile)


def test_a_key_precise_grant_covers_only_the_named_pair():
    profile = AdmissionProfile.untrusted_author({"ApprovalGate"},
                                                requires={("A", "x")})
    check_allowlist(_document({"name": "A", "requires": {"x": "ApprovalGate"}}),
                    profile)
    for name, key in (("B", "x"), ("A", "y")):
        with pytest.raises(RevlError) as exc:
            check_allowlist(_document(
                {"name": name, "requires": {key: "ApprovalGate"}}), profile)
        assert f"no grant names `{name}.{key}`" in str(exc.value)


def test_a_key_precise_grant_still_refuses_an_ungranted_service():
    profile = AdmissionProfile.untrusted_author({"ApprovalGate"},
                                                requires={("A", "x")})
    with pytest.raises(RevlError) as exc:
        check_allowlist(_document({"name": "A", "requires": {"w": "SourceWriter"}}),
                        profile)
    assert "not in the granted set" in str(exc.value)
