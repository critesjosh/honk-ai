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

    def test_hash_comment_inside_tilde_fence_untouched(self, bot_module):
        src = "intro\n~~~bash\n# not a header\necho hi\n~~~\n# Outside"
        out = bot_module.format_for_slack(src)
        assert "# not a header" in out
        assert "*not a header*" not in out
        assert "*Outside*" in out

    def test_backtick_line_does_not_close_tilde_fence(self, bot_module):
        # Only the marker that opened the fence may close it.
        src = "~~~\n```\n# still fenced\n~~~\n# Outside"
        out = bot_module.format_for_slack(src)
        assert "# still fenced" in out
        assert "*still fenced*" not in out
        assert "*Outside*" in out


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

    def test_strip_leading_bot_mention_addressing_forms(self, bot_module):
        f = bot_module.strip_leading_bot_mention
        assert f("<@UBOT> what is noir?", "UBOT") == "what is noir?"
        assert f("  <@UBOT>: what is noir?", "UBOT") == "what is noir?"
        assert f("<@UBOT>, what is noir?", "UBOT") == "what is noir?"

    def test_strip_leading_bot_mention_keeps_mid_text_token(self, bot_module):
        # In a DM a non-leading <@bot> token is content, not addressing.
        src = "why does <@UBOT> appear in my event payload?"
        assert bot_module.strip_leading_bot_mention(src, "UBOT") == src
        # Leading addressing stripped, the content token preserved.
        out = bot_module.strip_leading_bot_mention("<@UBOT> what does <@UBOT> mean?", "UBOT")
        assert out == "what does <@UBOT> mean?"


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


class TestConversationStateCache:
    def test_fresh_entry_protected_when_all_others_locked(self, bot_module, monkeypatch):
        """At cap with every OTHER entry's lock held, the just-created
        entry must NOT be the eviction victim (its own lock isn't held
        yet — ``_answer_question`` acquires it after the lookup). The
        cache may transiently exceed the cap instead. Mirrors the
        Discord bot's ``_evict_cache_if_needed`` guarantee."""
        monkeypatch.setattr(bot_module, "_THREAD_CACHE_MAX_ENTRIES", 3)
        bot_module.conversation_states.clear()

        async def driver():
            keys = [("T1", f"C{i}") for i in range(3)]
            states = [bot_module._get_conversation_state(k) for k in keys]
            for s in states:
                await s["lock"].acquire()
            try:
                fresh = bot_module._get_conversation_state(("T1", "C-new"))
                assert ("T1", "C-new") in bot_module.conversation_states, "fresh entry was evicted"
                assert bot_module.conversation_states[("T1", "C-new")] is fresh
                # Cap transiently exceeded rather than orphaning the new state.
                assert len(bot_module.conversation_states) == 4
            finally:
                for s in states:
                    s["lock"].release()

        asyncio.run(driver())
        bot_module.conversation_states.clear()


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


# --- /stream error-frame handling -------------------------------------------


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
    """On an in-band error the backend flushes buffered text, emits
    ``{"type": "error"}``, and returns WITHOUT ``id``/``end`` and WITHOUT
    writing a conversation_messages row — the client must surface that
    instead of treating the turn as a success."""

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


class _RecordingClient:
    """Records chat_postMessage / chat_update / chat_postEphemeral calls."""

    def __init__(self):
        self.posts = []
        self.updates = []
        self.ephemerals = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"{len(self.posts)}.0"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)
        return {"ok": True}


class TestFeedbackHandler:
    def test_feedback_submits_silently(self, bot_module, monkeypatch):
        """A 👍 click acks + submits but posts NO visible confirmation —
        the old ephemeral "Thanks for the feedback" was channel noise."""
        calls = []

        async def fake_submit(conversation_id, question_index, feedback):
            calls.append((conversation_id, question_index, feedback))
            return True

        monkeypatch.setattr(bot_module, "submit_feedback", fake_submit)
        client = _RecordingClient()
        acked = []

        async def ack():
            acked.append(True)

        body = {
            "actions": [{"action_id": bot_module._LIKE_ACTION, "value": "conv-1:2"}],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
        }
        asyncio.run(bot_module._handle_feedback(ack, body, client, "conv-1:2"))
        assert acked
        assert calls == [("conv-1", 2, "LIKE")]
        assert client.posts == []
        assert client.updates == []
        assert client.ephemerals == []


class TestAnswerQuestionErrorPaths:
    def _run(self, bot_module, monkeypatch, fake_generate):
        monkeypatch.setattr(bot_module, "generate_answer", fake_generate)
        client = _RecordingClient()
        key = ("T_UNCAPPED", "D1")
        bot_module.conversation_states.pop(key, None)
        asyncio.run(bot_module._answer_question(
            client, team_id="T_UNCAPPED", channel="D1", thread_ts=None,
            trigger_ts="1.0", user="U1", question="q", is_dm=True,
        ))
        state = bot_module.conversation_states[key]
        bot_module.conversation_states.pop(key, None)
        return client, state

    def test_error_frame_rolls_back_like_non_200(self, bot_module, monkeypatch):
        """An in-band error must not consume a feedback position: the
        backend wrote NO row, so advancing answer_count would desync
        every subsequent 👍/👎 in the conversation."""

        async def fake(question, messages, conversation_id):
            return {
                "answer": "partial", "conversation_id": conversation_id,
                "sources": [], "usage": None, "http_status": 200, "error": "boom",
            }

        client, state = self._run(bot_module, monkeypatch, fake)
        assert state["history"] == []  # phantom prompt rolled back
        assert state["answer_count"] == 0  # no feedback position consumed
        assert state["conversation_id"] is None
        assert len(client.posts) == 1  # the placeholder only — no feedback blocks
        assert len(client.updates) == 1  # placeholder became the error text
        assert "answering" in client.updates[0]["text"]

    def test_error_frame_on_follow_up_turn_rolls_back(self, bot_module, monkeypatch):
        """First-turn coverage masks the bug: there conversation_id is
        still None, so the pre-existing non-200 fallback would roll back
        anyway. On a follow-up turn the state carries a real
        conversation_id (no ``id`` frame is emitted on the error path, so
        it keeps the prior turn's), and ONLY the error-frame branch
        protects answer_count / feedback alignment."""

        async def fake(question, messages, conversation_id):
            assert conversation_id == "conv-REAL"
            return {
                "answer": "partial", "conversation_id": conversation_id,
                "sources": [], "usage": None, "http_status": 200, "error": "boom",
            }

        monkeypatch.setattr(bot_module, "generate_answer", fake)
        client = _RecordingClient()
        key = ("T_UNCAPPED", "D1")
        bot_module.conversation_states.pop(key, None)
        seeded = bot_module._get_conversation_state(key)
        seeded["conversation_id"] = "conv-REAL"
        seeded["answer_count"] = 1
        seeded["history"].append({"prompt": "first q", "response": "first a"})
        asyncio.run(bot_module._answer_question(
            client, team_id="T_UNCAPPED", channel="D1", thread_ts=None,
            trigger_ts="2.0", user="U1", question="follow-up q", is_dm=True,
        ))
        state = bot_module.conversation_states.pop(key)
        assert state["answer_count"] == 1  # NOT incremented — backend wrote no row
        assert state["conversation_id"] == "conv-REAL"
        assert len(state["history"]) == 1  # phantom prompt rolled back
        assert state["history"][0] == {"prompt": "first q", "response": "first a"}
        assert len(client.posts) == 1  # placeholder only — no feedback blocks posted
        assert not any(p.get("blocks") for p in client.posts)
        assert len(client.updates) == 1  # placeholder became the canned error
        assert "answering" in client.updates[0]["text"]

    def test_unexpected_exception_rolls_back_phantom_prompt(self, bot_module, monkeypatch):
        async def fake(question, messages, conversation_id):
            raise RuntimeError("kaput")

        client, state = self._run(bot_module, monkeypatch, fake)
        assert state["history"] == []  # guarded pop ran in the catch-all
        assert state["answer_count"] == 0
        assert len(client.updates) == 1  # placeholder replaced, not dangling
        assert "handling" in client.updates[0]["text"]


class TestDmMentionStrip:
    """A DM containing @Honk fires BOTH app_mention and message.im; when
    message.im wins the dedupe race the raw ``<@U…>`` token must not
    reach RAG."""

    def _run_on_message(self, bot_module, monkeypatch, text):
        captured = {}

        async def fake_answer(client, **kwargs):
            captured.update(kwargs)

        spawned = []
        monkeypatch.setattr(bot_module, "_answer_question", fake_answer)
        monkeypatch.setattr(bot_module, "_spawn", lambda coro: spawned.append(coro))
        monkeypatch.setattr(bot_module, "BOT_USER_ID", "UBOT")
        monkeypatch.setattr(bot_module, "SLACK_TEAM_IDS", [])
        bot_module._seen_messages.clear()
        event = {"channel_type": "im", "channel": "D1", "ts": "1700000000.1", "user": "U1", "text": text}
        asyncio.run(bot_module.on_message(event, {"team_id": "T1"}, client=None, logger=None))
        for coro in spawned:
            asyncio.run(coro)
        return captured

    def test_mention_token_stripped(self, bot_module, monkeypatch):
        captured = self._run_on_message(bot_module, monkeypatch, "<@UBOT> what is aztec?")
        assert captured["question"] == "what is aztec?"
        assert captured["is_dm"] is True

    def test_plain_dm_text_unchanged(self, bot_module, monkeypatch):
        captured = self._run_on_message(bot_module, monkeypatch, "what is aztec?")
        assert captured["question"] == "what is aztec?"

    def test_mid_text_mention_token_preserved(self, bot_module, monkeypatch):
        """Only a LEADING mention is the addressing form; a literal
        ``<@UBOT>`` in the middle of a DM is the question's content
        (e.g. asking about Slack event payloads) and must reach RAG."""
        src = "why does <@UBOT> appear in my event payload?"
        captured = self._run_on_message(bot_module, monkeypatch, src)
        assert captured["question"] == src


class TestSpawnDoneCallback:
    def test_cancelled_task_does_not_raise_in_callback(self, bot_module):
        """``Task.exception()`` RAISES CancelledError on cancelled tasks;
        the done-callback must guard with ``t.cancelled()`` first or the
        loop reports 'Exception in callback' for every cancelled spawn."""
        records = []

        async def go():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _lp, ctx: records.append(ctx))
            task = bot_module._spawn(asyncio.sleep(30))
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):  # let the cancellation + done-callback run
                await asyncio.sleep(0)

        asyncio.run(go())
        assert records == []

    def test_failed_task_still_logs(self, bot_module, caplog):
        import logging

        async def boom():
            raise RuntimeError("kaput")

        async def go():
            bot_module._spawn(boom())
            for _ in range(3):
                await asyncio.sleep(0)

        with caplog.at_level(logging.ERROR, logger=bot_module.logger.name):
            asyncio.run(go())
        assert any("background task failed" in r.message for r in caplog.records)
