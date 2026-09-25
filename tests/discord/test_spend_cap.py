"""Unit tests for the per-guild daily estimated-USD spend cap.

Covers:
- ``_parse_guild_usd_caps`` validation: malformed entries, duplicates,
  negatives, non-finite, kill-switch ``0`` is accepted.
- ``_today_utc_str`` / ``_seconds_until_utc_midnight`` / ``_format_cap_reset_eta``.
- ``_estimate_call_usd`` arithmetic + non-negative clamp.
- ``_reserve_guild_spend`` / ``_finalize_guild_spend`` happy path,
  reconcile (positive + negative delta), refund, keep_charged,
  UTC date rollover, uncapped guild + DM short-circuit.
- Concurrent same-guild reserves via ``asyncio.gather``: worst-case
  overshoot is bounded by N × reserve, never more.
- ``_claim_cap_notice`` per-channel dedup keyed by UTC date.

Reuses the bootstrap pattern from ``test_shared_429_breaker.py``: skip
when ``discord`` isn't installed, stub ``commands.Bot.run`` so importing
the bot module doesn't open the gateway.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("discord")


@pytest.fixture(scope="module")
def bot_module():
    """Import the bot module with a single capped guild configured.

    The cap config is read at import via ``_parse_guild_usd_caps``, so
    the env var must be set before the import happens. Subsequent tests
    that need a different cap table call ``_parse_guild_usd_caps`` again
    and monkeypatch ``_GUILD_USD_CAPS``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "discord"
    sys.path.insert(0, str(bot_dir))

    os.environ["DISCORD_TOKEN"] = "test"
    os.environ["API_BASE"] = "http://localhost"
    os.environ["API_KEY"] = "test"
    # Two capped guilds for testing; one uncapped guild is implied by
    # absence from the map.
    os.environ["DISCORD_GUILD_DAILY_USD_CAPS"] = "111=20,222=5"

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
def _clear_spend_state(bot_module):
    """Each test starts with empty per-guild spend dicts + warn/trip sets.

    Restores ``_GUILD_USD_CAPS`` to the import-time config so tests that
    monkeypatch it (e.g. via env re-parse) don't leak across cases.
    """
    bot_module._GUILD_SPEND_TODAY.clear()
    bot_module._GUILD_SPEND_LOCKS.clear()
    bot_module._GUILD_SPEND_WARN_FIRED.clear()
    bot_module._GUILD_SPEND_TRIP_FIRED.clear()
    bot_module._CAP_NOTIFIED.clear()
    yield
    bot_module._GUILD_SPEND_TODAY.clear()
    bot_module._GUILD_SPEND_LOCKS.clear()
    bot_module._GUILD_SPEND_WARN_FIRED.clear()
    bot_module._GUILD_SPEND_TRIP_FIRED.clear()
    bot_module._CAP_NOTIFIED.clear()


class TestParseGuildUsdCaps:
    """``_parse_guild_usd_caps`` is the only path that turns env strings
    into the in-memory cap table. Defensive parsing matters because a
    typo in the env shouldn't prevent the bot from starting at all."""

    def test_well_formed_pair(self, bot_module, monkeypatch):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "999=12.5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {999: 12.5}

    def test_multiple_pairs_with_whitespace(self, bot_module, monkeypatch):
        monkeypatch.setenv(
            "DISCORD_GUILD_DAILY_USD_CAPS",
            "  111 = 20  , 222=5 ,  333= 0  ",
        )
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {111: 20.0, 222: 5.0, 333: 0.0}

    def test_zero_is_accepted_as_kill_switch(self, bot_module, monkeypatch):
        # Operator emergency: set to 0 to refuse every call in a guild
        # without code changes. Not a typo guard — a typo of 0 is at
        # least loud (no answers ever) vs. a typo of 200 instead of 20.
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "444=0")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {444: 0.0}

    def test_missing_equals(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "111,222=5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {222: 5.0}
        assert "missing '='" in caplog.text

    def test_non_integer_guild_id(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "abc=5,222=5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {222: 5.0}
        assert "non-integer guild id" in caplog.text

    def test_non_numeric_usd(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "111=oops,222=5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {222: 5.0}
        assert "non-numeric USD cap" in caplog.text

    def test_negative_usd_rejected(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "111=-5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {}
        assert "non-finite/negative" in caplog.text

    def test_inf_rejected(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "111=inf")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {}
        assert "non-finite/negative" in caplog.text

    def test_duplicate_first_wins(self, bot_module, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_GUILD_DAILY_USD_CAPS", "111=20,111=5")
        caps = bot_module._parse_guild_usd_caps()
        assert caps == {111: 20.0}
        assert "Duplicate guild id" in caplog.text

    def test_empty_env(self, bot_module, monkeypatch):
        monkeypatch.delenv("DISCORD_GUILD_DAILY_USD_CAPS", raising=False)
        assert bot_module._parse_guild_usd_caps() == {}


class TestDateAndFormatHelpers:
    def test_today_utc_str_uses_utc_date(self, bot_module):
        # Fix the input clock so the test is deterministic regardless of
        # the machine's wallclock or timezone.
        fixed = datetime.datetime(2026, 5, 15, 23, 59, 59, tzinfo=datetime.timezone.utc)
        assert bot_module._today_utc_str(fixed) == "2026-05-15"

    def test_today_utc_str_rolls_at_midnight(self, bot_module):
        before = datetime.datetime(2026, 5, 15, 23, 59, 59, tzinfo=datetime.timezone.utc)
        after = datetime.datetime(2026, 5, 16, 0, 0, 0, tzinfo=datetime.timezone.utc)
        assert bot_module._today_utc_str(before) != bot_module._today_utc_str(after)
        assert bot_module._today_utc_str(after) == "2026-05-16"

    def test_seconds_until_utc_midnight_zero_when_noon(self, bot_module):
        noon = datetime.datetime(2026, 5, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
        secs = bot_module._seconds_until_utc_midnight(noon)
        assert secs == 12 * 3600  # exactly 12 hours

    def test_format_cap_reset_eta_under_minute(self, bot_module):
        assert bot_module._format_cap_reset_eta(45) == "in less than a minute"

    def test_format_cap_reset_eta_hours_and_minutes(self, bot_module):
        # 2h 30m 1s → ceil(151.0166m) = 151m → 2h 31m
        assert bot_module._format_cap_reset_eta(2 * 3600 + 30 * 60 + 1) == "in about 2h 31m"

    def test_format_cap_reset_eta_exact_hours(self, bot_module):
        # Exactly 3 hours: ceil(180.0m) = 180m → 3h (no trailing 0m).
        assert bot_module._format_cap_reset_eta(3 * 3600) == "in about 3h"

    def test_format_cap_reset_eta_minutes_only(self, bot_module):
        # 45m → "in about 45m" (no "0h " prefix).
        assert bot_module._format_cap_reset_eta(45 * 60) == "in about 45m"


class TestEstimateCallUsd:
    def test_basic_arithmetic(self, bot_module, monkeypatch):
        # 1M prompt tokens at $0.20 + 0 completion tokens = $0.20.
        monkeypatch.setattr(bot_module, "_USD_PER_PROMPT_MTOK", 0.20)
        monkeypatch.setattr(bot_module, "_USD_PER_COMPLETION_MTOK", 1.20)
        assert bot_module._estimate_call_usd(1_000_000, 0) == pytest.approx(0.20)

    def test_fractional_call(self, bot_module, monkeypatch):
        monkeypatch.setattr(bot_module, "_USD_PER_PROMPT_MTOK", 0.20)
        monkeypatch.setattr(bot_module, "_USD_PER_COMPLETION_MTOK", 1.20)
        # 10k prompt + 500 completion = (10_000 * 0.20 + 500 * 1.20) / 1e6
        # = (2000 + 600) / 1e6 = $0.0026
        usd = bot_module._estimate_call_usd(10_000, 500)
        assert usd == pytest.approx(0.0026)

    def test_negative_inputs_clamped(self, bot_module):
        # Defensive: the backend should never emit negative counts but
        # if a malformed frame arrives we don't want to drop into a
        # negative bucket value that bypasses the cap.
        assert bot_module._estimate_call_usd(-10, -10) == 0.0


class TestReserveAndFinalize:
    """End-to-end check + reserve + reconcile/refund/keep_charged.

    These tests poke ``_GUILD_USD_CAPS`` directly so they don't depend
    on module-level env parsing — the parse tests above cover that.
    """

    @pytest.fixture(autouse=True)
    def _set_caps(self, bot_module, monkeypatch):
        # 20 USD cap on guild 111; uncapped guild 999; tight 1¢ reserve.
        monkeypatch.setitem(bot_module._GUILD_USD_CAPS, 111, 20.0)
        monkeypatch.setattr(bot_module, "_PRE_CALL_RESERVE_USD", 0.01)

    @pytest.mark.asyncio
    async def test_reserve_succeeds_when_under_cap(self, bot_module):
        reservation = await bot_module._reserve_guild_spend(111)
        # First call: 0 + 0.01 ≤ 20, succeeds. Returns (amount, date).
        assert reservation is not None
        reserved, date = reservation
        assert reserved == pytest.approx(0.01)
        assert date == bot_module._today_utc_str()
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_reserve_returns_none_at_cap(self, bot_module):
        # Pre-load bucket to exactly the cap; even a 1¢ reserve crosses it.
        today = bot_module._today_utc_str()
        bot_module._GUILD_SPEND_TODAY[111] = {"utc_date": today, "estimated_usd": 20.0}
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is None

    @pytest.mark.asyncio
    async def test_uncapped_guild_short_circuits(self, bot_module):
        # Guild 999 not in caps → no accounting performed; the
        # sentinel reservation is returned so callers can pass it
        # through to ``_finalize_guild_spend`` unconditionally.
        reservation = await bot_module._reserve_guild_spend(999)
        assert reservation == (0.0, "")
        assert 999 not in bot_module._GUILD_SPEND_TODAY

    @pytest.mark.asyncio
    async def test_dm_short_circuits(self, bot_module):
        reservation = await bot_module._reserve_guild_spend(None)
        assert reservation == (0.0, "")

    @pytest.mark.asyncio
    async def test_reconcile_replaces_reserve_with_actual(self, bot_module):
        # Reserve $0.01, reconcile to actual $0.005 → bucket holds $0.005.
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is not None
        reserved, date = reservation
        await bot_module._finalize_guild_spend(
            111, reserved, date, outcome="reconcile", actual_usd=0.005
        )
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.005)

    @pytest.mark.asyncio
    async def test_reconcile_with_higher_actual(self, bot_module):
        # Reserve $0.01, reconcile to $0.05 (heavy call) → bucket holds $0.05.
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is not None
        reserved, date = reservation
        await bot_module._finalize_guild_spend(
            111, reserved, date, outcome="reconcile", actual_usd=0.05
        )
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_refund_credits_back_full_reserve(self, bot_module):
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is not None
        reserved, date = reservation
        await bot_module._finalize_guild_spend(111, reserved, date, outcome="refund")
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_keep_charged_leaves_reserve_in_bucket(self, bot_module, caplog):
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is not None
        reserved, date = reservation
        with caplog.at_level("WARNING"):
            await bot_module._finalize_guild_spend(
                111, reserved, date, outcome="keep_charged"
            )
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.01)
        assert "guild.spend.kept_charged" in caplog.text

    @pytest.mark.asyncio
    async def test_finalize_is_noop_for_uncapped(self, bot_module):
        # Uncapped guild → finalize is a no-op even if a non-zero reserve
        # is passed (defensive — caller shouldn't, but contracts win).
        await bot_module._finalize_guild_spend(
            999, 0.01, "2026-05-15", outcome="reconcile", actual_usd=0.005
        )
        assert 999 not in bot_module._GUILD_SPEND_TODAY

    @pytest.mark.asyncio
    async def test_finalize_is_noop_for_empty_reserve_date(self, bot_module):
        # Empty reserve_date is the sentinel from uncapped/DM reserves;
        # finalize must short-circuit rather than try to look up "today".
        await bot_module._finalize_guild_spend(
            111, 0.01, "", outcome="reconcile", actual_usd=0.005
        )
        assert 111 not in bot_module._GUILD_SPEND_TODAY

    @pytest.mark.asyncio
    async def test_utc_date_rollover_resets_bucket(self, bot_module):
        # Yesterday: $19.99 spent. Today: bucket should reset to 0 on
        # the next access (sweep-on-read, not a wall-clock cron).
        bot_module._GUILD_SPEND_TODAY[111] = {
            "utc_date": "2000-01-01",
            "estimated_usd": 19.99,
        }
        reservation = await bot_module._reserve_guild_spend(111)
        assert reservation is not None
        reserved, date = reservation
        assert reserved == pytest.approx(0.01)
        assert date == bot_module._today_utc_str()
        # The stale-date entry was replaced, not added to.
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.01)
        assert bot_module._GUILD_SPEND_TODAY[111]["utc_date"] != "2000-01-01"

    @pytest.mark.asyncio
    async def test_cross_midnight_finalize_is_dropped(self, bot_module, caplog):
        """A call that reserves before 00:00 UTC but finalizes after
        must NOT apply the delta to today's fresh bucket.

        Earlier (pre-Codex-fix) code recomputed ``today`` inside
        ``_finalize_guild_spend`` and applied the refund / reconcile
        to the swept bucket — yesterday's reserve was thrown away
        and today's bucket either got a free credit (refund) or
        silently absorbed yesterday's spend (reconcile). The fix
        passes ``reserve_date`` through and short-circuits when it
        doesn't match today, logging ``cross_midnight_dropped``.
        """
        # Reserve was credited yesterday; bucket reflects that.
        bot_module._GUILD_SPEND_TODAY[111] = {
            "utc_date": "2026-05-14",
            "estimated_usd": 0.01,
        }
        with caplog.at_level("WARNING"):
            # Today is 2026-05-15 (the test environment's actual UTC
            # date doesn't matter — we pass `now` for determinism via
            # the constant string mismatch with reserve_date below).
            # Simpler: just supply a stale reserve_date relative to
            # whatever ``_today_utc_str`` returns. The drop fires when
            # they differ.
            await bot_module._finalize_guild_spend(
                111, 0.01, "1999-01-01",
                outcome="reconcile",
                actual_usd=0.005,
            )
        # Today's bucket was NOT touched.
        assert bot_module._GUILD_SPEND_TODAY[111]["utc_date"] == "2026-05-14"
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.01)
        assert "guild.spend.cross_midnight_dropped" in caplog.text


class TestConcurrency:
    """Lock around check+reserve bounds worst-case overshoot.

    Without the lock, N concurrent reserves could ALL pass the cap check
    when spent + reserve ≤ cap, then all reserve, leaving the bucket at
    spent + N * reserve. The per-guild ``asyncio.Lock`` serializes the
    check-then-credit pair so at most one excess reserve can sneak
    through, and the cap-trip path returns None for the rest.
    """

    @pytest.fixture(autouse=True)
    def _set_caps(self, bot_module, monkeypatch):
        # Tight cap to expose any concurrency bug: 5 cents, 1 cent reserve.
        monkeypatch.setitem(bot_module._GUILD_USD_CAPS, 111, 0.05)
        monkeypatch.setattr(bot_module, "_PRE_CALL_RESERVE_USD", 0.01)

    @pytest.mark.asyncio
    async def test_concurrent_reserves_respect_cap(self, bot_module):
        # 10 concurrent reserves against a cap that fits exactly 5. The
        # first 5 succeed (each credits 0.01, reaching 0.05). The next 5
        # all fail (0.05 + 0.01 > 0.05).
        results = await asyncio.gather(
            *(bot_module._reserve_guild_spend(111) for _ in range(10))
        )
        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]
        assert len(successes) == 5
        assert len(failures) == 5
        # Each success returns a (usd, date) tuple; all successes share
        # the same date (we're inside one UTC day for the test).
        for usd, date in successes:
            assert usd == pytest.approx(0.01)
            assert date == bot_module._today_utc_str()
        # Bucket is exactly at cap, never over.
        assert bot_module._GUILD_SPEND_TODAY[111]["estimated_usd"] == pytest.approx(0.05)


class TestClaimCapNotice:
    def test_first_claim_for_channel_today_succeeds(self, bot_module):
        today = bot_module._today_utc_str()
        assert bot_module._claim_cap_notice(channel_id=42, today=today) is True

    def test_second_claim_same_day_dedup(self, bot_module):
        today = bot_module._today_utc_str()
        bot_module._claim_cap_notice(channel_id=42, today=today)
        assert bot_module._claim_cap_notice(channel_id=42, today=today) is False

    def test_different_channels_independent(self, bot_module):
        today = bot_module._today_utc_str()
        assert bot_module._claim_cap_notice(channel_id=42, today=today) is True
        # Sibling channel still allowed its own first notice.
        assert bot_module._claim_cap_notice(channel_id=43, today=today) is True

    def test_new_day_evicts_stale(self, bot_module):
        bot_module._claim_cap_notice(channel_id=42, today="2026-05-14")
        # New day: stale entry sweeps, fresh notice allowed.
        assert bot_module._claim_cap_notice(channel_id=42, today="2026-05-15") is True
        # And the stale entry was actually evicted from the dict.
        assert (42, "2026-05-14") not in bot_module._CAP_NOTIFIED
