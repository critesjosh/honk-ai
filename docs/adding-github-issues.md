## Adding GitHub Issues to Training

The bot can summarize GitHub issues and comments and store them in ChromaDB. Configure targets in `src/config/issues.json`.

### File: `src/config/issues.json`

- `GITHUB_REPO_OWNER`: org/user
- `GITHUB_REPO_ISSUES`: array of repo names to ingest issues from

### Example

```json
{
  "GITHUB_REPO_ISSUES": ["aztec-packages", "awesome-aztec"],
  "GITHUB_REPO_OWNER": "AztecProtocol"
}
```

### Behavior

- Fetches issues (state: all) and iterates comments
- Skips bot authors (e.g., `[bot]`)
- Builds a context blob and summarizes via the selected LLM
- Upserts into ChromaDB with the issue URL as source

### When does issue training run?

- On bot startup
- Weekly on Sunday 00:00 (cron), see `src/training/index.ts`

### Run issue training immediately

Issue training is part of startup flow. Restart the bot or run the training function explicitly after boot by calling `BeginTrainingProcesses` (already wired to `ready` event).

### Requirements

- `GITHUB_TOKEN` in `.env` with repo read access
- ChromaDB reachable and authenticated per `.env`

### Notes

- The trainer de-duplicates by keeping a local list in LMDB and skipping already-trained items
- If you change the configuration drastically, consider clearing the relevant documents from ChromaDB or use the repository cleanup logic in the repos trainer
