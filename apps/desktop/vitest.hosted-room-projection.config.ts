import { defineConfig } from 'vitest/config'

// Standalone config for the hosted-room projection unit test: it is pure TS with
// only type-only imports, so it needs neither jsdom nor the React/babel vite
// pipeline. Kept separate from vitest.config.ts (whose `ui` project extends
// vite.config.ts and requires @rolldown/plugin-babel) so the projection suite is
// provable without the full desktop dev install.
export default defineConfig({
  test: {
    name: 'hosted-room-projection',
    environment: 'node',
    include: ['src/plugins/hermes-bots/hosted-room-projection.test.ts'],
    globals: true
  }
})
