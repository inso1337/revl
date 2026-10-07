export function install(server: McpServer, config: { url?: string; pool_size?: number }) {
  const pool = host.Pool.open(config.url ?? 'pg://localhost/app', config.pool_size ?? 10)
  server.registerTool('db_query', {...})
  server.registerTool('db_execute', {...})
  return () => pool.close()
}
