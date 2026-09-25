"""Unit tests for the Discord bot's replayed-history trim (WS2).

A long casual thread that feeds the model many of its own verbose prior
answers drives qwen into repetition loops + speculative hallucination
(2026-06-29 honk-report: a 22-turn bridging thread). `_history_for_backend`
caps the replayed exchanges and truncates each turn before /stream.
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


def test_caps_exchanges_to_max(bot_module):
    n = bot_module.DISCORD_HISTORY_MAX_EXCHANGES
    # Runtime shape: completed exchanges followed by the in-flight prompt-only
    # entry that on_message appends just before calling the helper.
    hist = [{"prompt": f"q{i}", "response": f"a{i}"} for i in range(n + 5)]
    hist.append({"prompt": "current ask"})
    out = bot_module._history_for_backend(hist)
    # The current prompt survives AND a full N completed exchanges reach the
    # model — the +1 in the slice keeps the trailing prompt without costing a
    # completed exchange (the old [-N:] slice carried only N-1 completed).
    assert out[-1] == {"prompt": "current ask"}
    assert len([e for e in out if "response" in e]) == n
    # Keeps the most recent completed exchange, drops the oldest.
    assert out[-2]["prompt"] == f"q{n + 4}"
    assert all(e.get("prompt") != "q0" for e in out)


def test_truncates_long_response(bot_module):
    long = "x" * (bot_module.MAX_HISTORY_RESPONSE_CHARS + 5000)
    out = bot_module._history_for_backend([{"prompt": "q", "response": long}])
    assert len(out[0]["response"]) < len(long)
    assert "[truncated]" in out[0]["response"]


def test_truncates_long_prompt(bot_module):
    long = "y" * (bot_module.MAX_HISTORY_PROMPT_CHARS + 5000)
    out = bot_module._history_for_backend([{"prompt": long, "response": "a"}])
    assert "[truncated]" in out[0]["prompt"]


def test_short_entries_unchanged(bot_module):
    hist = [{"prompt": "hi", "response": "hello"}]
    assert bot_module._history_for_backend(hist) == [{"prompt": "hi", "response": "hello"}]


def test_does_not_mutate_input(bot_module):
    long = "x" * (bot_module.MAX_HISTORY_RESPONSE_CHARS + 5000)
    hist = [{"prompt": "q", "response": long}]
    bot_module._history_for_backend(hist)
    assert hist[0]["response"] == long


def test_current_prompt_only_entry_preserved(bot_module):
    # The in-flight turn appends {"prompt": ...} with no response yet.
    hist = [{"prompt": "q1", "response": "a1"}, {"prompt": "current ask"}]
    out = bot_module._history_for_backend(hist)
    assert out[-1] == {"prompt": "current ask"}
    assert "response" not in out[-1]


def test_empty_history(bot_module):
    assert bot_module._history_for_backend([]) == []
