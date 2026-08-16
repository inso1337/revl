import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globalSetup: './scripts/vitest-global-setup.ts',
    include: ['tests/**/*.test.ts'],
  },
})
