"""Tests for the SSE ``usage`` frame emitted on the success path of
``BaseAnswerResource.complete_stream``.

Two surfaces are covered:

1. ``_build_usage_frame`` — the module helper that lifts the agent's
   final ``llm.token_usage`` into an SSE payload. Tests cover:
   - happy path with both prompt + generated token counts present,
   - graceful skip when the agent has no LLM (test doubles),
   - graceful skip when ``token_usage`` is missing one of the fields,
   - ``model_id`` propagation.
2. ``process_response_stream`` — the OpenAI-compatible translator
   already parses ``/stream`` SSE for v1 endpoints. Asserts that an
   unknown ``type: "usage"`` frame doesn't break parsing or trip the
   ``KeyError`` path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from application.api.answer.routes.base import _build_usage_frame


class TestBuildUsageFrame:
    def test_happy_path_includes_all_fields(self):
        agent = SimpleNamespace(
            llm=SimpleNamespace(token_usage={"prompt_tokens": 1234, "generated_tokens": 567})
        )
        frame = _build_usage_frame(agent, "qwen/qwen3.6-flash")
        assert frame == {
            "type": "usage",
            "prompt_tokens": 1234,
            "generated_tokens": 567,
            "model_id": "qwen/qwen3.6-flash",
        }

    def test_model_id_can_be_none(self):
        # complete_stream falls back to self.default_model_id which
        # itself can be None in some test paths; the frame must still
        # emit so the client at least sees the token counts.
        agent = SimpleNamespace(
            llm=SimpleNamespace(token_usage={"prompt_tokens": 1, "generated_tokens": 1})
        )
        frame = _build_usage_frame(agent, None)
        assert frame is not None
        assert frame["model_id"] is None

    def test_no_llm_returns_none(self):
        # A test-double agent or a workflow path that didn't actually
        # build an LLM (e.g. an early bail in the retrieval phase).
        # Skipping the emit avoids forcing every test agent to grow a
        # fake ``llm.token_usage`` attribute.
        agent = SimpleNamespace()
        assert _build_usage_frame(agent, "any") is None

    def test_token_usage_wrong_shape_returns_none(self):
        # Defensive: a future refactor that changes the field type
        # (e.g. dataclass instead of dict) should not silently emit a
        # malformed frame to clients — better to skip + fix forward.
        agent = SimpleNamespace(llm=SimpleNamespace(token_usage="oops"))
        assert _build_usage_frame(agent, "any") is None

    def test_token_usage_missing_field_returns_none(self):
        agent = SimpleNamespace(
            llm=SimpleNamespace(token_usage={"prompt_tokens": 100})  # generated_tokens missing
        )
        assert _build_usage_frame(agent, "any") is None

    def test_non_int_field_returns_none(self):
        # gen_token_usage / stream_token_usage always store ints, but if
        # an LLM wrapper diverges (e.g. returns a float for streaming
        # estimates) we'd rather skip the frame than pass the wrong type
        # downstream into the bot's bucket arithmetic.
        agent = SimpleNamespace(
            llm=SimpleNamespace(
                token_usage={"prompt_tokens": 100, "generated_tokens": 12.5}
            )
        )
        assert _build_usage_frame(agent, "any") is None


class TestProcessResponseStreamBackCompat:
    """``BaseAnswerResource.process_response_stream`` is the
    OpenAI-compatible translator's parser for /stream events
    (application/api/v1/translator.py consumes it). Adding a new
    ``type: "usage"`` frame should be a no-op for this parser —
    unknown types fall out of the if/elif chain without raising.
    """

    def test_unknown_usage_type_silently_ignored(self):
        # Import lazily so this test file doesn't pay the route-module
        # cost when only the helper tests above are selected.
        from application.api.answer.routes.base import BaseAnswerResource

        resource = BaseAnswerResource.__new__(BaseAnswerResource)
        stream = [
            f'data: {json.dumps({"type": "id", "id": "abc-123"})}\n\n',
            f'data: {json.dumps({"type": "answer", "answer": "hello"})}\n\n',
            f'data: {json.dumps({"type": "usage", "prompt_tokens": 50, "generated_tokens": 5, "model_id": "qwen/qwen3.6-flash"})}\n\n',
            f'data: {json.dumps({"type": "end"})}\n\n',
        ]
        result = resource.process_response_stream(iter(stream))
        # The translator's caller cares about answer + id + a clean
        # success exit. The unknown usage frame must not corrupt any
        # of these.
        assert result["answer"] == "hello"
        assert result["conversation_id"] == "abc-123"
        assert result["error"] is None


@pytest.mark.unit
class TestUsageFrameOrdering:
    """The plan specifies SSE ordering ``id → usage → end`` on every
    success branch so a future client can correlate a usage tally to
    a ``conversation_id``. Codex flagged that the prior introspection
    test was too loose — it only proved each ``_build_usage_frame``
    had SOME earlier ``id`` and SOME later ``end`` anywhere in the
    function source, which would still pass if one branch dropped its
    usage emit. The tightened check below counts emit sites and
    asserts a strict 1:1 pairing of ``_build_usage_frame`` against
    branch-level ``id`` emits.
    """

    def test_usage_emit_count_matches_success_id_emit_count(self):
        import inspect

        from application.api.answer.routes import base as base_mod

        src = inspect.getsource(base_mod.BaseAnswerResource.complete_stream)
        # Two success-path id emits exist today: the paused tool-call
        # branch (emits id+end before continuing later) and the main
        # success branch (emits id, then the post-stream logging tail
        # ending in end). Both must precede a ``_build_usage_frame``
        # call before the next ``end`` they pair with — otherwise a
        # future refactor that adds a third success branch without an
        # accompanying usage emit will silently lose accounting.
        id_emits = src.count('{"type": "id", "id": str(conversation_id)}')
        usage_emits = src.count("_build_usage_frame(agent")
        end_emits = src.count('{"type": "end"}')
        assert id_emits == usage_emits, (
            f"Expected one _build_usage_frame call per id emit on success paths, "
            f"got id_emits={id_emits} usage_emits={usage_emits}. A new id-yielding "
            f"branch was added without a matching usage emit."
        )
        # End emits should at minimum equal id emits — one closing end
        # per id-bearing branch. May be higher if a failure path emits
        # its own end (it doesn't today).
        assert end_emits >= id_emits, (
            f"More id emits than end emits ({id_emits} vs {end_emits}); "
            f"a success branch is missing its end frame."
        )
