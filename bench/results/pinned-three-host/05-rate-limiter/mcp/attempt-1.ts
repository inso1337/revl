import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer, config: Record<string, unknown>) {
  const map = host.Map.new()

  server.registerTool('limiter_allow', {
    description: "Read the key's entry, returning its marker if present",
    inputSchema: { key: z.string() },
  }, async ({ key }) => ({
    content: [{ type: 'text', text: map.get(key) ?? '' }],
  }))

  server.registerTool('limiter_record', {
    description: 'Insert a marker for the key',
    inputSchema: { key: z.string() },
  }, async ({ key }) => {
    map.insert(key, '1')
    return { content: [{ type: 'text', text: 'ok' }] }
  })

  return () => {
    map.drop()
  }
}
