# DocsGPT for Aztec Development (v4.2.0-aztecnr-rc.2)

Local AI-powered Q&A trained on Aztec Network documentation and source code, pinned to version `4.2.0-aztecnr-rc.2`.

## Quick Start

Prerequisites: Docker and Docker Compose.

```bash
cd /workspaces/sandbox/DocsGPT/deployment
docker compose --env-file ../.env -f docker-compose-hub.yaml up -d
```

Open **http://localhost:5174** in a browser.

## Services

| Service | URL | Notes |
|---------|-----|-------|
| Frontend (UI) | http://localhost:5174 | Chat interface |
| Backend (API) | http://localhost:7091 | REST API |
| Worker | internal | Celery, 28 concurrency (prefork) |
| Redis | localhost:6379 | Broker/cache |
| MongoDB | localhost:27017 | Document store |

Port 5174 is used instead of the default 5173 (already occupied on this machine).

## Stop / Restart

```bash
cd /workspaces/sandbox/DocsGPT/deployment

# Stop all services
docker compose -f docker-compose-hub.yaml down

# Stop and wipe all data (vectors, MongoDB, etc.)
docker compose -f docker-compose-hub.yaml down -v

# Restart
docker compose --env-file ../.env -f docker-compose-hub.yaml up -d

# View logs
docker compose -f docker-compose-hub.yaml logs -f

# View worker logs only
docker compose -f docker-compose-hub.yaml logs worker -f
```

## Configuration

The `.env` file at the repo root (`/workspaces/sandbox/DocsGPT/.env`) controls the LLM provider:

```
LLM_PROVIDER=docsgpt        # Free public API, no key needed
VITE_API_STREAMING=true
INTERNAL_KEY=<generated>     # Worker-to-backend auth
```

To switch to a different LLM provider (e.g., OpenAI, Anthropic), update `.env`:

```
LLM_PROVIDER=openai
API_KEY=sk-...
LLM_NAME=gpt-4o
```

Then restart the backend and worker.

## Trained Data Sources

All source code is extracted from the `aztec-packages` repo at tag `v4.2.0-aztecnr-rc.2`.

### Documentation
| Source | Content |
|--------|---------|
| Aztec v4.2.0 Developer Docs | 94 versioned .md/.mdx files |
| Aztec Developer Docs Current | 114 current developer docs |
| Aztec Operator Docs | 40 operator/setup docs |
| Aztec Application Patterns | Application architecture patterns guide |
| Aztec Smart Contract Patterns | Smart contract patterns guide |

### Source Code
| Source | Content |
|--------|---------|
| Aztec.nr Framework | Core Noir contract framework library (~100 .nr files) |
| Aztec Example Contracts | Reference contract implementations (~200 .nr files) |
| Aztec Protocol Circuits | Internal protocol circuit logic (454 .nr files) |
| aztec.js SDK | TypeScript SDK (71 .ts files) |
| Aztec Accounts | Account contract implementations (24 .ts files) |
| Aztec CLI | CLI tooling (67 .ts files) |
| Aztec CLI Wallet | Wallet CLI (26 .ts files) |
| Aztec E2E Tests | Integration tests showing real usage patterns (243 .ts files) |
| Aztec L1 Contracts | Solidity L1 contracts (347 .sol files) |
| Barretenberg | C++ crypto backend (2421 .hpp/.cpp/.rs/.ts/.sol/.pil/.md files) |

## Querying via API

```bash
# Ask a question
curl -X POST http://localhost:7091/api/answer \
  -H "Content-Type: application/json" \
  -d '{"question":"How do I create a private state variable in Aztec?","history":"[]"}'

# List registered sources
curl -s http://localhost:7091/api/sources | python3 -m json.tool

# Check ingestion task status
curl -s "http://localhost:7091/api/task_status?task_id=<TASK_ID>"
```

## Re-ingesting Documentation

If you need to re-ingest or add new documentation:

1. DocsGPT only supports these file extensions: `.rst`, `.md`, `.pdf`, `.txt`, `.docx`, `.csv`, `.epub`, `.html`, `.mdx`, `.json`, `.xlsx`, `.pptx`
2. Source code files (`.nr`, `.ts`, `.sol`, `.hpp`, `.cpp`, etc.) must be renamed to `.txt` before uploading
3. Upload via the API:

```bash
# Single file
curl -X POST http://localhost:7091/api/upload \
  -F "user=local" \
  -F "name=My Source Name" \
  -F "file=@/path/to/file.md"

# Zip of files (auto-extracted)
curl -X POST http://localhost:7091/api/upload \
  -F "user=local" \
  -F "name=My Source Name" \
  -F "file=@/path/to/archive.zip"
```

The response includes a `task_id` you can poll for completion.

### Ingesting source code (example)

```bash
# Convert .nr files to .txt and zip them
mkdir -p /tmp/my-source
find /path/to/noir/code -name "*.nr" | while read f; do
  dir=$(dirname "$f")
  base=$(basename "$f")
  mkdir -p "/tmp/my-source/$dir"
  cp "$f" "/tmp/my-source/$dir/${base}.txt"
done
cd /tmp/my-source && zip -r /tmp/my-upload.zip .

# Upload
curl -X POST http://localhost:7091/api/upload \
  -F "user=local" \
  -F "name=My Noir Code" \
  -F "file=@/tmp/my-upload.zip"
```

## MCP Server (LLM Integration)

An MCP (Model Context Protocol) server is available so that LLM clients like Claude Desktop, Cursor, or Claude Code can query the Aztec knowledge base directly.

### Getting an API Key

1. Join the **Noir Discord**: https://discord.com/invite/JtqzkdeQ6G
2. Run the `/mcp-key` slash command in any channel
3. The bot responds with your personal API key in a private (ephemeral) message

### Installing the MCP Server

```bash
cd /workspaces/sandbox/DocsGPT/extensions/mcp-server
npm install && npm run build
```

### Configuring Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "aztec-docs": {
      "command": "node",
      "args": ["/workspaces/sandbox/DocsGPT/extensions/mcp-server/dist/index.js"],
      "env": {
        "API_URL": "http://localhost:7091",
        "API_KEY": "your-key-from-discord"
      }
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `ask_aztec` | AI-synthesized Q&A with source references |
| `search_aztec` | Raw vector search returning full-text document chunks |

See `extensions/mcp-server/README.md` for full configuration details (Cursor, Claude Code, etc.).

### Discord Bot `/mcp-key` Command

The Discord bot provisions personal API keys via a slash command. To enable this:

1. Set `MCP_PROVISIONING_KEY` in both the root `.env` and `extensions/discord/.env`
2. Set `NOIR_GUILD_ID` in `extensions/discord/.env` to restrict the command to the Noir Discord
3. Set `AZTEC_SOURCE_IDS` in the root `.env` to the comma-separated ObjectId strings of the Aztec sources
4. Restart the backend and Discord bot

## Known Issues

- **Internet access**: The Docker containers can reach the public internet (via `network_mode: host`). Cloud LLM providers (OpenRouter, Anthropic, OpenAI, etc.) work from the backend and worker containers. The tiktoken tokenizer cache is still pre-downloaded and mounted at `/tmp/tiktoken_cache` for faster startup.

- **Embedding speed**: The sentence-transformers model runs on CPU. Large source sets (like Barretenberg with 2421 files) take a long time to embed. Smaller documentation sources complete first.

## File Layout

```
DocsGPT/
  .env                          # LLM config
  deployment/
    docker-compose-hub.yaml     # Main compose file (modified: port 5174, tiktoken mount)
  application/
    indexes/                    # FAISS vector indexes
    inputs/                     # Uploaded source files
    vectors/                    # Vector store data
```
