"""Unit tests for ``format_for_discord``'s code-fence guard.

The header-to-bold conversion must not mangle ``# comment`` lines inside
``` fences (same ``in_fence`` toggle as the Slack bot's
``format_for_slack``). Reuses the bootstrap pattern from
``test_feedback.py``.
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


class TestFormatForDiscord:
    def test_headers_become_bold(self, bot_module):
        assert bot_module.format_for_discord("# Title") == "**Title**"
        assert bot_module.format_for_discord("### Deep") == "**Deep**"

    def test_non_header_lines_untouched(self, bot_module):
        src = "plain text\n#not-a-header (no space)\n#### four hashes"
        assert bot_module.format_for_discord(src) == src

    def test_hash_comment_inside_fence_untouched(self, bot_module):
        src = "intro\n```bash\n# not a header\necho hi\n```\ntail"
        out = bot_module.format_for_discord(src)
        assert "# not a header" in out
        assert "**not a header**" not in out
        assert out.count("```") == 2

    def test_header_after_fence_close_still_bolded(self, bot_module):
        src = "```\n# inside\n```\n# Outside"
        out = bot_module.format_for_discord(src)
        assert "# inside" in out
        assert "**Outside**" in out

    def test_indented_fence_lines_toggle(self, bot_module):
        # The fence detector matches lstripped lines, mirroring
        # format_for_slack — indented fences still open/close.
        src = "  ```python\n# comment\n  ```\n# Header"
        out = bot_module.format_for_discord(src)
        assert "# comment" in out
        assert "**Header**" in out

    def test_hash_comment_inside_tilde_fence_untouched(self, bot_module):
        src = "intro\n~~~bash\n# not a header\necho hi\n~~~\n# Outside"
        out = bot_module.format_for_discord(src)
        assert "# not a header" in out
        assert "**not a header**" not in out
        assert "**Outside**" in out

    def test_backtick_line_does_not_close_tilde_fence(self, bot_module):
        # Only the marker that opened the fence may close it.
        src = "~~~\n```\n# still fenced\n~~~\n# Outside"
        out = bot_module.format_for_discord(src)
        assert "# still fenced" in out
        assert "**still fenced**" not in out
        assert "**Outside**" in out
