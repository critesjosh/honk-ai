"""Unit tests for the Honk AI Discord bot's reply-to-bot trigger helper.

Covers the guild-channel rule that a message replying inline to one of
the bot's previous messages should trigger a response — no @-mention
required.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

discord = pytest.importorskip("discord")


@pytest.fixture(scope="module")
def bot_module():
    """Import extensions/discord/bot.py without actually starting the bot.

    Mirrors the bootstrap used by tests/discord/test_thread_context.py
    so this file can run standalone via ``pytest tests/discord``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "discord"
    sys.path.insert(0, str(bot_dir))

    os.environ.setdefault("DISCORD_TOKEN", "test")
    os.environ.setdefault("API_BASE", "http://localhost")
    os.environ.setdefault("API_KEY", "test")

    from discord.ext import commands

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


BOT_USER_ID = 9_876_543_210
OTHER_USER_ID = 1_234_567_890


def _stub_message(reference, msg_type=None):
    """Construct a minimal stand-in for ``discord.Message`` with a reference attr.

    ``msg_type`` defaults to ``discord.MessageType.reply`` so the
    helper's discord.py <2.5 fallback (which gates on
    ``message.type``) lets the reference through. Tests targeting the
    pin-add / crosspost paths override it.
    """

    class _StubMessage:
        pass

    msg = _StubMessage()
    msg.reference = reference
    msg.type = msg_type if msg_type is not None else discord.MessageType.reply
    return msg


def _stub_resolved_message(author_id):
    """Fake a ``discord.Message`` instance authored by ``author_id``.

    ``isinstance(resolved, discord.Message)`` is the gate in the
    helper, so subclass ``discord.Message`` via ``__new__`` to get the
    type without invoking its real constructor.
    """
    fake = discord.Message.__new__(discord.Message)

    class _Author:
        pass

    author = _Author()
    author.id = author_id
    fake.author = author
    return fake


def _make_reference(*, message_id=42, resolved=None, ref_type=None):
    """Construct a stand-in for ``discord.MessageReference``."""

    class _Ref:
        pass

    ref = _Ref()
    ref.message_id = message_id
    ref.resolved = resolved
    ref.type = ref_type
    return ref


def test_no_reference_returns_false(bot_module):
    assert bot_module._is_reply_to_bot(_stub_message(None), BOT_USER_ID) is False


def test_reference_without_message_id_returns_false(bot_module):
    ref = _make_reference(message_id=None)
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is False


def test_reply_to_bot_returns_true(bot_module):
    reply_type = getattr(discord, "MessageReferenceType", None)
    ref = _make_reference(
        resolved=_stub_resolved_message(BOT_USER_ID),
        ref_type=(reply_type.reply if reply_type is not None else None),
    )
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is True


def test_reply_to_other_user_returns_false(bot_module):
    reply_type = getattr(discord, "MessageReferenceType", None)
    ref = _make_reference(
        resolved=_stub_resolved_message(OTHER_USER_ID),
        ref_type=(reply_type.reply if reply_type is not None else None),
    )
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is False


def test_uncached_reference_returns_false(bot_module):
    """``resolved=None`` means the gateway didn't provide the parent — skip the trigger.

    The bot can still be invoked via @-mention; this just avoids
    unconditional ``channel.fetch_message`` rate-limit pressure for
    cache misses.
    """
    reply_type = getattr(discord, "MessageReferenceType", None)
    ref = _make_reference(
        resolved=None,
        ref_type=(reply_type.reply if reply_type is not None else None),
    )
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is False


def test_deleted_referenced_message_returns_false(bot_module):
    """A reply targeting a deleted message resolves to ``DeletedReferencedMessage``,
    which is not a ``discord.Message`` and must not trigger.
    """
    reply_type = getattr(discord, "MessageReferenceType", None)
    deleted = discord.DeletedReferencedMessage.__new__(discord.DeletedReferencedMessage)
    ref = _make_reference(
        resolved=deleted,
        ref_type=(reply_type.reply if reply_type is not None else None),
    )
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is False


def test_forward_reference_returns_false(bot_module):
    """A forward (non-reply ``MessageReferenceType``) must not trigger,
    even if it resolves to a bot-authored message.
    """
    reply_type = getattr(discord, "MessageReferenceType", None)
    if reply_type is None or not hasattr(reply_type, "forward"):
        pytest.skip("discord.py version lacks MessageReferenceType.forward")
    ref = _make_reference(
        resolved=_stub_resolved_message(BOT_USER_ID),
        ref_type=reply_type.forward,
    )
    assert bot_module._is_reply_to_bot(_stub_message(ref), BOT_USER_ID) is False


def test_pin_add_notice_returns_false_without_reference_type(bot_module, monkeypatch):
    """On discord.py <2.5 (no MessageReferenceType) the helper must
    still reject pin-add system notices, which carry a
    MessageReference but are NOT inline replies — they have empty
    content and would otherwise spam /stream.

    Simulates the prod 2.4.0 path by stripping the enum.
    """
    monkeypatch.delattr(discord, "MessageReferenceType", raising=False)
    ref = _make_reference(
        resolved=_stub_resolved_message(BOT_USER_ID),
        ref_type=None,
    )
    msg = _stub_message(ref, msg_type=discord.MessageType.pins_add)
    assert bot_module._is_reply_to_bot(msg, BOT_USER_ID) is False


def test_reply_returns_true_without_reference_type(bot_module, monkeypatch):
    """Mirror of the previous test: on discord.py <2.5 a real inline
    reply (parent ``MessageType.reply``) must still trigger.
    """
    monkeypatch.delattr(discord, "MessageReferenceType", raising=False)
    ref = _make_reference(
        resolved=_stub_resolved_message(BOT_USER_ID),
        ref_type=None,
    )
    msg = _stub_message(ref, msg_type=discord.MessageType.reply)
    assert bot_module._is_reply_to_bot(msg, BOT_USER_ID) is True


def test_pin_add_with_default_reference_type_returns_false(bot_module):
    """Critical regression: on discord.py >= 2.5 ``MessageReferenceType.reply``
    is an ALIAS for ``.default``, and ``.default`` covers pin-add /
    crosspost / channel-follow / thread-created references — they all
    pass a ``ref_type == MessageReferenceType.reply`` check. The
    primary gate is the parent ``Message.type``, which is distinct per
    system-message kind. This simulates the 2.5 shape: a pin-add
    notice whose reference type ``==`` ``MessageReferenceType.reply``
    must still be rejected because ``message.type`` is ``pins_add``.
    """
    reply_enum = getattr(discord, "MessageReferenceType", None)
    # Inject the enum on the discord module if the local version
    # doesn't expose it, so the test exercises the 2.5+ code path
    # regardless of the discord.py version pytest happens to find.

    class _RefEnum:
        reply = "reply-or-default"  # alias of default per discord.py 2.5+
        forward = "forward"

    if reply_enum is None:
        # Local discord lacks the enum; injected attribute lets the
        # helper take the 2.5 branch without monkeypatching all callers.
        discord.MessageReferenceType = _RefEnum  # type: ignore[attr-defined]
        try:
            ref = _make_reference(
                resolved=_stub_resolved_message(BOT_USER_ID),
                ref_type=_RefEnum.reply,
            )
            msg = _stub_message(ref, msg_type=discord.MessageType.pins_add)
            assert bot_module._is_reply_to_bot(msg, BOT_USER_ID) is False
        finally:
            del discord.MessageReferenceType
    else:
        ref = _make_reference(
            resolved=_stub_resolved_message(BOT_USER_ID),
            ref_type=reply_enum.reply,
        )
        msg = _stub_message(ref, msg_type=discord.MessageType.pins_add)
        assert bot_module._is_reply_to_bot(msg, BOT_USER_ID) is False
