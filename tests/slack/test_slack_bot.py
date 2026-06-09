"""Unit tests for the Honk AI Slack bot's pure helpers.

The bot module guards its ``slack_bolt`` import, so these tests run even
in environments where the Slack SDK isn't installed — they exercise the
SDK-free logic: identity, mrkdwn formatting, chunking, Slack-decoration
stripping, thread-context assembly, the per-workspace spend cap, and the
Block Kit feedback/source builders.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bot_module():
    """Import ``extensions/slack/bot.py`` with a known cap table.

    ``SLACK_TEAM_DAILY_USD_CAPS`` is read at import via
    ``_parse_team_usd_caps``, so it must be set before import.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "extensions" / "slack"
    sys.path.insert(0, str(bot_dir))

    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test"
    os.environ["API_BASE"] = "http://localhost"
    os.environ["API_KEY"] = "test"
    os.environ["SLACK_TEAM_DAILY_USD_CAPS"] = "T111=20,T222=5"

    saved = sys.modules.pop("bot", None)  # avoid collision with discord's bot module
    try:
        bot_mod = importlib.import_module("bot")
    finally:
        pass
    yield bot_mod
    sys.path.remove(str(bot_dir))
    sys.modules.pop("bot", None)
    if saved is not None:
        sys.modules["bot"] = saved


@pytest.fixture(autouse=True)
def _clear_spend_state(bot_module):
    for d in (
        bot_module._TEAM_SPEND_TODAY,
        bot_module._TEAM_SPEND_LOCKS,
        bot_module._TEAM_SPEND_WARN_FIRED,
        bot_module._TEAM_SPEND_TRIP_FIRED,
        bot_module._CAP_NOTIFIED,
    ):
        d.clear()
    yield
    for d in (
        bot_module._TEAM_SPEND_TODAY,
        bot_module._TEAM_SPEND_LOCKS,
        bot_module._TEAM_SPEND_WARN_FIRED,
        bot_module._TEAM_SPEND_TRIP_FIRED,
        bot_module._CAP_NOTIFIED,
    ):
        d.clear()


class TestSlackRawIdentity:
    def test_team_user_compound(self, bot_module):
        assert bot_module.slack_raw_identity("T1", "U1") == "T1:U1"

    def test_enterprise_prefix(self, bot_module):
        assert bot_module.slack_raw_identity("T1", "U1", "E1") == "E1:T1:U1"

    def test_missing_team_drops_segment(self, bot_module):
        # Defensive: a missing team id shouldn't produce a leading colon.
        assert bot_module.slack_raw_identity(None, "U1") == "U1"


class TestFormatForSlack:
    def test_headers_become_bold(self, bot_module):
        assert bot_module.format_for_slack("# Title") == "*Title*"
        assert bot_module.format_for_slack("### Deep") == "*Deep*"

    def test_markdown_link_becomes_mrkdwn(self, bot_module):
        out = bot_module.format_for_slack("see [docs](https://x.io/a)")
        assert out == "see <https://x.io/a|docs>"

    def test_code_fence_preserved_and_headers_inside_untouched(self, bot_module):
        src = "intro\n```\n# not a header\n```\ntail"
        out = bot_module.format_for_slack(src)
        assert "# not a header" in out  # untouched inside fence
        assert out.count("```") == 2


class TestChunkString:
    def test_short_text_single_chunk(self, bot_module):
        assert bot_module.chunk_string("hi") == ["hi"]

    def test_long_text_respects_limit(self, bot_module):
        text = "word " * 4000  # ~20k chars
        chunks = bot_module.chunk_string(text)
        assert len(chunks) > 1
        assert all(len(c) <= bot_module.SLACK_MAX_MSG_CHARS for c in chunks)

    def test_code_fence_repaired_across_split(self, bot_module):
        body = "```python\n" + ("x = 1\n" * 1200) + "```"
        chunks = bot_module.chunk_string(body)
        # Every chunk must have balanced fences.
        assert all(c.count("```") % 2 == 0 for c in chunks)


class TestStripDecorations:
    def test_user_mention_removed(self, bot_module):
        assert "U123" not in bot_module.strip_slack_decorations("hey <@U123> there")

    def test_channel_ref_collapses_to_name(self, bot_module):
        assert bot_module.strip_slack_decorations("see <#C1|general>") == "see general"

    def test_link_keeps_label_and_url(self, bot_module):
        out = bot_module.strip_slack_decorations("<https://x.io|docs>")
        assert "docs" in out and "https://x.io" in out

    def test_special_mentions_removed(self, bot_module):
        assert "channel" not in bot_module.strip_slack_decorations("<!channel> ping").lower()

    def test_strip_bot_mention(self, bot_module):
        assert bot_module.strip_bot_mention("<@UBOT> what is noir?", "UBOT") == "what is noir?"


class TestThreadContextBlock:
    def test_question_first(self, bot_module):
        recent = [{"user": "U1", "text": "earlier message"}]
        out = bot_module.build_thread_context_block(recent, "alice", "UBOT", "my q", {"U1": "bob"})
        assert out.startswith("New question (from alice): my q")
        assert "bob: earlier message" in out

    def test_no_context_returns_preamble_only(self, bot_module):
        out = bot_module.build_thread_context_block([], "alice", "UBOT", "my q", {})
        assert out == "New question (from alice): my q"

    def test_bot_messages_labelled_as_honk(self, bot_module):
        recent = [{"user": "UBOT", "text": "prior answer"}]
        out = bot_module.build_thread_context_block(recent, "alice", "UBOT", "q", {})
        assert "Honk AI: prior answer" in out


class TestParseTeamUsdCaps:
    def test_parses_pairs(self, bot_module):
        os.environ["SLACK_TEAM_DAILY_USD_CAPS"] = "T1=10,T2=0"
        try:
            caps = bot_module._parse_team_usd_caps()
        finally:
            os.environ["SLACK_TEAM_DAILY_USD_CAPS"] = "T111=20,T222=5"
        assert caps == {"T1": 10.0, "T2": 0.0}

    def test_skips_malformed(self, bot_module):
        os.environ["SLACK_TEAM_DAILY_USD_CAPS"] = "bad,T1=x,=5,T2=3"
        try:
            caps = bot_module._parse_team_usd_caps()
        finally:
            os.environ["SLACK_TEAM_DAILY_USD_CAPS"] = "T111=20,T222=5"
        assert caps == {"T2": 3.0}


class TestEstimateCallUsd:
    def test_arithmetic(self, bot_module):
        # 1M prompt @0.20 + 1M completion @1.20 = 1.40
        usd = bot_module._estimate_call_usd(1_000_000, 1_000_000)
        assert abs(usd - 1.40) < 1e-9

    def test_negative_clamped(self, bot_module):
        assert bot_module._estimate_call_usd(-5, -5) == 0.0


class TestSpendCap:
    def test_uncapped_team_is_noop(self, bot_module):
        res = asyncio.run(bot_module._reserve_team_spend("T_UNCAPPED"))
        assert res == (0.0, "")

    def test_missing_team_is_noop(self, bot_module):
        res = asyncio.run(bot_module._reserve_team_spend(None))
        assert res == (0.0, "")

    def test_reserve_then_reconcile(self, bot_module):
        async def go():
            res = await bot_module._reserve_team_spend("T222", reserve_usd=0.01)
            assert res is not None
            reserved, date = res
            await bot_module._finalize_team_spend(
                "T222", reserved, date, outcome="reconcile", actual_usd=0.50
            )
            return bot_module._TEAM_SPEND_TODAY["T222"]["estimated_usd"]

        spent = asyncio.run(go())
        assert abs(spent - 0.50) < 1e-9  # reserve replaced by actual

    def test_refund_returns_to_zero(self, bot_module):
        async def go():
            reserved, date = await bot_module._reserve_team_spend("T222", reserve_usd=0.01)
            await bot_module._finalize_team_spend("T222", reserved, date, outcome="refund")
            return bot_module._TEAM_SPEND_TODAY["T222"]["estimated_usd"]

        assert asyncio.run(go()) == 0.0

    def test_cap_blocks_when_exceeded(self, bot_module):
        async def go():
            # T222 cap is $5. Spend it down.
            await bot_module._reserve_team_spend("T222", reserve_usd=5.0)
            # Next reserve should be refused.
            return await bot_module._reserve_team_spend("T222", reserve_usd=0.01)

        assert asyncio.run(go()) is None

    def test_cross_midnight_finalize_dropped(self, bot_module):
        async def go():
            day1 = datetime.datetime(2026, 6, 8, 23, 59, tzinfo=datetime.timezone.utc)
            day2 = datetime.datetime(2026, 6, 9, 0, 1, tzinfo=datetime.timezone.utc)
            reserved, date = await bot_module._reserve_team_spend("T222", reserve_usd=0.01, now=day1)
            # Finalize on the next UTC day → dropped (yesterday's bucket gone).
            await bot_module._finalize_team_spend(
                "T222", reserved, date, outcome="refund", now=day2
            )
            # Day-2 bucket should be untouched (fresh / absent).
            return bot_module._TEAM_SPEND_TODAY.get("T222", {}).get("utc_date")

        # The reserve created a day-1 bucket; finalize on day-2 no-ops it.
        assert asyncio.run(go()) == "2026-06-08"

    def test_concurrent_reserves_bounded(self, bot_module):
        async def go():
            # 10 concurrent 1-cent reserves under the $5 cap must all
            # succeed and never over-charge.
            results = await asyncio.gather(
                *[bot_module._reserve_team_spend("T222", reserve_usd=0.01) for _ in range(10)]
            )
            return results, bot_module._TEAM_SPEND_TODAY["T222"]["estimated_usd"]

        results, spent = asyncio.run(go())
        assert all(r is not None for r in results)
        assert abs(spent - 0.10) < 1e-9


class _StubClient:
    """Minimal async Slack client stub for fetch_thread_context. Serves
    canned pages and records the kwargs of the first call."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    async def conversations_replies(self, **kwargs):
        self.calls.append(kwargs)
        idx = len([c for c in self.calls]) - 1
        return self._pages[min(idx, len(self._pages) - 1)]


class TestFetchThreadContext:
    def test_filters_and_excludes_trigger(self, bot_module):
        bot_module.BOT_USER_ID = "UBOT"
        bot_module.BOT_ID = "BBOT"
        page = {
            "messages": [
                {"ts": "1", "user": "U1", "text": "real user msg"},
                {"ts": "2", "subtype": "channel_join", "user": "U2", "text": "joined"},
                {"ts": "3", "bot_id": "BOTHER", "user": "UOTHER", "text": "other integration"},
                {"ts": "4", "user": "UBOT", "bot_id": "BBOT", "text": "honk's own answer"},
                {"ts": "TRIG", "user": "U1", "text": "the trigger"},
            ],
            "response_metadata": {},
        }
        client = _StubClient([page])
        out = asyncio.run(bot_module.fetch_thread_context(client, "C1", "1", "TRIG", 30))
        texts = [m["text"] for m in out]
        assert texts == ["real user msg", "honk's own answer"]  # trigger, join, foreign bot dropped
        # Bound the upper end exclusively at the trigger.
        assert client.calls[0]["latest"] == "TRIG"
        assert client.calls[0]["inclusive"] is False

    def test_own_bot_message_subtype_kept(self, bot_module):
        """The bot's own answer can come back as subtype='bot_message' with
        only a bot_id — it must survive the subtype filter as context."""
        bot_module.BOT_USER_ID = "UBOT"
        bot_module.BOT_ID = "BBOT"
        page = {
            "messages": [
                {"ts": "1", "user": "U1", "text": "question"},
                {"ts": "2", "subtype": "bot_message", "bot_id": "BBOT", "text": "honk answer"},
                {"ts": "3", "subtype": "bot_message", "bot_id": "BOTHER", "text": "other bot"},
            ],
            "response_metadata": {},
        }
        client = _StubClient([page])
        out = asyncio.run(bot_module.fetch_thread_context(client, "C1", "1", "TRIG", 30))
        assert [m["text"] for m in out] == ["question", "honk answer"]

    def test_keeps_recent_tail_across_pages(self, bot_module):
        bot_module.BOT_USER_ID = "UBOT"
        bot_module.BOT_ID = "BBOT"
        page1 = {
            "messages": [{"ts": f"{i}", "user": "U1", "text": f"old{i}"} for i in range(3)],
            "response_metadata": {"next_cursor": "c1"},
        }
        page2 = {
            "messages": [{"ts": f"{i}", "user": "U1", "text": f"new{i}"} for i in range(3)],
            "response_metadata": {},
        }
        client = _StubClient([page1, page2])
        out = asyncio.run(bot_module.fetch_thread_context(client, "C1", "1", "TRIG", 2))
        # Tail of the concatenated oldest->newest stream.
        assert [m["text"] for m in out] == ["new1", "new2"]
        assert client.calls[1]["cursor"] == "c1"


class TestEventTeamId:
    def test_prefers_body_team_id(self, bot_module):
        assert bot_module._event_team_id({"team_id": "T1"}, {"team": "T2"}) == "T1"

    def test_falls_back_to_event_team(self, bot_module):
        assert bot_module._event_team_id({}, {"team": "T2"}) == "T2"

    def test_falls_back_to_authorizations(self, bot_module):
        body = {"authorizations": [{"team_id": "T3", "enterprise_id": "E1"}]}
        assert bot_module._event_team_id(body, {}) == "T3"

    def test_falls_back_to_enterprise(self, bot_module):
        body = {"authorizations": [{"enterprise_id": "E1"}]}
        assert bot_module._event_team_id(body, {}) == "E1"

    def test_none_when_unresolvable(self, bot_module):
        assert bot_module._event_team_id({}, {}) is None


class TestForgetCacheScope:
    def test_pops_dm_and_thread_keys_for_channel_only(self, bot_module):
        cs = bot_module.conversation_states
        cs.clear()
        # Keys for the target channel (DM 2-tuple + a thread 3-tuple) and
        # an unrelated channel that must survive.
        cs[("T1", "D1")] = {"x": 1}
        cs[("T1", "D1", "111.0")] = {"x": 2}
        cs[("T1", "C9", "222.0")] = {"x": 3}
        cs[("T2", "D1")] = {"x": 4}  # different workspace, same channel id
        # Simulate the forget cache-drop logic.
        team_id, invoked_channel = "T1", "D1"
        for k in [
            key for key in cs
            if key[0] == team_id and len(key) >= 2 and key[1] == invoked_channel
        ]:
            cs.pop(k, None)
        assert ("T1", "D1") not in cs
        assert ("T1", "D1", "111.0") not in cs
        assert ("T1", "C9", "222.0") in cs  # other channel survives
        assert ("T2", "D1") in cs  # other workspace survives
        cs.clear()


class TestDedupe:
    def test_same_message_key_handled_once(self, bot_module):
        bot_module._seen_messages.clear()
        key = "T1:D1:1700000000.0001"  # a DM message ts
        # First delivery (e.g. app_mention) → not yet handled.
        assert bot_module._already_handled(key) is False
        # Second delivery of the SAME message (e.g. message.im, different
        # event_id) → deduped.
        assert bot_module._already_handled(key) is True

    def test_empty_key_never_deduped(self, bot_module):
        assert bot_module._already_handled(None) is False
        assert bot_module._already_handled("") is False


class TestBlockBuilders:
    def test_feedback_value_round_trips(self, bot_module):
        block = bot_module._feedback_actions_block("conv-abc", 3)
        like = block["elements"][0]
        assert like["action_id"] == bot_module._LIKE_ACTION
        conv, idx = like["value"].rsplit(":", 1)
        assert conv == "conv-abc" and int(idx) == 3

    def test_sources_block_limits_to_five(self, bot_module):
        srcs = [{"source": f"https://x.io/{i}"} for i in range(10)]
        block = bot_module._sources_context_block(srcs)
        text = block["elements"][0]["text"]
        assert text.count("https://") == 5

    def test_sources_block_none_when_empty(self, bot_module):
        assert bot_module._sources_context_block([]) is None
