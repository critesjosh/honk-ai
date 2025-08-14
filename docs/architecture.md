## Architecture Overview

### Components

- Discord Client: `src/index.ts` initializes `discord.js` client with intents/partials and delegates to `src/handlers/`
- Message/Interaction Handlers: `src/handlers/messages.ts`, `src/handlers/interactions.ts`, `src/handlers/index.ts`
- LLM + RAG: `src/utils/llm.ts`, `src/utils/models/{openai,anthropic}.ts`, `src/utils/chroma.ts`, `src/utils/embeddings.ts`
- Training Services: `src/training/{discord,issues,repos}/`
- Config/Prompts: `src/config/prompts.ts`, `src/config/{issues.json,training.json}`
- Persistence: LMDB databases created in `src/handlers/index.ts` under `env.LMDB_ROUTE`

### Runtime Flow (Message → Answer)

1. Discord message event → `newMessage()` in `src/handlers/messages.ts`
   - Filter by `AllowedChannelIds` and `AnalyticsChannelId`
   - In non-thread channels, requires mention of `<@BOT_ID>`
   - Creates a thread on first user message; posts a disclaimer and context note
   - Per-user rate limiting via `RequestsPerMinute`
2. Context building
   - Extract prompt (strip bot mention)
   - Gather recent thread chat history (merge consecutive turns, keep last N)
3. Retrieval & Generation
   - `invokeRagChain(prompt, history)` selects provider (`LLM_PROVIDER`)
   - Embedding for prompt via OpenAI embeddings -> query ChromaDB with `AMOUNT_OF_DOCS`
   - Build prompt from `src/config/prompts.ts` and call model (`OPENAI_MODEL` or `ANTHROPIC_MODEL`)
4. Response formatting & sending
   - Format sources via `formatSourcesForDiscord`
   - Chunk answer to Discord embed description limit (4096)
   - Send content chunks first, then a dedicated Sources embed and a Metadata embed (provider/model/tokens) at the bottom
   - Add feedback buttons (👍/👎) to the final message
5. Storage & analytics
   - Store prompt/answer/sources in LMDB `responses`
   - Increment `analytics.totalQuestions` counter

### Embed Chunking and Limits

- Discord embed description max length: 4096 characters
- The bot:
  - Splits large answers across multiple embeds
  - Appends Sources and Metadata as separate embeds after all content chunks
  - Truncates any overflow with "... (truncated)"

### Training Services

- Entry: `src/training/index.ts`
  - On `ready`: runs Discord Threads and GitHub Issues training immediately
  - Weekly cron: Discord Threads, GitHub Issues, and Repo training

- Discord Threads: `src/training/discord/index.ts`
  - Iterates `env.DiscordTrainingForums` (Guild Forum channels)
  - Fetches all messages per thread and summarizes with `getDiscordThreadsPrompt`
  - Upserts per-thread summaries into ChromaDB with Discord thread URLs as sources

- GitHub Issues: `src/training/issues/index.ts`
  - Uses Octokit with `GITHUB_TOKEN`
  - Iterates repos from `src/config/issues.json`, fetches issues+comments, summarizes, upserts to ChromaDB

- Repositories: `src/training/repos/index.ts`
  - Repos listed in `src/config/training.json`
  - Clones repo, preprocesses markdown includes, parses files by type, chunks, and upserts documents/sections
  - Cleans up old repo documents before re-training; converts Aztec docs paths to website URLs when applicable

### Persistence (LMDB)

Initialized in `src/handlers/index.ts`:

- `analytics`: counters, incl. `totalQuestions`, ratings tallies
- `documents`: documents pulled from ChromaDB (optional cache)
- `responses`: stored bot responses with prompt, sources, metadata
- `questions`: user questions for analytics
- `trainedThreads`: list of Discord thread IDs already trained
- `trainedIssues`: list of GitHub issue numbers already trained

### Services and Boundaries

- Discord: events, threads, interactions
- ChromaDB: vector store for RAG (documents upsert/query)
- LLM Providers: OpenAI or Anthropic for generations (select via `LLM_PROVIDER`)
- GitHub: Octokit for issues; simple-git for repo cloning

### Error Handling

- Try/catch around message processing and training tasks
- User-facing error embed on failure during response
- Logging to stdout; optional channel logging hooks are present and can be enabled
