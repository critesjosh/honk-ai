## Adding GitHub Repos to Training

This bot can ingest content from GitHub repos and store summaries/chunks into ChromaDB. Repos are configured in `src/config/training.json`.

### File: `src/config/training.json`

Each entry has:

- `name`: repository name
- `org`: GitHub org/user
- `branch`: branch to use
- `paths`: optional, reserved (not required)
- `patterns`: glob patterns to include (e.g., `docs/**/*.md`, `**/*.ts`)

### Example entries

```json
{
  "repositories": [
    {
      "name": "awesome-aztec",
      "org": "AztecProtocol",
      "branch": "main",
      "paths": [],
      "patterns": ["README.md"]
    },
    {
      "name": "aztec-handbook",
      "org": "defi-wonderland",
      "branch": "dev",
      "paths": [],
      "patterns": ["docs/**/*.md"]
    }
  ]
}
```

### Recommendations

- Docs-only ingestion: `"docs/**/*.md"`
- Code + docs: combine patterns (e.g., `"**/*.nr"`, `"**/*.ts"`, `"**/*.md"`)
- Very large binaries are ignored; markdown is preprocessed to resolve `#include_code` directives

### When does repo training run?

- Weekly on Sunday 00:00 (cron), see `src/training/index.ts`

### Run repo training immediately

```bash
# Option 1: one-off in-process
pnpm dlx tsx -e "import('./src/training/repos/index.ts').then(m=>m.StartTrainingService())"
```

### Troubleshooting

- Ensure `GITHUB_TOKEN` in `.env` has repo read access
- ChromaDB must be reachable and authenticated per `.env`
- Large repos may take time; logs will show patterns and file counts
