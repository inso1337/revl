// A real node/ts consumer of a py provider that runs an item-118
// `CorrelationGuard` — the production code path (the same
// backends/typescript/bridge.ts::makeProxy the placement runner drives), with no
// cordis, so the SEALING can be proven in isolation.
//
// Config JSON, argv[2]:
//   { socket: "<uds path>",
//     correlation: { composition_id, peer_identity, secret },
//     mode: "fresh" | "frozen",   // frozen replays ONE envelope on every call
//     calls: ["a", "b"] }
//
// Prints `OK <arg> <json>` or `ERR <arg> <message>` per call, then `DONE`.

import fs from 'node:fs'
import { makeProxy, sealCorrelation } from '../backends/typescript/bridge.ts'

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))

// "frozen" seals ONE envelope and stamps it on every call: an exact replay of a
// captured crossing, produced by the real sealer rather than hand-built.
const frozen = sealCorrelation(cfg.correlation, 'cache', 'get')
const correlation = cfg.mode === 'frozen' ? () => frozen : cfg.correlation

const { proxy, close } = makeProxy('cache', ['get'], cfg.socket, null, correlation)

for (const arg of cfg.calls as string[]) {
  try {
    console.log(`OK ${arg} ${JSON.stringify(proxy.get(arg))}`)
  } catch (error) {
    console.log(`ERR ${arg} ${(error as Error).message}`)
  }
}

close()
console.log('DONE')
process.exit(0)
