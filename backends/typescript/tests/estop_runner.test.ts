// The ts-tier E-Stop, runner half — roadmap item 443, issue #122.
//
// Slices 1 and 2 (`estop.test.ts`) landed the latch reader and the crossing
// seams that REFUSE a new crossing once the latch is armed. This suite pins the
// piece that lets the conductor report a node child HALTED rather than SIGKILLed
// as no-seam:
//
//   * the accept seam records each crossing WHILE its handler runs and clears it
//     on return, so a latch armed mid-crossing finds it in the inventory — the
//     ambiguous tier (item 440), the designed outcome of an operator halt;
//   * `estopInventory` shapes those in-flight crossings into the merged residue
//     schema the conductor's report reads (`src/revl/placement.py`), and keeps
//     an HONESTLY empty `stranded` book because this tier holds no witnessed
//     inverse ledger;
//   * `estopHaltLine` prints them on the one `[name] HALTED {json}` line the
//     conductor parses off stdout by prefix, the same contract the py runner
//     meets.
//
// The idle watcher itself (`placement_runner.ts::haltOnLatch`) ends in
// `process.exit`, so it is exercised end to end by the conductor suite
// (`tests/test_estop_ts_tier_443.py`) rather than unit-tested here.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { beginCrossing, endCrossing, inFlightCrossings, serve } from '../bridge.ts'
import { estopHaltLine, estopInventory } from '../estop.ts'

const dirs: string[] = []
const servers: net.Server[] = []

function tmpdir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_estop_runner_'))
  dirs.push(dir)
  return dir
}

afterEach(() => {
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

/** One request, one reply — the newline-delimited JSON a proxy speaks. */
function call(sock: string, request: unknown): Promise<any> {
  return new Promise((resolve, reject) => {
    const client = net.connect(sock)
    let buf = ''
    client.on('connect', () => client.write(JSON.stringify(request) + '\n'))
    client.on('data', (chunk) => {
      buf += chunk
      const nl = buf.indexOf('\n')
      if (nl >= 0) {
        client.end()
        resolve(JSON.parse(buf.slice(0, nl)))
      }
    })
    client.on('error', reject)
  })
}

async function waitFor(pred: () => boolean, ms = 1000): Promise<void> {
  const deadline = Date.now() + ms
  while (!pred()) {
    if (Date.now() > deadline) throw new Error('waitFor timed out')
    await new Promise((r) => setTimeout(r, 5))
  }
}

describe('the ts E-Stop in-flight inventory (item 443, issue #122)', () => {
  it('records a crossing and clears it on return', () => {
    const seq = beginCrossing('db', 'query', 'accept')
    expect(inFlightCrossings().some((c) => c.seq === seq && c.key === 'db' && c.method === 'query'))
      .toBe(true)
    endCrossing(seq)
    expect(inFlightCrossings().some((c) => c.seq === seq)).toBe(false)
  })

  it('the accept seam records a crossing WHILE its handler runs, and clears it after', async () => {
    const sock = path.join(tmpdir(), 'provider.sock')
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    let entered: ReturnType<typeof inFlightCrossings> = []

    const ctx = {
      work: {
        compute: async (x: string) => {
          entered = inFlightCrossings()
          await gate
          return x
        },
      },
    }
    const server = await serve(ctx as any, { work: ['compute'] }, sock)
    servers.push(server)

    const pending = call(sock, { key: 'work', method: 'compute', args: ['crossed'] })
    // the handler is mid-flight: the crossing must be in the inventory now.
    await waitFor(() => entered.length > 0)
    expect(entered.some((c) => c.key === 'work' && c.method === 'compute' && c.direction === 'accept'))
      .toBe(true)

    release()
    const reply = await pending
    expect(reply.ok).toBe(true)
    // and once the handler returns the crossing is no longer in flight.
    expect(inFlightCrossings().some((c) => c.key === 'work')).toBe(false)
  })

  it('clears the crossing even when the handler throws', async () => {
    const sock = path.join(tmpdir(), 'provider.sock')
    const ctx = { work: { boom: () => { throw new Error('kaboom') } } }
    const server = await serve(ctx as any, { work: ['boom'] }, sock)
    servers.push(server)

    const reply = await call(sock, { key: 'work', method: 'boom', args: [] })
    expect(reply.ok).toBe(false)
    expect(inFlightCrossings().some((c) => c.key === 'work')).toBe(false)
  })
})

describe('estopInventory (item 443, issue #122)', () => {
  it('names in-flight crossings AMBIGUOUS and keeps an honestly empty stranded book', () => {
    const inv = estopInventory(
      'edge',
      [{ seq: 7, key: 'work', method: 'compute', direction: 'accept' }],
      { reason: 'runaway loop', operator: 'ops@example' },
    )
    expect(inv.verdict).toBe('halted')
    expect(inv.reason).toBe('runaway loop')
    expect(inv.operator).toBe('ops@example')
    expect(inv.resumable).toBe(false)
    // this tier keeps no witnessed-inverse ledger, so stranded is empty — and
    // that is honest, because the ambiguous crossings ARE reported below.
    expect(inv.stranded).toEqual([])
    const flight = inv.inFlight as Array<Record<string, unknown>>
    expect(flight).toHaveLength(1)
    expect(flight[0]).toMatchObject({
      kind: 'estop-ambiguous',
      component: 'work',
      method: 'compute',
      seq: 7,
      entry: 'crossing',
      attemptedFlag: true,
      outcome: 'unknown',
    })
  })

  it('reports nothing in flight, and falls open on a null record, honestly', () => {
    const inv = estopInventory('edge', [], null)
    expect(inv.inFlight).toEqual([])
    expect(inv.stranded).toEqual([])
    expect(inv.reason).toBe('operator halt')
    expect(inv.operator).toBe('unknown')
  })
})

describe('estopHaltLine (item 443, issue #122)', () => {
  it('prints one HALTED line the conductor parses off stdout by prefix', () => {
    const line = estopHaltLine(
      'edge',
      [{ seq: 1, key: 'work', method: 'compute', direction: 'accept' }],
      { reason: 'r', operator: 'o' },
    )
    // the conductor's pump splits `[name] HALTED <json>` on the first two spaces
    // (src/revl/placement.py::pump), so the prefix and a parseable tail matter.
    expect(line.startsWith('[edge] HALTED ')).toBe(true)
    const parsed = JSON.parse(line.slice('[edge] HALTED '.length))
    expect(parsed.process).toBe('edge')
    expect(parsed.verdict).toBe('halted')
    expect(parsed.inFlight).toHaveLength(1)
  })
})
