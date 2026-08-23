import { Context, Service } from 'cordis'

export const name = 'mixed'

// A realistic mix: one well-typed operation next to one the author left
// untyped. With --mark-unrecovered the good one is generated and the bad one
// becomes a loud `// UNRECOVERED` marker, so the file still compiles.
export class Registry extends Service {
  constructor(ctx: Context) {
    super(ctx, 'registry')
  }

  /** Look up a plugin's version string. */
  version(id: string): string {
    return this.table.get(id)
  }

  // No type on `spec` — unrecoverable, must not be guessed.
  install(spec): void {
    this.table.set(spec.id, spec.version)
  }
}
