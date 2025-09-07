# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Building and Running
- `pnpm run build` - Compile TypeScript to JavaScript in dist/
- `pnpm run start` - Run the compiled bot from dist/src/index.js
- `pnpm run dev-run` - Run directly from TypeScript source using tsx
- `pnpm run docker-deploy` - Build and run with Docker Compose

### Development Tools
- `pnpm dlx tsx src/index.ts` - Run TypeScript directly without building
- `pnpm dlx tsx scripts/update-docs-urls.ts` - Update stored docs URLs after training
- `pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"` - Run repository training immediately

## Architecture Overview

Honk is an AI Discord bot that uses RAG (Retrieval-Augmented Generation) to answer questions about Aztec Protocol and Noir. The bot is trained on Discord forums, GitHub issues, and repositories.

### Core Components
- **Discord Client**: src/index.ts initializes discord.js client and delegates to handlers/
- **Message Handlers**: src/handlers/ manages Discord events and interactions
- **LLM Integration**: src/utils/llm.ts coordinates with OpenAI/Anthropic models
- **Vector Storage**: ChromaDB for embeddings and document retrieval
- **Training Services**: src/training/ handles data ingestion from Discord, GitHub issues, and repositories
- **Persistence**: LMDB databases for analytics, responses, and training state

### Message Flow
1. Discord message → src/handlers/messages.ts filters by AllowedChannelIds
2. Context building with thread history (up to MessageHistoryPerThread turns)
3. RAG pipeline: embed query → ChromaDB retrieval → LLM generation
4. Response formatting with source citations and Discord embed chunking
5. Storage in LMDB for analytics and feedback tracking

### Training Pipeline
- **Discord Forums**: Summarizes forum threads from DiscordTrainingForums channels
- **GitHub Issues**: Fetches and processes issues from repos in src/config/issues.json  
- **Repositories**: Clones and processes repos from src/config/training.json with language-specific parsers
- **Scheduling**: Immediate run on startup, weekly cron for full retraining

### Configuration Files
- **src/config/training.json**: Repository training sources with file patterns
- **src/config/issues.json**: GitHub repositories for issue training
- **src/config/prompts.ts**: System prompts and response templates

### Environment Setup
- Requires .env with Discord bot tokens, LLM API keys, ChromaDB credentials
- ChromaDB runs externally with basic auth (configured in chromadb/server.htpasswd)
- Supports both OpenAI and Anthropic models via LLM_PROVIDER setting

### Key Features
- Per-user rate limiting (RequestsPerMinute)
- Automatic thread creation in non-thread channels
- Multi-embed response chunking for Discord limits
- Feedback collection with 👍/👎 reactions
- Analytics tracking in dedicated Discord channel

### Development Notes
- Uses ES modules (type: "module" in package.json)
- TypeScript with strict mode enabled
- PNPM workspace configuration
- Docker support with unified compose setup including ChromaDB
- LMDB persistence under ./db/ and ./snapshots/ directories