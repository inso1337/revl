// Leaks a resource: the Map is acquired at install and nothing releases it.
// remove() deletes the registration and calls nothing on the handler, so the
// framework's own unload path has no way to reach it.
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer) {
  const store = host.Map.new()
  server.registerTool('kv_get', {
    description: 'Read a key',
    inputSchema: { key: z.string() },
  }, async ({ key }) => ({ content: [{ type: 'text', text: String(store.get(key) ?? '') }] }))
}
