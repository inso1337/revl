"""An untrusted swap sent as an export-area file may not delete a requirer's
realm placement (issue #2600).

A file inside an export directory is the author's text by its path (issue
#2524): `revl_swap` compiles it under the authoring profile, exactly as if it
had been sent inline. Issue #2587 drives the realm-removal refusal through the
swap door with inline source; this module sends the same candidate as an
export-area file in `files`, and nothing else.

The candidate keeps every running component, adds a shared-realm provider of
`kv`, and leaves out TenantAApp's clause: refused by name, the refusal
pointing at the exported file, and the running composition untouched.

A swap of operator files is exempt only while it carries no export-area text.
An operator file outside the export area that reaches an export-area module by
`use` compiles under the profile too, so the whole program is compared and the
same clause left out of the operator file is refused. Components are never
imported, so the refusal names the line in the operator file that holds it.

Controls: the same file placed by the operator outside the export area swaps
in under a trusted author and under the untrusted one when nothing it reaches
is in the export area, and an export-area file that keeps the clause swaps in
under the untrusted author.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from test_untrusted_requirer_realm_swap_2587 import (  # noqa: E402,F401
    APP_HEADER, REFUSAL, _as_untrusted_author, _call, _full_candidate,
    _line_of, _messages, _realms, _running, clean_session, loaded, pytestmark)

REMOVED = ("admission refused: this author cannot remove the realm placement "
           "of `TenantAApp`: the admitted `isolate kv in realm(\"tenant_a\")` "
           "would move its `kv` requirement to the shared realm")


def _candidate(*, keep_app_clause: bool) -> str:
    """The full candidate as the untrusted author writes it."""
    return _as_untrusted_author(_full_candidate(keep_app_clause=keep_app_clause))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _exported(base: Path, text: str) -> Path:
    """`text` as a file inside the root's export directory."""
    return _write(base / ".revl" / "export" / "tenants.rvl", text)


def _swap_files(path: Path) -> dict:
    return _call("revl_swap", {"files": [str(path)], "replacing": ["TenantAApp"]})


def test_untrusted_swap_of_an_exported_file_dropping_the_clause_is_refused(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    text = _candidate(keep_app_clause=False)
    exported = _exported(loaded.parent, text)
    swapped = _swap_files(exported)
    assert swapped["ok"] is False, swapped
    assert swapped.get("admitted") is False, swapped
    assert swapped.get("swapped") is False, swapped
    assert REFUSAL in _messages(swapped), swapped
    refusal = next(d for d in swapped["diagnostics"] if REFUSAL in d["message"])
    assert refusal["message"] == REMOVED, refusal
    assert refusal.get("code") == "G9", refusal
    # it points at the requirer, in the exported file
    assert refusal.get("file") == os.path.relpath(exported), refusal
    assert refusal.get("line") == _line_of(text, APP_HEADER), refusal
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert "SharedStore" not in _running()


def test_trusted_swap_of_the_same_file_outside_the_export_area_is_admitted(
        loaded):  # noqa: F811
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    placed = _write(loaded.parent / "candidate.rvl",
                    _candidate(keep_app_clause=False))
    swapped = _swap_files(placed)
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {}
    assert "SharedStore" in _running()


def test_untrusted_swap_of_an_exported_file_keeping_the_clause_is_admitted(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None
    exported = _exported(loaded.parent, _candidate(keep_app_clause=True))
    swapped = _swap_files(exported)
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
    assert "SharedStore" in _running()


def _operator_using_exported_service(base: Path) -> Path:
    """The candidate without TenantAApp's clause as an operator file outside
    the export area, its `Kv` service moved to an export-area module it
    reaches by `use`."""
    text = _candidate(keep_app_clause=False)
    service = text[text.index("service Kv"):text.index("component TenantAStore")]
    _write(base / ".revl" / "export" / "kv.rvl", service)
    return _write(base / "candidate.rvl",
                  'use ".revl/export/kv.rvl" { Kv }\n' + text.replace(service, ""))


def test_untrusted_swap_of_an_operator_file_reaching_exported_text_is_refused(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None
    placed = _operator_using_exported_service(loaded.parent)
    swapped = _swap_files(placed)
    assert swapped["ok"] is False, swapped
    assert swapped.get("admitted") is False, swapped
    assert swapped.get("swapped") is False, swapped
    assert REFUSAL in _messages(swapped), swapped
    refusal = next(d for d in swapped["diagnostics"] if REFUSAL in d["message"])
    assert refusal["message"] == REMOVED, refusal
    assert refusal.get("code") == "G9", refusal
    assert refusal.get("file") == os.path.relpath(placed), refusal
    assert refusal.get("line") == _line_of(
        placed.read_text(encoding="utf-8"), APP_HEADER), refusal
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert "SharedStore" not in _running()


def test_untrusted_swap_of_an_operator_file_with_no_exported_text_is_admitted(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None
    placed = _write(loaded.parent / "candidate.rvl",
                    _candidate(keep_app_clause=False))
    swapped = _swap_files(placed)
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {}
    assert "SharedStore" in _running()


def test_trusted_swap_of_an_exported_file_dropping_the_clause_is_admitted(
        loaded):  # noqa: F811
    """The exemption turns on who wrote the text, not where the file sits.

    Under a trusted author there is no profile, so an export-area file is not
    the agent's text and compiles exactly as it did before #2524. It stays the
    operator's own file and this door admits it, clause dropped or not, as it
    admitted it before #2600. Deciding by path alone would refuse this
    operator a swap that used to work, which is why the export-area part is
    read only while a profile is in play.
    """
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None  # the trusted operator
    placed = _exported(loaded.parent, _candidate(keep_app_clause=False))
    assert server_mod._export_jail.in_export_area(str(placed))  # really exported
    swapped = _swap_files(placed)
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {}
    assert "SharedStore" in _running()
