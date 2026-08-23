import type { Context } from 'cordis'
import { host } from './host.ts'

// cordis exposes ctx.provide/ctx.on at runtime; the cast keeps this file
// free of the module augmentation the emitted backend generates.
type Provider = { provide(key: string, value: unknown): () => void }

// clean: acquires a scratch Map (revertible) even though greet is pure.
export const plugin = {
  name: 'GreeterSvc',
  provide: ['greeter'],
  apply(ctx: Context) {
    ctx.effect(function* () {
      const scratch = host.Map.new()
      yield () => scratch.drop()
      yield (ctx as unknown as Provider).provide('greeter', {
        greet: (name: string) => `hello, ${name}`,
      })
    }, 'GreeterSvc.body')
  },
}
