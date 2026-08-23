import { Context } from 'cordis'

// The other common Cordis shape: a functional plugin that registers a plain
// object at a service key with `ctx.provide`, rather than a `Service` class.
export const name = 'greeter'
export const inject = ['i18n']

export function apply(ctx: Context) {
  ctx.provide('greeter')
  ctx.greeter = {
    /** Build a greeting. A pure string computation. */
    greet(name: string): string {
      return `hello, ${name}`
    },

    /** Record that a greeting was sent. Writes to the host log. */
    record(name: string, at: number): void {
      ctx.logger.info(name, at)
    },
  }
}
