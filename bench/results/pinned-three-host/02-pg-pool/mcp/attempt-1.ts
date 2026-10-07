import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer, config: { url: string; pool_size?: number }) {
  const pool = host.Pool.open(config.url, config.pool_size ?? 10)

  server.registerTool('db_query', {
    description: 'Run a read query',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => ({
    content: [{ type: 'text', text: JSON.stringify(pool.query(sql)) }],
  }))

  server.registerTool('db_execute', {
    description: 'Run a statement',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => {
    const result = pool.execute(sql)
    return { content: [{ type: 'text', text: String(result) }] }
  })

  return () => pool.close()
}
