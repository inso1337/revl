// CASE: a service operation passed as a function value.
//
// The emitted `run` builds `(async (msgs: any) => (ctx.model.complete(msgs)))`
// on EVERY call. Two separate costs:
//   1. the closure is allocated per call although it captures only `ctx`,
//      which is fixed for the whole activation;
//   2. it is `async` although its body only forwards an already-async call, so
//      every hop through it resolves through the async-function wrapper
//      instead of returning the callee's promise straight to the awaiter.
//
// Emitter site: backends/typescript/emit.py, the arrow branch around the
// `is_async = bool(node.get("async"))` / `prefix = "async " if is_async` lines.

import { Context } from 'cordis'
import { Agent } from '../emitted/async_arrow.ts'

export const name = 'async-arrow-value'
export const summary =
  'a service op passed as a function value is wrapped in a fresh `async` ' +
  'arrow per call; the closure is activation-invariant and the `async` adds a ' +
  'resolution hop over the promise the body already returns'

export const provenance = [
  { file: 'async_arrow.ts', snippet: '(async (msgs: any) => (ctx.model.complete(msgs)))' },
]

const ModelStub = {
  name: 'ModelStub',
  inject: [],
  provide: ['model'],
  apply (ctx: any) {
    ctx.effect(function* () {
      yield ctx.provide('model', { async complete (m: string) { return m } })
    }, 'ModelStub.body')
  },
}

async function makeCtx (): Promise<any> {
  const ctx: any = new Context()
  await ctx.plugin(ModelStub as any).await()
  await ctx.plugin(Agent as any).await()
  return ctx
}

// hand-written activation: the same program, the arrow hoisted out of the
// method and stripped of the redundant `async`
function handAgent (ctx: any) {
  const complete = (msgs: any) => ctx.model.complete(msgs)
  return {
    async run (prompt: string) {
      return (await agentLoop(prompt, complete))
    },
  }
}
async function agentLoop (current: string, complete: (a0: string) => Promise<string>): Promise<string> {
  return (await complete(current))
}

let emittedAgent: any = null
let handAgentImpl: any = null

export async function setup () {
  const ctx = await makeCtx()
  emittedAgent = ctx.agent
  handAgentImpl = handAgent(ctx)
  decolouredImpl = decolouredAgent(ctx)
  const a = await emittedAgent.run('probe')
  const b = await handAgentImpl.run('probe')
  if (a !== b) throw new Error(`async_arrow disagreement: ${a} vs ${b}`)
}

// MINIMAL FIX variant: the arrow is still built fresh on every call, exactly
// as the emitter does today; only the `async` keyword is dropped, which is
// sound because the rendered body contains no `await`. Isolates the colour
// cost from the allocation cost.
function decolouredAgent (ctx: any) {
  return {
    async run (prompt: string) {
      return (await agentLoop(prompt, (msgs: any) => (ctx.model.complete(msgs))))
    },
  }
}
let decolouredImpl: any = null

export const opsPerRun = 1

export const emitted = () => emittedAgent.run('x')
export const handMemo = () => decolouredImpl.run('x')
export const handMemoLabel = 'de-coloured'
export const hand = () => handAgentImpl.run('x')
// used ONLY by `run.mjs --timing` on an idle machine
export const emittedHot = async () => { for (let i = 0; i < 2000; i++) await emittedAgent.run('x') }
export const handHot = async () => { for (let i = 0; i < 2000; i++) await handAgentImpl.run('x') }

// shape metric, read off emitted/async_arrow.ts (not executed)
export const shape = {
  emitted: { closuresPerCall: 1, awaitsPerCall: 2 },  // fresh async arrow per run() call
  hand: { closuresPerCall: 0, awaitsPerCall: 2 },     // arrow hoisted to the activation, not async
}
