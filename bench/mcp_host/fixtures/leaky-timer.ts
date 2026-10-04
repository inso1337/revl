// Leaks a timer: a periodic flush that nothing clears.
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'

export function install(server: McpServer) {
  const flush = setInterval(() => {}, 60_000)
  flush.unref()
  server.registerTool('ping', { description: 'Ping' },
    async () => ({ content: [{ type: 'text', text: 'pong' }] }))
}
