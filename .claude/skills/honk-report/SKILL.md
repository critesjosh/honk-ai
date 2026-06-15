---
name: honk-report
description: Generate a Honk AI usage report — reads Postgres conversations + backend/discord-bot/slack-bot logs across the docs widget, Discord bot, Slack bot, /ask page, and MCP surfaces, and produces an analysis of what users are asking, sentiment, factual errors, and recommendations. Optional integer arg = window in days (default 7). Examples: `/honk-report`, `/honk-report 14`. Also triggers on natural-language requests like "honk usage report", "weekly bot report", "what are users asking honk this week".
argument-hint: "[days]"
---

# /honk-report — Honk AI usage report

Produces a markdown report covering all Honk AI surfaces from a single Claude
Code session running on josh-box. Source of truth is Postgres (`conversations`
+ `conversation_messages` + `agents.surface`) plus backend / discord-bot /
slack-bot container logs.

## Arguments

| Position | Name   | Type | Default | Meaning                                   |
|----------|--------|------|---------|-------------------------------------------|
| 1        | `days` | int  | `7`     | Trailing window. Reports cover `NOW() - INTERVAL '$days days'` to now. |

```
/honk-report               # last 7 days
/honk-report 14            # last 14 days
/honk-report 1             # yesterday only (useful for ad-hoc spot checks)
```

Parse the single optional argument as an integer. Reject non-integer or
`days <= 0` input by replying with the Usage block above and stopping —
do NOT silently fall back to the default. If no arg is supplied, set
`DAYS=7` and continue.

Writes the report to `/mnt/user-data/josh/honk-reports/honk-report-YYYY-MM-DD.md`
(create the directory if missing) and prints the path.

## Surfaces

The five production-relevant `agents.surface` values:

| surface  | agent `name`                | what it is                              |
|----------|-----------------------------|-----------------------------------------|
| `widget` | `docs.aztec.network`        | embedded chat on docs.aztec.network     |
| `discord`| `Aztec 4.3.0`               | Honk AI Discord bot (@-mention / reply) |
| `slack`  | `Honk AI — Slack`           | Honk AI Slack bot (Socket Mode, @-mention / thread reply) |
| `web_ask`| `Ask Aztec — public web`    | public `/ask` page                      |
| `mcp`    | `Aztec MCP` (per-user)      | `@aztec/mcp-server` consumers           |

Skip `surface='eval'` — that's the harness, not real users. Note: the agent
`name` drifts from the surface (e.g. Discord's display name tracks the corpus
version, not the surface) — always filter on `surface`, never `name`. Per-user
Slack MCP keys are `surface='mcp'` (distinguished structurally by
`mcp_provider='slack'`); only the shared Slack *chat* agent is `surface='slack'`.

## Pre-flight

Always prefix docker commands per the project's CLAUDE.md and `[[project_josh_box_docker]]`:

```bash
export PATH=/usr/bin:$PATH; unset DOCKER_HOST
```

`docker compose --since` does NOT accept `1d` / `7d` — use hours
(`--since 168h`). This bites every time.

`docker ps` first — there are two composes side by side. Only
`docsgpt-aztec-*` is production. `docsgpt-oss-*` is the dev smoke compose and
its data is noise for this report.

## Step 1 — Volume by surface

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -c "
SELECT a.surface,
       COUNT(DISTINCT c.id)                          AS conversations,
       COUNT(cm.id)                                  AS messages,
       COUNT(DISTINCT c.user_id)                     AS distinct_users,
       ROUND(AVG(LENGTH(cm.response))::numeric, 0)   AS avg_response_chars
FROM conversation_messages cm
JOIN conversations c ON c.id = cm.conversation_id
JOIN agents a        ON a.id = c.agent_id
WHERE cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
  AND a.surface IN ('widget','discord','web_ask','mcp','slack')
GROUP BY a.surface
ORDER BY messages DESC;
"
```

## Step 2 — Sample real conversations

Pull a representative sample per surface (cap at 50 messages per surface — that's
enough for the LLM to cluster topics + spot-check accuracy without blowing the
context window). Include `prompt`, `response`, `sources`, `model_id`,
`message_metadata->'citation_filter'`, `feedback`:

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -P pager=off -c "
SELECT a.surface,
       cm.timestamp,
       cm.model_id,
       cm.prompt,
       LEFT(cm.response, 1200) AS response,
       cm.message_metadata->'citation_filter' AS citation_filter,
       cm.feedback,
       cm.sources
FROM conversation_messages cm
JOIN conversations c ON c.id = cm.conversation_id
JOIN agents a        ON a.id = c.agent_id
WHERE cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
  AND a.surface IN ('widget','discord','web_ask','mcp','slack')
ORDER BY a.surface, RANDOM()
LIMIT 200;
" > /tmp/honk-sample.txt
```

Read the file with the Read tool; do NOT paste 200 rows into the bash output
buffer.

For the threaded chat surfaces (Discord + Slack), also pull thread-context
follow-ups (multi-turn conversations are where sentiment / dissatisfaction is
most visible):

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -P pager=off -c "
SELECT a.surface, cm.conversation_id, cm.position, cm.prompt, LEFT(cm.response, 600) AS resp
FROM conversation_messages cm
JOIN conversations c ON c.id = cm.conversation_id
JOIN agents a        ON a.id = c.agent_id
WHERE a.surface IN ('discord','slack')
  AND cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
  AND c.id IN (
    SELECT conversation_id FROM conversation_messages
    GROUP BY conversation_id HAVING COUNT(*) >= 3
  )
ORDER BY a.surface, cm.conversation_id, cm.position
LIMIT 100;
" > /tmp/honk-threads.txt
```

## Step 3 — Quality telemetry from Postgres

Aggregate the citation-filter audit and feedback signals:

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -c "
SELECT a.surface,
       COUNT(*) FILTER (WHERE cm.message_metadata->'citation_filter'->>'marker_present' = 'false') AS marker_missing,
       COUNT(*) FILTER (WHERE cm.message_metadata->'citation_filter'->>'marker_malformed' = 'true') AS marker_malformed,
       COUNT(*) FILTER (WHERE jsonb_array_length(cm.message_metadata->'citation_filter'->'invalid_indices') > 0) AS invalid_indices,
       COUNT(*) FILTER (WHERE (cm.message_metadata->'citation_filter'->>'filtered_count')::int = 0
                           AND cm.message_metadata->'citation_filter'->>'marker_present' = 'true') AS zero_cited,
       COUNT(*) FILTER (WHERE cm.feedback->>'rating' = 'positive' OR cm.feedback->>'feedback' = 'LIKE' OR cm.feedback->>'text' = 'like')     AS thumbs_up,
       COUNT(*) FILTER (WHERE cm.feedback->>'rating' = 'negative' OR cm.feedback->>'feedback' = 'DISLIKE' OR cm.feedback->>'text' = 'dislike') AS thumbs_down,
       COUNT(*) AS total_messages
FROM conversation_messages cm
JOIN conversations c ON c.id = cm.conversation_id
JOIN agents a        ON a.id = c.agent_id
WHERE cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
  AND a.surface IN ('widget','discord','web_ask','mcp','slack')
GROUP BY a.surface;
"
```

Note: `feedback` JSONB shape varies — the shape observed in prod is
`{"text": "like" | "dislike", "timestamp": …}` (handled by the `->>'text'`
clauses above), but older rows may use `rating`/`feedback` keys, so keep all
three. Always re-check actual values with
`SELECT DISTINCT feedback FROM conversation_messages WHERE feedback IS NOT NULL LIMIT 20;`
before reporting positive/negative counts. The bot writes via
`POST /api/feedback`; widget uses thumbs-up/down differently.

Pull all 👎 with their prompts — these are the highest-signal failures:

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -P pager=off -c "
SELECT a.surface, cm.prompt, LEFT(cm.response, 800) AS response, cm.feedback
FROM conversation_messages cm
JOIN conversations c ON c.id = cm.conversation_id
JOIN agents a        ON a.id = c.agent_id
WHERE cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
  AND cm.feedback IS NOT NULL
ORDER BY cm.timestamp DESC;
" > /tmp/honk-feedback.txt
```

## Step 4 — Quality telemetry from container logs

The DB doesn't record everything. Empty-response trips and breaker events
only live in container logs. The hub compose name is `docsgpt-aztec-`; use
the explicit file + env to avoid the dev compose:

```bash
HOURS=$((24 * $DAYS))

docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
  logs --since ${HOURS}h --no-color backend 2>&1 \
  | grep -E "llm\.empty_response|llm\.cited_(missing|malformed|invalid_index)|provider_error" \
  > /tmp/honk-backend-telemetry.txt

docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
  logs --since ${HOURS}h --no-color discord-bot 2>&1 \
  | grep -E "shared_429|breaker|cap_reached|cross_midnight|429" \
  > /tmp/honk-discord-telemetry.txt

docker compose -f deployment/docker-compose-hub.yaml --env-file .env \
  logs --since ${HOURS}h --no-color slack-bot 2>&1 \
  | grep -E "shared_429|breaker|cap_reached|cross_midnight|429" \
  > /tmp/honk-slack-telemetry.txt
```

Counts to extract per surface from these files:
- `llm.empty_response` — content-filter / silent provider failures
- `llm.cited_missing` — model didn't emit citation marker (fail-open)
- `llm.cited_malformed` / `llm.cited_invalid_index` — model emitted bad indices
- Discord breaker trips, shared-429 hits, per-guild cap exhaustion
- Slack breaker trips, shared-429 hits, per-workspace cap exhaustion

## Step 5 — Top sources cited

What docs are actually being retrieved? Helpful for spotting coverage gaps
(if users keep asking about X but X never gets cited, the corpus is weak).

```bash
docker exec docsgpt-aztec-postgres-1 psql -U docsgpt -d docsgpt -c "
WITH src AS (
  SELECT a.surface, jsonb_array_elements(cm.sources)->>'source' AS source_path
  FROM conversation_messages cm
  JOIN conversations c ON c.id = cm.conversation_id
  JOIN agents a        ON a.id = c.agent_id
  WHERE cm.timestamp >= NOW() - INTERVAL '\$DAYS days'
    AND a.surface IN ('widget','discord','web_ask','mcp','slack')
)
SELECT surface, source_path, COUNT(*) AS hits
FROM src
GROUP BY surface, source_path
ORDER BY surface, hits DESC
LIMIT 60;
"
```

## Step 6 — Analyze and write the report

Read the sample files (`/tmp/honk-sample.txt`, `/tmp/honk-threads.txt`,
`/tmp/honk-feedback.txt`, `/tmp/honk-*-telemetry.txt`) and produce the
following report. Use real quoted examples (with the prompt only — never the
pseudonym).

```
# Honk AI weekly report — YYYY-MM-DD (last $DAYS days)

## Volume
[per-surface table: conversations, messages, distinct users, avg response chars]

## What users are asking
Per surface, 4–6 topic clusters with 1-line example prompts. Distinguish
intent buckets (debugging, conceptual, how-to, installation, comparison).
Note any topic that's high-volume on one surface but absent on others.

## User intentions / pain signals
What are they trying to build or solve? Look for:
- repeated frustration phrasing ("still doesn't work", "I already tried")
- multi-turn follow-ups in the same thread (signal: first answer wasn't enough)
- direct asks for docs/links that the bot didn't surface
- terminology mismatches (users using v3 names for v4 concepts)

## Quality signals
- empty_response count / rate, with sample finish_reasons
- cited_missing / malformed / invalid_index counts (rates per surface)
- 👎 messages with prompts, grouped by failure mode (wrong answer, off-topic,
  hallucinated identifier, missing source, rude tone, etc.)
- citation filter dropping all sources (`zero_cited`) — is the answer still
  good, or is the model refusing to ground?

## Factual accuracy spot-check
Pick 8–12 answers across surfaces (mix of cited / uncited / 👎). For each,
either:
  (a) confirm against the cited source URL,
  (b) flag a specific claim as suspicious and explain why
      (e.g. wrong function signature, deprecated API, v3-ism in a v4 answer).
Don't just summarize — name the file:line or doc URL that contradicts the
answer. If unsure, mark "needs human review" rather than asserting wrong.

## Sentiment
Mostly neutral/positive is expected. Flag negative or frustrated examples
explicitly. Surface where sentiment differs (Discord skews more frustrated
than widget, etc.).

## Coverage gaps
Cross-reference frequent question topics with the `sources` cited. If a
topic recurs but the same 2–3 docs always get cited, the corpus may be thin
on that subject. Flag specific docs that should exist or be expanded.

## Recommendations
Concrete, ranked. Each one should be: (1) one-line statement, (2) the
evidence that supports it, (3) the proposed action (prompt tweak, source
add, retrieval knob, etc.). Avoid generic advice.

## Other patterns
Anything notable that didn't fit above — burst hours, repeat users,
cross-surface drift, model behaviour differences.
```

## Notes

- This skill READS prod state only. No writes, no restarts, no compose changes.
- The Postgres role grants in migration `0006_mcp_admin_tables.py` give the
  `docsgpt_mcp_ro` MCP role a narrower view than the queries above use; this
  skill uses the privileged `docsgpt` role via `docker exec`, which is fine
  for an on-host operator session but would not be portable to a remote
  read-only consumer. If/when running via the MCP `honk_sql.*` surface,
  drop the `conversations.api_key`, `agents.key`, etc. selections — they're
  REVOKED on that role.
- The 200-row sample cap in Step 2 is per the user's "verbose docs" friction —
  Claude can cluster topics off ~200 prompts faster and more accurately than
  off 2000, and large pastes blow context.
- Compression-on agents (default ON, see CLAUDE.md "Discord reaction →
  feedback" → known limitations) will record an extra row per conversation.
  Filter to `position % 2 = 0` or similar if message counts look inflated.
- Discord pseudonyms (`discord_p_v1:<hex>`) and Slack pseudonyms
  (`slack_p_v1:<hex>`) cannot be reversed — that's a feature
  ([[project_agent_surface_map]]). Talk about cohorts, not individuals. Slack
  raw identity is workspace-scoped (`team_id:user_id`, or
  `enterprise_id:team_id:user_id` on Grid) before hashing, so the same human in
  two workspaces is two distinct pseudonyms — don't dedupe across them.
