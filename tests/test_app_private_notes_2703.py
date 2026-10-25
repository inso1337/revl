"""The authorization half of the exemplary web application (issue #2703, slice 1).

examples/app/notes.rvl is the public control: its routes reach no
principal-taking service. examples/app/private_notes.rvl is the other side:
its store operations take a `Principal`, so each handler has to call
`Auth.validate` first. The checks here:

* the shipped file is admitted, and its routes bind the credential from the
  `Authorization` header;
* the same program with the `auth.validate` step removed is refused at
  admission, naming `Principal` and `Auth.validate`, one mutant per route;
* a mutant that fabricates a principal is refused the same way; and
* the public notes service still carries no principal, so it stays the control.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402

APP = ROOT / "examples" / "app" / "private_notes.rvl"
PUBLIC = ROOT / "examples" / "app" / "notes.rvl"
AUTH = ROOT / "stdlib" / "auth.rvl"

GET_CHECK = """\
      return match auth.validate(bearer) {
        Err(e)  => Err(e),
        Ok(who) => store.get(who, id),
      }"""

LIST_CHECK = """\
      return match auth.validate(bearer) {
        Err(e)  => Err(e),
        Ok(who) => Ok(store.mine(who)),
      }"""


def _compile(app: Path) -> dict:
    return compile_files([str(app), str(AUTH)])


def _mutant(tmp_path: Path, old: str, new: str) -> Path:
    source = APP.read_text(encoding="utf-8")
    assert source.count(old) == 1, "the example no longer has the expected handler"
    out = tmp_path / "private_notes.rvl"
    out.write_text(source.replace(old, new), encoding="utf-8")
    return out


def _refusal(app: Path) -> str:
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(app)
    err = exc.value
    if isinstance(err, RevlErrors):
        return "\n".join(str(e) for e in err.errors)
    return str(err)


def test_the_example_with_the_check_is_admitted():
    ir = _compile(APP)
    api = ir["services"]["PrivateNotesApi"]["methods"]
    assert set(api) == {"get_private_note", "list_private_notes"}
    for method in api.values():
        assert method["route"]["auth"] == "bearer"
        assert method["route"]["bind"]["bearer"]["kind"] == "header"
    # the store is the only place a note is read, and it wants a Principal
    store = ir["services"]["OwnedNoteStore"]["methods"]
    assert store["get"]["params"][0] == {"name": "who", "type": "Principal"}
    assert store["mine"]["params"][0] == {"name": "who", "type": "Principal"}


def test_get_route_without_the_check_is_refused(tmp_path):
    app = _mutant(tmp_path, GET_CHECK, "      return store.get(bearer, id)")
    msg = _refusal(app)
    assert "Principal" in msg and "Auth.validate" in msg


def test_list_route_without_the_check_is_refused(tmp_path):
    app = _mutant(tmp_path, LIST_CHECK, "      return Ok(store.mine(bearer))")
    msg = _refusal(app)
    assert "Principal" in msg and "Auth.validate" in msg


def test_a_fabricated_principal_is_refused(tmp_path):
    app = _mutant(tmp_path, GET_CHECK,
                  '      return store.get({ subject: "alice" }, id)')
    msg = _refusal(app)
    assert "Principal" in msg and "Auth.validate" in msg


def test_the_public_notes_service_is_the_control():
    ir = compile_files([str(PUBLIC)])
    for name, service in ir["services"].items():
        if name != "NotesApi":
            continue
        for method in service["methods"].values():
            assert "auth" not in method["route"]
            assert all(p["type"] != "Principal" for p in method["params"])
