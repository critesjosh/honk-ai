"""Unit tests for the Slack bot's replayed-history trim (WS2).

Mirrors the Discord bot's `_history_for_backend` — caps replayed exchanges
and truncates each turn before /stream so a long thread can't feed the model
a huge self-repetitive context.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bot_module():
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "slack"
    sys.path.insert(0, str(bot_dir))
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test"
    os.environ["API_BASE"] = "http://localhost"
    os.environ["API_KEY"] = "test"
    saved = sys.modules.pop("bot", None)
    try:
        bot_mod = importlib.import_module("bot")
    finally:
        pass
    yield bot_mod
    sys.path.remove(str(bot_dir))
    sys.modules.pop("bot", None)
    if saved is not None:
        sys.modules["bot"] = saved


def test_caps_exchanges_to_max(bot_module):
    n = bot_module.SLACK_HISTORY_MAX_EXCHANGES
    # Runtime shape: completed exchanges followed by the in-flight prompt-only
    # entry that _answer_question appends just before calling the helper.
    hist = [{"prompt": f"q{i}", "response": f"a{i}"} for i in range(n + 5)]
    hist.append({"prompt": "current ask"})
    out = bot_module._history_for_backend(hist)
    # Current prompt survives AND a full N completed exchanges reach the model
    # (the +1 in the slice; the old [-N:] slice carried only N-1 completed).
    assert out[-1] == {"prompt": "current ask"}
    assert len([e for e in out if "response" in e]) == n
    assert out[-2]["prompt"] == f"q{n + 4}"


def test_truncates_long_response(bot_module):
    long = "x" * (bot_module.MAX_HISTORY_RESPONSE_CHARS + 5000)
    out = bot_module._history_for_backend([{"prompt": "q", "response": long}])
    assert "[truncated]" in out[0]["response"]
    assert len(out[0]["response"]) < len(long)


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
    hist = [{"prompt": "q1", "response": "a1"}, {"prompt": "current ask"}]
    out = bot_module._history_for_backend(hist)
    assert out[-1] == {"prompt": "current ask"}


def test_empty_history(bot_module):
    assert bot_module._history_for_backend([]) == []
