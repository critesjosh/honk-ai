## Adding Discord Training Forums

The bot can summarize Discord forum threads and store them in ChromaDB. Forums are configured via the `DiscordTrainingForums` environment variable as a comma-separated list of channel IDs.

### 1) Get Forum Channel IDs

- Enable Developer Mode in Discord (User Settings → Advanced → Developer Mode)
- Right-click the target forum channel → Copy ID
- Repeat for each forum you want to include

### 2) Update `.env`

Set `DiscordTrainingForums` to a comma-separated string of forum channel IDs:

```bash
DiscordTrainingForums="111122223333444455,666677778888999900"
```

Notes:

- No spaces; commas only
- You can add/remove IDs at any time; restart the bot to apply
-

### 3) Required Permissions

Ensure the bot has permissions on those forums:

- View Channels
- Read Message History
- Read Messages in Threads

### 4) Behavior

- The trainer fetches active and archived threads for each forum
- All messages from each thread are summarized and upserted into ChromaDB
- The bot de-duplicates via LMDB (`trainedThreads`) to avoid re-processing threads

### 5) Troubleshooting

- If nothing happens on startup: verify channel IDs, bot permissions, and that the channels are "Forum" type (Guild Forum)
- Check logs for "Empty Forum" or permission errors
- Confirm ChromaDB connectivity and `.env` values
