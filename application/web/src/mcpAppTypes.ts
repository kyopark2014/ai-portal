/** MCP Apps iframe 페이로드(html + 초기 toolResult). */
export interface McpAppPayload {
  resourceUri: string;
  html: string;
  tool: string;
  mcpServer?: string;
  mimeType?: string;
  csp?: Record<string, unknown>;
  toolResult?: {
    content: Array<{ type: string; text?: string; [key: string]: unknown }>;
    isError?: boolean;
  };
}
