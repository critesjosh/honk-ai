# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Honk is an AI assistant for the Aztec ecosystem that provides Q&A capabilities through:
- **Discord Bot**: Real-time Q&A in the Aztec Discord server
- **MCP Server**: Model Context Protocol server for integration with Cursor, Claude Code, and Claude Desktop

The system uses RAG (Retrieval-Augmented Generation) with ChromaDB as a vector database, trained on Discord forum posts, GitHub repositories, and GitHub issues related to Aztec and Noir.

## Development Commands

### Basic Operations
```bash
# Install dependencies
pnpm install

# Development (fast, no build)
pnpm run dev-run
# OR
pnpm dlx tsx src/index.ts

# Build TypeScript
pnpm run build

# Run production build
pnpm start

# Run MCP server standalone
pnpm run mcp        # Production (requires build)
pnpm run mcp:dev    # Development
```

### Docker Operations
```bash
# Start all services (ChromaDB + Discord bot + MCP server)
docker compose up -d --build

# View logs
docker compose logs -f

# Stop all services
docker compose down
```

### ChromaDB Setup
```bash
# Create ChromaDB credentials (requires Docker)
mkdir -p chromadb
docker run --rm --entrypoint htpasswd httpd:2 -nbB admin 'yourPassword' > chromadb/server.htpasswd

# Set CHROMADB_AUTH in .env to the generated username:hash line
```

### Training Operations
```bash
# Run repository training immediately (one-time)
pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"

# Update stored docs URLs to public docs site
pnpm dlx tsx scripts/update-docs-urls.ts

# Test include preprocessing
pnpm dlx tsx scripts/test-includes.ts

# Delete specific documents from ChromaDB
pnpm dlx tsx scripts/delete.ts
```

## Architecture

### Entry Points
- **src/index.ts**: Discord bot entry point - initializes client, connects to Discord, starts handlers
- **src/mcp-server.ts**: MCP server entry point - HTTP server for Model Context Protocol integration
- **src/handlers/index.ts**: Initializes LMDB databases, registers slash commands, starts MCP server (if enabled), begins training processes

### Core Systems

#### 1. Discord Bot Handler Flow
- **src/handlers/messages.ts**: Main message handler with rate limiting, thread creation, RAG chain invocation
- Message flow: User message → Rate limit check → Thread creation (if needed) → Chat history retrieval → RAG query → Response chunking → Feedback buttons
- Rate limiting: Per-user requests tracked in memory with 60-second expiration
- Thread history: Fetches last N messages (configured via `MessageHistoryPerThread`), merges consecutive messages from same speaker, keeps last 4 merged turns
- Response formatting: Chunks responses to fit Discord's 4096-char embed limit, sources on last chunk or separate embed if needed

#### 2. RAG System (src/utils/llm.ts)
- Invokes RAG chain: Generate embedding → Similarity search ChromaDB → Format context → Call LLM (OpenAI or Anthropic)
- Provider selection via `LLM_PROVIDER` env var
- Chat history support: Converts conversation history to provider-specific format
- Token management: Configurable via `MAX_RESPONSE_TOKENS`, includes thinking tokens for Anthropic

#### 3. Training System (src/training/)
- **Immediate on startup**: Discord threads + GitHub issues
- **Weekly cron (Sunday 00:00)**: Discord threads, GitHub issues, repositories
- **Hourly polling**: Checks for new aztec-packages releases, triggers full re-indexing on detection

**Training Sources:**
- **Discord** (src/training/discord/): Fetches forum threads, summarizes with LLM, stores to ChromaDB
- **GitHub Issues** (src/training/issues/): Configured in src/config/issues.json, fetches issues + comments, summarizes
- **Repositories** (src/training/repos/): Configured in src/config/training.json, clones repos, parses code into chunks, stores to ChromaDB
  - Parsers for: TypeScript/JavaScript (src/training/repos/parsing/typescript/), Noir (src/training/repos/parsing/noir/), Markdown (src/training/repos/parsing/markdown/)
  - Markdown preprocessing (src/training/repos/preprocessing/codeInclusion.ts): Resolves `#include_code` directives before storage
  - Smart chunking: Small Noir/Rust files (<1500 tokens) stored whole; large files chunked by parser
  - Cleanup: Deletes old documents for repository before re-indexing

#### 4. Release Tracking (src/training/releasePoller.ts)
- **Auto mode** (AZTEC_DOCS_VERSION not set): Polls GitHub hourly for new stable releases, auto re-indexes
- **Manual mode** (AZTEC_DOCS_VERSION set): Detects version changes in env, triggers re-index on change
- Version change detection: Stores last version in LMDB `releaseTracking` database, compares on startup and hourly

#### 5. MCP Server (src/mcp-server.ts)
- HTTP server using Fastify with JSON-RPC 2.0 protocol
- Endpoints: GET /mcp (SSE streaming), POST /mcp (JSON-RPC requests), GET /health
- Authentication: Token-based (tokens generated via Discord `/mcp` slash command), stored in LMDB `mcpTokens` database
- Rate limiting: Per-token, configurable requests per minute (default: 10)
- Session management: In-memory sessions with 5-minute inactivity cleanup
- Available tool: `query_knowledge` - semantic search against ChromaDB with configurable result limit (1-10, default 4)
- Analytics: Tracks all requests in LMDB `mcpRequests` database for usage stats

#### 6. Slash Commands (src/commands/)
- **/mcp** (src/commands/mcp.ts): Generates persistent authentication token for MCP server, shows setup instructions
- **/mcp-stats** (src/commands/mcp-stats.ts): Shows usage statistics (personal for all users, enhanced global stats for ADMIN_USERS)
- **/analytics** (src/commands/analytics.ts): Shows response analytics with thumbs up/down ratings (triggered by sending any message in AnalyticsChannelId)

### Data Storage (LMDB)

All databases stored in `./db/` directory (configurable via `LMDB_ROUTE`):
- **analytics**: Total questions, ratings counters
- **documents**: ChromaDB document cache
- **responses**: Bot responses with ratings, sources, embeddings
- **questions**: User questions for analytics
- **trainedThreads**: Discord threads already processed
- **trainedIssues**: GitHub issues already processed
- **releaseTracking**: Last known aztec-packages version for change detection
- **mcpTokens**: MCP authentication tokens (userId → token mapping)
- **mcpRequests**: MCP request history for analytics

### Environment Configuration (src/env.ts)

Critical environment variables (see GETTING_STARTED.md for complete list):
- **Discord**: BOT_TOKEN, BOT_ID, AllowedChannelIds, AnalyticsChannelId, MessageHistoryPerThread, DiscordTrainingForums
- **ChromaDB**: CHROMADB_URL_PATH, CHROMADB_URL_PORT, CHROMADB_URL_SSL, CHROMADB_KNOWLEDGEBASE_ID, CHROMADB_AUTH
- **LLM**: LLM_PROVIDER (openai|anthropic), OPENAI_MODEL, ANTHROPIC_MODEL, ANTHROPIC_THINKING, MAX_RESPONSE_TOKENS
- **Training**: AZTEC_DOCS_VERSION (optional - pins to specific version/branch, disables auto-release polling), TRAINING_LIMIT
- **MCP**: MCP_ENABLED, MCP_PORT, MCP_SERVER_URL, MCP_AUTH_REQUIRED, MCP_RATE_LIMIT_ENABLED, MCP_RATE_LIMIT_PER_MINUTE
- **Storage**: LMDB_ROUTE, SNAPSHOT_ROUTE
- **GitHub**: GITHUB_TOKEN (for training)

## Development Patterns

### Adding New Training Sources

1. Add repository to `src/config/training.json` with patterns for file types to train on
2. For GitHub issues, add to `src/config/issues.json`
3. Training runs automatically on weekly cron or can be triggered manually

### Modifying Code Parsers

Code parsers live in `src/training/repos/parsing/`:
- Each parser returns `Data[]` with `{ title, content, start, end }` chunks
- TypeScript/JS: Uses AST to extract functions, classes, interfaces
- Noir: Extracts functions, structs, impls
- Markdown: Chunks by headers with configurable depth

### ChromaDB Document Format

Documents stored with:
- **id**: Unique identifier (e.g., `{repoUrl}/{path} {title}`)
- **document**: Full text content with title header
- **metadata**: `{ link: string, date: number }` (link is GitHub URL or docs website URL)
- **embeddings**: Generated using OpenAI's embedding model (configured via `EMBEDDING_MODEL`)

### LLM Provider Switching

System supports both OpenAI and Anthropic:
- Set `LLM_PROVIDER=openai` or `LLM_PROVIDER=anthropic`
- Both providers use unified interface in `src/utils/llm.ts`
- Provider-specific implementations in `src/utils/models/openai.ts` and `src/utils/models/anthropic.ts`
- Anthropic supports extended thinking mode via `ANTHROPIC_THINKING=true`

### Response Formatting

Responses chunked to fit Discord's embed limits:
- Max 4096 chars per embed
- Sources appended to last chunk if space allows, otherwise separate embed
- Multiple embeds sent as separate messages
- Feedback buttons only on final message

## Testing & Debugging

### Testing MCP Server
```bash
# Test MCP server health
curl http://localhost:3000/health

# Test with Claude Code
claude mcp add --transport http aztec-knowledge --scope local http://localhost:3000/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

### Debugging Training
- Training logs output to console during execution
- Check LMDB databases in `./db/` for stored data
- Use `scripts/test-includes.ts` to debug markdown include preprocessing
- Use `scripts/delete.ts` to remove specific documents from ChromaDB

### Common Issues
- **ChromaDB auth errors**: Ensure `CHROMADB_AUTH` is the exact `username:hash` line from `chromadb/server.htpasswd`
- **Bot not responding**: Check `AllowedChannelIds`, verify bot mention in non-thread channels, check Discord intents enabled
- **MCP 401 errors**: Generate new token via Discord `/mcp` command, verify token in `Authorization: Bearer <token>` header
- **Training failures**: Check `GITHUB_TOKEN` has repo read access, verify repo/branch exists in training config

## Production Deployment

The Docker Compose setup includes:
- **chromadb**: ChromaDB vector database with basic auth
- **honk-ai**: Discord bot + MCP server (integrated)
- **cloudflared**: Cloudflare Tunnel for public MCP access (optional)

All services have `restart: unless-stopped` for auto-restart on failures and system boots.

For production MCP usage, update `MCP_SERVER_URL` to your public domain in `.env`.
