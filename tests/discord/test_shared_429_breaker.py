"""Unit tests for the per-guild shared-bucket 429 circuit breaker.

Covers:
- ``_is_shared_429`` correctly identifies 429 + code 40062, rejects
  other 429s (e.g. cloudflare 40333) and other status codes.
- ``_trip_breaker`` opens the breaker, ``_breaker_open`` reflects it.
- Expired entries auto-clear on read so the dict can't grow unbounded.
- Per-guild scoping: tripping guild A doesn't suppress guild B; ``None``
  (DM) is its own key.

Reuses the bootstrap pattern from ``test_feedback.py``: skip the whole
module when ``discord`` isn't importable, stub ``commands.Bot.run`` to
avoid touching the gateway during import.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")


@pytest.fixture(scope="module")
def bot_module():
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "discord"
    sys.path.insert(0, str(bot_dir))

    os.environ.setdefault("DISCORD_TOKEN", "test")
    os.environ.setdefault("API_BASE", "http://localhost")
    os.environ.setdefault("API_KEY", "test")

    from discord.ext import commands  # noqa: WPS433

    original_run = commands.Bot.run
    commands.Bot.run = lambda self, *a, **kw: None  # type: ignore[assignment]
    try:
        if "bot" in sys.modules:
            del sys.modules["bot"]
        bot_mod = importlib.import_module("bot")
    finally:
        commands.Bot.run = original_run  # type: ignore[assignment]

    yield bot_mod

    sys.path.remove(str(bot_dir))


@pytest.fixture(autouse=True)
def _clear_breaker(bot_module):
    """Each test starts with an empty tripped-guilds dict."""
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()
    yield
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()


class TestIsShared429:
    """``_is_shared_429`` is duck-typed on ``status`` and ``code`` so tests
    don't have to construct a full ``discord.HTTPException`` (which
    requires a live ``aiohttp.ClientResponse``)."""

    def test_429_with_code_40062_is_shared(self, bot_module):
        exc = SimpleNamespace(status=429, code=40062)
        assert bot_module._is_shared_429(exc) is True

    def test_429_with_other_code_is_not_shared(self, bot_module):
        # Cloudflare-edge block code; explicitly NOT a shared bucket.
        exc = SimpleNamespace(status=429, code=40333)
        assert bot_module._is_shared_429(exc) is False

    def test_429_with_no_code_is_not_shared(self, bot_module):
        # discord.py's HTTPException sets ``code=0`` when the response
        # body has no JSON ``code`` field (e.g. a global / per-bot
        # rate-limit response). Must not trip the breaker.
        exc = SimpleNamespace(status=429, code=0)
        assert bot_module._is_shared_429(exc) is False

    def test_500_with_40062_is_not_shared(self, bot_module):
        # Defensive: never trip on non-429 statuses even if code matches.
        exc = SimpleNamespace(status=500, code=40062)
        assert bot_module._is_shared_429(exc) is False

    def test_missing_attributes_does_not_crash(self, bot_module):
        # Using getattr(..., None) so a malformed exception object
        # falls through cleanly instead of AttributeError-ing the
        # whole reply path.
        assert bot_module._is_shared_429(SimpleNamespace()) is False

    def test_rate_limited_exception_is_recognized(self, bot_module):
        # discord.RateLimited is raised when discord.py's HTTP layer
        # short-circuits before retrying because retry_after exceeds
        # max_ratelimit_timeout (we set this to 2.0s — see the comment
        # in bot.py near the bot.http.max_ratelimit_timeout assignment).
        # 40062 always returns retry_after=3, so this is the typical
        # failure shape now. Must trip the breaker just like the
        # post-retry HTTPException path.
        exc = discord.RateLimited(retry_after=3.0)
        assert bot_module._is_shared_429(exc) is True

    def test_rate_limited_with_short_retry_still_recognized(self, bot_module):
        # Even if Discord returns a small retry_after (unusual but
        # possible), RateLimited is treated the same: discord.py
        # already decided not to retry, so we honour that and trip.
        exc = discord.RateLimited(retry_after=0.5)
        assert bot_module._is_shared_429(exc) is True


class TestMaxRateLimitTimeoutOverride:
    """The module-level ``bot.http.max_ratelimit_timeout = 2.0`` line
    bypasses discord.py's constructor floor of 30s. Verify the
    override actually took effect at import time so a future refactor
    that loses the assignment is caught by tests rather than by
    Discord re-flagging us in production."""

    def test_max_ratelimit_timeout_capped_at_2s(self, bot_module):
        # The constructor would have clamped to max(30, 2)=30; setting
        # post-init writes 2.0 directly into the attribute.
        assert bot_module.bot.http.max_ratelimit_timeout == 2.0


class TestBreakerLifecycle:
    def test_fresh_guild_is_not_tripped(self, bot_module):
        assert bot_module._breaker_open(123) is False

    def test_trip_then_open(self, bot_module):
        bot_module._trip_breaker(123, "test")
        assert bot_module._breaker_open(123) is True

    def test_per_guild_scoping(self, bot_module):
        bot_module._trip_breaker(111, "test")
        assert bot_module._breaker_open(111) is True
        assert bot_module._breaker_open(222) is False

    def test_dm_uses_none_key(self, bot_module):
        # DMs have no guild; the breaker is keyed by guild_id with None
        # as the DM bucket. Tripping a guild must not silence DMs.
        bot_module._trip_breaker(555, "test")
        assert bot_module._breaker_open(None) is False
        bot_module._trip_breaker(None, "dm test")
        assert bot_module._breaker_open(None) is True
        assert bot_module._breaker_open(555) is True

    def test_expired_entry_auto_clears(self, bot_module):
        # Insert an already-expired entry directly. The next read
        # should report False AND remove the entry, so the dict can't
        # grow unbounded across long-running deploys.
        bot_module._SHARED_429_TRIPPED_GUILDS[42] = time.monotonic() - 1.0
        assert bot_module._breaker_open(42) is False
        assert 42 not in bot_module._SHARED_429_TRIPPED_GUILDS

    def test_trip_extends_existing_entry(self, bot_module):
        # Repeated trips during a sustained incident should refresh
        # the cooldown so the breaker stays open until the bucket
        # actually clears, rather than expiring mid-incident.
        bot_module._SHARED_429_TRIPPED_GUILDS[7] = time.monotonic() + 1.0
        old_expiry = bot_module._SHARED_429_TRIPPED_GUILDS[7]
        bot_module._trip_breaker(7, "second hit")
        new_expiry = bot_module._SHARED_429_TRIPPED_GUILDS[7]
        assert new_expiry > old_expiry

    def test_cooldown_uses_configured_seconds(self, bot_module, monkeypatch):
        # The cooldown is sourced from a module-level constant; flexing
        # it should change how long a fresh trip stays open.
        monkeypatch.setattr(bot_module, "_SHARED_429_COOLDOWN_SECONDS", 99)
        before = time.monotonic()
        bot_module._trip_breaker(8, "cooldown test")
        expiry = bot_module._SHARED_429_TRIPPED_GUILDS[8]
        # Allow a small slack for time-call jitter between the calls.
        assert 98 <= (expiry - before) <= 100


class TestSignalBreakerOpen:
    """``_signal_breaker_open`` swallows reaction failures so the bot
    doesn't log an additional warning chain after a 40062 trip."""

    def test_adds_warning_reaction(self, bot_module):
        message = SimpleNamespace(add_reaction=AsyncMock())
        asyncio.get_event_loop().run_until_complete(bot_module._signal_breaker_open(message))
        message.add_reaction.assert_awaited_once_with(bot_module._BREAKER_REACTION)

    def test_swallows_http_exception(self, bot_module):
        # Mock raises HTTPException-shaped error; the helper must NOT
        # propagate (we already know writes are degraded).
        async def raise_http(*_a, **_kw):
            raise discord.HTTPException(
                SimpleNamespace(status=429, reason="Too Many Requests"),
                {"code": 40062, "message": "Service resource is being rate limited"},
            )

        message = SimpleNamespace(add_reaction=raise_http)
        # Should not raise.
        asyncio.get_event_loop().run_until_complete(bot_module._signal_breaker_open(message))

    def test_swallows_forbidden(self, bot_module):
        async def raise_forbidden(*_a, **_kw):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                {"code": 50013, "message": "Missing Permissions"},
            )

        message = SimpleNamespace(add_reaction=raise_forbidden)
        asyncio.get_event_loop().run_until_complete(bot_module._signal_breaker_open(message))


class TestConcurrentTrip:
    """Document the per-guild scope of the breaker against per-thread
    locking. Two concurrent mentions in DIFFERENT threads of the same
    guild don't share a lock, so coroutine A can trip the breaker
    after coroutine B has passed its initial check. The bot's /stream
    re-check must catch this — without it, B would burn an LLM call
    whose answer can't be delivered (sends will hit the same 40062).

    These tests are state-only: they assert the breaker dict reflects
    the cross-coroutine visibility we depend on. The actual recheck
    callsite in ``on_message`` is exercised via the breaker dict's
    ``_breaker_open`` reads.
    """

    def test_trip_in_one_path_visible_to_other(self, bot_module):
        # Coroutine A trips. Coroutine B reads after the trip and
        # sees the breaker open for the same guild — even though B's
        # initial on_message check ran BEFORE the trip.
        bot_module._trip_breaker(123, "coroutine A typing failed")
        assert bot_module._breaker_open(123) is True

    def test_recheck_distinguishes_other_guild(self, bot_module):
        # The recheck before /stream uses the same guild_id key, so
        # an unrelated guild's incident doesn't suppress this one.
        bot_module._trip_breaker(111, "guild 111 incident")
        assert bot_module._breaker_open(222) is False
        assert bot_module._breaker_open(111) is True
