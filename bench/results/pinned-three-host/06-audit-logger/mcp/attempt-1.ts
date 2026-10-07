import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer, config: { url?: string; pool_size?: number }) {
  const pool = host.Pool.open(config.url ?? 'pg://localhost/app', config.pool_size ?? 10)

  server.registerTool('db_query', {
    description: 'Run a read query',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => ({
    content: [{ type: 'text', text: JSON.stringify(pool.query(sql)) }],
  }))

  server.registerTool('db_execute', {
    description: 'Run a statement',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => ({
    content: [{ type: 'text', text: String(pool.execute(sql)) }],
  }))

  server.registerTool('audit_log', {
    description: 'Log an audit event',
    inputSchema: { event: z.string() },
  }, async ({ event }) => {
    pool.execute(`INSERT INTO audit_log (event) VALUES ('${event}')`)
    return { content: [{ type: 'text', text: 'ok' }] }
  })

  return () => pool.close()
}
