# Honk AI Discord bot

User-facing name is **Honk AI**. "Aztec DocsGPT" is the project; "Aztec MCP" is the service the `/mcp-key` slash command grants access to. Source lives in `bot.py`; image built from `Dockerfile` and run as the `discord-bot` service in `deployment/docker-compose-hub.yaml`.

## Guild allowlist + reply behavior

- `NOIR_GUILD_IDS` (comma-separated) replaces legacy `NOIR_GUILD_ID` (still honored). Current prod allowlist:
  - `1399477876461404252` — Aztec Bot Testing (owner: josh)
  - `1113924620781883405` — Noir (public community server)
  - `1144692727120937080` — third authorized guild (confirmed intentional 2026-05-14)
- Slash-command sync is per-guild with per-guild error isolation.
- The bot always replies in place: same channel for top-level guild mentions, inside the thread for thread mentions, in the DM for DMs.
- **Threads are NOT auto-created** on top-level mentions (removed 2026-05-12). `POST /channels/.../messages/.../threads` was the only Discord write route reliably tripping shared-bucket `40062` (anti-abuse heuristics flag rapid repeated thread creation from a single author in small/low-trust servers — the Aztec Bot Testing server hit this during dev iteration; Noir never did). Plain `send`/`typing` aren't throttled the same way.

## Reply-to-bot trigger (`_is_reply_to_bot`)

In guild channels the bot also responds when a message is a Discord inline reply to one of its previous messages, not only on explicit `@HonkAI` mentions. Lets users follow up without re-typing the tag.

Resolution uses `message.reference.resolved` populated by the gateway's `referenced_message` field — no `channel.fetch_message` fallback on cache miss (uncached replies are rare; the user can still @-mention).

Non-reply rejection is gated on `message.type == discord.MessageType.reply` (parent `Message.type`, value 19), **not** `MessageReferenceType.reply` — in discord.py 2.5+ that enum value is an alias for `.default` and covers pins/crossposts/channel-follow/thread-created/poll-result, all of which would otherwise pass an enum-only check. `MessageReferenceType.forward` is rejected as a secondary belt-and-braces filter on 2.5+. `on_message` also bails on any bot-authored input (`getattr(message.author, "bot", False)`) so the new reply trigger can't drag Honk into a bot-to-bot loop. DM behaviour is unchanged (DMs always trigger).

## Thread-context awareness

When @-mentioned inside a `discord.Thread`, the bot fetches the thread starter + up to `DISCORD_THREAD_CONTEXT_LIMIT` (default 30) most-recent messages before the trigger and bundles them as a labelled context block AFTER the user's question (question-first ordering).

History fetch uses `before=triggering_message, oldest_first=False` then reverses — `oldest_first=True` returns the wrong end of the thread.

Per-thread state lives in LRU `thread_conversation_histories` (cap 500), separate from the per-user `conversation_histories` cache (also a bounded LRU, cap 500, same lock-aware eviction — held locks are never evicted out from under an in-flight reply); an `asyncio.Lock` per entry. `/forget-me` and `!reset` still prune per-user entries eagerly.

**Privacy:** forwards other users' message bodies + display names to the LLM provider.

Knobs: `DISCORD_THREAD_CONTEXT_LIMIT` (`0` disables), `DISCORD_THREAD_CONTEXT_MAX_CHARS` (default 12000).

## Citation footer (`_format_sources_footer`)

Appends a `-#` (subtext) "Sources" block listing up to 5 URLs (`_DISCORD_FOOTER_SOURCE_LIMIT`); URLs wrapped in `<...>` to suppress auto-embed cards.

**Merged into the last answer chunk when `last_chunk + "\n\n" + footer <= 2000` chars**; otherwise sent as a separate `target.send(...)` after all chunks. Goes through the same `_aztec_source_url` rewriter as the widget.

The chunker (`chunk_string`) also packs to ≥80% of the 2000-char limit so most answers ship in 1–2 sends instead of 3–4 (added 2026-05-12 after a per-channel write 429 fired on a 3-chunk + footer reply into a fresh thread).

## Per-guild daily spend cap

Bot-side soft brake on OpenRouter spend, scoped per Discord guild — see `_reserve_guild_spend` / `_finalize_guild_spend` / `_signal_guild_cap_reached`; backend support via `_build_usage_frame` in `application/api/answer/routes/base.py`.

**Why bot-side:** every Honk AI guild posts to the same backend agent key, so the backend's per-agent `limited_token_mode` can't gate one guild without gating all of them.

Mechanism:
1. Backend emits a `{type: "usage", prompt_tokens, generated_tokens, model_id}` SSE frame on the success path (ordered `id → usage → end` so future clients can correlate to `conversation_id`).
2. Bot converts via `_USD_PER_PROMPT_MTOK` × `_USD_PER_COMPLETION_MTOK` constants (sourced from env `DISCORD_USD_PER_PROMPT_MTOK` / `DISCORD_USD_PER_COMPLETION_MTOK`, defaults `0.20` / `1.20` slightly above qwen3.6-flash actuals) into estimated USD and accumulates into a per-guild UTC-day bucket.
3. **Pre-call reserves** `DISCORD_PRE_CALL_RESERVE_USD` (default 1¢) under a per-guild `asyncio.Lock` — bounds concurrent-burst overshoot to ≤ `N * reserve` without serializing the LLM call.
4. **Post-call reconciles** to actual, or **refunds** on backend non-200 / breaker-trip / typing-429 (paths where the LLM provably didn't run), or **keeps charged** when an aiohttp timeout / missing usage frame means we can't tell.

**Cross-midnight finalize is dropped:** reserve returns `(usd, date)`; finalize checks the date and no-ops + WARN-logs `cross_midnight_dropped` if today rolled past — yesterday's bucket is already swept, refunding into today would be a free credit.

Caps configured via `DISCORD_GUILD_DAILY_USD_CAPS=<gid>=<usd>,<gid>=<usd>` — guilds absent from the map are uncapped; `=0` is a hard kill-switch.

**Soft brake, not a hard accounting boundary:** tiktoken uses `cl100k_base` (~10–20% drift vs qwen's tokenizer), and a bot restart wipes the in-memory bucket. The cap exists to prevent runaway spend, not enforce a numeric SLA.

Cap-reached notice: deduped per-channel per UTC-day, "💸 This server's daily AI quota ($X) is exhausted. Resets at 00:00 UTC (~Yh Zm from now)."

Tests: `tests/discord/test_spend_cap.py`, `tests/api/answer/routes/test_usage_frame.py`.

**Operator workflow:** changing `DISCORD_GUILD_DAILY_USD_CAPS` is bot-side only — `docker compose -f deployment/docker-compose-hub.yaml --env-file .env up -d --force-recreate discord-bot` (restart won't pick up env). The backend only needs a one-time recreate when the SSE `usage` frame support first ships; once deployed, cap changes don't touch it.

## Shared-bucket 429 breaker

Per-guild circuit breaker on Discord `code: 40062` / `discord.RateLimited` (any `X-RateLimit-Scope: shared` 429).

**Single-hit:** one observed `40062` trips it; no threshold or counter. While open, the bot refuses to call `/stream` for that guild and replies to silenced @-mentions with a deduped (per-channel, per-cooldown-window) "rate-limited, try again in about N minute(s)" notice — falling back to a ⏳ reaction if the notice itself 429s.

Cooldown defaults to **60s** (`DISCORD_SHARED_429_COOLDOWN_SECONDS`, min 10s); rationale + sizing evidence in the comment block at the constant.

Backed by `bot.http.max_ratelimit_timeout = 2.0` which kills discord.py's default 5-retry loop so `40062`s surface as observable exceptions rather than amplifying — that is the actual structural defense against the 2026-05-08 Noir anti-abuse flagging incident; the cooldown is belt-and-suspenders. Cooldown is decoupled from Discord's returned `retry_after` (which only describes when *Discord* will accept our next write, not when external shared-bucket contention clears).

Four bail-out sites all funnel through `_signal_breaker_silenced`: front-door `on_message`, `typing`, pre-`/stream` recheck, post-`/stream` answer/footer send (the previous `create_thread` site was removed with auto-thread creation on 2026-05-12).

## Reaction → feedback

👍/👎 on Honk AI replies POSTs `/api/feedback` with `{conversation_id, question_index, feedback}` → `conversation_messages.feedback` (JSONB). Implementation in `on_raw_reaction_add`, `submit_feedback`, `_register_feedback_target`.

The bot mirrors the backend's per-conversation position counter locally (`answer_count`); backend allocates atomically via `MAX(position)+1`, one row per `/stream`. Increment + `conversation_id` persistence happens IMMEDIATELY after `/stream` (before any Discord send). Per-thread + per-user `asyncio.Lock` serializes concurrent same-target mentions. Non-200 (`conversation_id=None`) early-returns without consuming a position.

`message_id → (conversation_id, question_index)` in LRU `feedback_targets` (cap 1000); restart loses the cache.

Auth: `/api/feedback` accepts anonymous because `AUTH_TYPE` is unset → `user_id="local"` matches the bot agent. REMOVE events not handled. Read via `GET /api/get_feedback_analytics`.

**Known limitation:** when `ENABLE_CONVERSATION_COMPRESSION=true` (default in `application/core/settings.py`), compression appends an extra `conversation_messages` row and silently no-ops post-compression feedback. Mitigation: set `ENABLE_CONVERSATION_COMPRESSION=false` in prod `.env`. Real fix (TODO): bot reads backend-assigned position via `position` SSE event.

## Right-to-erasure (`/forget-me`)

`/forget-me` revokes the user's MCP key, deletes pseudonym-keyed MCP data, drops the bot's in-memory per-user cache, **and erases the user's own chat Q&A turns**.

Chat turns are stored under the shared chat agent's owner (`user_id='local'`), so to make them per-user erasable the bot sends the raw Discord user id (`requester_provider`/`requester_provider_id`, built identically to what `/forget-me` sends) on every `/stream` call. The backend stamps each turn with the requester's HMAC pseudonym (`conversation_messages.requester_user_id`, migration 0011). On forget the backend **redacts those turns in place** — NULLs prompt/response/sources/etc., sets `erased_at`, but **keeps the row and its `position`** so the `MAX(position)+1` ↔ local `answer_count`/feedback contract (above) is NOT corrupted (a physical delete would let a position be reused). It also scrubs every off-message copy of the content: the conversation title + `compression_metadata`, synthetic compression-summary messages, `pending_tool_state`, and the `user_logs`/`stack_logs` rows that copy the prompt/response.

In a **shared thread** (multiple users mention the bot → one `conversation_id`), only the requesting user's own turns are scrubbed; co-participants' turns remain.

Caveats (stated in the command's reply): only turns sent **after this shipped** carry the tag and are erasable — older turns age out via the retention purge; and a turn in-flight at erase time can re-create rows, so a re-run catches it.

**Privacy:** the thread-context feature (above) forwards other users' message bodies + display names to the LLM provider. And note the trade-off this erasure makes: each stored turn now carries a one-way HMAC pseudonym of the requester (previously bot chat was fully anonymous). The pseudonym is not reversible without `USER_ID_PEPPER`, and the raw id is never stored or logged in plaintext.

## Env vars summary

See `.env-template` for full descriptions. Bot-only knobs:

| Var | Default | Effect |
|---|---|---|
| `DISCORD_TOKEN` | — | Required |
| `NOIR_GUILD_IDS` | — | CSV guild allowlist |
| `DISCORD_THREAD_CONTEXT_LIMIT` | 30 | 0 disables thread context |
| `DISCORD_THREAD_CONTEXT_MAX_CHARS` | 12000 | Cap on appended context block |
| `DISCORD_HISTORY_MAX_EXCHANGES` | 6 | Replayed prior exchanges sent to `/stream` (`_history_for_backend`); curbs long-thread qwen degeneration |
| `DISCORD_HISTORY_PROMPT_MAX_CHARS` / `DISCORD_HISTORY_RESPONSE_MAX_CHARS` | 1500 / 1000 | Per-turn truncation of replayed history; current question is sent separately + untruncated |
| `DISCORD_GUILD_DAILY_USD_CAPS` | empty (uncapped) | `<gid>=<usd>` CSV; `=0` is kill-switch |
| `DISCORD_PRE_CALL_RESERVE_USD` | 0.01 | Pre-call reserve before reconcile |
| `DISCORD_SHARED_429_COOLDOWN_SECONDS` | 60 | Breaker cooldown |
| `DISCORD_USD_PER_PROMPT_MTOK` / `DISCORD_USD_PER_COMPLETION_MTOK` | 0.20 / 1.20 (qwen3.6-flash) | OpenRouter $/Mtok for spend estimation. Loaded by `_env_float` into the module-level `_USD_PER_PROMPT_MTOK` / `_USD_PER_COMPLETION_MTOK` constants. |
