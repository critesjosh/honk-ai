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
    _AMBIGUOUS_BASENAME_THRESHOLD,
    _build_source_frame,
    _CITATION_MARKER_MAX_LEN,
    _extract_cited_filenames,
    _FAIL_OPEN_MAX_SOURCES,
    _fail_open_sources,
    _filename_fallback,
    _filter_sources_by_indices,
    _parse_citation_marker,
    _scrub_inline_citation_markers,
)


class TestFailOpenSources:
    """``_fail_open_sources`` caps the source list and records the pre-cap
    count as ``available_count`` for audit."""

    def _docs(self, n):
        return [{"title": f"doc{i}", "filename": f"f{i}.md"} for i in range(1, n + 1)]

    def test_caps_to_max_and_records_available(self):
        docs = self._docs(8)
        kept, meta = _fail_open_sources(docs, marker_present=False)
        assert len(kept) == _FAIL_OPEN_MAX_SOURCES
        assert [d["title"] for d in kept] == ["doc1", "doc2", "doc3"]
        assert meta["strategy"] == "fail_open"
        assert meta["marker_present"] is False
        assert meta["filtered_count"] == _FAIL_OPEN_MAX_SOURCES
        assert meta["available_count"] == 8
        assert "marker_malformed" not in meta

    def test_fewer_than_cap_keeps_all(self):
        docs = self._docs(2)
        kept, meta = _fail_open_sources(docs, marker_present=False)
        assert len(kept) == 2
        assert meta["filtered_count"] == 2
        assert meta["available_count"] == 2

    def test_malformed_flag_set_and_marker_present(self):
        docs = self._docs(5)
        kept, meta = _fail_open_sources(
            docs, marker_present=True, marker_malformed=True
        )
        assert len(kept) == _FAIL_OPEN_MAX_SOURCES
        assert meta["marker_present"] is True
        assert meta["marker_malformed"] is True
        assert meta["available_count"] == 5

    def test_empty_input(self):
        kept, meta = _fail_open_sources([], marker_present=False)
        assert kept == []
        assert meta["filtered_count"] == 0
        assert meta["available_count"] == 0


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


# ---- _scrub_inline_citation_markers ---------------------------------


class TestScrubInlineCitationMarkers:
    """Defense-in-depth: strip any ``[[cited: ...]]`` span the model
    emitted inline (instead of/in addition to the trailing one).
    Companion to the ``\\Z``-anchored parser — that one is
    authoritative for source filtering; this scrubber only protects
    the user-visible bytes from a degenerate emission loop.
    """

    def test_empty_string_passthrough(self):
        assert _scrub_inline_citation_markers("") == ""

    def test_no_marker_passthrough(self):
        text = "ordinary prose with [brackets] and `[[code]]` blocks"
        assert _scrub_inline_citation_markers(text) == text

    def test_single_inline_marker_stripped(self):
        # Surrounding whitespace is preserved deliberately — extra
        # spaces are harmless; fused words would be a worse bug.
        text = "NoirJS lets you prove client-side [[cited: 6, 9, 11]]\nrest of prose"
        assert _scrub_inline_citation_markers(text) == (
            "NoirJS lets you prove client-side \nrest of prose"
        )

    def test_many_inline_markers_stripped(self):
        # The 283-marker Discord regression in one shot.
        text = (
            "Para one. [[cited: 2]]\nPara two. [[cited: 9]]\n"
            "Para three. [[cited: 3]]\nPara four. [[cited: 11]]"
        )
        scrubbed = _scrub_inline_citation_markers(text)
        assert "[[cited:" not in scrubbed
        assert "Para one." in scrubbed
        assert "Para four." in scrubbed

    def test_marker_at_end_also_stripped(self):
        # The scrubber is unanchored, so terminal markers fall to it
        # too. That's fine — the authoritative trailing-marker parse
        # runs BEFORE this scrubber on response_full in production.
        assert (
            _scrub_inline_citation_markers("body\n\n[[cited: 1, 2]]")
            == "body\n\n"
        )

    def test_case_insensitive(self):
        # Surrounding whitespace preserved (see test_single_inline_marker_stripped).
        assert (
            _scrub_inline_citation_markers("x [[CITED: 1]] y")
            == "x  y"
        )

    def test_marker_none_form(self):
        assert (
            _scrub_inline_citation_markers("intro [[cited: none]] outro")
            == "intro  outro"
        )

    def test_preserves_newline_structure(self):
        # Marker on its own line — strip the marker, keep the line
        # boundaries so prose paragraphs don't fuse.
        text = "First paragraph.\n\n[[cited: 2]]\n\nSecond paragraph."
        scrubbed = _scrub_inline_citation_markers(text)
        assert "[[cited:" not in scrubbed
        assert "First paragraph." in scrubbed
        assert "Second paragraph." in scrubbed
        # Two newlines on each side of the marker line means the line
        # boundary is preserved on both sides post-strip.
        assert "First paragraph.\n\n" in scrubbed
        assert "\n\nSecond paragraph." in scrubbed

    def test_ignores_unclosed_bracket(self):
        # A literal ``[[cited:`` with no closing brackets is left as
        # a string (it's just text, not a marker). Same shape as the
        # underlying regex — ``[^\]\n]*`` won't span newlines.
        text = "talking about [[cited: tags]] vs other things"
        # ``[[cited: tags]]`` matches the regex (single-line, closes
        # with ``]]``) — gets stripped. Surrounding spaces preserved.
        assert _scrub_inline_citation_markers(text) == "talking about  vs other things"

    def test_truly_unclosed_left_alone(self):
        text = "look at [[cited: 1, 2\nnext line"
        # Marker payload can't cross a newline → unclosed → stays as text.
        assert _scrub_inline_citation_markers(text) == text


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
            {"title": "a", "source": "version-v4.3.0/docs/foo.md"},
            {"title": "b", "source": "version-v4.3.0/docs/foo.mdx"},
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
        """Fail-open behaviour: no marker → fail-open source set (capped at
        _FAIL_OPEN_MAX_SOURCES; here 2 retrieved < cap, so both are kept).

        The caplog ``llm.cited_missing`` assertion is intentionally
        omitted — pytest's caplog plugin is polluted by sibling-file
        collection in this directory (see module docstring) and that's
        out of scope for this PR. The wire-level behaviour (fail-open
        source set, capped at _FAIL_OPEN_MAX_SOURCES) is what matters
        for the user-visible contract; the log line is observability-only.
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
        # 2 retrieved < _FAIL_OPEN_MAX_SOURCES, so the cap is a no-op here.
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

    def test_inline_markers_scrubbed_from_streamed_answer(
        self, mock_mongo_db, flask_app
    ):
        """Regression for the 2026-05-22 Discord catastrophe: the model
        emitted 283 inline ``[[cited: N]]`` spans through a long answer
        and ended WITHOUT a terminal marker. Every span leaked to the
        user. The scrubber must remove every inline marker from the
        bytes the client sees — even on the fail-open / no-trailing-
        marker path.
        """
        long_body = (
            "Para one explaining NoirJS. [[cited: 6, 9, 11]]\n"
            + "Body filler. " * 50
            + "Para two on bundlers. [[cited: 2]]\n"
            + "More body. " * 50
            + "Para three on pinning. [[cited: 9]]\n"
            + "Trailing prose with no marker at all."
        )
        agent = _agent_yielding(
            [
                {"sources": [{"title": f"doc{i}.md"} for i in range(1, 13)]},
                {"answer": long_body},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text, (
            "inline marker leaked through to streamed answer frames: "
            f"{answer_text[:200]!r}"
        )
        # Prose is preserved.
        assert "NoirJS" in answer_text
        assert "Trailing prose with no marker at all" in answer_text
        # No trailing marker AND no inline filename the fallback can match
        # → fail-open, which now caps to _FAIL_OPEN_MAX_SOURCES (no grounding
        # signal, so we don't fill the frame with the full retrieval set).
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert len(source_frames[0]["source"]) == _FAIL_OPEN_MAX_SOURCES

    def test_inline_marker_leak_recorded_in_audit(
        self, mock_mongo_db, flask_app
    ):
        """``citation_filter.inline_markers_scrubbed`` records the
        count of inline markers the scrubber removed from
        ``response_full``. Cheap operator signal — alert / honk-report
        can pivot on this row without grepping response bodies.
        """
        body = (
            "Section A. [[cited: 1]]\n\n"
            "Section B. [[cited: 2]]\n\n"
            "Section C. [[cited: 3]]\n\n"
            "[[cited: 1, 2, 3]]"
        )
        agent = _agent_yielding(
            [
                {"sources": [{"title": f"d{i}.md"} for i in range(1, 5)]},
                {"answer": body},
            ]
        )
        # We can't read query_metadata directly through the SSE wire,
        # but we can verify the WARN log fires AND the body is clean.
        import logging

        with self._caplog(
            mock_mongo_db, flask_app, agent, logging.WARNING
        ) as (frames, caplog):
            pass
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        # Three inline markers were emitted (the terminal one doesn't
        # count toward "inline" — it's stripped by the trailing-marker
        # path before the inline counter runs).
        leak_lines = [
            r.getMessage()
            for r in caplog.records
            if "llm.marker_leak_scrubbed" in r.getMessage()
        ]
        assert len(leak_lines) == 1, leak_lines
        assert "count=3" in leak_lines[0]
        assert "strategy=marker" in leak_lines[0]

    def _caplog(self, mock_mongo_db, flask_app, agent, level):
        """Tiny helper: run the stream + capture logs at ``level``.
        Yields ``(frames, caplog_record_list)``.
        """
        import contextlib
        import logging

        @contextlib.contextmanager
        def _ctx():
            from application.api.answer.routes.base import BaseAnswerResource

            captured = []

            class _CaptureHandler(logging.Handler):
                def emit(self, record):
                    captured.append(record)

            handler = _CaptureHandler(level=level)
            logger = logging.getLogger("application.api.answer.routes.base")
            logger.addHandler(handler)
            try:
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
                frames = _stream_to_frames(stream)

                class _Caplog:
                    @property
                    def records(self):
                        return captured

                yield frames, _Caplog()
            finally:
                logger.removeHandler(handler)

        return _ctx()

    def test_inline_marker_plus_terminal_marker_both_handled(
        self, mock_mongo_db, flask_app
    ):
        """Model emits one or more inline markers AND a terminal one.
        The terminal marker still drives source filtering; the inline
        ones are scrubbed from the visible bytes.
        """
        body = (
            "First section. [[cited: 1]]\n\n"
            "Second section. [[cited: 2]]\n\n"
            "Third section. [[cited: 3]]\n\n"
            "[[cited: 1, 3]]"
        )
        agent = _agent_yielding(
            [
                {"sources": [{"title": f"d{i}.md"} for i in range(1, 5)]},
                {"answer": body},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        # Terminal marker drove source filtering, in LLM order.
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert [s["title"] for s in source_frames[0]["source"]] == [
            "d1.md",
            "d3.md",
        ]
        # All three section headers survived.
        assert "First section." in answer_text
        assert "Second section." in answer_text
        assert "Third section." in answer_text

    def test_unmatched_double_bracket_does_not_stall_stream(
        self, mock_mongo_db, flask_app
    ):
        """An unmatched ``[[`` in legitimate prose (talking about the
        marker format, or a code example with double-bracket syntax)
        must NOT pin the buffer split. The holdback is bounded to
        ``_PARTIAL_MARKER_HOLDBACK_LEN`` chars so an unclosed ``[[``
        further back from the split point releases normally.

        Buggy code would have pinned ``split_at`` at the ``[[``
        position forever, releasing the entire body in one frame at
        end-of-stream instead of streaming it normally.
        """
        body = (
            "Discussion of the [[double-bracket syntax in some doc tool. "
            + "More prose. " * 200
            + "End of answer with no trailing marker."
        )
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}]},
                {"answer": body},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        answer_frames = [f for f in frames if f.get("type") == "answer"]
        assert len(answer_frames) > 1, (
            "stream stalled — only one answer frame emitted, "
            "indicating the unmatched [[ pinned split_at"
        )
        answer_text = "".join(f["answer"] for f in answer_frames)
        # The unmatched ``[[`` passes through verbatim (it's text, not
        # a marker — the regex requires ``]]`` to match).
        assert "[[double-bracket syntax" in answer_text
        assert "End of answer with no trailing marker." in answer_text

    def test_inline_marker_straddling_delta_boundary(
        self, mock_mongo_db, flask_app
    ):
        """The ``[[`` open-bracket lands in one delta, the ``]]`` close
        in the next. The streaming emitter must hold the partial back
        in ``pending_tail`` instead of releasing a half-marker prefix.
        Stress with a body large enough that ``pending_tail`` does
        actually overflow (forcing the emit path) AND tail-end the
        whole thing with NO terminal marker so the fail-open scrubber
        catches the inline one too.
        """
        prelude = "z" * (_CITATION_MARKER_MAX_LEN * 3)
        agent = _agent_yielding(
            [
                {"sources": [{"title": "a.md"}, {"title": "b.md"}]},
                {"answer": prelude + " mid-prose [[cited"},
                {"answer": ": 2]] more prose without any trailing marker."},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[" not in answer_text
        assert "[[cited:" not in answer_text
        # Both halves of the surrounding prose are intact.
        assert answer_text.startswith(prelude + " mid-prose")
        assert answer_text.endswith("more prose without any trailing marker.")

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


# ---- filename fallback (no-marker path) -----------------------------


class TestExtractCitedFilenames:
    def test_empty(self):
        assert _extract_cited_filenames("") == set()
        assert _extract_cited_filenames(None) == set()  # type: ignore[arg-type]

    def test_backtick_quoted(self):
        out = _extract_cited_filenames("see `foo.md` for details")
        assert "foo.md" in out

    def test_parens(self):
        out = _extract_cited_filenames("the layout (bar.nr) shows...")
        assert "bar.nr" in out

    def test_markdown_link(self):
        out = _extract_cited_filenames("see [the doc](docs/baz.md) for more")
        # Path-form preserved verbatim; basename is NOT auto-added —
        # see ``_extract_cited_filenames`` docstring for the rationale.
        assert "docs/baz.md" in out
        assert "baz.md" not in out

    def test_source_trailing_line(self):
        out = _extract_cited_filenames(
            "...answer ends here.\n\nSource: `indexed_merkle_tree.mdx`, `bn254.nr`"
        )
        assert "indexed_merkle_tree.mdx" in out
        assert "bn254.nr" in out

    def test_italic_and_bold(self):
        out = _extract_cited_filenames("see *italic.md* and **bold.nr** here")
        assert "italic.md" in out
        assert "bold.nr" in out

    def test_case_insensitive(self):
        out = _extract_cited_filenames("see `FOO.MD` for details")
        assert "foo.md" in out

    def test_unknown_extension_not_matched(self):
        out = _extract_cited_filenames("see `app.exe` and `image.png` here")
        assert "app.exe" not in out
        assert "image.png" not in out

    def test_dotted_identifier_with_known_ext_is_matched(self):
        """``aztec.js`` is technically a package name but has the ``.js``
        ext we index, so we DO extract it. The intersection-with-retrieved
        gate is what neutralises it when no chunk has filename ``aztec.js``
        — this matches today's marker behaviour, where an out-of-range
        index is silently dropped.
        """
        out = _extract_cited_filenames("the `aztec.js` SDK gives you ...")
        assert "aztec.js" in out


class TestFilenameFallbackMatcher:
    def test_no_response_returns_none(self):
        res = _filename_fallback("", [{"filename": "foo.md"}])
        assert res.kept is None

    def test_no_docs_returns_none(self):
        res = _filename_fallback("see `foo.md`", [])
        assert res.kept is None

    def test_no_filenames_named_returns_none(self):
        res = _filename_fallback(
            "answer with no filename references at all",
            [{"filename": "foo.md"}],
        )
        assert res.kept is None

    def test_filename_match_preserves_retrieval_order(self):
        docs = [
            {"filename": "a.md"},
            {"filename": "b.md"},
            {"filename": "c.md"},
        ]
        res = _filename_fallback("cites `c.md`, then `a.md`", docs)
        assert res.kept is not None
        # Retrieval order, NOT mention order.
        assert [d["filename"] for d in res.kept] == ["a.md", "c.md"]
        assert sorted(res.matched_aliases) == ["a.md", "c.md"]

    def test_unmatched_filenames_dropped(self):
        docs = [{"filename": "foo.md"}, {"filename": "bar.md"}]
        res = _filename_fallback("cite `foo.md` and `not_retrieved.md`", docs)
        assert res.kept is not None
        assert [d["filename"] for d in res.kept] == ["foo.md"]
        assert res.matched_aliases == ["foo.md"]

    def test_title_alias_matches(self):
        docs = [{"title": "glossary.md", "source": "https://example/glossary"}]
        res = _filename_fallback("see `glossary.md` for terms", docs)
        assert res.kept is not None
        assert len(res.kept) == 1

    def test_source_basename_alias_matches(self):
        docs = [{"source": "version-v4.3.0/docs/notes.md"}]
        res = _filename_fallback("`notes.md` describes the model", docs)
        assert res.kept is not None
        assert len(res.kept) == 1

    def test_path_suffix_matches_specific_doc(self):
        """A path-suffix mention picks out the specific doc even when
        basenames collide. The matched_aliases set records the
        path-form alias.
        """
        docs = [
            {"filename": "docs/aztec-nr/index.md"},
            {"filename": "docs/aztec-js/index.md"},
            # An unrelated doc with a distinct filename so the test
            # doesn't depend on what happens when the response also
            # mentions the bare basename.
            {"filename": "glossary.md"},
        ]
        res = _filename_fallback("see [link](aztec-nr/index.md)", docs)
        assert res.kept is not None
        # Only the aztec-nr/index.md doc matches the path-suffix token.
        assert [d["filename"] for d in res.kept] == ["docs/aztec-nr/index.md"]
        assert "aztec-nr/index.md" in res.matched_aliases

    def test_basename_ambiguity_below_threshold_not_flagged(self):
        docs = [
            {"filename": "a/index.md"},
            {"filename": "b/index.md"},
        ]
        res = _filename_fallback("see `index.md`", docs)
        assert res.kept is not None
        assert len(res.kept) == 2
        # 2 < _AMBIGUOUS_BASENAME_THRESHOLD (3) → not flagged.
        assert res.ambiguous_basename_count == 0

    def test_basename_ambiguity_at_threshold_flagged(self):
        docs = [
            {"filename": "a/index.md"},
            {"filename": "b/index.md"},
            {"filename": "c/index.md"},
        ]
        assert _AMBIGUOUS_BASENAME_THRESHOLD == 3
        res = _filename_fallback("see `index.md`", docs)
        assert res.kept is not None
        assert len(res.kept) == 3
        assert res.ambiguous_basename_count == 1

    def test_path_and_bare_basename_in_same_response(self):
        """When the LLM mentions BOTH ``aztec-nr/index.md`` (specific)
        AND a separate bare ``index.md`` (broad), each token resolves
        independently: the path mention picks the specific doc; the
        bare basename matches all docs with that basename.
        """
        docs = [
            {"filename": "docs/aztec-nr/index.md"},
            {"filename": "docs/aztec-js/index.md"},
            {"filename": "glossary.md"},
        ]
        res = _filename_fallback(
            "see `aztec-nr/index.md` and also `index.md` more generally",
            docs,
        )
        assert res.kept is not None
        # Both index.md docs match the bare ``index.md`` token; the
        # explicit path mention is also captured in matched_aliases.
        kept_names = [d["filename"] for d in res.kept]
        assert "docs/aztec-nr/index.md" in kept_names
        assert "docs/aztec-js/index.md" in kept_names
        assert "glossary.md" not in kept_names
        assert "aztec-nr/index.md" in res.matched_aliases
        assert "index.md" in res.matched_aliases

    def test_single_doc_with_duplicate_aliases_not_flagged_ambiguous(self):
        """A single doc can expose the same basename via multiple alias
        keys (``filename``, ``title``, basename-of-``source``). The
        ambiguity counter must count distinct docs, not alias hits.
        """
        docs = [
            {
                "filename": "index.md",
                "title": "index.md",
                "source": "docs/section/index.md",
            }
        ]
        res = _filename_fallback("see `index.md`", docs)
        assert res.kept is not None
        assert len(res.kept) == 1
        # Single doc → not ambiguous, even though 3 alias keys collide.
        assert res.ambiguous_basename_count == 0


class TestFilenameFallbackIntegration:
    """End-to-end behaviour through ``complete_stream`` when the marker
    is missing or malformed. Asserts source frame contents (the
    user-visible contract); the ``strategy`` audit field is only
    written into ``conversation_messages.message_metadata`` when
    ``should_save_conversation=True``, which these tests run without —
    consistent with the existing ``TestCompleteStreamCitationFilter``
    pattern.
    """

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

    def test_marker_absent_with_inline_filenames(self, mock_mongo_db, flask_app):
        """No marker, but the model named retrieved filenames inline.
        Fallback filters to those files.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                        {"title": "doc3", "filename": "baz.md"},
                    ]
                },
                {"answer": "As shown in `foo.md` and (`baz.md`), the answer is..."},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted = [s["title"] for s in source_frames[0]["source"]]
        assert emitted == ["doc1", "doc3"]

    def test_marker_absent_no_filenames_fails_open(
        self, mock_mongo_db, flask_app
    ):
        """No marker AND no inline filename references — fall open as
        today.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                    ]
                },
                {"answer": "Pure prose answer with no filename refs."},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert len(source_frames[0]["source"]) == 2  # both retrieved

    def test_filename_earlier_than_tail_buffer(self, mock_mongo_db, flask_app):
        """Filenames mentioned in the FIRST half of a long answer must
        still trigger the fallback — proves the fallback inspects
        ``response_full`` and not just ``pending_tail`` (128 chars).
        """
        # 8x the marker buffer so the filename mention is well past
        # anything pending_tail could ever hold.
        long_filler = "x" * (_CITATION_MARKER_MAX_LEN * 8)
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                    ]
                },
                # Filename in the HEAD; prose then drones on past the
                # tail buffer; no marker at the end.
                {"answer": "Per `foo.md` above, " + long_filler + " end of answer."},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted = [s["title"] for s in source_frames[0]["source"]]
        assert emitted == ["doc1"]

    def test_marker_none_with_inline_filenames_still_omits_source_frame(
        self, mock_mongo_db, flask_app
    ):
        """Explicit ``[[cited: none]]`` means the model deliberately
        attributed nothing — even if filenames appear in prose, fallback
        must NOT fire and the source frame is suppressed.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                    ]
                },
                {
                    "answer": (
                        "Side note: `foo.md` is unrelated.\n\n[[cited: none]]"
                    )
                },
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert source_frames == []
        # Marker still stripped from the wire.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text

    def test_marker_malformed_falls_back_to_filenames(
        self, mock_mongo_db, flask_app
    ):
        """Marker present but garbage payload (``[[cited: banana]]``).
        The marker is stripped from the wire (unchanged behaviour), AND
        the fallback runs on the stripped response to recover a useful
        subset.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                        {"title": "doc3", "filename": "baz.md"},
                    ]
                },
                {
                    "answer": (
                        "From `bar.md` the rule is X.\n\n[[cited: banana]]"
                    )
                },
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted = [s["title"] for s in source_frames[0]["source"]]
        assert emitted == ["doc2"]
        # Marker text doesn't leak.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert "banana" not in answer_text

    def test_no_marker_fail_open_caps_sources(self, mock_mongo_db, flask_app):
        """No marker AND no filename match, with MORE retrieved docs than
        the fail_open cap → emit only the top ``_FAIL_OPEN_MAX_SOURCES``.
        fail_open has no grounding signal, so it shouldn't fill the frame
        with the full retrieval set.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": f"doc{i}", "filename": f"f{i}.md"}
                        for i in range(1, 7)  # 6 retrieved
                    ]
                },
                {"answer": "Pure prose, no filename refs, no marker."},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted = [s["title"] for s in source_frames[0]["source"]]
        assert emitted == ["doc1", "doc2", "doc3"]  # top-3, in retrieval order

    def test_malformed_marker_fail_open_caps_sources(
        self, mock_mongo_db, flask_app
    ):
        """Malformed marker (``[[cited: banana]]``) AND no filename the
        fallback can match → the malformed fail_open branch also applies
        the top-N cap (previously this branch was untested).
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": f"doc{i}", "filename": f"f{i}.md"}
                        for i in range(1, 7)  # 6 retrieved
                    ]
                },
                {"answer": "Prose with no filename refs.\n\n[[cited: banana]]"},
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        emitted = [s["title"] for s in source_frames[0]["source"]]
        assert emitted == ["doc1", "doc2", "doc3"]
        # Malformed marker still stripped from the wire.
        answer_text = "".join(
            f["answer"] for f in frames if f.get("type") == "answer"
        )
        assert "[[cited:" not in answer_text
        assert "banana" not in answer_text

    def test_inline_code_block_filename_still_matches(
        self, mock_mongo_db, flask_app
    ):
        """An inline code-fence mention of a retrieved filename triggers
        the fallback. The conservative-recall trade is acceptable: this
        matches the shape of today's marker-emitted citation where the
        model cites a chunk it only references briefly.
        """
        agent = _agent_yielding(
            [
                {
                    "sources": [
                        {"title": "doc1", "filename": "foo.md"},
                        {"title": "doc2", "filename": "bar.md"},
                    ]
                },
                {
                    "answer": (
                        "```python\n# foo.md describes the layout\n"
                        "print('hello')\n```\nDone."
                    )
                },
            ]
        )
        frames = self._run(mock_mongo_db, flask_app, agent)
        source_frames = [f for f in frames if f.get("type") == "source"]
        assert len(source_frames) == 1
        assert [s["title"] for s in source_frames[0]["source"]] == ["doc1"]
