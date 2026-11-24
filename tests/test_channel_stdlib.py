"""The same-origin page-to-page provider of the stdlib WIRE contract (issue #3418).

`stdlib/channel.rvl` is a second provider of `stdlib/wire.rvl`'s `Wire`, beside
`stdlib/socket.rvl`: pages of one origin join a composition hosted in a page
over the browser's `BroadcastChannel`, and the program reads the same `Knock`s.
It runs on the ts tier only; its behaviour is pinned by
backends/typescript/tests/ts_channel_wire.test.ts. What this file pins is what
the compiler and audit see of it:

  * the provider's three verbs declare the contract's tokens, `wire.take`,
    `wire.say`, `wire.close`, and `revl audit` prints the extern names beside
    them, so a program that is written against `Wire` and picked a channel row
    shows the page-to-page crossing a socket row would show;
  * the provider emits on ts: each extern is a stdlib-root ref into
    `revl_channel_ts.ts` pinned to its digest, the component acquires the hub
    and releases it in its teardown;
  * every limit is the row's config (`channel`, `most`, `held`, `lines`,
    `beat`, `silence`), none a constant in the program;
  * the row `row @wire from "stdlib/channel.rvl" provides wire` resolves.

Every test fails on the base commit: `stdlib/channel.rvl` does not exist there.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.composition import compile_composition, resolve_file  # noqa: E402

CHANNEL = ROOT / "stdlib" / "channel.rvl"
TS_ENGINE = "backends/typescript/revl_channel_ts.ts"

IR = compile_files([str(CHANNEL)])

GAME = '''\
use "stdlib/wire.rvl" { Wire, Knock, Kind, Why }

service Beat { emission fn tick() -> Int }

component Game requires wire: Wire provides beat: Beat {
  provide beat {
    fn tick() {
      let knocks = emit wire.take(512)
      var heard = 0
      for (k of knocks) {
        let ok = match k.kind {
          Said => emit wire.say(k.line, k.text),
          Opened => true,
          Shut(_) => false,
        }
        if (ok) { heard = heard + 1 }
      }
      return heard
    }
  }
}
'''

CROSSINGS = {
    "channel_take": ("emission", ["wire.take"]),
    "channel_say": ("emission", ["wire.say"]),
    "channel_close": ("emission", ["wire.close"]),
    "channel_listen": ("acquire", []),
    "channel_shut": ("acquire", []),
}

TS_REFS = {
    "channel_listen": "listen",
    "channel_shut": "shut",
    "settled": "settled",
    "channel_take": "take",
    "channel_say": "say",
    "channel_close": "close",
}


def test_audit_names_the_crossings(tmp_path, capsys):
    game = tmp_path / "game.rvl"
    game.write_text(GAME, encoding="utf-8")
    assert main(["audit", str(game), str(CHANNEL), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    externs = {row["name"]: row
               for row in document["boundary"]["ChannelWire"]["externs"]}
    for name, (klass, caps) in CROSSINGS.items():
        assert externs[name]["class"] == klass, name
        assert externs[name].get("capabilities", []) == caps, name
        assert externs[name]["backends"] == ["ts"], name

    assert main(["audit", str(game), str(CHANNEL)]) == 0
    text = capsys.readouterr().out
    for line in ("channel_take [wire.take] (emission, ts)",
                 "channel_say [wire.say] (emission, ts)",
                 "channel_close [wire.close] (emission, ts)",
                 "channel_listen (acquire, ts)",
                 "channel_shut (acquire, ts)"):
        assert line in text, line
    assert "capabilities: wire.say, wire.take" in text


def test_ts_emit_of_channel_provider(tmp_path):
    """Every extern is a stdlib-root ref into `revl_channel_ts.ts`, pinned to
    the file's digest, and the component acquires the hub and releases it in
    its teardown."""
    externs = {e["name"]: e for e in IR["externs"]}
    digest = hashlib.sha256((ROOT / TS_ENGINE).read_bytes()).hexdigest()
    for name, symbol in TS_REFS.items():
        ref = externs[name]["refs"]["ts"]
        assert ref == {"symbol": symbol, "path": TS_ENGINE, "sha256": digest,
                       "root": "stdlib"}, name
        assert "py" not in externs[name]["refs"], name

    out = tmp_path / "channel.ts"
    assert main(["emit", "--backend", "ts", str(CHANNEL), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    for line in ("export function channel_listen(door: Door): Hub {",
                 "export function channel_shut(h: Hub): void {",
                 "export function channel_take(h: Hub, most: bigint): Raw[] {",
                 "export function channel_say(h: Hub, line: bigint, text: string)"
                 ": boolean {",
                 "export function channel_close(h: Hub, line: bigint, why: string,"
                 " text: string): boolean {"):
        assert line in text, line
    load = f'_revl_require(_revl_ref_path_stdlib("{TS_ENGINE}"))'
    assert text.count(load) == len(TS_REFS)
    assert "export const ChannelWire = {" in text
    assert '"channel_shut", () => channel_shut(door))' in text


CHANNEL_ROW = """
composition World {
  row @wire from "stdlib/channel.rvl" provides wire
    config { channel: "world-1", most: 4096, beat: 500, silence: 3000 }
}
"""


def test_a_row_picks_the_channel_provider_and_carries_every_limit(tmp_path):
    """The channel name and each limit are the row's config, none a constant
    in the program: `most`, `held`, `lines`, `beat` and `silence` pass through
    to the door."""
    doc = tmp_path / "world.rvl"
    doc.write_text(CHANNEL_ROW)
    table = resolve_file(str(doc), str(tmp_path))
    (row,) = table.rows
    assert row.component == "ChannelWire"
    assert row.source == "stdlib:stdlib/channel.rvl"
    assert row.config["channel"] == "world-1"
    assert row.config["silence"] == 3000
    document = compile_composition(str(doc), str(tmp_path))
    assert [c["name"] for c in document["components"]] == ["ChannelWire"]
    (component,) = [c for c in IR["components"] if c["name"] == "ChannelWire"]
    text = json.dumps(component["config"])
    for name in ("channel", "most", "held", "lines", "beat", "silence"):
        assert f'"{name}"' in text, name
    # each door field is the config field of its own name, not another's
    out = tmp_path / "world.ts"
    assert main(["emit", "--backend", "ts", str(CHANNEL), "-o", str(out)]) == 0
    emitted = out.read_text(encoding="utf-8")
    assert ("channel_listen({channel: config.channel, most: config.most, "
            "held: config.held, lines: config.lines, beat: config.beat, "
            "silence: config.silence})") in emitted
