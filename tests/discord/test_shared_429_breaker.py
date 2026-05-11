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
    """Each test starts with empty tripped-guilds and notified-channels dicts."""
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()
    bot_module._BREAKER_NOTIFIED.clear()
    yield
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()
    bot_module._BREAKER_NOTIFIED.clear()


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

    def test_breaker_reaction_is_hourglass(self, bot_module):
        # Locked in so a future rename of the constant doesn't silently
        # revert to ⚠️ (warning sign). The hourglass communicates
        # "waiting / will get back to it later"; the warning sign read
        # as "error / danger" to users and was not actionable.
        assert bot_module._BREAKER_REACTION == "\N{HOURGLASS WITH FLOWING SAND}"

    def test_adds_breaker_reaction(self, bot_module):
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


class TestBreakerTimeRemaining:
    """``_breaker_time_remaining`` is the source of truth for the ETA
    we render to users. Must never go negative (would render gibberish
    via ``_format_breaker_eta``) and must NOT mutate state on read —
    eviction is ``_breaker_open``'s job."""

    def test_zero_when_no_entry(self, bot_module):
        assert bot_module._breaker_time_remaining(999) == 0.0

    def test_positive_after_trip(self, bot_module):
        bot_module._trip_breaker(123, "test")
        remaining = bot_module._breaker_time_remaining(123)
        # Default cooldown is 60s; allow generous slack for the time
        # between _trip_breaker's monotonic() call and ours.
        assert 50 <= remaining <= 61

    def test_zero_when_expired(self, bot_module, monkeypatch):
        # Insert an already-elapsed expiry directly. Must read as 0.0
        # without raising, so the eta-formatter renders cleanly.
        bot_module._SHARED_429_TRIPPED_GUILDS[42] = time.monotonic() - 5.0
        assert bot_module._breaker_time_remaining(42) == 0.0

    def test_does_not_evict_expired_entry(self, bot_module):
        # Eviction is _breaker_open's job; this helper must not have
        # side effects on the dict. Otherwise concurrent readers could
        # race on the dict mutation.
        bot_module._SHARED_429_TRIPPED_GUILDS[42] = time.monotonic() - 5.0
        bot_module._breaker_time_remaining(42)
        assert 42 in bot_module._SHARED_429_TRIPPED_GUILDS

    def test_dm_uses_none_key(self, bot_module):
        bot_module._trip_breaker(None, "dm trip")
        remaining = bot_module._breaker_time_remaining(None)
        assert remaining > 0


class TestFormatBreakerEta:
    """The notice copy is the only user-facing surface of the breaker.
    Boundary semantics are deliberately conservative — we ``ceil``
    rather than ``round`` so the user is never told to retry into a
    still-open breaker."""

    def test_zero_renders_less_than_a_minute(self, bot_module):
        assert bot_module._format_breaker_eta(0) == "in less than a minute"

    def test_thirty_seconds(self, bot_module):
        assert bot_module._format_breaker_eta(30) == "in less than a minute"

    def test_fifty_nine_seconds(self, bot_module):
        assert bot_module._format_breaker_eta(59) == "in less than a minute"

    def test_sixty_seconds_is_one_minute_singular(self, bot_module):
        # 60s ceil = 1 minute; copy must use the singular form.
        assert bot_module._format_breaker_eta(60) == "in about 1 minute"

    def test_eighty_nine_seconds_is_two_minutes_not_one(self, bot_module):
        # 89s rounds to 1m with round() — that would have the user
        # retry too soon. Ceil gives 2m, which is conservative.
        assert bot_module._format_breaker_eta(89) == "in about 2 minutes"

    def test_one_nineteen_seconds_is_two_minutes(self, bot_module):
        assert bot_module._format_breaker_eta(119) == "in about 2 minutes"

    def test_one_twenty_seconds_is_two_minutes(self, bot_module):
        assert bot_module._format_breaker_eta(120) == "in about 2 minutes"

    def test_two_ninety_nine_seconds_is_five_minutes(self, bot_module):
        assert bot_module._format_breaker_eta(299) == "in about 5 minutes"

    def test_three_hundred_seconds_is_five_minutes(self, bot_module):
        assert bot_module._format_breaker_eta(300) == "in about 5 minutes"


class TestClaimBreakerNotice:
    """Per-channel, per-window dedup: one notice per (channel,
    breaker-window). Prevents the bot from queueing one reply per
    silenced mention in a busy channel during a sustained outage."""

    def test_first_claim_succeeds(self, bot_module):
        bot_module._trip_breaker(111, "test")
        assert bot_module._claim_breaker_notice(channel_id=222, guild_id=111) is True

    def test_second_claim_same_channel_returns_false(self, bot_module):
        bot_module._trip_breaker(111, "test")
        assert bot_module._claim_breaker_notice(222, 111) is True
        assert bot_module._claim_breaker_notice(222, 111) is False

    def test_different_channels_independent(self, bot_module):
        bot_module._trip_breaker(111, "test")
        assert bot_module._claim_breaker_notice(222, 111) is True
        assert bot_module._claim_breaker_notice(333, 111) is True

    def test_new_trip_re_arms_same_channel(self, bot_module):
        # If the breaker re-trips (e.g. a second 429 after the cooldown
        # would have started ticking down), _trip_breaker writes a new
        # expiry. The dedup is keyed by expiry, so the next mention
        # gets a fresh notice.
        bot_module._trip_breaker(111, "first trip")
        assert bot_module._claim_breaker_notice(222, 111) is True
        # Sleep nothing — _trip_breaker uses monotonic+cooldown, so a
        # second call mid-test produces a strictly-greater expiry.
        bot_module._trip_breaker(111, "second trip")
        assert bot_module._claim_breaker_notice(222, 111) is True

    def test_no_breaker_open_allows_notice(self, bot_module):
        # Defensive: if the breaker raced closed between the caller's
        # _breaker_open() check and this claim, send the notice anyway
        # rather than swallowing it. No record is stored.
        assert bot_module._claim_breaker_notice(222, 999) is True
        assert 222 not in bot_module._BREAKER_NOTIFIED

    def test_elapsed_but_unevicted_expiry_treated_as_raced_closed(self, bot_module):
        # Edge case: the breaker entry is still in the dict (because
        # nobody called _breaker_open() to evict it) but the recorded
        # expiry has already elapsed. We must NOT record this stale
        # expiry — otherwise a fresh trip with the SAME-valued expiry
        # (unlikely, but possible across clock resolution boundaries)
        # would be wrongly deduped. Treat exactly like raced-closed.
        bot_module._SHARED_429_TRIPPED_GUILDS[111] = time.monotonic() - 5.0
        assert bot_module._claim_breaker_notice(222, 111) is True
        assert 222 not in bot_module._BREAKER_NOTIFIED

    def test_lazy_gc_evicts_stale_entries(self, bot_module):
        # A long-running deploy with sporadic incidents shouldn't grow
        # _BREAKER_NOTIFIED unboundedly. Entries whose recorded expiry
        # has elapsed are evicted on the next claim call.
        bot_module._BREAKER_NOTIFIED[999] = time.monotonic() - 10.0
        bot_module._trip_breaker(111, "test")
        bot_module._claim_breaker_notice(222, 111)
        assert 999 not in bot_module._BREAKER_NOTIFIED


class TestSignalBreakerSilenced:
    """``_signal_breaker_silenced`` is the user-facing handler for
    every silenced mention. Critical properties:

    - Sends a text notice on the FIRST silenced mention per channel.
    - Falls back to ⏳ reaction if the notice send 429s.
    - Does NOT call ``_trip_breaker`` on notice failure (would extend
      the cooldown via our own retries).
    - Dedups subsequent mentions in the same channel.
    """

    def _make_message(
        self, *, channel_id=222, guild_id=111, send=None, add_reaction=None
    ):
        target = SimpleNamespace(
            id=channel_id,
            send=send if send is not None else AsyncMock(),
        )
        guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        return SimpleNamespace(
            channel=target,
            guild=guild,
            add_reaction=add_reaction if add_reaction is not None else AsyncMock(),
        )

    def test_sends_text_notice_on_first_mention(self, bot_module):
        bot_module._trip_breaker(111, "test")
        message = self._make_message()
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.channel.send.assert_awaited_once()
        sent_text = message.channel.send.await_args.args[0]
        assert "rate-limited" in sent_text
        assert "in about 3 minutes" in sent_text
        message.add_reaction.assert_not_awaited()

    def test_dedups_second_mention_same_channel(self, bot_module):
        bot_module._trip_breaker(111, "test")
        m1 = self._make_message()
        m2 = self._make_message()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            bot_module._signal_breaker_silenced(m1, eta_seconds=180.0)
        )
        loop.run_until_complete(
            bot_module._signal_breaker_silenced(m2, eta_seconds=180.0)
        )
        m1.channel.send.assert_awaited_once()
        m2.channel.send.assert_not_awaited()
        m2.add_reaction.assert_not_awaited()

    def test_explicit_target_overrides_channel(self, bot_module):
        # When a thread was created before the breaker tripped, the
        # caller passes the thread as ``target`` so the notice lands
        # where the user is waiting.
        bot_module._trip_breaker(111, "test")
        message = self._make_message()
        thread = SimpleNamespace(id=999, send=AsyncMock())
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, target=thread, eta_seconds=180.0)
        )
        thread.send.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    def test_fallback_reaction_when_send_raises_rate_limited(self, bot_module):
        bot_module._trip_breaker(111, "test")

        async def raise_rate_limited(*_a, **_kw):
            raise discord.RateLimited(retry_after=3.0)

        message = self._make_message(send=raise_rate_limited)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.add_reaction.assert_awaited_once_with(bot_module._BREAKER_REACTION)

    def test_fallback_reaction_when_send_raises_http_exception(self, bot_module):
        bot_module._trip_breaker(111, "test")

        async def raise_http(*_a, **_kw):
            raise discord.HTTPException(
                SimpleNamespace(status=429, reason="Too Many Requests"),
                {"code": 40062, "message": "Service resource is being rate limited"},
            )

        message = self._make_message(send=raise_http)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.add_reaction.assert_awaited_once()

    def test_fallback_reaction_when_send_raises_forbidden(self, bot_module):
        bot_module._trip_breaker(111, "test")

        async def raise_forbidden(*_a, **_kw):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                {"code": 50013, "message": "Missing Permissions"},
            )

        message = self._make_message(send=raise_forbidden)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.add_reaction.assert_awaited_once()

    def test_fallback_reaction_when_send_raises_not_found(self, bot_module):
        bot_module._trip_breaker(111, "test")

        async def raise_not_found(*_a, **_kw):
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10003, "message": "Unknown Channel"},
            )

        message = self._make_message(send=raise_not_found)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.add_reaction.assert_awaited_once()

    def test_notice_failure_does_not_extend_breaker(self, bot_module):
        # If we re-trip on the notice send itself, the breaker's
        # original expiry gets pushed back by another full cooldown —
        # making the silence WORSE. The silenced-handler must NOT
        # call _trip_breaker, even on a 40062 response.
        bot_module._trip_breaker(111, "test")
        expiry_before = bot_module._SHARED_429_TRIPPED_GUILDS[111]

        async def raise_shared_429(*_a, **_kw):
            raise discord.RateLimited(retry_after=3.0)

        message = self._make_message(send=raise_shared_429)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        # Expiry should be unchanged: no _trip_breaker call from the
        # silence path.
        assert bot_module._SHARED_429_TRIPPED_GUILDS[111] == expiry_before

    def test_dm_uses_none_guild_key(self, bot_module):
        # DMs have message.guild = None. The breaker keys DMs under
        # None; the silenced helper must read the right key.
        bot_module._trip_breaker(None, "dm trip")
        message = self._make_message(channel_id=42, guild_id=None)
        asyncio.get_event_loop().run_until_complete(
            bot_module._signal_breaker_silenced(message, eta_seconds=180.0)
        )
        message.channel.send.assert_awaited_once()
        # Dedup should record under target.id keyed against the
        # None-guild's expiry.
        assert 42 in bot_module._BREAKER_NOTIFIED
