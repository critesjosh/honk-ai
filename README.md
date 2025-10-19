# Honk The Aztec Assistant

Honk is an AI assistant provided by Aztec and interfaced through the [Aztec Discord server](https://discord.gg/aztec).
Honk is trained on all sorts of Aztec related data, including:

- Discord Forum Posts
- Github Repositories
- Github Issues

## Getting Started

See [GETTING_STARTED.md](./GETTING_STARTED.md) for complete setup instructions including:

- Discord Bot configuration and deployment
- MCP Server setup for Claude Desktop, Cursor, and Claude Code
- ChromaDB vector database setup
- Training configuration

The unified Docker Compose setup includes both the Discord bot, MCP server, and ChromaDB with proper dependency ordering and auto-restart capabilities.

## Features

### Discord Bot

- Real-time Q&A on the Aztec Discord server
- Automatic knowledge base updates on new releases
- Trained on latest Aztec documentation and code
- Generate MCP authentication tokens via `/mcp` slash command
- View MCP usage analytics via `/mcp-stats` slash command

### MCP Server (Model Context Protocol)

The integrated MCP server runs alongside the Discord bot (toggle via `MCP_ENABLED` environment variable):

- Query Aztec knowledge base from Cursor, Claude Code, or any MCP-compatible tool
- Token-based authentication with rate limiting
- Direct access to RAG-powered semantic search on Aztec documentation
- JSON-RPC 2.0 protocol with SSE streaming support

## Documentation

See [docs](./docs) for more information, regarding technical details, how Honk works, modify training, etc.
