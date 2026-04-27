# DocsGPT MCP Server for Aztec Network

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that gives LLMs access to the Aztec Network knowledge base via DocsGPT. Install it to query the v4.2.0 corpus from Claude Desktop, Cursor, Claude Code, or any MCP-compatible client.

## What's indexed

The default `Aztec 4.2.0` agent searches all 12 v4.2.0 corpora:

- Aztec Developer Docs (`docs.aztec.network` rendered docs)
- Aztec Network Docs
- Aztec.nr Framework (smart-contract framework, Noir)
- Example Contracts (`noir-contracts`)
- Aztec Protocol Circuits
- aztec.js SDK (TypeScript)
- Aztec CLI + CLI Wallet
- Aztec E2E Tests (working examples in TypeScript)
- Aztec L1 Contracts (Solidity)
- Aztec TypeScript API reference
- Noir Language Docs (the Noir language itself)
- Noir stdlib (`std::hash`, `std::ec`, `BoundedVec`, etc.)

## Getting Your API Key

1. Join the **Noir Discord**: https://discord.com/invite/JtqzkdeQ6G
2. Run the `/mcp-key` slash command
3. The bot will send you a personal API key in a private (ephemeral) message

**Important:** Use the agent API key from Discord. Do NOT use JWT tokens from the DocsGPT login flow.

## Installation

### From Source

```bash
cd extensions/mcp-server
npm install
npm run build
```

### From npm (after publishing)

```bash
npm install -g docsgpt-mcp-server
```

## Configuration

### Claude Desktop

Add to your `claude_desktop_config.json`:

**From source:**

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "node",
      "args": ["/path/to/extensions/mcp-server/dist/index.js"],
      "env": {
        "API_URL": "http://your-docsgpt:7091",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

**From npm (after publishing):**

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "npx",
      "args": ["docsgpt-mcp-server"],
      "env": {
        "API_URL": "http://your-docsgpt:7091",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

### Claude Code

Add to your MCP server settings:

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "node",
      "args": ["/path/to/extensions/mcp-server/dist/index.js"],
      "env": {
        "API_URL": "http://your-docsgpt:7091",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "node",
      "args": ["/path/to/extensions/mcp-server/dist/index.js"],
      "env": {
        "API_URL": "http://your-docsgpt:7091",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_KEY` | Yes | - | Agent API key from the `/mcp-key` Discord command |
| `API_URL` | No | `http://localhost:7091` | URL of the DocsGPT backend |
| `REQUEST_TIMEOUT` | No | `60000` | HTTP request timeout in milliseconds |

## Available Tools

### `ask_aztec`

Ask a question about Aztec Network development. Returns an AI-synthesized answer with source references.

**Parameters:**
- `question` (string, required) - The question to ask
- `chunks` (number, optional, default: 2) - Number of document chunks to retrieve

**Example:** "How do I create a private state variable in Aztec.nr?"

### `search_aztec`

Search the Aztec knowledge base for relevant document chunks. Returns raw text excerpts without AI synthesis.

**Parameters:**
- `query` (string, required) - Search query
- `chunks` (number, optional, default: 5) - Number of results to return

**Example:** "PrivateContext struct definition"

## Alternative Integration

For programmatic integration without MCP, DocsGPT also exposes an OpenAI-compatible API at `/v1/chat/completions` with Bearer token auth using the same agent API key.

## License

MIT
