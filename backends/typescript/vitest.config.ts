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

// The wall-clock bound on ONE generated ts test module's `vitest run`, raised
// from the same `REVL_TS_TIMEOUT` the runner in src/revl/test.py reads (issue
// #2229). 180s there / 60s here is the ratio these two literals already had, so
// an unset variable is exactly today's 60s and an exported `900` gives 300s.
// Vitest's bound is deliberately the LOWER of the two: on a loaded host it then
// reports first, naming the test that ran out of time, instead of the runner's
// outer bound killing the whole run without saying which test was working.
const TS_TIMEOUT_S = Number(process.env.REVL_TS_TIMEOUT || 180)
const TS_TIMEOUT_MS = (TS_TIMEOUT_S * 1000) / 3

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
    testTimeout: TS_TIMEOUT_MS,
    hookTimeout: TS_TIMEOUT_MS,
  },
})
