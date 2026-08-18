import { defineConfig } from 'vitest/config'
import { emitFixtures } from './scripts/emit-fixtures'

// Emit the fixture modules NOW, while this config is being evaluated — vitest
// imports the config before it resolves the `include` glob below.  One of the
// emitted modules is itself a test file (`tests/generated/v3_tests.test.ts`);
// generating it here guarantees it exists on disk before collection, so a cold
// checkout (no `tests/generated/` yet) collects exactly the same files as a
// warm one.  Doing this in a `globalSetup` instead runs it *after* glob
// resolution, silently dropping that file on cold runs.
emitFixtures()

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    // `emitter.test.ts > emits IR v3 test blocks as runnable vitest its`
    // spawns a *nested* `vitest run`, which boots another node process and
    // re-evaluates this config (seven python3 emit.py invocations) before it
    // executes a single assertion. Standalone that takes ~1.4s; sharing the
    // machine with the other files it regularly crossed vitest's 5s default
    // and failed as "Test timed out" — a red herring that says nothing about
    // the emitter, and one that got worse every time a file was added to the
    // suite. The assertion is untouched; only the clock is.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
})
