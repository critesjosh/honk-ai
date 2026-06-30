"""0012 content_encryption — blind-index column for api_key lookups.

Part of the application-layer content-encryption-at-rest rollout (see
``PLAN-content-encryption.md``). Once ``user_logs.data`` is stored as a
whole-blob ciphertext, the legacy ``data->>'api_key'`` equality filter can no
longer reach the key, so ``UserLogsRepository`` looks up by a deterministic
HMAC fingerprint instead (``api_key_fingerprint`` in
``application/security/content_registry.py``).

This migration adds the ``api_key_fp`` column + a partial index and nothing
else. Specifically it does NOT add the per-column ``honkenc:``/``__enc__`` CHECK
constraints: those would reject the plaintext writes that still happen while
``CONTENT_ENCRYPTION_ENABLED`` is off (the pre-flip rollout window, dev, tests).
The CHECK constraints are added + VALIDATEd in a later migration at the strict
cutover, after the backfill proves zero plaintext (rollout §13 step 6).

``api_key_fp`` is intentionally NOT granted to ``docsgpt_mcp_ro`` (per the 0006
audit policy: new columns stay unreadable to the MCP role until reviewed).

Revision ID: 0012_content_encryption
Revises: 0011_requester_attribution
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0012_content_encryption"
down_revision: Union[str, None] = "0011_requester_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_logs ADD COLUMN api_key_fp text")
    # Partial index: only the rows that actually carry a key (anonymous/widget
    # traffic has none), keeping the index small. Non-unique — many log rows
    # share one agent key.
    op.execute("CREATE INDEX user_logs_api_key_fp_idx ON user_logs (api_key_fp) WHERE api_key_fp IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS user_logs_api_key_fp_idx")
    op.execute("ALTER TABLE user_logs DROP COLUMN api_key_fp")
