# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is DocsGPT

DocsGPT is an open-source AI platform for building intelligent agents and assistants with document-grounded Q&A. It features an Agent Builder, deep research tools, document analysis, multi-model LLM support, and rich API connectivity. The stack is: Flask backend (Python), React frontend (TypeScript/Vite), MongoDB, Redis, Celery workers, and pluggable vector stores.

## Development Environment

**Always run services via Docker.** Do not install MongoDB, Redis, or app dependencies natively. The compose file used is `deployment/docker-compose-hub.yaml` which pulls pre-built images and uses `network_mode: host` so all ports are on localhost. Configuration lives in `.env` at the repo root.

Before starting services, check if containers are already running (`docker compose ... ps`). If they show as exited, just bring them back up — do not recreate from scratch.

## Common Commands

### Running services (Docker)
```bash
docker compose -f deployment/docker-compose-hub.yaml up -d     # Start all services (detached)
docker compose -f deployment/docker-compose-hub.yaml ps         # Check status
docker compose -f deployment/docker-compose-hub.yaml logs -f    # Tail logs
docker compose -f deployment/docker-compose-hub.yaml down       # Stop all services
docker compose -f deployment/docker-compose-hub.yaml restart backend  # Restart a single service
```

Services (all on localhost via host networking): frontend (:5173), backend (:7091), Celery worker, Redis (:6379), MongoDB (:27017).

The backend and worker containers volume-mount `application/core/model_configs.py`, `application/indexes/`, `application/inputs/`, and `application/vectors/` from the host, so edits to those paths take effect on container restart.

### Tests & linting (from repo root)
```bash
python -m pytest                          # Run all unit tests (integration tests excluded by default)
python -m pytest tests/api/               # Run tests for a specific module
python -m pytest tests/api/test_routes.py::TestClassName::test_method  # Run a single test
python -m pytest -m unit                  # Run only unit-marked tests
python -m pytest -m integration           # Run integration tests (excluded by default)
ruff check .                              # Lint Python code
ruff format .                             # Format Python code
```

### Frontend linting/build (from `frontend/`)
```bash
npm run lint                # ESLint check
npm run lint-fix            # ESLint auto-fix + Prettier
npm run build               # TypeScript check + production build
```

## Architecture

### Backend (`application/`)

**Entry points:** `app.py` (Flask app), `wsgi.py` (WSGI), `worker.py` (Celery worker).

**Flask blueprints registered in `app.py`:**
- `user` (`/api/user/*`) — agents, sources, conversations, prompts, tools
- `answer` (`/api/answer`) — main Q&A streaming endpoint
- `internal` (`/api/internal/*`) — admin operations
- `connector` (`/api/connectors/*`) — Google Drive, SharePoint connectors
- `v1` (`/v1/*`) — OpenAI-compatible chat completions API

**Factory pattern is used pervasively** — each subsystem has a Creator class:
- `LLMCreator` (`llm/llm_creator.py`) — instantiates LLM providers (OpenAI, Anthropic, Google, Groq, OpenRouter, Novita, llama_cpp, SageMaker, etc.)
- `AgentCreator` (`agents/agent_creator.py`) — classic (basic RAG), agentic (tool-calling loop), research (multi-turn plan/execute/report), workflow (sequential/conditional)
- `VectorCreator` (`vectorstore/vector_creator.py`) — FAISS (default), Elasticsearch, Qdrant, MongoDB Atlas, Milvus, LanceDB, PGVector
- `RetrieverCreator` (`retriever/retriever_creator.py`) — classic RAG with vector similarity search
- `StorageCreator` (`storage/storage_creator.py`) — local filesystem or S3

**Configuration:** `core/settings.py` uses Pydantic BaseSettings loading from `.env`. Model metadata lives in `core/model_configs.py`.

**Celery tasks** (`api/user/tasks.py`): document ingestion (`ingest`), remote source ingestion (`ingest_remote`), re-indexing (`reingest_source_task`), periodic syncs (`schedule_syncs`), attachment storage, webhooks. Broker and result backend are both Redis.

**Document parsing pipeline** (`parser/`): file parsers (PDF with Docling OCR, DOCX, XLSX, EPUB, HTML, Markdown, images, audio) -> chunking (`chunking.py`) -> embedding (`embedding_pipeline.py`) -> vector store insertion.

**Auth:** JWT-based (`simple_jwt` or `session_jwt` mode), configured via `AUTH_TYPE` env var.

### Frontend (`frontend/`)

React 19 + TypeScript + Vite 8. State management via Redux Toolkit (`store.ts`). Routing via React Router.

**Key areas in `src/`:**
- `conversation/` — chat interface and message rendering
- `agents/` — agent management, workflow builder (React Flow)
- `settings/` — settings pages (General, Analytics, Sources, Tools, Prompts, Logs)
- `api/services/` — API client layer
- `components/ui/` — Radix UI + shadcn/ui pattern components
- `locale/` — i18n translations (i18next)

**Styling:** Tailwind CSS v4 with CSS custom properties for theming/dark mode.

### Extensions (`extensions/`)

Discord bot, Slack bot, Chatwoot integration, React widget (npm package), web widget (vanilla JS), Chrome extension.

## CI/CD

GitHub Actions workflows in `.github/workflows/`:
- **pytest.yml** — Python tests with coverage, uploads to Codecov
- **lint.yml** — Ruff linting via chartboost/ruff-action
- **bandit.yaml** — Python security scanning on `application/`
- **docker-develop-build.yml / docker-develop-fe-build.yml** — Docker image builds on push to main
- **ci.yml** — Multi-arch release builds to DockerHub + ghcr.io

## Code Style

- **Python:** Ruff with 120 char line length (`.ruff.toml`). PEP 8. Type hints expected. Google-style docstrings. Keep changes narrow in `api`, `auth`, `security`, `parser`, `retriever`, and `storage` areas.
- **Frontend:** ESLint + Prettier. 80 char print width. Single quotes. Semicolons. Tailwind CSS plugin for class sorting. Husky pre-commit hooks run lint-staged on `.{js,jsx,ts,tsx}` files. Prefer small, reusable functional components and hooks. Use Redux for shared state — do not introduce new global state libraries. Avoid broad UI refactors unless explicitly asked. Do not re-create components that already exist in the app.

## Test Setup

- **Python:** pytest with `pytest-cov`. Coverage target is `application/`. Tests use `mongomock`. Config in `pytest.ini`. Test fixtures/structure mirrors `application/` layout under `tests/`.
- **Frontend:** No test runner configured (no Jest/Vitest).

## PR Readiness

Before opening a PR:
- Run the relevant validation commands (ruff/pytest for backend, lint/build for frontend)
- Confirm backend changes work end-to-end after ingesting sample data when applicable
- Summarize user-visible behavior changes
- Note any config, dependency, or deployment implications
