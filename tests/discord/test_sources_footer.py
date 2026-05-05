"""Unit tests for the Honk AI Discord bot's citation footer formatter.

The bot lives in extensions/discord/bot.py and depends on `discord.py`
(only listed in extensions/discord/requirements.txt, not the repo-root
requirements). Skip the whole module when discord isn't importable —
matches the pattern in test_thread_context.py.
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

    Even with the `if __name__ == "__main__":` guard around bot.run,
    the import path itself touches the gateway-bound `commands.Bot()`
    constructor and reads several env vars. Bootstrap the same way
    test_thread_context does:

    1. Stub `dotenv.load_dotenv` indirectly via env vars set before
       import (so a stray .env on disk doesn't override).
    2. Monkey-patch `commands.Bot.run` to a no-op for the import,
       belt-and-suspenders against any future regression of the
       __main__ guard.
    3. Add `extensions/discord` to sys.path so `bot` resolves.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "discord"
    sys.path.insert(0, str(bot_dir))

    os.environ.setdefault("DISCORD_TOKEN", "test")
    os.environ.setdefault("API_BASE", "http://localhost")
    os.environ.setdefault("API_KEY", "test")

    from discord.ext import commands  # noqa: WPS433 — needed at import time

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


class TestFormatSourcesFooter:
    def test_returns_none_when_empty(self, bot_module):
        assert bot_module._format_sources_footer([]) is None
        assert bot_module._format_sources_footer(None) is None

    def test_returns_none_when_no_usable_urls(self, bot_module):
        # Sources with missing or non-http URLs are filtered out; if
        # nothing usable remains we return None so the caller can
        # skip sending an empty footer message.
        assert (
            bot_module._format_sources_footer(
                [
                    {"title": "no url here"},
                    {"source": ""},
                    {"source": "ftp://nope"},
                ]
            )
            is None
        )

    def test_renders_subtext_block(self, bot_module):
        sources = [
            {"source": "https://docs.aztec.network/developers/docs/aztec-nr/api"},
            {"source": "https://noir-lang.org/docs/getting_started/quick_start"},
        ]
        out = bot_module._format_sources_footer(sources)
        assert out is not None
        # Discord subtext marker
        lines = out.split("\n")
        assert lines[0] == "-# **Sources**"
        # Numbered list, URLs wrapped in <> to suppress link previews
        assert lines[1] == "-# 1. <https://docs.aztec.network/developers/docs/aztec-nr/api>"
        assert lines[2] == "-# 2. <https://noir-lang.org/docs/getting_started/quick_start>"

    def test_caps_at_limit(self, bot_module):
        sources = [{"source": f"https://docs.aztec.network/page-{i}"} for i in range(20)]
        out = bot_module._format_sources_footer(sources)
        assert out is not None
        # 1 header line + N source lines
        assert out.count("\n") == bot_module._DISCORD_FOOTER_SOURCE_LIMIT
        # Last source rendered is the Nth, in stable order.
        assert (
            f"-# {bot_module._DISCORD_FOOTER_SOURCE_LIMIT}. "
            f"<https://docs.aztec.network/page-{bot_module._DISCORD_FOOTER_SOURCE_LIMIT - 1}>" in out
        )

    def test_skips_malformed_entries(self, bot_module):
        sources = [
            "not a dict",
            {"source": None},
            {"source": "https://docs.aztec.network/ok"},
            {},
        ]
        out = bot_module._format_sources_footer(sources)
        assert out is not None
        # Only the one valid URL renders.
        assert out == "-# **Sources**\n-# 1. <https://docs.aztec.network/ok>"

    def test_preserves_order(self, bot_module):
        urls = [
            "https://docs.aztec.network/developers/overview",
            "https://docs.aztec.network/operate/operators",
            "https://noir-lang.org/docs/noir/concepts/data_types",
        ]
        out = bot_module._format_sources_footer([{"source": u} for u in urls])
        assert out is not None
        for i, u in enumerate(urls, start=1):
            assert f"-# {i}. <{u}>" in out
