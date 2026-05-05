"""Tests for the Honk AI Discord bot's citation footer formatter."""

from __future__ import annotations

import os

# The module reads several env vars at import time and contacts Discord
# only inside `bot.run(...)`. Stub the required ones so the import
# succeeds on a clean CI runner.
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("API_KEY", "test")

from extensions.discord.bot import (  # noqa: E402  — env vars must be set first
    _DISCORD_FOOTER_SOURCE_LIMIT,
    _format_sources_footer,
)


class TestFormatSourcesFooter:
    def test_returns_none_when_empty(self):
        assert _format_sources_footer([]) is None
        assert _format_sources_footer(None) is None  # type: ignore[arg-type]

    def test_returns_none_when_no_usable_urls(self):
        # Sources with missing or non-http URLs are filtered out; if
        # nothing usable remains we return None so the caller can
        # skip sending an empty footer message.
        assert (
            _format_sources_footer(
                [
                    {"title": "no url here"},
                    {"source": ""},
                    {"source": "ftp://nope"},
                ]
            )
            is None
        )

    def test_renders_subtext_block(self):
        sources = [
            {"source": "https://docs.aztec.network/developers/docs/aztec-nr/api"},
            {"source": "https://noir-lang.org/docs/getting_started/quick_start"},
        ]
        out = _format_sources_footer(sources)
        assert out is not None
        # Discord subtext marker
        lines = out.split("\n")
        assert lines[0] == "-# **Sources**"
        # Numbered list, URLs wrapped in <> to suppress link previews
        assert lines[1] == "-# 1. <https://docs.aztec.network/developers/docs/aztec-nr/api>"
        assert lines[2] == "-# 2. <https://noir-lang.org/docs/getting_started/quick_start>"

    def test_caps_at_limit(self):
        sources = [{"source": f"https://docs.aztec.network/page-{i}"} for i in range(20)]
        out = _format_sources_footer(sources)
        assert out is not None
        # 1 header line + N source lines
        assert out.count("\n") == _DISCORD_FOOTER_SOURCE_LIMIT
        # Last source rendered is the Nth, in stable order.
        assert (
            f"-# {_DISCORD_FOOTER_SOURCE_LIMIT}. "
            f"<https://docs.aztec.network/page-{_DISCORD_FOOTER_SOURCE_LIMIT - 1}>" in out
        )

    def test_skips_malformed_entries(self):
        sources = [
            "not a dict",  # type: ignore[list-item]
            {"source": None},
            {"source": "https://docs.aztec.network/ok"},
            {},
        ]
        out = _format_sources_footer(sources)
        assert out is not None
        # Only the one valid URL renders.
        assert out == "-# **Sources**\n-# 1. <https://docs.aztec.network/ok>"

    def test_preserves_order(self):
        urls = [
            "https://docs.aztec.network/developers/overview",
            "https://docs.aztec.network/operate/operators",
            "https://noir-lang.org/docs/noir/concepts/data_types",
        ]
        out = _format_sources_footer([{"source": u} for u in urls])
        assert out is not None
        for i, u in enumerate(urls, start=1):
            assert f"-# {i}. <{u}>" in out
