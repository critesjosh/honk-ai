# MCP server — moved

The standalone TypeScript MCP server that previously lived here has
been **removed from this fork**. It was never published to npm and is
superseded by the official Aztec MCP server.

## Where the actual MCP server lives

- **npm:** [`@aztec/mcp-server`](https://www.npmjs.com/package/@aztec/mcp-server)
- **GitHub:** [`AztecProtocol/mcp-server`](https://github.com/AztecProtocol/mcp-server)

That package implements the MCP server end users install locally to
query Aztec docs from Claude Desktop, Claude Code, Cursor, Codex, etc.

This DocsGPT-Aztec backend's only role in the MCP flow is to:

1. **Provision personal API keys** via the Discord `/mcp-key` command
   (`extensions/discord/bot.py` → `POST /api/internal/create_mcp_key`).
2. **Serve the search endpoint** at `/api/search`, hit by the MCP server
   when `API_KEY` is present (semantic search over the indexed corpora).

## Configuring an MCP client

Run `/mcp-key` in Discord to get a key, then in your MCP client config:

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "npx",
      "args": ["-y", "@aztec/mcp-server"],
      "env": {
        "API_URL": "https://aztec.adjacentpossible.dev",
        "API_KEY": "<paste your key here>"
      }
    }
  }
}
```

The bot's `/mcp-key` command emits the same snippet for Claude Desktop,
Claude Code, and Codex.

## DocsGPT semantic-search backend

DocsGPT semantic search in `@aztec/mcp-server` is gated on the `API_KEY`
env var and is being added in
[AztecProtocol/mcp-server#18](https://github.com/AztecProtocol/mcp-server/pull/18).
Until that PR merges and a new version publishes, the MCP server runs
in ripgrep-only mode over locally cloned Aztec docs (the `API_KEY` env
is harmlessly ignored). After it lands, semantic search activates
automatically with no client-side config changes.
