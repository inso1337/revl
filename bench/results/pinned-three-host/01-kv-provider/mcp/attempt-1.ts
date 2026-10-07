import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer, config: Record<string, unknown>) {
  const map = host.Map.new()
  const undos: Array<() => void> = []

  server.registerTool('kv_get', {
    description: 'Get a value by key',
    inputSchema: { key: z.string() },
  }, async ({ key }) => {
    const v = map.get(key)
    return { content: [{ type: 'text', text: v ?? '' }] }
  })

  server.registerTool('kv_put', {
    description: 'Insert a key-value pair',
    inputSchema: { key: z.string(), value: z.string() },
  }, async ({ key, value }) => {
    map.insert(key, value)
    undos.push(() => map.remove(key))
    return { content: [{ type: 'text', text: 'ok' }] }
  })

  return () => {
    for (const undo of undos) {
      undo()
    }
    map.drop()
  }
}
