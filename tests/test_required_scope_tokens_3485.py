"""Issue #3485: a consumer method that emits only through a required service's
verbs can narrow its declaration to those crossings, one verb at a time.

The token is the required KEY followed by the VERB the body calls (`k.m`):
`emit wire.take(...)` under `requires wire: Wire` is covered by `emission[wire]`
or by `emission[wire.take]`, and by nothing else. What `Wire.take` itself
declares does not enter, so `emission[fs.write]` on a body that emits
`fs.ingest` stays refused (`examples/rejections/g4_dotted_capability_key.rvl`)
while `emission[fs.ingest]` is accepted (`examples/g4_required_verb_token.rvl`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

SOCKET = ROOT / "stdlib" / "socket.rvl"
EXAMPLES = ROOT / "examples"

GAME = '''\
use "stdlib/wire.rvl" { Wire, Knock, Kind, Why }

service Beat { DECL fn tick() -> Int }

component Game requires wire: Wire provides beat: Beat {
  provide beat {
    fn tick() {
      let knocks = emit wire.take(512)
      var heard = 0
      for (k of knocks) {
        let ok = emit wire.say(k.line, k.text)
        if (ok) { heard = heard + 1 }
      }
      return heard
    }
  }
}
'''

# the issue's own sample, verbatim shape: every verb of the key is named
ISSUE_SAMPLE = '''\
use "stdlib/wire.rvl" { Wire, Knock }

service Beat {
  emission[wire.take, wire.say, wire.close] fn beat() -> Int
}

component Game requires wire: Wire provides b: Beat {
  provide b {
    fn beat() -> Int {
      let knocks = emit wire.take(512)
      let ok = emit wire.say(1, "hi")
      return 0
    }
  }
}
'''

WITNESSED = '''\
service Journal { witnessed fn note(s: Str) -> Int }
service Api { DECL fn save(s: Str) -> Int }
component Front requires j: Journal provides api: Api {
  provide api { fn save(s) = j.note(s) }
}
'''


def _game(tmp_path: Path, decl: str) -> Path:
    game = tmp_path / "game.rvl"
    game.write_text(GAME.replace("DECL", decl), encoding="utf-8")
    return game


def _refusal(game: Path) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_files([str(game), str(SOCKET)])
    return caught.value


def test_the_issue_sample_is_accepted(tmp_path):
    source = tmp_path / "game.rvl"
    source.write_text(ISSUE_SAMPLE, encoding="utf-8")
    compile_files([str(source), str(SOCKET)])


@pytest.mark.parametrize("decl", [
    "emission[wire.take, wire.say]",
    "emission[wire.take, wire.say, wire.close]",
    "emission[wire.say, wire.take, db]",
])
def test_key_dot_verb_covers_the_crossing(tmp_path, capsys, decl):
    game = _game(tmp_path, decl)
    assert main(["audit", str(game), str(SOCKET), "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["boundary"]["Game"]
    assert entry["emissions"] == ["wire.say", "wire.take"]


def test_the_binding_name_is_still_admitted(tmp_path):
    compile_files([str(_game(tmp_path, "emission[wire]")), str(SOCKET)])


def test_a_verb_the_method_does_not_declare_leaves_the_key_uncovered(tmp_path):
    error = _refusal(_game(tmp_path, "emission[wire.take]"))
    message = str(error)
    assert error.code == "G4"
    assert ("`Beat.tick` is declared `emission[wire.take]`, but this "
            "implementation emits through `wire` (reaching `wire.take`, "
            "`wire.say`)") in message, message


def test_a_dotted_token_that_is_not_a_verb_of_the_key_is_refused(tmp_path):
    error = _refusal(_game(tmp_path, "emission[wire.shout, wire.whisper]"))
    message = str(error)
    assert error.code == "G4"
    assert ("emits through `wire` (reaching `wire.take`, `wire.say`)"
            in message), message


def test_a_declaration_naming_none_of_the_tokens_names_the_binding(tmp_path):
    error = _refusal(_game(tmp_path, "emission[db]"))
    assert "emits through `wire`" in str(error), str(error)


def test_a_bare_verb_is_nameable_by_key_and_verb():
    """The token is the key and the verb, not what the verb declares: a bare
    `emission fn send` crossing is still `bus.send` from this component."""
    compile_source('''\
service Bus { emission fn send(n: Int) -> Int }
service Beat { emission[bus.send] fn tick() -> Int }

component Game requires bus: Bus provides beat: Beat {
  provide beat {
    fn tick() = emit bus.send(1)
  }
}
''', "bare.rvl")


def test_the_verbs_own_token_does_not_stand_in_for_the_crossing():
    """`examples/rejections/g4_dotted_capability_key.rvl`, unchanged:
    `Task.go` declares `emission[fs.write]`, which is what `Store.ingest`
    declares, and emits `fs.ingest`. `fs.write` is not `fs.ingest`."""
    pinned = EXAMPLES / "rejections" / "g4_dotted_capability_key.rvl"
    with pytest.raises(RevlError) as caught:
        compile_files([str(pinned)])
    assert ("`Task.go` is declared `emission[fs.write]`, but this "
            "implementation emits through `fs` (reaching `fs.ingest`)"
            in str(caught.value)), str(caught.value)


def test_the_accepted_example_narrows_to_the_verb(capsys):
    """`examples/g4_required_verb_token.rvl`: the same body under
    `emission[fs.ingest]`. The audit labels the crossing by that token and
    keeps composing the chain (`Store.ingest` declares `fs.write`)."""
    example = EXAMPLES / "g4_required_verb_token.rvl"
    assert main(["audit", str(example), "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["boundary"]["Worker"]
    assert entry["emissions"] == ["fs.ingest"]
    assert entry["capabilities"] == {"fs.ingest": ["fs.write"]}


def test_a_token_not_qualified_by_the_key_stays_the_providers():
    """`Cache.put` declares `emission[db]`: `db` is the provider's downstream
    key, not a name for the `cache` crossing, so it still cannot stand in."""
    source = '''\
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str) -> Int }
service Api { emission[db] fn save(key: Str) -> Int }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key) = emit db.execute(key) }
}
component Front requires cache: Cache provides api: Api {
  provide api { fn save(key) = emit cache.put(key) }
}
'''
    with pytest.raises(RevlError) as caught:
        compile_source(source, "front.rvl")
    assert "emits through `cache`" in str(caught.value), str(caught.value)


def test_witnessed_narrows_to_the_verb_the_same_way():
    compile_source(WITNESSED.replace("DECL", "witnessed[j.note]"), "wit.rvl")
    compile_source(WITNESSED.replace("DECL", "witnessed[j]"), "wit.rvl")


def test_witnessed_with_a_token_that_is_not_a_verb_is_refused():
    with pytest.raises(RevlError) as caught:
        compile_source(WITNESSED.replace("DECL", "witnessed[j.other]"),
                       "wit.rvl")
    assert ("`Api.save` is declared `witnessed[j.other]`, but this "
            "implementation reaches `j` (reaching `j.note`)"
            in str(caught.value)), str(caught.value)
