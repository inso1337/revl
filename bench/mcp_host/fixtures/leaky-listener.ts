// Leaks a listener on a process-wide emitter that outlives the tool pack.
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'

export function install(server: McpServer) {
  process.on('beforeExit', () => {})
  server.registerTool('ping', { description: 'Ping' },
    async () => ({ content: [{ type: 'text', text: 'pong' }] }))
}
