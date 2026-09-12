"""The authorization machinery — roadmap item 457, Slice 2
(docs/design/457-endpoint-one-definition.md, "Authorization: explicit, and not
derivable").

Slice 1 landed the `route` clause and proved that generated metadata grants
nothing (no auth vocabulary on the clause, no `security` in the export, a
documentation-only `x-revl-auth: bearer` marker). Slice 2 lands the value that
IS the right to reach user data and the checker invariant that makes skipping
the check impossible:

  * `stdlib/auth.rvl` declares `Bearer` (the untrusted credential) and the
    `Auth` service whose `validate` is the SOLE producer of a `Principal`;
  * `Principal` is opaque — it has no revl declaration and no constructor, and
    the name is RESERVED so a `type Principal = ...` cannot mint one;
  * a user-scoped operation takes a `Principal`, so a routed handler that drops
    the explicit `auth.validate` step is REFUSED AT ADMISSION, naming
    `Principal` and its sole producer — the stronger-than-runtime guarantee
    (the mutant fails to type-check, it does not merely fail closed at runtime).

This module pins the surface and the admission refusal (design exit test 4,
first half). The live Cordis SSO binding (`host @auth` over
`ctx.sso.validateSession`, design 529) is Slice 3; here the py stub stands in.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError, RevlErrors  # noqa: E402

# The exemplary routed half from the design (§"Authorization"): a `NotesApi`
# whose `get_note` user-scopes a `NoteStore` behind the explicit auth step. The
# `{body}` slot is the handler under test — the admitted shape or a mutant.
APP = """\
use "stdlib/http.rvl" {{ ApiError }}
use "stdlib/auth.rvl" {{ Auth, Bearer }}

type Note = {{ id: Str, owner: Str, title: Str, body: Str }}

service NoteStore {{
  fn get(who: Principal, id: Str) -> Result[Note, ApiError]
}}

service NotesApi {{
  route get "/notes/{{id}}"
  fn get_note(bearer: Bearer, id: Str) -> Result[Note, ApiError]
}}

component NotesHttp requires auth: Auth, store: NoteStore
                    provides notes_api: NotesApi {{
  provide notes_api {{
    fn get_note(bearer, id) {{
{body}
    }}
  }}
}}
"""

# the admitted handler: the explicit `auth.validate` step, `who` reaching the
# store only inside the `Ok` arm.
ADMITTED = """\
      return match auth.validate(bearer) {
        Err(e) => Err(e),
        Ok(who) => store.get(who, id),
      }
"""

# the mutant of design exit test 4: the `auth.validate` step is GONE, so the
# handler tries to reach the store with the raw `Bearer` — a value that is not a
# `Principal` and cannot become one without the step.
MUTANT_DROP = "      return store.get(bearer, id)"


def _compile(tmp_path, source: str) -> dict:
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    return compile_files([str(app)])


def _errmsg(exc) -> str:
    err = exc.value
    if isinstance(err, RevlErrors):
        return "\n".join(str(e) for e in err.errors)
    return str(err)


# ---------------------------------------------------------------- the surface

def test_auth_stdlib_surface_compiles(tmp_path):
    """`stdlib/auth.rvl` declares `Bearer` and the `Auth` service; `Principal`
    has NO declaration — it is only ever the result of `Auth.validate`."""
    ir = compile_files([str(ROOT / "stdlib" / "auth.rvl")])
    assert ir["types"]["Bearer"]["kind"] == "record"
    assert set(ir["types"]["Bearer"]["fields"]) == {"token"}
    validate = ir["services"]["Auth"]["methods"]["validate"]
    assert validate["params"] == [{"name": "b", "type": "Bearer"}]
    assert validate["returns"] == "Result[Principal, ApiError]"
    # `validate` is a plain (non-emission) read: it decides nothing on the world
    assert validate["emission"] is False
    # `Principal` is opaque — no revl type carries it
    assert "Principal" not in ir.get("types", {})


def test_bearer_from_stdlib_still_marks_the_s1_hook(tmp_path):
    """The Slice-2 `Bearer` wires the Slice-1 authorization hook: a routed op
    with a stdlib `Bearer` parameter binds it from the header and earns the
    documentation-only `auth: "bearer"` marker (grants nothing on its own)."""
    ir = _compile(tmp_path, APP.format(body=ADMITTED))
    route = ir["services"]["NotesApi"]["methods"]["get_note"]["route"]
    assert route["auth"] == "bearer"
    assert route["bind"]["bearer"]["kind"] == "header"


# ---------------------------------------------------------------- admitted

def test_handler_with_the_step_is_admitted(tmp_path):
    """With the explicit `auth.validate` step, `who: Principal` is in scope in
    the `Ok` arm and reaches the user-scoped store: the handler is admitted."""
    ir = _compile(tmp_path, APP.format(body=ADMITTED))
    assert "NotesHttp" in ir["components"] or "NotesApi" in ir["services"]


# ---------------------------------------------------------------- the refusal

def test_dropping_the_step_is_refused_at_admission(tmp_path):
    """Design exit test 4 (first half): the mutant that removes `auth.validate`
    is REFUSED at admission with a diagnostic naming `Principal` and its sole
    producer. The raw `Bearer` is not a `Principal` and no revl body can turn it
    into one except through `Auth.validate`."""
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, APP.format(body=MUTANT_DROP))
    msg = _errmsg(exc)
    assert "Principal" in msg
    assert "Auth.validate" in msg


def test_fabricated_principal_is_refused(tmp_path):
    """A handler cannot fabricate a `Principal` with a record literal: the
    opaque type has no constructor, so the literal is not a `Principal` and the
    refusal again names the sole producer."""
    body = '      return store.get({ subject: "root" }, id)'
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, APP.format(body=body))
    msg = _errmsg(exc)
    assert "Principal" in msg and "Auth.validate" in msg


def test_principal_name_is_reserved(tmp_path):
    """The opacity is a checker invariant, not an accident: a `type Principal =
    ...` declaration is refused, so a body cannot mint its own record-shaped
    principal and slip past every user-scoped guard."""
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, "type Principal = { spoofed: Str }")
    msg = _errmsg(exc)
    assert "Principal" in msg and "reserved" in msg


def test_route_cannot_bind_a_principal_from_transport(tmp_path):
    """A `Principal` can never arrive over the wire: a routed operation that
    declares a `Principal` parameter is refused by the Slice-1 bind table (it is
    neither a scalar query value nor a record body), so the transport cannot
    smuggle in a principal the handler never authorized."""
    src = """\
use "stdlib/http.rvl" { ApiError }
type Note = { id: Str }
service NotesApi {
  route get "/notes/{id}"
  fn get_note(who: Principal, id: Str) -> Result[Note, ApiError]
}
"""
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, src)
    assert "Principal" in _errmsg(exc)


def test_principal_flows_only_from_validate(tmp_path):
    """The positive twin of the refusal: a `Principal` obtained from
    `Auth.validate` flows into the user-scoped store, and nothing else can. A
    second declaration whose result carries a `Principal` is refused by the
    reservation pinned below, so `Auth.validate` is provably the one door."""
    ir = _compile(tmp_path, APP.format(body=ADMITTED))
    # the store op's parameter is the opaque principal, reachable only via the
    # validate step checked above
    store_get = ir["services"]["NoteStore"]["methods"]["get"]
    assert store_get["params"][0] == {"name": "who", "type": "Principal"}


# ------------------------------------------------- the producer reservation

# A `Principal` is an undeclared opaque nominal, so reserving the NAME (`type
# Principal = ...` is refused above) is not the whole invariant: nothing else
# objects to a *signature* that RETURNS one. Each shape below is a second
# producer, and each must be refused — otherwise the admission refusal that
# makes a dropped auth step a compile error is bypassable by declaring a door of
# one's own. The shipped `stdlib/auth.rvl` is the one module that may declare a
# result carrying `Principal` (the `Auth.validate` signature and the
# `host_validate` extern behind it); every shape here is application source.

# an `extern` with an arbitrary host body: the producer in its plainest form
MUTANT_EXTERN = """\
use "stdlib/http.rvl" { ApiError }
use "stdlib/auth.rvl" { Auth, Bearer }

type Note = { id: Str, owner: Str, title: Str, body: Str }

service NoteStore {
  fn get(who: Principal, id: Str) -> Result[Note, ApiError]
}

service NotesApi {
  route get "/notes/{id}"
  fn get_note(bearer: Bearer, id: Str) -> Result[Note, ApiError]
}

extern pure fn mint() -> Result[Principal, ApiError] = @py {
    return Ok({"subject": "attacker"})
}

component NotesHttp requires auth: Auth, store: NoteStore
                    provides notes_api: NotesApi {
  provide notes_api {
    fn get_note(bearer, id) {
      return match mint() {
        Err(e) => Err(e),
        Ok(who) => store.get(who, id),
      }
    }
  }
}
"""

# the extern is hidden behind a plain revl `fn`, so the call site names no
# extern at all and reads as ordinary revl to a reviewer
MUTANT_LAUNDERED = """\
use "stdlib/http.rvl" { ApiError }

extern pure fn raw() -> Principal = @py {
    return {"subject": "attacker"}
}

fn mint() -> Result[Principal, ApiError] { return Ok(raw()) }
"""


def _refused(tmp_path, source: str) -> str:
    with pytest.raises((RevlError, RevlErrors)) as exc:
        _compile(tmp_path, source)
    return _errmsg(exc)


def test_a_declared_principal_producer_is_refused(tmp_path):
    """Design exit test 4 depends on `Auth.validate` being the SOLE producer.
    An `extern` that declares `Result[Principal, ApiError]` is a second door:
    the handler above reaches the user-scoped store through it without ever
    authorizing, so it is refused at admission."""
    msg = _refused(tmp_path, MUTANT_EXTERN)
    assert "Principal" in msg
    assert "reserved" in msg
    assert "Auth.validate" in msg
    assert "mint" in msg


def test_a_plain_fn_cannot_launder_a_principal(tmp_path):
    """Wrapping the producer in a plain `fn` changes nothing: the result still
    carries `Principal`, so the wrapper is refused too — the shape whose call
    site names no extern is exactly the one a reviewer cannot see through."""
    msg = _refused(tmp_path, MUTANT_LAUNDERED)
    assert "Principal" in msg and "Auth.validate" in msg
    # both ends of the chain are producers, and the `fn` is the one the app
    # would call, so the refusal names it
    assert "`fn mint`" in msg


def test_a_service_cannot_declare_a_principal_producer(tmp_path):
    """A `service` method is a producer slot like any other: an unimplemented
    `fn steal() -> Result[Principal, ApiError]` still hands every consumer a
    signature that mints a principal, so it is refused on the declaration."""
    src = """\
use "stdlib/http.rvl" { ApiError }
service Evil {
  fn steal() -> Result[Principal, ApiError]
}
"""
    msg = _refused(tmp_path, src)
    assert "Evil.steal" in msg and "Auth.validate" in msg


def test_a_box_of_principal_cannot_be_returned(tmp_path):
    """The reservation is transitive: `type Box = { who: Principal }` is a
    harmless consumer on its own, but a declaration whose result is `Box` mints
    a principal just as surely as one that spells `Principal` out."""
    src = """\
type Box = { who: Principal, note: Str }
extern pure fn mint() -> Box = @py {
    return {"who": {"subject": "attacker"}, "note": "x"}
}
"""
    msg = _refused(tmp_path, src)
    assert "`extern mint`" in msg and "Auth.validate" in msg


def test_a_nested_principal_result_is_refused(tmp_path):
    """A generic wrapper is not a hiding place: `Opt[Principal]` and
    `(Str) -> Principal` both carry the opaque type in the result position."""
    for returns in ("Opt[Principal]", "(Str) -> Principal", "List[Principal]"):
        msg = _refused(
            tmp_path,
            f"extern pure fn mint() -> {returns} = @py {{ return None }}")
        assert "`extern mint`" in msg and "Principal" in msg


def test_consuming_a_principal_is_still_allowed(tmp_path):
    """The reservation bounds PRODUCTION, not mention. A `Principal` parameter
    is a consumer — it cannot be called without one — so the user-scoped store
    signature and a helper that passes a principal along stay admissible, and
    the shipped `stdlib/auth.rvl` keeps its `Auth.validate` result."""
    src = """\
type Box = { who: Principal, note: Str }
fn peek(b: Box) -> Str { return b.note }
fn hand(who: Principal) -> Str { return "x" }
"""
    assert _compile(tmp_path, src)["types"]["Box"]["fields"] == {
        "who": "Principal", "note": "Str"}
    # and the shipped producer is not caught by its own reservation
    ir = compile_files([str(ROOT / "stdlib" / "auth.rvl")])
    assert (ir["services"]["Auth"]["methods"]["validate"]["returns"]
            == "Result[Principal, ApiError]")
    assert [e for e in ir["externs"] if e["name"] == "host_validate"]


def test_a_shadowing_auth_module_cannot_mint_a_principal(tmp_path):
    """The exemption is the SHIPPED stdlib, not the path spelling: a local
    `stdlib/auth.rvl` that shadows it is application source, so a producer
    declared there is refused too — the sole-producer guarantee cannot be
    replaced by distribution either."""
    shadow = tmp_path / "stdlib"
    shadow.mkdir()
    (shadow / "auth.rvl").write_text(
        "pub extern pure fn mint() -> Principal = @py { return {\"subject\": \"x\"} }\n",
        encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text('use "stdlib/auth.rvl" { mint }\n', encoding="utf-8")
    with pytest.raises((RevlError, RevlErrors)) as exc:
        compile_files([str(app)])
    msg = _errmsg(exc)
    assert "Principal" in msg and "Auth.validate" in msg
