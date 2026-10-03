// Clean: one Map, released by the teardown install returns; both tools are
// removed by the host through the handles registerTool returned.
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer) {
  const store = host.Map.new()
  server.registerTool('kv_get', {
    description: 'Read a key',
    inputSchema: { key: z.string() },
  }, async ({ key }) => ({ content: [{ type: 'text', text: String(store.get(key) ?? '') }] }))
  server.registerTool('kv_put', {
    description: 'Write a key',
    inputSchema: { key: z.string(), value: z.string() },
  }, async ({ key, value }) => {
    store.insert(key, value)
    return { content: [{ type: 'text', text: 'ok' }] }
  })
  return () => store.drop()
}
