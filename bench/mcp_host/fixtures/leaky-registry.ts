// Leaks a registration: a second tool registered after install returned, so
// the host never saw its handle and never removed it.
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'

let n = 0
export function install(server: McpServer) {
  server.registerTool('ping', { description: 'Ping' },
    async () => ({ content: [{ type: 'text', text: 'pong' }] }))
  const late = `late_${n++}`
  setTimeout(() => {
    server.registerTool(late, { description: 'Registered late' },
      async () => ({ content: [{ type: 'text', text: 'late' }] }))
  }, 0)
}
