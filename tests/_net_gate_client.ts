// A real node/ts consumer of the py `Gate` over the **TCP + mutual TLS** network
// path (item 149) — the production code path: it drives the same
// backends/typescript/bridge.ts::makeProxy the placement runner uses, only
// without cordis so the transport can be proven in isolation.
//
// Config JSON, argv[2]:
//   { endpoint: { host, port, tls: { cert, key, ca, identity, server_hostname? } },
//     deadlineMs?: number, calls?: [method, arg][], watch?: bool, watchMs?: number }
//
// Prints one `VERDICT <arg> <json>` per call (or `CALL SEAM_DEADLINE|ERROR ...`),
// `WITHDRAWN` when the proxy withdraws (deadline breach or peer death), and a
// final `DONE`/`DONE withdrawn` line before exiting.

import fs from 'node:fs'
import { makeProxy, SeamDeadlineError } from '../backends/typescript/bridge.ts'

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const deadlineMs: number | null = cfg.deadlineMs ?? null

const { proxy, onPeerLost, monitorReady, close } = makeProxy(
  'gate', ['admit', 'admit_case'], cfg.endpoint, deadlineMs)

let withdrawn = false
onPeerLost(() => {
  withdrawn = true
  console.log('WITHDRAWN')
})

for (const [method, arg] of (cfg.calls ?? []) as [string, string][]) {
  try {
    const value = proxy[method](arg)
    console.log(`VERDICT ${arg} ${String(value)}`)
  } catch (error) {
    const kind = error instanceof SeamDeadlineError ? 'SEAM_DEADLINE' : 'ERROR'
    console.log(`CALL ${kind} ${arg} ${(error as Error).message}`)
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

if (cfg.watch) {
  // Hold a live monitor and report a peer-death withdrawal (a dropped remote
  // provider): the mTLS monitor connection EOFs and fires onPeerLost. Wait for
  // the monitor handshake to actually complete before signalling readiness —
  // the blocking seam call above starves the event loop, so the mTLS
  // secureConnect may still be pending right after it returns.
  await monitorReady
  console.log('WATCHING')
  const until = Date.now() + (cfg.watchMs ?? 5000)
  while (!withdrawn && Date.now() < until) await sleep(50)
} else {
  await sleep(200)
}

close()
console.log(withdrawn ? 'DONE withdrawn' : 'DONE')
process.exit(0)
