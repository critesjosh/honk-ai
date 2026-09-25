"""Unit tests for the Honk AI Discord bot's reaction-driven feedback path.

Reuses the bootstrap pattern from test_sources_footer.py: skip the whole
module when ``discord`` isn't importable, and stub ``commands.Bot.run``
to avoid touching the gateway during the import.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
def _clear_feedback_cache(bot_module):
    """Each test starts with an empty feedback_targets cache."""
    bot_module.feedback_targets.clear()
    yield
    bot_module.feedback_targets.clear()


class TestRegisterFeedbackTarget:
    def test_basic_register_and_lookup(self, bot_module):
        bot_module._register_feedback_target(111, "conv-a", 0)
        assert bot_module.feedback_targets[111] == ("conv-a", 0)

    def test_register_overwrites_existing(self, bot_module):
        bot_module._register_feedback_target(111, "conv-a", 0)
        bot_module._register_feedback_target(111, "conv-b", 5)
        assert bot_module.feedback_targets[111] == ("conv-b", 5)

    def test_lru_evicts_oldest_first(self, bot_module, monkeypatch):
        monkeypatch.setattr(bot_module, "_FEEDBACK_CACHE_MAX_ENTRIES", 3)
        bot_module._register_feedback_target(1, "conv", 0)
        bot_module._register_feedback_target(2, "conv", 1)
        bot_module._register_feedback_target(3, "conv", 2)
        bot_module._register_feedback_target(4, "conv", 3)
        # Oldest (id=1) is gone; newest three are preserved.
        assert 1 not in bot_module.feedback_targets
        assert set(bot_module.feedback_targets) == {2, 3, 4}

    def test_re_register_refreshes_lru_position(self, bot_module, monkeypatch):
        monkeypatch.setattr(bot_module, "_FEEDBACK_CACHE_MAX_ENTRIES", 3)
        bot_module._register_feedback_target(1, "conv", 0)
        bot_module._register_feedback_target(2, "conv", 1)
        bot_module._register_feedback_target(3, "conv", 2)
        # Touch id=1 — it should now be the newest.
        bot_module._register_feedback_target(1, "conv", 0)
        bot_module._register_feedback_target(4, "conv", 3)
        # id=2 was the oldest after the touch, so it gets evicted.
        assert 2 not in bot_module.feedback_targets
        assert set(bot_module.feedback_targets) == {1, 3, 4}


class TestOnRawReactionAdd:
    """Drive the @bot.event handler directly with a fake payload.

    We call the underlying coroutine via the registered listener so the
    decorator wiring is also exercised. Reactions are fired through
    ``bot.dispatch`` in the real flow, but ``Client.dispatch`` requires
    a running event loop and the gateway being up; instead we look up
    the listener in ``bot.extra_events`` and await it directly.
    """

    def _resolve_handler(self, bot_module):
        # `@bot.event` (Client.event) sets the coroutine as an
        # attribute on the bot, not in `extra_events`. Pull it from
        # there so we exercise the same callable the gateway invokes.
        return bot_module.bot.on_raw_reaction_add

    def _make_payload(self, message_id: int, emoji_name: str, user_id: int = 999):
        # discord.RawReactionActionEvent has many fields; the bot
        # touches user_id, message_id, emoji.name, and member.bot.
        # ``member`` is None for DM-channel events (matches the real
        # discord.py payload). Using duck-typed SimpleNamespaces keeps
        # the test independent of discord.py's internal constructor
        # signature.
        return SimpleNamespace(
            user_id=user_id,
            message_id=message_id,
            emoji=SimpleNamespace(name=emoji_name),
            member=None,
        )

    def test_thumbs_up_posts_like(self, bot_module):
        bot_module._register_feedback_target(7777, "conv-x", 3)
        handler = self._resolve_handler(bot_module)
        payload = self._make_payload(7777, bot_module._LIKE_EMOJI)

        captured: dict = {}

        async def fake_submit(conv_id, idx, fb):
            captured["args"] = (conv_id, idx, fb)
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert captured["args"] == ("conv-x", 3, "LIKE")

    def test_thumbs_down_posts_dislike(self, bot_module):
        bot_module._register_feedback_target(8888, "conv-y", 1)
        handler = self._resolve_handler(bot_module)
        payload = self._make_payload(8888, bot_module._DISLIKE_EMOJI)

        captured: dict = {}

        async def fake_submit(conv_id, idx, fb):
            captured["args"] = (conv_id, idx, fb)
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert captured["args"] == ("conv-y", 1, "DISLIKE")

    def test_unknown_emoji_is_ignored(self, bot_module):
        bot_module._register_feedback_target(9999, "conv-z", 0)
        handler = self._resolve_handler(bot_module)
        payload = self._make_payload(9999, "\N{HEAVY BLACK HEART}")

        called = False

        async def fake_submit(*_a, **_kw):
            nonlocal called
            called = True
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert called is False

    def test_unknown_message_is_ignored(self, bot_module):
        # No _register_feedback_target call, so the cache miss path
        # silently returns without invoking the backend.
        handler = self._resolve_handler(bot_module)
        payload = self._make_payload(424242, bot_module._LIKE_EMOJI)

        called = False

        async def fake_submit(*_a, **_kw):
            nonlocal called
            called = True
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert called is False

    def test_other_bot_reaction_is_ignored(self, bot_module):
        # Reactions whose member.bot is True (i.e. another bot in a
        # guild reacted) must not be treated as user feedback. Only
        # guild events populate ``payload.member`` — DMs leave it
        # None and fall back to the self-id check.
        bot_module._register_feedback_target(6666, "conv-r", 2)
        handler = self._resolve_handler(bot_module)
        payload = SimpleNamespace(
            user_id=42,
            message_id=6666,
            emoji=SimpleNamespace(name=bot_module._LIKE_EMOJI),
            member=SimpleNamespace(bot=True),
        )

        called = False

        async def fake_submit(*_a, **_kw):
            nonlocal called
            called = True
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert called is False

    def test_human_member_reaction_is_recorded(self, bot_module):
        # Same path as above but with member.bot=False (a real user).
        # Confirms the payload.member check doesn't false-positive on
        # human reactions.
        bot_module._register_feedback_target(7000, "conv-h", 4)
        handler = self._resolve_handler(bot_module)
        payload = SimpleNamespace(
            user_id=99,
            message_id=7000,
            emoji=SimpleNamespace(name=bot_module._DISLIKE_EMOJI),
            member=SimpleNamespace(bot=False),
        )

        captured: dict = {}

        async def fake_submit(conv_id, idx, fb):
            captured["args"] = (conv_id, idx, fb)
            return True

        with patch.object(bot_module, "submit_feedback", fake_submit):
            asyncio.get_event_loop().run_until_complete(handler(payload))

        assert captured["args"] == ("conv-h", 4, "DISLIKE")

    def test_bot_self_reaction_is_ignored(self, bot_module):
        bot_module._register_feedback_target(5555, "conv-q", 0)
        handler = self._resolve_handler(bot_module)
        # `Bot.user` is a property without a setter, so patch the
        # internal `_connection` slot that the property reads from.
        # Simpler: patch the property at the class level for this test.
        fake_user = SimpleNamespace(id=12345)
        with patch.object(
            type(bot_module.bot),
            "user",
            property(lambda self: fake_user),
        ):
            payload = self._make_payload(
                5555,
                bot_module._LIKE_EMOJI,
                user_id=12345,
            )

            called = False

            async def fake_submit(*_a, **_kw):
                nonlocal called
                called = True
                return True

            with patch.object(bot_module, "submit_feedback", fake_submit):
                asyncio.get_event_loop().run_until_complete(handler(payload))

            assert called is False
