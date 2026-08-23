import { Context, Service } from 'cordis'

export const name = 'untyped'
export const provide = 'weather'

// A plugin whose method is written in the loosest TypeScript: no parameter
// type at all. There is nothing to recover, and the importer must refuse
// rather than invent a plausible-looking `Str`.
export class Weather extends Service {
  constructor(ctx: Context) {
    super(ctx, 'weather')
  }

  // No annotation on `city`, no JSDoc — the signature cannot be recovered.
  forecast(city): string {
    return 'sunny in ' + city
  }
}
