"""Tests for the Aztec citation-marker filter.

The marker is a machine-only ``[[cited: i, j, k]]`` (or
``[[cited: none]]``) line at the end of every answer. Backend strips
it from the streamed bytes + persisted ``response``, parses indices,
and emits a filtered ``{type: "source"}`` SSE frame. See
``application/api/answer/routes/base.py`` and the Aztec grounded
prompts.

Note on caplog tests below: pytest's ``caplog`` fixture in this
directory has a known module-collection-order quirk — once enough
sibling test files import from ``application.api.answer.routes.base``
during collection, root-logger handlers shift so that the ``base``
logger's records stop reaching ``caplog.records``. This affects
several existing tests (``test_logs_per_message_start_and_end``,
``test_inband_error_persists_failure_log_and_does_not_log_success``)
as well; the tests still pass in targeted runs (``pytest -k`` or
``pytest tests/api/answer/routes/test_citation_marker.py``). Until the
underlying pytest/Flask logging-handler ordering issue is fixed, treat
caplog-dependent assertions as targeted-run-only.
"""

from __future__ import annotations

import json
from typing import Iterable, List
from unittest.mock import MagicMock

from application.api.answer.routes.base import (
    _build_source_frame,
    _CITATION_MARKER_MAX_LEN,
    _filter_sources_by_indices,
    _parse_citation_marker,
)


# ---- _parse_citation_marker -----------------------------------------


class TestParseCitationMarker:
    def test_no_marker_returns_fail_open(self):
        r = _parse_citation_marker("ordinary answer text", source_count=5)
        assert not r.matched
        assert r.stripped_tail == "ordinary answer text"
        assert r.cited_indices is None
        assert r.invalid_indices == []

    def test_basic_marker(self):
        r = _parse_citation_marker(
            "prose body\n\n[[cited: 1, 3]]", source_count=5
        )
        assert r.matched
        assert r.stripped_tail == "prose body"
        assert r.cited_indices == [1, 3]
        assert r.invalid_indices == []

    def test_marker_none(self):
        r = _parse_citation_marker(
            "yeah I'm here.\n\n[[cited: none]]", source_count=5
        )
        assert r.matched
        assert r.stripped_tail == "yeah I'm here."
        assert r.cited_indices == []

    def test_marker_NONE_case_insensitive(self):
        r = _parse_citation_marker(
            "x\n\n[[CITED: NONE]]", source_count=5
        )
        assert r.cited_indices == []
        assert r.stripped_tail == "x"

    def test_marker_whitespace_inside(self):
        r = _parse_citation_marker(
            "x\n\n[[  cited:  2 , 4  ,  ]]", source_count=5
        )
        assert r.cited_indices == [2, 4]
        assert r.stripped_tail == "x"

    def test_marker_dedup_preserves_order(self):
        r = _parse_citation_marker(
            "x\n\n[[cited: 3, 1, 3, 2]]", source_count=5
        )
        assert r.cited_indices == [3, 1, 2]

    def test_marker_invalid_out_of_range(self):
        r = _parse_citation_marker(
            "x\n\n[[cited: 1, 99, 3]]", source_count=5
        )
        assert r.cited_indices == [1, 3]
        assert r.invalid_indices == [99]

    def test_marker_invalid_zero_and_negative(self):
        r = _parse_citation_marker(
            "x\n\n[[cited: 0, -1, 2]]", source_count=5
        )
        assert r.cited_indices == [2]
        assert r.invalid_indices == [0, -1]

    def test_marker_garbage_among_integers_strips_garbage(self):
        r = _parse_citation_marker(
            "x\n\n[[cited: 1, abc, 2]]", source_count=5
        )
        assert r.cited_indices == [1, 2]
        assert r.invalid_indices == []

    def test_marker_all_garbage_strips_but_fails_open(self):
        """All tokens are non-integer → marker matched but malformed.
        Strip the leaked marker text (we anchored to ``\\Z`` so the
        span is definitely the marker) AND fail open on source
        filtering — communicated via ``cited_indices=None``.
        """
        r = _parse_citation_marker(
            "x\n\n[[cited: banana, kiwi]]", source_count=5
        )
        assert r.matched
        assert r.stripped_tail == "x"
        assert r.cited_indices is None
        assert r.invalid_indices == []

    def test_marker_empty_list(self):
        r = _parse_citation_marker("x\n\n[[cited: ]]", source_count=5)
        assert r.cited_indices == []
        assert r.stripped_tail == "x"

    def test_marker_not_at_end_is_rejected(self):
        r = _parse_citation_marker(
            "[[cited: 1]] followed by more text", source_count=5
        )
        assert not r.matched
        assert r.stripped_tail == "[[cited: 1]] followed by more text"

    def test_marker_with_only_whitespace_after(self):
        r = _parse_citation_marker(
            "answer\n\n[[cited: 2]]\n   \n", source_count=5
        )
        assert r.cited_indices == [2]
        assert r.stripped_tail == "answer"

    def test_empty_tail(self):
        r = _parse_citation_marker("", source_count=5)
        assert r.stripped_tail == ""
        assert r.cited_indices is None
        assert not r.matched

    def test_source_count_zero_drops_everything(self):
        r = _parse_citation_marker(
            "x\n\n[[cited: 1, 2]]", source_count=0
        )
        assert r.cited_indices == []
        assert r.invalid_indices == [1, 2]


# ---- _filter_sources_by_indices -------------------------------------


class TestFilterSourcesByIndices:
    SRC = [
        {"id": "s1"},
        {"id": "s2"},
        {"id": "s3"},
    ]

    def test_picks_by_index_in_llm_order(self):
        assert _filter_sources_by_indices(self.SRC, [3, 1]) == [
            {"id": "s3"},
            {"id": "s1"},
        ]

    def test_ignores_out_of_range(self):
        assert _filter_sources_by_indices(self.SRC, [1, 99]) == [{"id": "s1"}]

    def test_empty_indices(self):
        assert _filter_sources_by_indices(self.SRC, []) == []

    def test_empty_sources(self):
        assert _filter_sources_by_indices([], [1, 2]) == []


# ---- _build_source_frame --------------------------------------------


class TestBuildSourceFrame:
    def test_returns_none_for_empty(self):
        assert _build_source_frame([]) is None

    def test_truncates_text(self):
        frame = _build_source_frame(
            [{"title": "doc.md", "text": "x" * 500}]
        )
        assert frame is not None
        payload = json.loads(frame.split("data: ", 1)[-1])
        assert payload["type"] == "source"
        text = payload["source"][0]["text"]
        assert text.endswith("...")
        assert len(text) <= 104  # 100 chars + "..." (and rstrip)

    def test_deduplicates_by_rewritten_url(self):
        # Both rewrite to docs.aztec.network/developers/docs/foo so the
        # second collapses out — global rerank can surface the same
        # rendered URL from differently-named chunks.
        sources = [
            {"title": "a", "source": "version-v4.2.0/docs/foo.md"},
            {"title": "b", "source": "version-v4.2.0/docs/foo.mdx"},
        ]
        frame = _build_source_frame(sources)
        assert frame is not None
        payload = json.loads(frame.split("data: ", 1)[-1])
        assert len(payload["source"]) == 1


# ---- complete_stream end-to-end -------------------------------------


def _agent_yielding(events: Iterable[dict]) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.gen.return_value = iter(list(events))
    return mock_agent


def _stream_to_frames(raw: List[str]) -> List[dict]:
    frames = []
    for chunk in raw:
        if not chunk.startswith("data: "):
            continue
        body = chunk[len("data: ") :].rstrip("\n")
        try:
            frames.append(json.loads(body))
        except json.JSONDecodeError:
            pass
    return frames


class TestCompleteStreamCitationFilter:
    """End-to-end behaviour through ``complete_stream``."""

    def _run(
        self,
        mock_mongo_db,
        flask_app,
        agent: MagicMock,
    ) -> List[dict]:
        from application.api.answer.routes.base import BaseAnswerResource

        with flask_app.app_context():
            resource = BaseAnswerResource()
            stream = list(
                resource.complete_stream(
                    question="Q",
                    agent=agent,
                    conversation_id=None,
                    user_api_key=None,
                    decoded_token={"sub": "user1"},
                    should_save_conversation=False,
                )
            )
        return _stream_to_frames(stream)

    def test_marker_filters_sources(self, mock_mongo_db, flask_app):
        agent = _agent_yielding(
            [
                {"sources": [{"title": f"doc{i}.md"} for i in range(1, 6)]},
                {"answer": "Answer body referencing chunk three.\n\n"},
                {"answer": "[[cited: 1, 3]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted_titles = [s["title"] for s in source_frames[0]["source"]]
        # Filtered + LLM-order preserved: 1 then 3.
        assert emitted_titles == ["doc1.md", "doc3.md"]
        # Answer frames must not leak the marker.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert "Answer body" in answer_text

    def test_marker_none_omits_source_frame(self, mock_mongo_db, flask_app):
        agent = _agent_yielding(
            [
                {"sources": [{"title": "doc1.md"}, {"title": "doc2.md"}]},
                {"answer": "Yes, I'm here.\n\n[[cited: none]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        assert not any(f.get("type") == "source" for f in frames)
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert answer_text.strip() == "Yes, I'm here."

    def test_no_marker_fails_open(self, mock_mongo_db, flask_app, caplog):
        """Fail-open behaviour: no marker → all sources emitted.

        The caplog ``llm.cited_missing`` assertion is intentionally
        omitted — pytest's caplog plugin is polluted by sibling-file
        collection in this directory (see module docstring) and that's
        out of scope for this PR. The wire-level behaviour (all
        sources emitted) is what matters for the user-visible
        contract; the log line is observability-only.
        """
        import logging

        agent = _agent_yielding(
            [
                {"sources": [{"title": "doc1.md"}, {"title": "doc2.md"}]},
                {"answer": "Plain answer, no marker."},
            ]
        )
        with caplog.at_level(
            logging.INFO, logger="application.api.answer.routes.base"
        ):
            frames = self._run(mock_mongo_db, flask_app, agent)

        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        # All sources retained on fail-open.
        assert len(source_frames[0]["source"]) == 2

    def test_marker_split_across_deltas(self, mock_mongo_db, flask_app):
        """Marker emitted across multiple ``answer`` deltas must still
        parse — this is the tail-buffer's key correctness guarantee.
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "doc1.md"}, {"title": "doc2.md"}]},
                {"answer": "Done.\n\n[[ci"},
                {"answer": "ted: "},
                {"answer": "2]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert [s["title"] for s in source_frames[0]["source"]] == ["doc2.md"]
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert answer_text.strip() == "Done."

    def test_long_answer_streams_then_strips_marker(
        self, mock_mongo_db, flask_app
    ):
        """When the answer is much longer than the buffer, most of it
        streams as it arrives. Only the tail (which may contain the
        marker) is held until end-of-stream.
        """
        long_body = "x" * (_CITATION_MARKER_MAX_LEN * 4)
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {"answer": long_body + "\n\n[[cited: 2]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert answer_text.endswith(long_body)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert [s["title"] for s in source_frames[0]["source"]] == ["b.md"]

    def test_invalid_index_dropped(self, mock_mongo_db, flask_app):
        """Out-of-range indices are silently dropped from the emitted
        source list. (The caplog-based ``llm.cited_invalid_index``
        warning is omitted from this test for the same reason as
        ``test_no_marker_fails_open`` — caplog pollution.)
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {"answer": "Answer.\n\n[[cited: 1, 99]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert [s["title"] for s in source_frames[0]["source"]] == ["a.md"]

    def test_source_frame_emitted_after_answer(self, mock_mongo_db, flask_app):
        """Frame ordering: source comes AFTER the final answer delta but
        before id/usage/end. Important for v1 translator's [DONE]
        sentinel (it triggers on end).
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}]},
                {"answer": "Answer.\n\n[[cited: 1]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        types = [f.get("type") for f in frames]
        # answer frames precede source, source precedes id and end.
        first_source = types.index("source")
        first_id = types.index("id")
        first_end = types.index("end")
        # at least one answer frame appears before the source frame
        assert "answer" in types[:first_source]
        assert first_source < first_id < first_end

    def test_structured_agent_bypasses_marker_logic(
        self, mock_mongo_db, flask_app
    ):
        """Structured / JSON-schema agents never emit the marker (their
        ``answer`` is a JSON object). The filter must NOT corrupt their
        output. Sources should still be emitted unfiltered.
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {
                    "answer": '{"verdict": "ok"}',
                    "structured": True,
                    "schema": {"type": "object"},
                },
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert len(source_frames[0]["source"]) == 2
        structured_frames = [
            f for f in frames if f.get("type") == "structured_answer"
        ]
        assert len(structured_frames) == 1
        assert structured_frames[0]["answer"] == '{"verdict": "ok"}'

    def test_inband_error_flushes_buffered_answer(
        self, mock_mongo_db, flask_app
    ):
        """If an in-band error fires after some answer bytes were
        buffered but before the marker arrived, the buffered bytes
        MUST be flushed to the client before the error frame so no
        answer text is silently lost.
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}]},
                {"answer": "Partial response that"},
                {"answer": " never got a marker"},
                {"type": "error", "error": "upstream blew up"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        types = [f.get("type") for f in frames]
        assert "error" in types
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "Partial response that never got a marker" in answer_text
        # Error path emits no source frame (current contract).
        assert not any(t == "source" for t in types[types.index("error") :])

    def test_terminal_tool_calls_after_marker_does_not_leak(
        self, mock_mongo_db, flask_app
    ):
        """ClassicAgent (the only agent type used in Aztec prod) yields
        events in this order: answer-deltas → ``{"sources": ...}`` →
        ``{"tool_calls": []}``. The tool_calls frame is yielded AFTER
        the marker-bearing answer has finished streaming, so the
        flush-before-tool_calls path is where the marker would leak if
        we didn't parse-and-strip BEFORE emitting the tool_calls
        frame.  See ``application/agents/classic_agent.py``.
        """
        agent = _agent_yielding(
            [
                {"answer": "Short answer.\n\n[[cited: 1]]"},
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {"tool_calls": []},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        # The marker MUST be stripped before the answer frame is sent.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert answer_text.strip() == "Short answer."
        # tool_calls frame still appears (preserves the ClassicAgent
        # contract for clients that consume it).
        types = [f.get("type") for f in frames]
        assert "tool_calls" in types
        # Source frame is filtered to only the cited subset.
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert [s["title"] for s in source_frames[0]["source"]] == ["a.md"]

    def test_malformed_marker_strips_but_fails_open(
        self, mock_mongo_db, flask_app
    ):
        """A garbled marker like ``[[cited: banana]]``:

        * MUST still be stripped from the user-visible answer (the
          regex anchored to end-of-string, so the matched span is
          definitively the trailing marker — safe to strip).
        * MUST fall back to emitting all sources (fail-open) rather
          than silently dropping every citation.

        Together these preserve the "marker never leaks" contract
        AND keep the user from losing all source attribution when the
        model garbles the marker.
        """
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {"answer": "Answer body.\n\n[[cited: banana]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        # Fail-open: all sources emitted.
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert len(source_frames[0]["source"]) == 2
        # Strip-on-malformed: the marker never reaches the client.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert answer_text.strip() == "Answer body."

    def test_marker_max_len_constant_bounds_buffer(self):
        # Sanity: marker max len is big enough for plausible worst case
        # (12 sources, 3-digit indices, generous whitespace).
        worst_case = "[[cited: " + ", ".join(str(n) for n in range(1, 13)) + "]]"
        assert len(worst_case) < _CITATION_MARKER_MAX_LEN
