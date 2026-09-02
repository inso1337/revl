// Load-robust measurement primitives for the codegen benchmarks.
//
// This machine is routinely busy, so the primitives that decide a finding are
// the COUNTING ones: microtask turns, Promise allocations, retained-byte
// deltas. They are deterministic and do not move when another process eats a
// core. Wall clock appears only as an INTERLEAVED RATIO with its spread
// reported, never as an absolute duration.

import { createHook } from 'node:async_hooks'
import { performance } from 'node:perf_hooks'

// ---------------------------------------------------------------------------
// Promise allocation count (exact, deterministic).
//
// `async_hooks` fires `init` for every Promise the VM creates, including the
// implicit ones an `async` function and an `await` allocate. Counting them is
// the cleanest proxy for "how much promise machinery did this shape cost".
// ---------------------------------------------------------------------------
export async function promiseAllocs (fn) {
  let n = 0
  let counting = false
  const hook = createHook({
    init (_id, type) { if (counting && type === 'PROMISE') n++ }
  })
  hook.enable()
  counting = true
  try {
    await fn()
  } finally {
    counting = false
    hook.disable()
  }
  return n
}

// ---------------------------------------------------------------------------
// Microtask depth (exact, deterministic).
//
// A spinner reschedules itself on the microtask queue for as long as the
// workload runs. Because the queue is FIFO and single-threaded, the number of
// spinner turns is the number of microtask turns the workload occupied. No
// timer, no I/O, so OS load does not change the number.
// ---------------------------------------------------------------------------
export async function microtaskTurns (fn) {
  let turns = 0
  let running = true
  const spin = () => { if (running) { turns++; Promise.resolve().then(spin) } }
  spin()
  await fn()
  running = false
  return turns
}

// ---------------------------------------------------------------------------
// Retained-byte delta. Needs --expose-gc; returns null without it. Coarser
// than the counters above but still far steadier than wall clock.
// ---------------------------------------------------------------------------
export async function heapDelta (fn) {
  if (typeof globalThis.gc !== 'function') return null
  globalThis.gc(); globalThis.gc()
  const before = process.memoryUsage().heapUsed
  await fn()
  const after = process.memoryUsage().heapUsed
  globalThis.gc(); globalThis.gc()
  return after - before
}

// ---------------------------------------------------------------------------
// Interleaved A/B ratio.
//
// A and B alternate inside ONE process, one round each, so a load spike lands
// on both sides of the same round. The result is a per-round ratio; we report
// the median and the interquartile range and NEVER the absolute times, which
// are meaningless on a loaded machine.
// ---------------------------------------------------------------------------
export async function ratio (a, b, { rounds = 25, warmup = 5 } = {}) {
  for (let i = 0; i < warmup; i++) { await a(); await b() }
  const rs = []
  for (let i = 0; i < rounds; i++) {
    // alternate which side goes first so a cache-warming order effect cancels
    let ta, tb
    if (i % 2 === 0) {
      let t = performance.now(); await a(); ta = performance.now() - t
      t = performance.now(); await b(); tb = performance.now() - t
    } else {
      let t = performance.now(); await b(); tb = performance.now() - t
      t = performance.now(); await a(); ta = performance.now() - t
    }
    if (tb > 0) rs.push(ta / tb)
  }
  rs.sort((x, y) => x - y)
  const q = (p) => rs[Math.min(rs.length - 1, Math.floor(p * rs.length))]
  return { median: q(0.5), q1: q(0.25), q3: q(0.75), n: rs.length }
}

export function fmtRatio (r) {
  if (!r || !r.n) return 'n/a'
  return `${r.median.toFixed(2)}x (IQR ${r.q1.toFixed(2)}-${r.q3.toFixed(2)}, n=${r.n})`
}
