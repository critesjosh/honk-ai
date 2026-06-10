"""Unit tests for the bounded per-user conversation cache.

``conversation_histories`` reuses the same lock-aware LRU machinery as
the per-thread cache (``_get_cached_state`` / ``_evict_cache_if_needed``)
— without the bound, every distinct user that ever DM'd or mentioned the
bot stayed resident for the life of the process. Reuses the bootstrap
pattern from ``test_feedback.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

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
def _clear_user_cache(bot_module):
    bot_module.conversation_histories.clear()
    yield
    bot_module.conversation_histories.clear()


def test_state_schema_matches_thread_cache(bot_module):
    state = bot_module._get_user_state(42)
    assert set(state) == {"history", "conversation_id", "answer_count", "lock"}
    assert isinstance(state["lock"], asyncio.Lock)
    # Same entry returned on re-access.
    assert bot_module._get_user_state(42) is state


def test_user_cache_evicts_oldest_when_over_cap(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "_USER_CACHE_MAX_ENTRIES", 3)
    for uid in (1, 2, 3, 4):
        bot_module._get_user_state(uid)
    assert 1 not in bot_module.conversation_histories
    assert set(bot_module.conversation_histories) == {2, 3, 4}


def test_touch_refreshes_lru_position(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "_USER_CACHE_MAX_ENTRIES", 3)
    for uid in (1, 2, 3):
        bot_module._get_user_state(uid)
    bot_module._get_user_state(1)  # touch 1 — now the newest
    bot_module._get_user_state(4)  # evicts 2, not 1
    assert 2 not in bot_module.conversation_histories
    assert set(bot_module.conversation_histories) == {1, 3, 4}


def test_eviction_skips_locked_entry(bot_module, monkeypatch):
    """An in-flight reply (lock held across the long ``generate_answer``
    await) must not have its state popped out from under it — same
    guarantee the per-thread cache provides."""
    monkeypatch.setattr(bot_module, "_USER_CACHE_MAX_ENTRIES", 3)

    async def driver():
        held = bot_module._get_user_state(1)
        async with held["lock"]:
            for uid in range(2, 7):
                bot_module._get_user_state(uid)
            assert 1 in bot_module.conversation_histories, "locked entry was evicted"

    asyncio.run(driver())


def test_fresh_entry_protected_when_all_others_locked(bot_module, monkeypatch):
    """At cap with every OTHER entry's lock held, the just-created entry
    must NOT be the eviction victim (its own lock isn't held yet — the
    caller acquires it after ``_get_cached_state`` returns). The cache
    may transiently exceed the cap instead."""
    monkeypatch.setattr(bot_module, "_USER_CACHE_MAX_ENTRIES", 3)

    async def driver():
        states = [bot_module._get_user_state(uid) for uid in (1, 2, 3)]
        for s in states:
            await s["lock"].acquire()
        try:
            fresh = bot_module._get_user_state(99)
            assert 99 in bot_module.conversation_histories, "fresh entry was evicted"
            assert bot_module.conversation_histories[99] is fresh
            # Cap transiently exceeded rather than orphaning the new state.
            assert len(bot_module.conversation_histories) == 4
        finally:
            for s in states:
                s["lock"].release()

    asyncio.run(driver())


def test_forget_me_style_pop_still_works(bot_module):
    bot_module._get_user_state(7)
    bot_module.conversation_histories.pop(7, None)  # /forget-me idiom
    assert 7 not in bot_module.conversation_histories


def test_reset_style_del_still_works(bot_module):
    bot_module._get_user_state(8)
    if 8 in bot_module.conversation_histories:  # !reset idiom
        del bot_module.conversation_histories[8]
    assert 8 not in bot_module.conversation_histories
