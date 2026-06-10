"""Unit tests for /stream in-band error handling and the on_message catch-all.

Backend contract (application/api/answer/routes/base.py): on an in-band
error, ``/stream`` flushes buffered text, emits ``{"type": "error"}``,
and returns WITHOUT ``id``/``end`` and WITHOUT writing a
conversation_messages row. The bot must therefore NOT advance
``answer_count`` or register feedback targets for such a turn —
otherwise every subsequent 👍/👎 in the conversation writes feedback
against the wrong DB row.

Reuses the bootstrap pattern from ``test_feedback.py``; the on_message
tests stub the gateway surface (``bot.user`` via the connection state,
``bot.get_context``) so the handler can be driven directly.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")

_BOT_USER_ID = 999


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
def _clean_state(bot_module):
    bot_module.conversation_histories.clear()
    bot_module.feedback_targets.clear()
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()
    bot_module._BREAKER_NOTIFIED.clear()
    yield
    bot_module.conversation_histories.clear()
    bot_module.feedback_targets.clear()
    bot_module._SHARED_429_TRIPPED_GUILDS.clear()
    bot_module._BREAKER_NOTIFIED.clear()


# --- generate_answer: SSE error-frame parsing -------------------------------


class _FakeStreamContent:
    """Async-iterates canned SSE byte lines like ``resp.content``."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStreamResponse:
    def __init__(self, status, lines):
        self.status = status
        self.content = _FakeStreamContent(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return self._response


def _patch_stream(bot_module, monkeypatch, lines, status=200):
    response = _FakeStreamResponse(status, lines)
    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(response))


class TestGenerateAnswerErrorFrame:
    def test_error_frame_stops_stream_and_surfaces_error(self, bot_module, monkeypatch):
        lines = [
            b'data: {"type": "answer", "answer": "partial"}\n',
            b'data: {"type": "error", "error": "LLM exploded"}\n',
            b'data: {"type": "answer", "answer": "never read"}\n',
        ]
        _patch_stream(bot_module, monkeypatch, lines)
        result = asyncio.run(bot_module.generate_answer("q", [], "conv-prior"))
        assert result["error"] == "LLM exploded"
        assert result["http_status"] == 200
        assert "never read" not in result["answer"]  # reading stopped at the error frame
        # No id frame on this path — conversation_id stays the prior turn's.
        assert result["conversation_id"] == "conv-prior"

    def test_success_path_has_no_error(self, bot_module, monkeypatch):
        lines = [
            b'data: {"type": "answer", "answer": "hello"}\n',
            b'data: {"type": "id", "id": "conv-1"}\n',
        ]
        _patch_stream(bot_module, monkeypatch, lines)
        result = asyncio.run(bot_module.generate_answer("q", [], None))
        assert result["error"] is None
        assert result["conversation_id"] == "conv-1"
        assert result["answer"] == "hello"


# --- on_message: caller behaviour --------------------------------------------


def _stub_gateway(bot_module, monkeypatch):
    """Make on_message drivable without a gateway login: stub the bot
    user (split_string reads ``bot.user.id``) and short-circuit the
    prefix-command path."""
    monkeypatch.setattr(bot_module.bot._connection, "user", SimpleNamespace(id=_BOT_USER_ID), raising=False)

    async def fake_get_context(message):
        return SimpleNamespace(valid=False)

    monkeypatch.setattr(bot_module.bot, "get_context", fake_get_context, raising=False)
    monkeypatch.setattr(bot_module, "_GUILD_USD_CAPS", {})


def _make_message(channel, *, author_id=1, guild_id=777):
    return SimpleNamespace(
        id=12345,
        author=SimpleNamespace(id=author_id, bot=False, display_name="alice"),
        content=f"<@{_BOT_USER_ID}> what is noir?",
        guild=SimpleNamespace(id=guild_id),
        channel=channel,
        reference=None,
        type=None,
    )


def _make_channel():
    return SimpleNamespace(
        id=555,
        send=AsyncMock(return_value=SimpleNamespace(id=42)),
        typing=AsyncMock(),
    )


def _stream_result(**overrides):
    result = {
        "answer": "the answer",
        "conversation_id": "conv-1",
        "sources": [],
        "usage": None,
        "http_status": 200,
        "error": None,
    }
    result.update(overrides)
    return result


class TestOnMessageErrorFrame:
    def test_error_frame_rolls_back_like_non_200(self, bot_module, monkeypatch):
        _stub_gateway(bot_module, monkeypatch)

        async def fake_generate(question, messages, conversation_id):
            return _stream_result(answer="partial", conversation_id=conversation_id, error="boom")

        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        channel = _make_channel()
        asyncio.run(bot_module.on_message(_make_message(channel)))

        state = bot_module.conversation_histories[1]
        assert state["history"] == []  # phantom prompt rolled back
        assert state["answer_count"] == 0  # no feedback position consumed
        assert state["conversation_id"] is None
        assert bot_module.feedback_targets == {}  # no affordance registered
        channel.send.assert_awaited_once()
        assert "answering" in channel.send.await_args.args[0]

    def test_error_frame_on_follow_up_turn_rolls_back(self, bot_module, monkeypatch):
        """First-turn coverage masks the bug: there conversation_id is
        still None, so the pre-existing non-200 fallback would roll back
        anyway. On a follow-up turn the state carries a real
        conversation_id (no ``id`` frame is emitted on the error path, so
        it keeps the prior turn's), and ONLY the error-frame branch
        protects answer_count / feedback alignment."""
        _stub_gateway(bot_module, monkeypatch)
        seeded = bot_module._get_user_state(1)
        seeded["conversation_id"] = "conv-REAL"
        seeded["answer_count"] = 1
        seeded["history"].append({"prompt": "first q", "response": "first a"})

        async def fake_generate(question, messages, conversation_id):
            assert conversation_id == "conv-REAL"
            return _stream_result(answer="partial", conversation_id=conversation_id, error="boom")

        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        channel = _make_channel()
        asyncio.run(bot_module.on_message(_make_message(channel)))

        state = bot_module.conversation_histories[1]
        assert state["answer_count"] == 1  # NOT incremented — backend wrote no row
        assert state["conversation_id"] == "conv-REAL"
        assert len(state["history"]) == 1  # phantom prompt rolled back
        assert state["history"][0] == {"prompt": "first q", "response": "first a"}
        assert bot_module.feedback_targets == {}  # no affordance registered
        channel.send.assert_awaited_once()  # canned error only
        assert "answering" in channel.send.await_args.args[0]

    def test_success_path_still_registers_feedback(self, bot_module, monkeypatch):
        """Sanity guard: the new error branch must not break the
        success path's eager position bookkeeping."""
        _stub_gateway(bot_module, monkeypatch)

        async def fake_generate(question, messages, conversation_id):
            return _stream_result()

        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        channel = _make_channel()
        asyncio.run(bot_module.on_message(_make_message(channel)))

        state = bot_module.conversation_histories[1]
        assert state["answer_count"] == 1
        assert state["conversation_id"] == "conv-1"
        assert state["history"][-1]["response"] == "the answer"
        assert bot_module.feedback_targets[42] == ("conv-1", 0)


class TestOnMessageCatchAll:
    def test_unexpected_exception_rolls_back_and_notifies(self, bot_module, monkeypatch):
        """Anything past the handled timeout / discord-write failures
        used to leave the user with typing-then-silence and a phantom
        prompt queued in history."""
        _stub_gateway(bot_module, monkeypatch)

        async def fake_generate(question, messages, conversation_id):
            raise RuntimeError("kaput")

        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        channel = _make_channel()
        # Must not propagate out of the handler.
        asyncio.run(bot_module.on_message(_make_message(channel)))

        state = bot_module.conversation_histories[1]
        assert state["history"] == []  # guarded pop ran
        assert state["answer_count"] == 0
        channel.send.assert_awaited_once()
        assert "handling" in channel.send.await_args.args[0]

    def test_notice_send_failure_is_swallowed(self, bot_module, monkeypatch):
        _stub_gateway(bot_module, monkeypatch)

        async def fake_generate(question, messages, conversation_id):
            raise RuntimeError("kaput")

        async def failing_send(*_a, **_kw):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                {"code": 50013, "message": "Missing Permissions"},
            )

        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        channel = _make_channel()
        channel.send = failing_send
        # Neither the RuntimeError nor the Forbidden may escape.
        asyncio.run(bot_module.on_message(_make_message(channel)))
        assert bot_module.conversation_histories[1]["history"] == []
