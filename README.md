<h1 align="center">
  DocsGPT  🦖
</h1>

<p align="center">
  <strong>Private AI for agents, assistants and enterprise search</strong>
</p>

> **Aztec Labs fork.** Internal knowledge-base assistant for the Aztec Network,
> built on upstream [`arc53/DocsGPT`](https://github.com/arc53/DocsGPT) starting
> at tag `0.17.0`. Corpus pinned to [`aztec-packages@v4.2.0`](https://github.com/AztecProtocol/aztec-packages/tree/v4.2.0).
> Live at **[aztec.adjacentpossible.dev](https://aztec.adjacentpossible.dev)**
> (Cloudflare Access SSO). The rest of this README is upstream documentation.

## Aztec fork overview

### Access paths
- **Web chat UI** — `https://aztec.adjacentpossible.dev`. Log in via Cloudflare
  Access (email SSO). The default `Aztec 4.2.0` agent is scoped to all 9
  corpora (Developer Docs, Network Docs, Aztec.nr, Example Contracts, Protocol
  Circuits, aztec.js SDK, CLI, E2E Tests, L1 Contracts).
- **Discord** — run `/mcp-key` in the Noir Discord to provision a personal
  MCP API key. `@`-mention the Aztec DocsGPT bot in any channel (or DM it)
  to chat directly. Responses are formatted for Discord (no Mermaid, no
  Markdown tables) via a custom system prompt.
- **MCP clients** (Claude Desktop, Claude Code, Codex) — paste the key from
  `/mcp-key` with `API_URL=https://aztec.adjacentpossible.dev`. The bot
  response includes ready-to-paste config snippets for each client and
  exposes a `search_aztec` tool backed by this deployment's `/api/search`.

### What this fork adds on top of upstream 0.17.0

- **MCP key provisioning** — `POST /api/internal/create_mcp_key`
  self-authenticates via `MCP_PROVISIONING_KEY` and upserts a per-Discord-user
  agent row (`agents.mcp_provider`, `mcp_provider_user_id`, `mcp_purpose`).
- **Discord bot** (`extensions/discord/`) — containerized and bundled into
  the production compose. Implements the `/mcp-key` slash command and
  @-mention chat passthrough. Reaches the backend on the internal compose
  network (bypasses Cloudflare Access).
- **Standalone TypeScript MCP server** (`extensions/mcp-server/`) — stdio
  MCP server that end users install locally (`npx docsgpt-mcp-server`).
  Exposes a `search_aztec` tool that hits `/api/search` with the user's key.
- **Ingest deny-list** (`application/parser/file/bulk.py:_IGNORED_PATH_SEGMENTS`)
  skips `fixtures/`, `dumps/`, `node_modules/`, `target/`, `dist/`, `build/`,
  `_out/`, `__pycache__/`, and `.git/` directories during corpus ingest —
  prevents blockchain-state test fixtures from burning OpenAI embedding credit.
- **Chunking filter** (`application/parser/chunking.py`) drops chunks with
  `token_count < 50`.
- **Cloudflare Tunnel deployment topology** — `deployment/docker-compose-hub.yaml`
  runs cloudflared as an outbound-only connector; no public IP required.
  Caddy (`deployment/Caddyfile`) sits behind it in HTTP-only mode and
  handles path routing, streaming (`flush_interval -1` for SSE), security
  headers, and `Cf-Access-Authenticated-User-Email → X-Auth-Email`
  propagation.
- **Slim backend image (1.34 GB)** — `application/Dockerfile` drops torch,
  transformers, sentence-transformers, docling, rapidocr, onnxruntime, and
  the bundled mpnet model in favor of remote OpenAI embeddings. The
  upstream image is ~12 GB.
- **Gunicorn + SSE hardening** — gthread workers (2 × 8), 75 s keep-alive,
  30 s graceful shutdown (paired with compose `stop_grace_period: 40s`), and
  a 15 s `: ping` heartbeat in `application/api/answer/routes/base.py` so
  Cloudflare doesn't idle-close long SSE streams during multi-hop retrieval.
- **Widget source URL rewriting** — the SSE `{type: "source"}` frame
  emitted on `/stream` rewrites each chunk's in-corpus path into a
  clickable public URL: rendered Developer Docs → `docs.aztec.network`,
  everything else (code, non-rendered docs) → GitHub blob at the
  `v4.2.0` tag. Sources are capped at 5 per answer.
- **RAG context cap** (`RAG_MAX_DOC_TOKENS`, default 15k, prod 6k) —
  upstream feeds the model's full context window (~198k tokens) of
  retrieved docs on every query, which made answers take 60s+. The
  cap keeps generation to 10–20s while still grounding well.
- **Primary-source-first retrieval fix** — upstream dropped
  `agent.source_id` when `extra_source_ids` existed, so the "primary"
  source was never actually searched. Our `stream_processor` prepends
  it back onto `sources_list`.
- **Reasoning-disable shim** — auto-injects
  `extra_body={"reasoning": {"exclude": true}}` for reasoning-mode
  OpenRouter models (e.g. `x-ai/grok-4.1-fast`) that would otherwise
  stream 100% chain-of-thought tokens with no visible answer.
- **CORS glob patterns** — `CORS_ALLOWED_ORIGINS` supports shell-style
  globs so Netlify preview URLs (`https://deploy-preview-*--aztec-docs-dev.netlify.app`)
  and local dev (`http://localhost:*`) don't have to be re-added per PR.
- **Widget embedding** — the React widget (`extensions/react-widget`)
  works from any origin in the CORS allowlist. Cloudflare Access must
  have **path-scoped Bypass applications** for the three widget
  endpoints (`/stream`, `/api/search`, `/api/feedback`), otherwise
  browsers hit the SSO challenge inside an XHR and fail. The rest of
  the hostname stays SSO-gated.
- **Agent-edit lockdown** (`VITE_DISABLE_AGENT_EDIT=true` build-arg) —
  replaces the in-UI agent create/edit form with a notice directing
  admins to manage agents via SQL or `/api/internal/create_mcp_key`.
  The UI was silently resetting `source_id` and swapping `prompt_id`
  on save; the lockdown prevents that drift.
- **Custom settings** — `MCP_PROVISIONING_KEY`, `AZTEC_SOURCE_IDS`,
  `CORS_ALLOWED_ORIGINS`, `EMBEDDINGS_DIMENSION`, `RAG_MAX_DOC_TOKENS`,
  `VITE_DISABLE_AGENT_EDIT`.

### Repository map (fork-specific)

- [`CLAUDE.md`](./CLAUDE.md) — architecture and ops guide for this fork
  (read first for any code or deploy work).
- [`AZTEC_SETUP.md`](./AZTEC_SETUP.md) — end-to-end production deployment
  walkthrough (bootstrap, secrets, corpus ingest, verification, rollback).
- [`deployment/docker-compose-hub.yaml`](./deployment/docker-compose-hub.yaml) —
  production compose (cloudflared + Caddy + backend + worker + frontend +
  discord-bot + postgres/pgvector + redis).
- [`deployment/Caddyfile`](./deployment/Caddyfile) — reverse-proxy config,
  HTTP-only for the tunnel path.
- [`.env-template`](./.env-template) — every required env var with generation
  commands (`openssl rand -hex 32` for each `*_KEY`).
- [`extensions/discord/`](./extensions/discord/) — Discord bot source +
  Dockerfile.
- [`extensions/mcp-server/`](./extensions/mcp-server/) — TypeScript MCP
  server (end users run this locally).

---

<p align="left">
  <strong><a href="https://www.docsgpt.cloud/">DocsGPT</a></strong> is an open-source AI platform for building intelligent agents and assistants. Features Agent Builder, deep research tools, document analysis (PDF, Office, web content, and audio), Multi-model support (choose your provider or run locally), and rich API connectivity for agents with actionable tools and integrations. Deploy anywhere with complete privacy control.
</p>

<div align="center">
  
  <a href="https://github.com/arc53/DocsGPT">![link to main GitHub showing Stars number](https://img.shields.io/github/stars/arc53/docsgpt?style=social)</a>
  <a href="https://github.com/arc53/DocsGPT">![link to main GitHub showing Forks number](https://img.shields.io/github/forks/arc53/docsgpt?style=social)</a>
  <a href="https://github.com/arc53/DocsGPT/blob/main/LICENSE">![link to license file](https://img.shields.io/github/license/arc53/docsgpt)</a>
  <a href="https://www.bestpractices.dev/projects/9907"><img src="https://www.bestpractices.dev/projects/9907/badge"></a>
  <a href="https://discord.gg/vN7YFfdMpj">![link to discord](https://img.shields.io/discord/1070046503302877216)</a>
  <a href="https://x.com/docsgptai">![X (formerly Twitter) URL](https://img.shields.io/twitter/follow/docsgptai)</a>

<a href="https://docs.docsgpt.cloud/quickstart">⚡️ Quickstart</a> • <a href="https://app.docsgpt.cloud/">☁️ Cloud Version</a> • <a href="https://discord.gg/vN7YFfdMpj">💬 Discord</a>
<br>
<a href="https://docs.docsgpt.cloud/">📖 Documentation</a> • <a href="https://github.com/arc53/DocsGPT/blob/main/CONTRIBUTING.md">👫 Contribute</a> • <a href="https://blog.docsgpt.cloud/">🗞 Blog</a>
<br>

</div>


<div align="center">
  <br>
<img src="https://d3dg1063dc54p9.cloudfront.net/videos/demo-26.gif" alt="video-example-of-docs-gpt" width="800" height="480">
</div>
<h3 align="left">
  <strong>Key Features:</strong>
</h3>
<ul align="left">
    <li><strong>🗂️ Wide Format Support:</strong> Reads PDF, DOCX, CSV, XLSX, EPUB, MD, RST, HTML, MDX, JSON, PPTX, images, and audio files such as MP3, WAV, M4A, OGG, and WebM.</li>
    <li><strong>🎙️ Speech Workflows:</strong> Record voice input into chat, transcribe audio on the backend, and ingest meeting recordings or voice notes as searchable knowledge.</li>
    <li><strong>🌐 Web & Data Integration:</strong> Ingests from URLs, sitemaps, Reddit, GitHub and web crawlers.</li>
    <li><strong>✅ Reliable Answers:</strong> Get accurate, hallucination-free responses with source citations viewable in a clean UI.</li>
    <li><strong>🔑 Streamlined API Keys:</strong>  Generate keys linked to your settings, documents, and models, simplifying chatbot and integration setup.</li>
    <li><strong>🔗 Actionable Tooling:</strong> Connect to APIs, tools, and other services to enable LLM actions.</li>
    <li><strong>🧩 Pre-built Integrations:</strong> Use readily available HTML/React chat widgets, search tools, Discord/Telegram bots, and more.</li>
    <li><strong>🔌 Flexible Deployment:</strong> Works with major LLMs (OpenAI, Google, Anthropic) and local models (Ollama, llama_cpp).</li>
    <li><strong>🏢 Secure & Scalable:</strong> Run privately and securely with Kubernetes support, designed for enterprise-grade reliability.</li>
</ul>

## Roadmap
- [x] Add OAuth 2.0 authentication for MCP ( September 2025 )
- [x] Deep Agents ( October 2025 )
- [x] Prompt Templating ( October 2025 )
- [x] Full api tooling ( Dec 2025 )
- [ ] Agent scheduling ( Jan 2026 )

You can find our full roadmap [here](https://github.com/orgs/arc53/projects/2). Please don't hesitate to contribute or create issues, it helps us improve DocsGPT!

### Production Support / Help for Companies:

We're eager to provide personalized assistance when deploying your DocsGPT to a live environment.

[Get a Demo :wave:](https://www.docsgpt.cloud/contact)⁠

[Send Email :email:](mailto:support@docsgpt.cloud?subject=DocsGPT%20support%2Fsolutions)

## Join the Lighthouse Program 🌟

Calling all developers and GenAI innovators! The **DocsGPT Lighthouse Program** connects technical leaders actively deploying or extending DocsGPT in real-world scenarios. Collaborate directly with our team to shape the roadmap, access priority support, and build enterprise-ready solutions with exclusive community insights.

[Learn More & Apply →](https://docs.google.com/forms/d/1KAADiJinUJ8EMQyfTXUIGyFbqINNClNR3jBNWq7DgTE)

## QuickStart

> [!Note]
> Make sure you have [Docker](https://docs.docker.com/engine/install/) installed

A more detailed [Quickstart](https://docs.docsgpt.cloud/quickstart) is available in our documentation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/arc53/DocsGPT.git
   cd DocsGPT
   ```

**For macOS and Linux:**

2. **Run the setup script:**

   ```bash
   ./setup.sh
   ```

**For Windows:**

2. **Run the PowerShell setup script:**

   ```powershell
   PowerShell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

Either script will guide you through setting up DocsGPT. Five options available: using the public API, running locally, connecting to a local inference engine, using a cloud API provider, or build the docker image locally. Scripts will automatically configure your `.env` file and handle necessary downloads and installations based on your chosen option.

**Navigate to http://localhost:5173/**

To stop DocsGPT, open a terminal in the `DocsGPT` directory and run:

```bash
docker compose -f deployment/docker-compose.yaml down
```

(or use the specific `docker compose down` command shown after running the setup script).

> [!Note]
> For development environment setup instructions, please refer to the [Development Environment Guide](https://docs.docsgpt.cloud/Deploying/Development-Environment).

## Contributing

Please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for information about how to get involved. We welcome issues, questions, and pull requests.

## Architecture

![Architecture chart](https://github.com/user-attachments/assets/fc6a7841-ddfc-45e6-b5a0-d05fe648cbe2)

## Project Structure

- Application - Flask app (main application).

- Extensions - Extensions, like react widget or discord bot.

- Frontend - Frontend uses <a href="https://vitejs.dev/">Vite</a> and <a href="https://react.dev/">React</a>.

- Scripts - Miscellaneous scripts.

## Code Of Conduct

We as members, contributors, and leaders, pledge to make participation in our community a harassment-free experience for everyone, regardless of age, body size, visible or invisible disability, ethnicity, sex characteristics, gender identity and expression, level of experience, education, socio-economic status, nationality, personal appearance, race, religion, or sexual identity and orientation. Please refer to the [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) file for more information about contributing.

## Many Thanks To Our Contributors⚡

<a href="https://github.com/arc53/DocsGPT/graphs/contributors" alt="View Contributors">
  <img src="https://contrib.rocks/image?repo=arc53/DocsGPT" alt="Contributors" />
</a>

## License

The source code license is [MIT](https://opensource.org/license/mit/), as described in the [LICENSE](LICENSE) file.

## This project is supported by:

<p>
  <a href="https://www.digitalocean.com/?utm_medium=opensource&utm_source=DocsGPT">
    <img src="https://opensource.nyc3.cdn.digitaloceanspaces.com/attribution/assets/SVG/DO_Logo_horizontal_blue.svg" width="201px">
  </a>
</p>
<p>
  <a href="https://get.neon.com/docsgpt">
    <img width="201" alt="color" src="https://github.com/user-attachments/assets/7d9813b7-0e6d-403f-b5af-68af066b326f" />
  </a>
  
</p>
