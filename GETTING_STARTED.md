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

2. Launch ChromaDB using the provided compose file:

```bash
docker compose -f docker-compose.chromadb.yml up -d
```

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
```

Notes

- `AllowedChannelIds`: the bot only responds in these channels (or in their threads). In non-thread channels, you must mention the bot (`@YourBot`).
- `AnalyticsChannelId`: sending any message here shows the analytics UI.
- `DiscordTrainingForums`: Forum channel IDs that will be summarized into the knowledge base.
- `MAX_RESPONSE_TOKENS`: cap the response length. Keep within provider limits. Also consisder the fact reasoning tokens are considered in this

---

## 4) Run the bot

### Option A: Docker

This repo includes a Dockerfile to containerize the app. The compose file is named `docker-compose.yml`. Use `-f` when running compose.

```bash
# Build + run the app container, with db/snapshots mounted
docker compose -f docker-compose.yml up -d --build
```

- The container builds with pnpm, compiles TypeScript, and starts `dist/src/index.js`.
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

## 5) Training overview

When the bot is ready, it kicks off training:

- Discord forum threads: summarizes messages into ChromaDB
- GitHub issues: fetches issues and comments, summarizes into ChromaDB
- Repository training: scheduled weekly (Sunday 00:00) by default

File: `src/training/index.ts`

- Immediate runs on startup: Discord threads + GitHub issues
- Weekly cron: Discord threads, GitHub issues, and repositories
- To run repository training immediately, you can either:
  - Temporarily call it once:
    ```bash
    pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"
    ```
  - Or edit `src/training/index.ts` to call `StartTrainingService()` during startup.

Repository sources are configured in `src/config/training.json`. GitHub issues sources are in `src/config/issues.json`.

---

## 6) Useful scripts

- Update stored docs URLs to the public docs site after training:

```bash
pnpm dlx tsx scripts/update-docs-urls.ts
```

- Delete a specific document or test includes: see scripts in `scripts/`.

---

## 7) Operational tips

- Permissions: ensure the bot has permission to create threads and send messages in the target channels.
- Rate limiting: per-user per-minute limit is controlled via `RequestsPerMinute`.
- Threads: in non-thread channels, you must mention the bot; it will auto-create a thread and remember the topic plus recent turns.
- Persistence: LMDB data is under `./db`; snapshots under `./snapshots`. Both are mounted in Docker compose.

---

## 8) Troubleshooting

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

---
