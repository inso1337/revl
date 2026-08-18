import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globalSetup: './scripts/vitest-global-setup.ts',
    include: ['tests/**/*.test.ts'],
    // `emitter.test.ts > emits IR v3 test blocks as runnable vitest its`
    // spawns a *nested* `vitest run`, which boots another node process and
    // re-runs this global setup (six python3 emit.py invocations) before it
    // executes a single assertion. Standalone that takes ~1.4s; sharing the
    // machine with the other seven files it regularly crossed vitest's 5s
    // default and failed as "Test timed out" — a red herring that says
    // nothing about the emitter, and one that got worse every time a file
    // was added to the suite. The assertion is untouched; only the clock is.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
})
