"""The ts REACTIVE tier runs under plain node — item 130 (roadmap #81).

`tests/test_stream_reactive.py` pins what the ts emitter LOWERS (the emitted
bytes). This file pins that the runtime those bytes target actually RUNS: a
stream program, driven through the exact `host.Stream` API the emitter emits
(`source` / `subscribe(..., ctx, opts)` / `next` / `isClosed` / `close`, plus
the `merge` fan-in and the Slice 2 combinator chain + backpressure), executed by
the plain node the ts tier ships on (docs/ts-runtime-contract.md) — no vitest,
no bundler, no cordis. The async-generator mirror (design §4.6) is the same
queue-vs-cancel race the py reference runs, so this is the tier's own end of the
`test_stream_runtime.py` proof: items arrive transformed and in order, a
`Closed` ends an `every … in` loop, a `Faulted` throws out of `next`, the
cancellation-first `close` resolves a parked `next`, and a full LIFO teardown
leaves `Stream.pending()` at zero (no dangling listener, no orphaned chain).

Skips (never passes) when this machine's node cannot execute an emitted module
at all — a toolchain absence, not a by-design refusal.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.test import _node_can_run_emitted, _node_version  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _node_can_run_emitted(_node_version()),
    reason="the ts runtime contract needs node >= 22.18 or >= 23.6 to execute "
           "an emitted module (docs/ts-runtime-contract.md)",
)


# The driver imports `./runtime.ts` and drives the SAME surface the emitter
# emits. Written into the backend directory (so the relative import resolves)
# under a pid-unique name, and removed in `finally`.
_DRIVER = textwrap.dedent("""
    import { Stream, host, StreamFaulted } from './runtime.ts'

    const log: string[] = []

    async function main(): Promise<void> {
      // Slice 1 + 2: the combinator chain + a lossy backpressure policy, driven
      // through a plain `every … in`-shaped loop over `host.Stream.isClosed`.
      const src = host.Stream.source()
      const sub = host.Stream.subscribe(src, 'drop_oldest', null, {
        stages: [
          ['map', (x: any) => x + 1],
          ['filter', (x: any) => x % 2 === 0],
          ['take', 3],
        ],
        capacity: 8,
      })
      for (const v of [1, 2, 3, 4, 5, 6, 7, 8]) src.emit(v)
      const items: number[] = []
      for (;;) {
        const item = await sub.next()
        if (host.Stream.isClosed(item)) break   // a Closed terminal ends the loop
        items.push(item as number)
      }
      sub.close()
      src.close()
      // map -> 2..9, filter even -> 2,4,6,8, take 3 -> 2,4,6 then Closed.
      log.push('chain=' + JSON.stringify(items))

      // Slice 3: the merge fan-in — one derived stream from two sources.
      const a = host.Stream.source()
      const b = host.Stream.source()
      const subm = host.Stream.subscribe(host.Stream.merge(a, b), 'error', null)
      a.emit('x')
      b.emit('y')
      log.push('merge=' + JSON.stringify([await subm.next(), await subm.next()]))
      subm.close()   // one bracket unwinds the whole fan-in chain
      a.close()
      b.close()

      // The cancellation-first race (§9 Part A): a parked `next` resolves as
      // Closed the instant the subscription closes, so the bracket inverse is
      // reachable off the teardown path — it never waits for the provider.
      const s2 = host.Stream.source()
      const sub2 = host.Stream.subscribe(s2, 'error', null)
      const parked = sub2.next()
      sub2.close()
      log.push('cancel_first_closed=' + host.Stream.isClosed(await parked))
      s2.close()

      // A provider abort delivers `Faulted`, which THROWS out of `next` (never a
      // silent end of stream) — the terminal that fails an activation.
      const s3 = host.Stream.source()
      const sub3 = host.Stream.subscribe(s3, 'error', null)
      const p3 = sub3.next()
      s3.fault('boom')
      let reason = ''
      try {
        await p3
      } catch (e) {
        reason = (e as StreamFaulted).reason
      }
      sub3.close()
      s3.close()
      log.push('fault=' + reason)

      // Residue (R4): after a full LIFO teardown, nothing is left open.
      log.push('pending=' + Stream.pending())
    }

    main().then(
      () => { process.stdout.write(log.join('\\n') + '\\n') },
      (e) => { console.error(e); process.exit(1) },
    )
""").lstrip()


def test_a_ts_stream_program_runs_under_plain_node():
    driver = BACKEND / f"__revl_stream_driver_{__import__('os').getpid()}.mts"
    driver.write_text(_DRIVER, encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", driver.name],
            cwd=BACKEND, capture_output=True, text=True, timeout=120,
        )
    finally:
        driver.unlink(missing_ok=True)
    detail = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"driver failed under plain node:\n{detail}"
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    # the combinator chain transformed and bounded the stream, in order
    assert lines["chain"] == "[2,4,6]"
    # the fan-in delivered both sources' items on one subscription
    assert lines["merge"] == '["x","y"]'
    # a parked `next` resolved as Closed the moment the subscription closed
    assert lines["cancel_first_closed"] == "true"
    # a provider fault surfaced as a throw carrying the reason
    assert lines["fault"] == "boom"
    # a full teardown left no listener, source or derived link behind
    assert lines["pending"] == "0"
