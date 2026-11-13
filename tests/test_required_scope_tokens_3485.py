"""Issue #3485: a consumer method that emits only through a required service's
scoped verbs can declare those verbs' tokens.

`Wire`'s verbs declare `emission[wire.take]`, `emission[wire.say]` and
`emission[wire.close]`. A method of `component Game requires wire: Wire` that
emits `wire.take` and `wire.say` used to be refused unless its declaration
named the binding `wire` (or was a bare `emission`). It may now name the
tokens the verbs declare, and the refusal stays for a verb whose token it did
not declare.
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
from revl.errors import RevlError  # noqa: E402

SOCKET = ROOT / "stdlib" / "socket.rvl"

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


def _game(tmp_path: Path, decl: str) -> Path:
    game = tmp_path / "game.rvl"
    game.write_text(GAME.replace("DECL", decl), encoding="utf-8")
    return game


def _refusal(game: Path) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_files([str(game), str(SOCKET)])
    return caught.value


@pytest.mark.parametrize("decl", [
    "emission[wire.take, wire.say]",
    "emission[wire.take, wire.say, wire.close]",
])
def test_scoped_tokens_of_the_required_verbs_are_admitted(tmp_path, capsys, decl):
    game = _game(tmp_path, decl)
    assert main(["audit", str(game), str(SOCKET), "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["boundary"]["Game"]
    assert entry["emissions"] == ["wire.say", "wire.take"]
    assert entry["capabilities"] == {"wire.say": ["wire.say"],
                                     "wire.take": ["wire.take"]}


def test_the_binding_name_is_still_admitted(tmp_path):
    compile_files([str(_game(tmp_path, "emission[wire]")), str(SOCKET)])


def test_an_undeclared_verb_token_is_refused_naming_the_token(tmp_path):
    error = _refusal(_game(tmp_path, "emission[wire.take]"))
    message = str(error)
    assert error.code == "G4"
    assert ("`Beat.tick` is declared `emission[wire.take]`, but this "
            "implementation emits through `wire.say`") in message, message
    assert "emission[wire.take, wire.say] fn tick" in message, message


def test_a_declaration_naming_none_of_the_tokens_names_the_binding(tmp_path):
    error = _refusal(_game(tmp_path, "emission[db]"))
    assert "emits through `wire`" in str(error), str(error)


def test_a_bare_verb_has_no_token_to_declare(tmp_path):
    source = tmp_path / "bare.rvl"
    source.write_text('''\
service Bus { emission fn send(n: Int) -> Int }
service Beat { emission[bus.send] fn tick() -> Int }

component Game requires bus: Bus provides beat: Beat {
  provide beat {
    fn tick() = emit bus.send(1)
  }
}
''', encoding="utf-8")
    with pytest.raises(RevlError) as caught:
        compile_files([str(source)])
    assert "emits through `bus`" in str(caught.value), str(caught.value)


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
    from revl.compiler import compile_source
    with pytest.raises(RevlError) as caught:
        compile_source(source, "front.rvl")
    assert "emits through `cache`" in str(caught.value), str(caught.value)
