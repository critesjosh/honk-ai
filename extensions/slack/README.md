# Honk AI Slack bot

User-facing name is **Honk AI** (same as the Discord bot). Source lives in `bot.py`; image built from `Dockerfile` and run as the `slack-bot` service in `deployment/docker-compose-hub.yaml`. It is the Slack sibling of `extensions/discord/` and shares the backend (`/stream`, `/api/feedback`, `/api/internal/create_mcp_key`, `/api/internal/forget_discord_user`).

## Connectivity — Socket Mode

The bot uses **Socket Mode** (an outbound websocket), so like the Discord bot it needs **no public ingress** and reaches `backend:7091` internally. Socket Mode carries events, slash commands, AND interactive components (the feedback buttons) over the same connection — there is **no signing secret and no public Request URL** to configure.

Required tokens:
- `SLACK_BOT_TOKEN` (`xoxb-…`) — Bot User OAuth Token.
- `SLACK_APP_TOKEN` (`xapp-…`) — App-Level Token with the `connections:write` scope.

## Slack app setup (manifest)

**Fast path:** at <https://api.slack.com/apps> → **Create New App** → **From an app manifest** → pick the workspace → paste `extensions/slack/slack-app-manifest.json`. That sets everything below in one shot (Socket Mode on, scopes, events, slash commands, interactivity). Then:

1. **Socket Mode → Generate an App-Level Token** with `connections:write` → `SLACK_APP_TOKEN` (`xapp-…`).
2. **Install App → Install to Workspace** → copy the Bot User OAuth Token → `SLACK_BOT_TOKEN` (`xoxb-…`).
3. `/invite @Honk AI` in each channel where it should answer (DMs work once installed; `app_mention` only fires in channels the bot is a member of).

What the manifest configures (and why):

- **OAuth scopes (bot):** `app_mentions:read`, `chat:write`, `commands`, `users:read`, `im:history`, `channels:history`, `groups:history`, `mpim:history`. The last two are only needed to read thread context in **private** channels / **group DMs** — drop them if you don't want that (`conversations.replies` just fails best-effort there and the bot answers without prior context). The bot does **not** need claudebox's `reactions:*` / `*:read` companion scopes (it posts + reads history + resolves user names, nothing more).
- **Event subscriptions (bot events):** `app_mention`, `message.im`. **Not** `message.channels` — channel messages are handled via `app_mention`; subscribing to both would double-fire and make the bot ambient/noisy. (`channels:history` is for `conversations.replies` thread context, never a trigger.) Group-DM support is mention-only: `app_mention` fires for mentions in an `mpim`, so there's no need for `message.mpim` (which would make the bot answer every group-DM message).
- **Interactivity:** ON (required for the feedback buttons + slash commands over Socket Mode — no Request URLs needed).
- **Slash commands:** `/aztec-mcp-key`, `/aztec-forget-me`.

## Triggers

- **`app_mention`** — answers in the channel, threaded under the mention (`thread_ts = event.thread_ts or event.ts`) to avoid channel noise. Group-DM (`mpim`) mentions arrive here too.
- **`message.im`** — DM Q&A (linear, not threaded). Strict subtype guard: any `subtype` (`message_changed`/`message_deleted`/joins) or `bot_id` is ignored, plus the bot's own `user`. (`app_mention` gets the same subtype/bot guard.)
- **Dedupe on message identity** (`team:channel:ts`, `_already_handled`), NOT `event_id` — Slack redelivers on a slow ack, and a single message can reach us via more than one event type; keying on the message id catches both. (Same `channel+ts` claim pattern as claudebox.) The dedup is **process-local** (in-memory LRU) — running more than one `slack-bot` replica or a rolling deploy could double-answer; the service runs as a single container.
- **Eager acknowledgement:** since Slack has no bot typing indicator, the bot posts a `🪿 Looking into it…` placeholder (or `Queued…` if a prior turn in the conversation holds the lock) and **updates that same message into the first answer chunk** — no dangling status message. The heavy work runs in a fire-and-forget `asyncio.create_task` so the event handler acks within Slack's ~3s window. `_event_team_id` resolves the workspace id with a fallback chain (`team_id` → `event.team` → `authorizations[0]` → `enterprise_id`) for Grid/Connect payloads.
- **Workspace allowlist:** `SLACK_TEAM_IDS` (CSV of `T…` ids). Empty = allow all (mirrors `NOIR_GUILD_IDS`).

## Identity — workspace-scoped pseudonyms

A Slack `user_id` is only unique within a workspace, so the raw identity handed to the backend for pseudonymization is the **compound `team_id:user_id`** (`enterprise_id:team_id:user_id` on Enterprise Grid) — see `slack_raw_identity`. The `/aztec-mcp-key` and `/aztec-forget-me` commands MUST build it the same way or the pseudonyms won't match. The backend (`create_mcp_key` / `forget_discord_user`, provider-aware) stores only the HMAC pseudonym (`slack_p_v1:` prefix), never the plaintext id.

## Conversation state + thread context

Per-conversation cache keyed by `(team_id, channel_id, thread_ts)` for threads and `(team_id, channel_id)` for DMs — Slack `ts` is channel-scoped, so the channel is part of the key. Same bounded-LRU + per-key `asyncio.Lock` discipline as the Discord thread cache (a long `/stream` call can't have its lock evicted out from under it).

Thread context is fetched best-effort via `conversations.replies` (bounded by `SLACK_THREAD_CONTEXT_LIMIT`, default 30; `0` disables) and assembled question-first into an untrusted-context block (`build_thread_context_block`). **Privacy:** this forwards other users' message bodies + display names to the LLM provider, same as the Discord thread-context feature.

## Formatting + chunking

- `format_for_slack`: Markdown headers → `*bold*`, `[label](url)` → `<url|label>` mrkdwn; fenced code blocks left intact.
- `chunk_string`: packs to ≥80% of `SLACK_MAX_MSG_CHARS` (3500) and repairs code fences across a split — ported from the Discord chunker.
- `chat_postMessage` sends use `unfurl_links=False, unfurl_media=False` to suppress link-preview cards (cleaner than the Discord `<…>` trick). Caveat: the FIRST chunk lands by editing the placeholder via `chat_update`, which accepts no unfurl flags — links in that chunk may still unfurl per workspace defaults.

## Feedback — Block Kit buttons

Each answer is followed by a small "Was this helpful?" message with 👍/👎 **buttons** (not reactions). The `conversation_id:question_index` is encoded in each button's `value`, so the mapping **survives a bot restart** (no in-memory `ts→conversation` LRU to lose). The action handler parses the value and POSTs `/api/feedback` (`LIKE`/`DISLIKE`) silently — no visible confirmation (a "Thanks for the feedback" reply is channel noise; the interaction `ack()` is the only response). Position counter (`answer_count`) is advanced once per successful `/stream`, mirroring the Discord eager-persist discipline.

## Rate limiting — no bespoke breaker

Slack's Web API rate limiting is per-method/workspace with a `Retry-After` header — there is no Discord-style shared-bucket `40062` anti-abuse problem to defend against. The bot relies on `slack_sdk`'s `AsyncRateLimitErrorRetryHandler` (attached to the web client) plus the ≤2-send chunk packing instead of a circuit breaker.

## Per-workspace daily spend cap

Bot-side soft brake on OpenRouter spend, scoped per Slack **workspace** (`team_id`) — direct port of the Discord per-guild cap (`_reserve_team_spend` / `_finalize_team_spend`, reconciled against the backend `usage` SSE frame). **Why bot-side:** every workspace posts to the same backend agent key, so the backend's per-agent `limited_token_mode` can't gate one workspace without gating all. Configure via `SLACK_TEAM_DAILY_USD_CAPS=<team>=<usd>,…` (`=0` is a kill-switch). Soft brake, not a hard accounting boundary (tiktoken drift + restart wipes the in-memory bucket).

## `/forget-me` scope

`/aztec-forget-me` revokes the user's MCP key and deletes MCP-originated data keyed to their pseudonym, and drops the bot's in-memory conversation cache for the **channel the command was invoked in** (typically the user's DM with the bot — both the DM key and any thread keys in that channel; other channels' state is left alone and ages out of the LRU). **It does not erase shared bot Q&A in channels** — those `/stream` turns are written under the shared chat agent's owner (`user_id='local'`), not the per-user pseudonym, so they aren't individually erasable. This is identical to the Discord bot's behaviour. The command message states this explicitly.

## The Slack chat agent

The bot's `API_KEY` is a dedicated agent with `surface='slack'` (NOT shared with Discord, so per-surface analytics stay clean). Provision it once with `scripts/db/create_slack_chat_agent.py` (bind-mount `scripts/`, see CLAUDE.md) and put the printed key in `.env` as `SLACK_API_KEY`. The provisioner wires the agent to the **Slack-grounded prompt** ("Honk AI (Slack bot) — Aztec 4.3.0 grounded"), seeding the row from `application/prompts/aztec_4_3_0_grounded_slack.txt` on first run; `SLACK_PROMPT_ID` overrides. Don't point it at the Discord prompt — that made the bot introduce itself as a Discord bot. Adding `slack` to the `agents.surface` enum is migration `0010_agents_surface_slack`. Per-user MCP keys remain `surface='mcp'` with `mcp_provider='slack'`.

## Env vars summary

See `.env-template` for full descriptions. Bot-only knobs:

| Var | Default | Effect |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Required (`xoxb-`) |
| `SLACK_APP_TOKEN` | — | Required (`xapp-`, `connections:write`) |
| `SLACK_API_KEY` | — | Required; the `surface='slack'` chat agent key (compose maps it to the service `API_KEY`) |
| `SLACK_TEAM_IDS` | empty (all) | CSV workspace allowlist |
| `SLACK_THREAD_CONTEXT_LIMIT` | 30 | 0 disables thread context |
| `SLACK_THREAD_CONTEXT_MAX_CHARS` | 12000 | Cap on appended context block |
| `SLACK_TEAM_DAILY_USD_CAPS` | empty (uncapped) | `<team>=<usd>` CSV; `=0` is kill-switch |
| `SLACK_PRE_CALL_RESERVE_USD` | 0.01 | Pre-call reserve before reconcile |
| `SLACK_USD_PER_PROMPT_MTOK` / `SLACK_USD_PER_COMPLETION_MTOK` | 0.20 / 1.20 | OpenRouter $/Mtok for spend estimation |

## Operator workflow

```bash
# Build + (re)create the bot after a code change:
docker compose -f deployment/docker-compose-hub.yaml --env-file .env build slack-bot
docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate slack-bot

# .env changes need --force-recreate (restart re-uses the old env).
```

## Tests

`tests/slack/test_slack_bot.py` — pure-helper unit tests (identity, formatting, chunking, decoration stripping, thread-context assembly, the per-workspace spend cap, Block Kit builders). The module guards its `slack_bolt` import so these run without the Slack SDK installed.
