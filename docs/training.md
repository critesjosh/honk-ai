## Training Guide

This guide explains how Honk ingests knowledge from Discord forums, GitHub issues, and GitHub repositories into ChromaDB for RAG.

### What gets trained

- **Discord forums**: Summaries of entire threads from configured forum channels
- **GitHub issues**: Summaries of issues and their comments from configured repositories
- **GitHub repositories**: Code and docs processed per patterns; parsed/chunked and stored with source links

### Prerequisites

- ChromaDB running and reachable (see `GETTING_STARTED.md`)
- `.env` configured with:
  - `DiscordTrainingForums` (comma-separated forum channel IDs)
  - `GITHUB_TOKEN` (repo read access)
  - Chroma config and auth (see `GETTING_STARTED.md`)

### Schedules

- On bot ready: runs
  - **Discord Threads** training
  - **GitHub Issues** training
- Weekly (cron: Sunday 00:00): runs
  - **Discord Threads**, **GitHub Issues**, **Repositories** training

See `src/training/index.ts` for the scheduler.

### Source details

- **Discord forums** (`src/training/discord/index.ts`)
  - Reads `env.DiscordTrainingForums` (comma-separated IDs)
  - Fetches active + archived threads; pulls all messages
  - Summarizes via `getDiscordThreadsPrompt()` and upserts per-thread doc
  - Source link: Discord thread URL
  - De-dup via LMDB `trainedThreads`

- **GitHub issues** (`src/training/issues/index.ts`)
  - Reads `src/config/issues.json`
  - Uses Octokit with `GITHUB_TOKEN`
  - Skips bot-authored content, summarizes issues+comments
  - Upserts with issue URL as source
  - De-dup via LMDB `trainedIssues`

- **Repositories** (`src/training/repos/index.ts`)
  - Reads `src/config/training.json`
  - Clones repo/branch, cleans old documents for that repo
  - Processes by patterns:
    - Markdown: resolves `#include_code`, stores full docs, and/or chunks sections
    - Noir/Rust small files: stored whole if under a token threshold
    - TS/JS: parsed and chunked
  - Converts Aztec docs paths to website URLs when applicable
  - Batches upserts; logs progress and failures

### Configure sources

- Discord forums: see `docs/adding-discord-forums.md`
- GitHub issues: see `docs/adding-github-issues.md`
- GitHub repos: see `docs/adding-github-repos.md`

### Run training immediately

- Repositories (one-off):

```bash
pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"
```

- Discord Threads / GitHub Issues: triggered on startup; restart the bot to re-run.

### De-duplication and reset

- Discord: `trainedThreads` LMDB set prevents re-training the same thread ID
- Issues: `trainedIssues` LMDB set prevents re-training the same issue number
- Reset options:
  - Delete specific records from LMDB (advanced) in `env.LMDB_ROUTE`
  - For repositories, old documents are cleaned on each run based on repo URL

### Verifying ingestion

- Check logs for per-source progress and counts
- Inspect ChromaDB:
  - Use `returnAllDocuments()` (programmatic) to list stored docs

### Performance and cost tips

- Tune `AMOUNT_OF_DOCS` (retrieval breadth)
- Set reasonable `MAX_RESPONSE_TOKENS` for summaries
- Narrow repository `patterns` to reduce volume

### Troubleshooting

- Discord
  - Ensure forum channels are type "Guild Forum"
  - Bot must have: View Channels, Read Message History, Read Messages in Threads
  - Log shows "Empty Forum" when nothing is fetched
- GitHub
  - Validate `GITHUB_TOKEN` scope and repository access
  - API rate limits may slow processing
- ChromaDB
  - Confirm connectivity and `CHROMADB_AUTH` value
  - Large batches: allow time; logs show batch numbers

### Where summaries come from

- Summarization prompts live in `src/config/prompts.ts`
- LLM provider is selected via `LLM_PROVIDER` (`openai` | `anthropic`)
- Embeddings use OpenAI’s `EMBEDDING_MODEL` for similarity search
