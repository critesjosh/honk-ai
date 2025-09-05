## Services and Integrations

### Discord

- Library: `discord.js`
- Intents: Guilds, GuildMessages, MessageContent, GuildMembers
- Partials: Message, Channel, User, ThreadMember, GuildMember
- Channels
  - `AllowedChannelIds`: bot responds here (or thread children)
  - `AnalyticsChannelId`: sending a message here renders the analytics UI embed
  - Logging channel hooks exist but are disabled by default
- Threads
  - First non-thread message creates a thread with a disclaimer and memory note
  - History: last N messages are merged into few turns for context
- Rate limiting: `RequestsPerMinute` per user; `MessageHistoryPerThread` controls history depth

### ChromaDB (Vector Store)

- Client: `chromadb` with `@chroma-core/openai` embeddings
- Config (`.env`)
  - `CHROMADB_URL_PATH`, `CHROMADB_URL_PORT`, `CHROMADB_URL_SSL`
  - `CHROMADB_KNOWLEDGEBASE_ID`
  - `CHROMADB_AUTH`: username:hash string copied verbatim from `chromadb/server.htpasswd`
- Requests include header: `Authorization: Basic <CHROMADB_AUTH>`
- Documents store: `UpsertDocument`, `similaritySearch`, `returnAllDocuments`, `deleteDocument`, `updateDocumentSource`

### LLM Providers

- Selection: `LLM_PROVIDER` = `openai` or `anthropic`
- OpenAI
  - `OPEN_AI_API` (API key)
  - `OPENAI_MODEL`
  - `MAX_RESPONSE_TOKENS` (used as `max_completion_tokens`)
- Anthropic
  - `ANTHROPIC_API` (API key)
  - `ANTHROPIC_MODEL`
  - `ANTHROPIC_THINKING` (true/false)
  - `MAX_RESPONSE_TOKENS` (used as `max_tokens`)
- Prompting: `src/config/prompts.ts` defines system/human prompts and style rules
- Concision and anti one-shot policy are enforced via the system prompt

### Embeddings

- Model: `EMBEDDING_MODEL` (OpenAI) for generating embeddings used in Chroma similarity search
- `AMOUNT_OF_DOCS`: number of documents to retrieve per query

### GitHub

- Issues training: Octokit with `GITHUB_TOKEN`
  - Configure in `src/config/issues.json`
- Repo training: simple-git clone + glob parsing
  - Configure in `src/config/training.json`
  - Markdown includes preprocessing and docs URL conversion supported

### Storage (LMDB)

- Path: `LMDB_ROUTE` (e.g., `./db/`)
- Databases: `analytics`, `documents`, `responses`, `questions`, `trainedThreads`, `trainedIssues`

### Docker & Compose

- App container: `Dockerfile` builds with pnpm, runs `dist/src/index.js`
- Volumes: `./db` and `./snapshots` are mounted
- ChromaDB service: `docker-compose.yml` exposes `localhost:8001`, uses `chromadb/server.htpasswd` for auth
