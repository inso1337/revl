"""The authorization half of the exemplary web application (issue #2703, slice 1).

examples/app/notes.rvl is the public control: its routes reach no
principal-taking service. examples/app/private_notes.rvl is the other side:
its store operations take a `Principal`, so each handler has to call
`Auth.validate` first. The checks here:

* the shipped file is admitted, and its routes bind the credential from the
  `Authorization` header;
* the same program with the `auth.validate` step removed is refused at
  admission, naming `Principal` and `Auth.validate`, one mutant per route;
* a mutant that fabricates a principal is refused the same way;
* on the real runtime, with the dev stub validator, the routes serve an owner
  their own notes, answer another owner's note exactly as an absent one, and
  answer a missing credential 401;
* the host read of a principal's subject raises on anything that is not a
  principal with a subject, instead of reading as the owner "" (run on the
  emitted python module in a subprocess); and
* the public notes service still carries no principal, so it stays the control.
"""

import importlib.util
import json
import subprocess
import sys
import textwrap
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


PY_RUNTIME = ROOT / "backends" / "python"

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="standing a live composition up needs the cordis-py runtime; install "
           "it with `sh backends/python/setup.sh`")


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
    assert "expects `Principal`, got `Bearer`" in msg and "Auth.validate" in msg


def test_list_route_without_the_check_is_refused(tmp_path):
    app = _mutant(tmp_path, LIST_CHECK, "      return Ok(store.mine(bearer))")
    msg = _refusal(app)
    assert "expects `Principal`, got `Bearer`" in msg and "Auth.validate" in msg


def test_a_fabricated_principal_is_refused(tmp_path):
    app = _mutant(tmp_path, GET_CHECK,
                  '      return store.get({ subject: "alice" }, id)')
    msg = _refusal(app)
    assert "expects `Principal`, got `{subject: Str}`" in msg
    assert "Auth.validate" in msg


def test_the_public_notes_service_is_the_control():
    ir = compile_files([str(PUBLIC)])
    for name, service in ir["services"].items():
        if name != "NotesApi":
            continue
        for method in service["methods"].values():
            assert "auth" not in method["route"]
            assert all(p["type"] != "Principal" for p in method["params"])


# ---------------------------------------------------------------- live

@pytest.fixture
def dev_stub(monkeypatch):
    """The documented any-token dev validator: a non-blank token is that
    subject. It stands in for the HS256 validator so the test needs no key."""
    for name in ("REVL_AUTH_HS256_SECRET", "REVL_AUTH_HS256_SECRET_FILE",
                 "REVL_AUTH_ISSUER", "REVL_AUTH_AUDIENCE", "REVL_AUTH_LEEWAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REVL_AUTH_INSECURE_DEV_STUB", "1")


def _get(face, path, token=None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return face.dispatch_http("GET", path, b"", headers)


@needs_runtime
def test_live_routes_scope_notes_to_the_validated_subject(dev_stub):
    from revl.mcp.http_face import HttpComposedServer
    from revl.mcp.session import Session
    from revl.mcp.surface import declared_param_types

    roots = [str(APP), str(AUTH)]
    session = Session()
    session.load(compile_files(roots), {}, record=True, origin=None)
    face = HttpComposedServer(session, declared=declared_param_types(roots))
    try:
        own = _get(face, "/private/notes/pn-0", "alice")
        assert own.status == 200
        assert json.loads(own.body)["owner"] == "alice"

        # another owner's note is answered exactly as an absent one
        other = _get(face, "/private/notes/pn-0", "bob")
        absent = _get(face, "/private/notes/pn-99", "bob")
        assert other.status == absent.status == 404
        assert other.body == absent.body

        mine = json.loads(_get(face, "/private/notes", "bob").body)
        assert [n["id"] for n in mine] == ["pn-1"]

        # no credential: the typed 401 from validate, no note read
        assert _get(face, "/private/notes/pn-0").status == 401
        assert _get(face, "/private/notes").status == 401
    finally:
        session.unload()


def test_subject_of_raises_on_a_value_that_is_not_a_principal(tmp_path):
    """`subject_of` is a host read of an opaque value. A malformed argument
    cannot come from revl source (nothing but `Auth.validate` makes a
    `Principal`), so the path is reached on the emitted python module: it must
    raise, never read as the subject "" and so as the owner of a row."""
    from revl.__main__ import main

    out = tmp_path / "private_notes.py"
    assert main(["emit", "--backend", "python", str(APP), str(AUTH),
                 "-o", str(out)]) == 0
    probe = textwrap.dedent("""
        import importlib.util, sys
        sys.path.insert(0, sys.argv[1])
        spec = importlib.util.spec_from_file_location("pn", sys.argv[2])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.subject_of({"subject": "alice"}) == "alice"
        for bad in ("", "alice", None, {}, {"subject": ""}, {"subject": "  "},
                    {"subject": 3}):
            try:
                mod.subject_of(bad)
            except ValueError:
                continue
            raise SystemExit("did not raise for %r" % (bad,))
    """)
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(PY_RUNTIME), str(out)],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr + done.stdout
