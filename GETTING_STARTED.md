### Honk AI — Getting Started

This guide walks you through running the Discord bot locally or with Docker, configuring ChromaDB, and wiring up training (Discord forums, GitHub issues, repositories).

---

## Prerequisites

- Node.js 22+ (matches the Docker image)
- pnpm (or use corepack)
- Docker + Docker Compose (for ChromaDB and/or running the bot in containers)

Optional (for repo training)

- Git installed on the host (container already installs git)

---

## Create a Discord Bot

https://discord.com/developers/applications

---

## 2) Start ChromaDB (Vector DB)

This project expects an external ChromaDB with basic auth.

1. Create credentials file for the ChromaDB server:

```bash
# Create the folder if it doesn't exist
mkdir -p chromadb

# Generate a bcrypt htpasswd entry: username "admin" with your chosen password
# Requires Docker installed
docker run --rm --entrypoint htpasswd httpd:2 -nbB admin 'yourPassword' > chromadb/server.htpasswd
```

2. ChromaDB is now included in the main Docker Compose setup and will start automatically with the bot.

ChromaDB will be available on `localhost:8001` with basic auth. Keep `chromadb/server.htpasswd` safe.

3. Client credentials for the bot

- The htpasswd file stores `username:hash` (e.g., `admin:$2y$...`).
- The bot is configured to send `Authorization: Basic <username:hash>` directly.
- Set `CHROMADB_AUTH` in `.env` to exactly the `username:hash` line from `chromadb/server.htpasswd`.

Example:

```bash
# .env
CHROMADB_AUTH="admin:$2y$05$KcpzhLxxTNskFo3rwWISROvFFQBwDaYwbo5u9aaeFTxJfa7humHyW"
```

---

## 3) Configure environment variables

Create a `.env` file at the project root. You can use the provided example as a base:

```bash
cp .env-example .env
# then edit .env to add your real tokens/IDs (Discord, OpenAI/Anthropic, GitHub)
```

```bash
# Discord
BOT_TOKEN="YOUR_DISCORD_BOT_TOKEN"
BOT_ID="YOUR_APPLICATION_ID"
AllowedChannelIds="123456789012345678,234567890123456789"
LoggingChannelId="123456789012345678"
AnalyticsChannelId="123456789012345678"
MessageHistoryPerThread="3"
DiscordTrainingForums="111122223333444455,666677778888999900"  # comma-separated forum channel IDs
RequestsPerMinute="3"
ADMIN_USERS="123456789012345678,234567890123456789"  # (optional) comma-separated Discord user IDs for enhanced MCP analytics

# ChromaDB
CHROMADB_URL_PATH="localhost"
CHROMADB_URL_PORT="8001"
CHROMADB_URL_SSL="false"
CHROMADB_KNOWLEDGEBASE_ID="knowledge-base"
CHROMADB_AUTH="admin:$2y$05$..."   # copy username:hash from chromadb/server.htpasswd

# Retrieval
AMOUNT_OF_DOCS="4"

# LLM provider (choose one)
LLM_PROVIDER="openai"           # or "anthropic"
# OpenAI
OPENAI_MODEL="gpt-5"      # example; set the model you plan to use
OPEN_AI_API="sk-openai-..."
EMBEDDING_MODEL="text-embedding-3-large"
# Anthropic
ANTHROPIC_API="sk-ant-..."
ANTHROPIC_MODEL="claude-sonnet-4-20250514"
ANTHROPIC_THINKING="false"
MAX_RESPONSE_TOKENS="8500"

# GitHub (for issues + repo training)
GITHUB_TOKEN="ghp_..."          # token with repo read access

# Storage
LMDB_ROUTE="./db/"              # persisted by volume when using Docker
SNAPSHOT_ROUTE="./snapshots/"

# Docs/testnet tag used in markdown processing
DOCS_TESTNET_TAG="v1.2.0"

# MCP Server (integrated with bot)
MCP_ENABLED="true"              # set to "false" to disable the MCP server
MCP_PORT="3000"                 # port for the MCP server
MCP_SERVER_URL="http://localhost:3000"  # public URL for Discord /mcp command
MCP_AUTH_REQUIRED="true"        # require authentication tokens
MCP_RATE_LIMIT_ENABLED="true"   # enable rate limiting
MCP_RATE_LIMIT_PER_MINUTE="10"  # requests per minute per token

# Cloudflare Tunnel (optional - for public MCP access)
CLOUDFLARED_CONFIG_PATH="/home/user/.cloudflared/config.yml"
CLOUDFLARED_CREDENTIALS_PATH="/home/user/.cloudflared/credentials.json"
```

Notes

- `AllowedChannelIds`: the bot only responds in these channels (or in their threads). In non-thread channels, you must mention the bot (`@YourBot`).
- `AnalyticsChannelId`: sending any message here shows the analytics UI.
- `DiscordTrainingForums`: Forum channel IDs that will be summarized into the knowledge base.
- `ADMIN_USERS`: (optional) comma-separated Discord user IDs. Users in this list will see enhanced global analytics (total requests, unique users, averages) when running the `/mcp-stats` slash command. If not set or empty, all users see only their personal statistics.
- `MAX_RESPONSE_TOKENS`: cap the response length. Keep within provider limits. Also consisder the fact reasoning tokens are considered in this
- `AZTEC_DOCS_VERSION`: (optional) controls which version of aztec-packages to train on. Can be:
  - A stable release tag (e.g., `"aztec-packages-v2.0.1"`)
  - A nightly version (e.g., `"v3.0.0-nightly.20251027"`)
  - A branch name (e.g., `"my-feature-branch"`)
  - If not set (or empty), the bot automatically tracks the latest stable release and re-indexes hourly when new releases are detected.
  - **When explicitly set, automatic release detection is disabled**, allowing you to pin to a specific version.
- `MCP_ENABLED`: controls whether the MCP server starts with the bot. Set to `"false"` to run only the Discord bot.
- `MCP_SERVER_URL`: the public URL shown in the `/mcp` Discord command. Change this to your production domain when deploying.
- `CLOUDFLARED_CONFIG_PATH` & `CLOUDFLARED_CREDENTIALS_PATH`: (optional) paths to Cloudflare Tunnel configuration files. Only needed if using the cloudflared service for public MCP access. Set these to your local `.cloudflared` directory paths.

---

## 4) Run the bot

### Option A: Docker

This repo includes a Dockerfile to containerize the app. The unified `docker-compose.yml` now includes both the bot and ChromaDB services.

```bash
# Build + run both ChromaDB and the app container
docker compose up -d --build
```

- ChromaDB starts first, then the bot starts automatically with proper dependency ordering
- The container builds with pnpm, compiles TypeScript, and starts `dist/src/index.js`.
- Both services have `restart: unless-stopped` so they'll start automatically when your computer boots
- Ensure your `.env` is present at the repo root prior to starting.

### Option B: Local development

```bash
# Install dependencies
pnpm install

# Fast dev run (package script)
pnpm run dev-run

# Or run in dev without building (tsx via dlx so you don't need it installed)
pnpm dlx tsx src/index.ts

# Or build + run
pnpm run build
node dist/src/index.js
```

If you prefer installing `tsx`, add it as a dev dependency:

```bash
pnpm add -D tsx
pnpm tsx src/index.ts
```

---

## 5) MCP Server Usage

The MCP (Model Context Protocol) server runs integrated with the Discord bot and provides access to Honk's knowledge base from Cursor, Claude Code, and other MCP-compatible tools.

### Getting Your Authentication Token

1. Run the `/mcp` slash command in the Discord server where the bot is running
2. The bot will then reply with a unique authentication token and setup instructions
3. Your token is persistent - you only need to get it once

### Viewing Your Usage Statistics

Run the `/mcp-stats` slash command to see your MCP usage statistics:
- Personal request counts (24h, 7d, 30d, lifetime)
- Admin users (configured via `ADMIN_USERS` env variable) also see:
  - Total requests across all users
  - Unique user counts
  - Average requests per user

### Setup for Claude Code

Run one of these commands in your terminal:

```bash
# Local scope (current project only)
claude mcp add --transport http aztec-knowledge --scope local http://localhost:3000/mcp --header "Authorization: Bearer YOUR_TOKEN"

# User scope (all your projects)
claude mcp add --transport http aztec-knowledge --scope user http://localhost:3000/mcp --header "Authorization: Bearer YOUR_TOKEN"

# Project scope (shared via .mcp.json)
claude mcp add --transport http aztec-knowledge --scope project http://localhost:3000/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

Replace `YOUR_TOKEN` with the token from the `/mcp` command, and update the URL if deploying to production.

### Setup for Cursor

1. Open Cursor settings (Cmd/Ctrl + ,)
2. Search for "MCP" or "Model Context Protocol"
3. Add this configuration:

```json
{
  "url": "http://localhost:3000/mcp",
  "headers": {
    "Authorization": "Bearer YOUR_TOKEN"
  }
}
```

### Using the MCP Server

Once configured, you can query the Aztec knowledge base using the `query_knowledge` tool:

```
Query: "How do I deploy an Aztec contract?"
```

The server will return relevant documentation chunks with source links and relevance scores.

### Disabling the MCP Server

To run only the Discord bot without the MCP server, set in your `.env`:

```bash
MCP_ENABLED="false"
```

---

## 6) Training overview

When the bot is ready, it kicks off training:

- Discord forum threads: summarizes messages into ChromaDB
- GitHub issues: fetches issues and comments, summarizes into ChromaDB
- Repository training: scheduled weekly (Sunday 00:00) by default
- **Automatic re-indexing**: polls GitHub every hour for new aztec-packages releases and automatically re-indexes when detected

File: `src/training/index.ts`

- Immediate runs on startup: Discord threads + GitHub issues
- Weekly cron: Discord threads, GitHub issues, and repositories
- Hourly polling: checks for new releases and triggers full re-indexing when found
- To run repository training immediately, you can either:
  - Temporarily call it once:
    ```bash
    pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"
    ```
  - Or edit `src/training/index.ts` to call `StartTrainingService()` during startup.

Repository sources are configured in `src/config/training.json`. GitHub issues sources are in `src/config/issues.json`.

### Automatic Release Detection & Version Change Detection

The bot monitors for changes every hour and automatically re-indexes when needed:

**When `AZTEC_DOCS_VERSION` is NOT set (auto mode):**
- Checks GitHub for new stable releases (non-prerelease only)
- Automatically re-indexes when a new release is detected
- Logs events to Discord

**When `AZTEC_DOCS_VERSION` IS set (manual mode):**
- Skips automatic release polling
- Detects when you change the configured version in `.env`
- Automatically re-indexes when the version changes
- Supports:
  - Stable releases (e.g., `"aztec-packages-v2.0.1"`)
  - Nightly builds (e.g., `"v3.0.0-nightly.20251027"`)
  - Custom branches (e.g., `"my-feature-branch"`)

**To trigger re-indexing:** Simply change `AZTEC_DOCS_VERSION` in your `.env` file and restart the bot. It will detect the change and automatically retrain on startup.

---

## 7) Useful scripts

- Update stored docs URLs to the public docs site after training:

```bash
pnpm dlx tsx scripts/update-docs-urls.ts
```

- Delete a specific document or test includes: see scripts in `scripts/`.

---

## 8) Operational tips

- Permissions: ensure the bot has permission to create threads and send messages in the target channels.
- Rate limiting: per-user per-minute limit is controlled via `RequestsPerMinute`.
- Threads: in non-thread channels, you must mention the bot; it will auto-create a thread and remember the topic plus recent turns.
- Persistence: LMDB data is under `./db`; snapshots under `./snapshots`. Both are mounted in Docker compose.

---

## 9) Troubleshooting

- Bot not responding
  - Check that you’re chatting in an `AllowedChannelIds` channel, or its thread
  - In non-thread channels, ensure you mentioned the bot (`<@BOT_ID>`) and used the correct `BOT_ID`
  - Verify Discord privileged intents are enabled and saved in the Developer Portal
  - Check container logs or terminal output for errors
- ChromaDB auth errors
  - Ensure `CHROMADB_AUTH` is base64 of `username:password` (not the htpasswd hash) and that `chromadb/server.htpasswd` contains a matching user
  - Confirm ChromaDB is running on `localhost:8001` and `CHROMADB_URL_SSL=false`
- OpenAI/Anthropic errors
  - Verify API keys, selected model names, and token limits
- GitHub training issues
  - Ensure `GITHUB_TOKEN` has repo read scopes and your config in `src/config/issues.json` and `src/config/training.json` points to valid repos
- MCP Server not starting
  - Verify `MCP_ENABLED="true"` in your `.env` file
  - Check that port `MCP_PORT` (default 3000) is not already in use
  - Review logs for Fastify startup errors
- MCP authentication failures (401 errors)
  - Ensure you're using the token from the Discord `/mcp` command
  - Verify the `Authorization: Bearer <token>` header format is correct
  - Check that the bot has initialized the `mcpTokens` database
- MCP rate limiting (429 errors)
  - Default limit is 10 requests per minute per token
  - Adjust `MCP_RATE_LIMIT_PER_MINUTE` in `.env` if needed
  - Or disable rate limiting with `MCP_RATE_LIMIT_ENABLED="false"`

---
