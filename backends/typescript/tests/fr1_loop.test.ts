// FR-1 (roadmap 77a) — the TS emitter binds arrow parameters in component
// scope (docs/expressible-iteration.md, FEATURE-REQUESTS.md FR-1).
//
// The frontend fix (commit 1debdf2) binds `ExprArrow.params` in provide-method
// scope, so the harness's agent-loop shape compiles: a bounded recursive
// `run_loop` whose callback arrow carries the emission
// (`msgs2 => decode_response(emit model.complete(msgs2))`). This test proves
// the *emitter* half of the same promise on the ts tier — the emitted arrow
// must reference its parameter correctly. Before the fix, emitting this
// composition refused with `reference to unbound name 'msgs2' in component
// 'Agent'`; the arrow rendered its body against the method scope, which did
// not know the parameter.
//
// Driven on real cordis v4 (fixture: tests/fixtures/fr1_loop.ir.json -> the
// generated fr1_loop.ts): a stateful mock `Model` answers tool calls until it
// returns a final answer, and the loop must actually iterate through the
// callback — the second `complete` call's argument is the grown message list,
// which only lands if `msgs2` bound to the lambda parameter.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { Agent, captured, type Step } from './generated/fr1_loop.ts'
import { resetHost } from '../runtime.ts'

beforeEach(() => resetHost())

/** A mock `Model` service: answers each `complete` call from `script`, in
 * order (the last entry repeats). Records every argument list it saw. */
function modelMock(script: string[]) {
  const seen: string[][] = []
  const mock = {
    name: 'ModelMock',
    provide: ['model'],
    apply(ctx: Context) {
      ctx.provide('model', {
        complete: (msgs: string[]): string => {
          seen.push(msgs)
          const at = Math.min(seen.length - 1, script.length - 1)
          return script[at]
        },
      })
    },
  }
  return { mock, seen }
}

async function boot(script: string[]) {
  const ctx = new Context()
  const { mock, seen } = modelMock(script)
  await ctx.plugin(mock)
  const agent = await ctx.plugin(Agent)
  return {
    ctx,
    agent,
    seen,
    dispose: async () => {
      await agent.dispose()
    },
  }
}

describe('FR-1 — arrow parameters bind in provide-method scope (ts tier)', () => {
  it('the emitting callback runs, and the loop iterates through it', async () => {
    const { ctx, seen, dispose } = await boot(['TOOL_CALL m1', 'FINAL done'])
    const result = (await ctx.agent.run('s1')) as Step
    expect(result).toEqual({ kind: 'Final', value: 'done' })
    // the loop took two model steps and the second one saw the grown message
    // list — the arrow's `msgs2` bound to the emitted lambda's parameter
    // (decode_response slices the tool name as the first 10 chars, so the
    // grown list carries "TOOL_CALL ", not "m1")
    expect(seen).toEqual([['prompt'], ['prompt', 'TOOL_CALL ']])
    await dispose()
  })

  it('respects max_steps when the model never finishes', async () => {
    const { ctx, seen, dispose } = await boot(['TOOL_CALL m1'])
    const result = (await ctx.agent.run('s1')) as Step
    expect(result).toEqual({ kind: 'Final', value: 'max_steps exhausted' })
    // bounded by the config default (8): the recursion stopped without
    // running away
    expect(seen.length).toBe(8)
    await dispose()
  })

  it('snapshots captured mutable vars by value at arrow-creation time', () => {
    // `captured()` is `x => x + n` with `n` rebound after the arrow is made;
    // the arrow must see the value at creation (1), not the rebound one (11)
    expect(captured()).toBe(6n)
  })
})
