-- verify_pseudonymization.sql — post-deploy verification for
-- migration 0005_pseudonymize_discord_user_ids.
--
-- Usage:
--   docker compose exec postgres psql -U "$POSTGRES_USER" \
--     -d "$POSTGRES_DB" -f /tmp/verify_pseudonymization.sql
--
-- Every count under "negative sentinels" must be 0. The "positive
-- sentinels" must be > 0 if any Discord users have ever provisioned
-- an MCP key on this deploy.

\echo
\echo '================================================================='
\echo 'Negative sentinels — every value below must be 0'
\echo '================================================================='

-- "user_id still contains discord:<raw>" — across every user-keyed table.
SELECT 'users'                  AS tbl, count(*) AS plaintext_rows
       FROM users                  WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'prompts',                       count(*)
       FROM prompts                WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'user_tools',                    count(*)
       FROM user_tools             WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'token_usage',                   count(*)
       FROM token_usage            WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'user_logs',                     count(*)
       FROM user_logs              WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'stack_logs',                    count(*)
       FROM stack_logs             WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'agent_folders',                 count(*)
       FROM agent_folders          WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'sources',                       count(*)
       FROM sources                WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'agents',                        count(*)
       FROM agents                 WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'attachments',                   count(*)
       FROM attachments            WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'memories',                      count(*)
       FROM memories               WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'todos',                         count(*)
       FROM todos                  WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'notes',                         count(*)
       FROM notes                  WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'connector_sessions',            count(*)
       FROM connector_sessions     WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'conversations',                 count(*)
       FROM conversations          WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'conversation_messages',         count(*)
       FROM conversation_messages  WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'shared_conversations',          count(*)
       FROM shared_conversations   WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'pending_tool_state',            count(*)
       FROM pending_tool_state     WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'workflows',                     count(*)
       FROM workflows              WHERE user_id LIKE 'discord:%'
UNION ALL SELECT 'workflow_runs',                 count(*)
       FROM workflow_runs          WHERE user_id LIKE 'discord:%'
ORDER BY tbl;

-- agents.mcp_provider_user_id is bare 32-char hex post-migration.
-- A row that is still all-digits means it's still a Discord snowflake —
-- this is a NEGATIVE sentinel (must be 0), not the positive validator
-- for the new format.
SELECT 'agents.mcp_provider_user_id still raw' AS check_name,
       count(*)                                 AS still_raw_snowflake
FROM agents
WHERE mcp_provider = 'discord' AND mcp_provider_user_id ~ '^[0-9]+$';

-- agents.name should be exactly 'Aztec MCP' for every Discord agent —
-- no embedded usernames.
SELECT 'agents.name still has username' AS check_name,
       count(*)                          AS still_named_with_username
FROM agents
WHERE mcp_provider = 'discord' AND name <> 'Aztec MCP';


\echo
\echo '================================================================='
\echo 'Positive sentinels — should be > 0 if any Discord users exist'
\echo '================================================================='

-- Two distinct shapes to verify since the column types differ:
--   1. user_id columns carry the prefixed form ``discord_p_v1:<32hex>``.
--   2. agents.mcp_provider_user_id carries the BARE 32-char hex (no prefix).
-- Checking only one shape would misread a correctly migrated deployment
-- as broken in the other column.

SELECT 'users.user_id pseudonymized'           AS check_name,
       count(*)                                AS pseudo_users
FROM users WHERE user_id LIKE 'discord_p_v1:%';

SELECT 'agents.mcp_provider_user_id is hex'    AS check_name,
       count(*)                                AS pseudo_agents
FROM agents
WHERE mcp_provider = 'discord' AND mcp_provider_user_id ~ '^[a-f0-9]{32}$';
