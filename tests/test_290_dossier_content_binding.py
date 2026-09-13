"""Item 290 §4/§6.2 — a gauntlet dossier binds to the program it graded.

`mcp requires evidence [gauntlet admissible]` is satisfied by the session's
gauntlet dossier, which `revl_gauntlet` records through
`Session.record_gauntlet`. The dossier named the components it graded by BARE
NAME, so the evidence bound to a *name* rather than to the program the gauntlet
actually graded: a dossier earned by one component satisfied the gate for any
later composition carrying a component merely *called* the same, whatever its
body. That is the shape `docs/design/290-confidence-evidence-admission.md` §6.1
refuses — "a dossier copied wholesale from a simpler component" — and §6.2
forbids: evidence is verified "against the ADMITTED component's REBUILT IR",
"never merely re-read from the bundle in transit; otherwise a
resolved-then-modified source rides into the composition on the original,
still-valid attestation".

Two facets, one gate:

  * BINDING — `component_digest` (gauntlet.py) hashes a component's
    `name`/`config`/`requires`/`provides`/`body`, and `record_gauntlet` keys the
    dossier by `(name, digest)`. `source` is deliberately NOT a digest input:
    the file a component was authored in is not its content, so grading
    `cand.rvl` and admitting `c.rvl` still satisfies the gate.
  * SESSION LIFETIME — `_reset()` clears the map, so evidence earned in one
    session cannot satisfy the next on a reused `Session` object (invariant 5:
    session-scoped state dies with the session).

A dossier whose components carry no digest records NOTHING (fail-closed), so a
hand-built or older dossier cannot be trusted by name either.

Every assertion here is a live `Session` driven through the real MCP verb
`revl_gauntlet`, so the reachable path is the tested one.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import gauntlet, server  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

POLICY = "mcp requires evidence [gauntlet admissible]\n"

# `Foo` reaches the `db` boundary, so `audit_report(ir)["boundary"]` names it and
# the evidence rule is in force for it.
GRADED = """
service Store { emission fn put(key: Str, value: Str) }
component Foo requires store: Store { emit store.put("a", "b") }
"""

# The same component NAME, a different body — the collision the gate missed.
# Only the emitted literal changes, so nothing but the component's content
# distinguishes the two programs.
OTHER_BODY = GRADED.replace('"a", "b"', '"zz", "yy"')


@pytest.fixture
def session(monkeypatch):
    """A fresh session whose sandbox carries the `mcp` evidence rule.

    `server.SESSION` is monkeypatched rather than assigned, so the real verb
    records into THIS session and the global is restored for the next test --
    an assigned global leaks an evidence-bearing session into every later
    test file in the same process.
    """
    fresh = Session()
    fresh.sandbox = parse_policy(POLICY)
    monkeypatch.setattr(server, "SESSION", fresh)
    return fresh


def _grade(session, source, filename="cand.rvl"):
    """Run the real MCP verb, so the dossier is recorded the production way."""
    result = server._tool_gauntlet({"source": source, "filename": filename})
    assert result.get("verdict") == "admissible", result
    return result


def _load(session, source, filename="c.rvl"):
    return session.load(compile_source(source, filename))


# --------------------------------------------------------------- the binding

def test_a_dossier_admits_the_program_it_graded(session):
    """The legitimate path stays open: grade the source, then admit it."""
    _grade(session, GRADED)
    _load(session, GRADED)
    assert session.loaded is True


def test_a_dossier_does_not_admit_a_different_body_under_the_same_name(session):
    """The exploit. Same name, different body: the evidence must not carry."""
    _grade(session, GRADED)

    # the collision is real — both programs really do name the component `Foo`
    assert [c["name"] for c in compile_source(OTHER_BODY, "c.rvl")["components"]] \
        == ["Foo"]

    with pytest.raises(SessionError) as refused:
        _load(session, OTHER_BODY)
    message = str(refused.value)
    assert "`Foo`" in message and "gauntlet" in message
    assert "unavailable" in message


def test_the_digest_ignores_the_filename_but_not_the_content():
    """`source` is not a digest input; every other field is."""
    same_a = compile_source(GRADED, "cand.rvl")["components"][0]
    same_b = compile_source(GRADED, "c.rvl")["components"][0]
    other = compile_source(OTHER_BODY, "cand.rvl")["components"][0]

    assert gauntlet.component_digest(same_a) == gauntlet.component_digest(same_b)
    assert gauntlet.component_digest(same_a) != gauntlet.component_digest(other)


def test_the_digest_moves_with_every_bound_field():
    """Each `_DIGEST_FIELDS` entry is load-bearing."""
    base = {"name": "Foo", "config": {}, "requires": [], "provides": [],
            "body": []}
    digest = gauntlet.component_digest(base)
    for field, moved in (("name", "Bar"),
                         ("config", {"k": 1}),
                         ("requires", ["store"]),
                         ("provides", ["cache"]),
                         ("body", [{"op": "emit"}])):
        changed = dict(base, **{field: moved})
        assert gauntlet.component_digest(changed) != digest, field


def test_a_dossier_without_digests_records_nothing(session):
    """Fail-closed: a dossier that does not bind to content is not evidence."""
    session.record_gauntlet({
        "verdict": "admissible",
        "candidate": {"components": [{"name": "Foo", "requires": ["store"],
                                      "provides": []}]},
    })
    assert session._gauntlet_dossiers == {}
    with pytest.raises(SessionError):
        _load(session, GRADED)


def test_a_refused_dossier_records_nothing(session):
    session.record_gauntlet({
        "verdict": "rejected",
        "candidate": {"components": [{"name": "Foo", "digest": "x"}]},
    })
    assert session._gauntlet_dossiers == {}


# ------------------------------------------------------------ session lifetime

def test_the_evidence_does_not_survive_unload(session):
    """Invariant 5: session-scoped evidence dies with the session."""
    _grade(session, GRADED)
    assert len(session._gauntlet_dossiers) == 1

    _load(session, GRADED)
    session.unload()
    assert session._gauntlet_dossiers == {}

    with pytest.raises(SessionError):
        _load(session, GRADED)


def test_regrading_in_the_fresh_session_restores_admission(session):
    """The recovery control: the gate is a gate, not a dead end."""
    _grade(session, GRADED)
    _load(session, GRADED)
    session.unload()

    _grade(session, GRADED)
    _load(session, GRADED)
    assert session.loaded is True
