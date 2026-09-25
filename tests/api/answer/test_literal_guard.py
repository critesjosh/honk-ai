"""Tests for the literal-identifier guardrail.

The guard verifies long ``0x`` hex literals (contract addresses, tx/block
hashes, public keys) in grounded answers against an allowlist of what the
model was actually given — retrieved chunks, the question, prior turns, and
live tool results — correcting single-nibble corruptions and scrubbing
unverifiable literals. See ``application/api/answer/routes/base.py``.

Motivation (2026-06-29): a widget answer corrupted the $AZTEC contract
address by one nibble (``…245217Ff08…`` → ``…245217Df08…``) while the
retrieved chunk was correct — a funds-loss risk.
"""

from __future__ import annotations

import json
from typing import Iterable, List
from unittest.mock import MagicMock

from application.api.answer.routes.base import (
    _build_literal_allowlist,
    _guard_hex_literals,
    _hamming_within,
    _resolve_literal,
    _trailing_partial_hex_start,
    _UNVERIFIED_LITERAL_PLACEHOLDER,
)

# A real-shaped 40-nibble (Ethereum) address and its single-nibble corruption.
SRC_ADDR = "0xA27EC0006e59f245217Ff08CD52A7E8b169E62D2"
CORRUPT_ADDR = "0xA27EC0006e59f245217Df08CD52A7E8b169E62D2"  # Ff -> Df
# A 64-nibble (hash / AztecAddress) literal.
SRC_HASH = "0x" + "ab" * 32
CORRUPT_HASH = "0x" + "ab" * 31 + "ac"  # last nibble pair corrupted
# Wholly invented address, far from anything in the allowlist.
HALLUCINATED = "0x" + "1" * 40


def _allowlist_from(*texts: str):
    """Allowlist built from a fake agent whose retrieved_docs carry ``texts``."""
    agent = MagicMock()
    agent.retrieved_docs = [{"text": t} for t in texts]
    agent.chat_history = []
    agent.tool_calls = []
    return _build_literal_allowlist(agent, question=None)


class TestHammingWithin:
    def test_identical(self):
        assert _hamming_within("abcd", "abcd", 2)

    def test_single_substitution(self):
        assert _hamming_within("abcd", "abce", 2)

    def test_two_substitutions(self):
        assert _hamming_within("abcd", "abef", 2)

    def test_three_substitutions_exceeds(self):
        assert not _hamming_within("abcd", "axyz", 2)

    def test_case_insensitive(self):
        assert _hamming_within("0xABCD", "0xabcd", 0)

    def test_length_mismatch_is_false(self):
        # An insertion/deletion is not a near-miss — never guessed.
        assert not _hamming_within("abcd", "abcde", 2)


class TestTrailingPartialHex:
    def test_unterminated_run_returns_0x_index(self):
        text = "the address is 0xABCD1234"
        idx = _trailing_partial_hex_start(text)
        assert text[idx:] == "0xABCD1234"

    def test_bare_0x_prefix_held(self):
        text = "value: 0x"
        idx = _trailing_partial_hex_start(text)
        assert text[idx:] == "0x"

    def test_terminated_literal_not_held(self):
        assert _trailing_partial_hex_start("0xABCD1234 done") == -1

    def test_no_hex_tail(self):
        assert _trailing_partial_hex_start("plain prose") == -1

    def test_trailing_hex_without_0x_prefix(self):
        # ``deadbeef`` with no 0x prefix is not a literal start.
        assert _trailing_partial_hex_start("just deadbeef") == -1

    def test_bare_trailing_zero_held(self):
        # A lone trailing ``0`` may be the ``0`` of a ``0x`` whose ``x`` hasn't
        # streamed yet — hold it so the split never lands between ``0`` and ``x``.
        text = "the value is 0"
        assert _trailing_partial_hex_start(text) == len(text) - 1


class TestBuildLiteralAllowlist:
    def test_harvests_from_retrieved_docs(self):
        al = _allowlist_from(f"contract at {SRC_ADDR} is canonical")
        assert SRC_ADDR.lower() in al.literal_set

    def test_harvests_from_question(self):
        agent = MagicMock()
        agent.retrieved_docs = []
        agent.chat_history = []
        agent.tool_calls = []
        al = _build_literal_allowlist(agent, question=f"is {SRC_ADDR} right?")
        assert SRC_ADDR.lower() in al.literal_set

    def test_harvests_from_tool_results(self):
        agent = MagicMock()
        agent.retrieved_docs = []
        agent.chat_history = []
        # The aztec_network tool returns live addresses NOT in the corpus.
        agent.tool_calls = [{"result_full": f"node owner {SRC_ADDR}"}]
        al = _build_literal_allowlist(agent, question=None)
        assert SRC_ADDR.lower() in al.literal_set

    def test_harvests_from_chat_history(self):
        agent = MagicMock()
        agent.retrieved_docs = []
        agent.chat_history = [{"prompt": "q", "response": f"earlier {SRC_ADDR}"}]
        agent.tool_calls = []
        al = _build_literal_allowlist(agent, question=None)
        assert SRC_ADDR.lower() in al.literal_set

    def test_buckets_by_length_with_original_casing(self):
        al = _allowlist_from(SRC_ADDR)
        assert SRC_ADDR in al.by_length[len(SRC_ADDR)]


class TestResolveLiteral:
    def test_exact_match_verified(self):
        al = _allowlist_from(SRC_ADDR)
        verdict, repl = _resolve_literal(SRC_ADDR, al)
        assert verdict == "verified"
        assert repl is None

    def test_case_insensitive_exact_match_verified(self):
        al = _allowlist_from(SRC_ADDR.lower())
        verdict, _ = _resolve_literal(SRC_ADDR.upper().replace("0X", "0x"), al)
        assert verdict == "verified"

    def test_single_nibble_corruption_corrected(self):
        al = _allowlist_from(SRC_ADDR)
        verdict, repl = _resolve_literal(CORRUPT_ADDR, al)
        assert verdict == "corrected"
        assert repl == SRC_ADDR  # canonical source casing restored

    def test_hash_corruption_corrected(self):
        al = _allowlist_from(SRC_HASH)
        verdict, repl = _resolve_literal(CORRUPT_HASH, al)
        assert verdict == "corrected"
        assert repl == SRC_HASH

    def test_hallucinated_unverified(self):
        al = _allowlist_from(SRC_ADDR)
        verdict, repl = _resolve_literal(HALLUCINATED, al)
        assert verdict == "unverified"
        assert repl == _UNVERIFIED_LITERAL_PLACEHOLDER

    def test_ambiguous_nearmiss_scrubbed_not_guessed(self):
        # Two distinct allowlisted addresses both within distance 2 → ambiguous.
        a = "0x" + "a" * 40
        b = "0x" + "a" * 38 + "bb"
        target = "0x" + "a" * 39 + "b"  # distance 1 to a, distance 1 to b
        al = _allowlist_from(a, b)
        verdict, repl = _resolve_literal(target, al)
        assert verdict == "unverified"
        assert repl == _UNVERIFIED_LITERAL_PLACEHOLDER

    def test_empty_allowlist_unverified(self):
        al = _allowlist_from("no addresses here")
        verdict, _ = _resolve_literal(SRC_ADDR, al)
        assert verdict == "unverified"


class TestGuardHexLiterals:
    def test_verified_passthrough(self):
        al = _allowlist_from(SRC_ADDR)
        text = f"The contract is {SRC_ADDR}."
        res = _guard_hex_literals(text, al, apply=True)
        assert res.text == text
        assert res.checked == 1
        assert res.corrected == 0 and res.scrubbed == 0

    def test_enforce_corrects_corruption(self):
        al = _allowlist_from(SRC_ADDR)
        text = f"Use {CORRUPT_ADDR} to interact."
        res = _guard_hex_literals(text, al, apply=True)
        assert SRC_ADDR in res.text
        assert CORRUPT_ADDR not in res.text
        assert res.corrected == 1

    def test_enforce_scrubs_hallucinated(self):
        al = _allowlist_from(SRC_ADDR)
        text = f"Send to {HALLUCINATED} now."
        res = _guard_hex_literals(text, al, apply=True)
        assert HALLUCINATED not in res.text
        assert _UNVERIFIED_LITERAL_PLACEHOLDER in res.text
        assert res.scrubbed == 1

    def test_audit_counts_but_does_not_alter(self):
        al = _allowlist_from(SRC_ADDR)
        text = f"Use {CORRUPT_ADDR} and also {HALLUCINATED}."
        res = _guard_hex_literals(text, al, apply=False)
        assert res.text == text  # byte-identical
        assert res.corrected == 1
        assert res.scrubbed == 1
        assert res.checked == 2

    def test_short_hex_ignored(self):
        # 0xdeadbeef (8 nibbles) and a 4-byte selector are below the guard
        # threshold — never touched, even with an empty allowlist.
        al = _allowlist_from("nothing")
        text = "selector 0xdeadbeef and 0x12345678 are fine"
        res = _guard_hex_literals(text, al, apply=True)
        assert res.text == text
        assert res.checked == 0

    def test_tool_returned_address_preserved(self):
        # Address only in tool results (not docs) must survive enforce.
        agent = MagicMock()
        agent.retrieved_docs = []
        agent.chat_history = []
        agent.tool_calls = [{"result_full": f"coinbase {SRC_ADDR}"}]
        al = _build_literal_allowlist(agent, question=None)
        text = f"Latest block minted by {SRC_ADDR}."
        res = _guard_hex_literals(text, al, apply=True)
        assert res.text == text
        assert res.scrubbed == 0


# --------------------------------------------------------------------------
# Integration through complete_stream
# --------------------------------------------------------------------------


def _agent_yielding(events: Iterable[dict], **attrs) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.gen.return_value = iter(list(events))
    mock_agent.retrieved_docs = attrs.get("retrieved_docs", [])
    mock_agent.chat_history = attrs.get("chat_history", [])
    mock_agent.tool_calls = attrs.get("tool_calls", [])
    return mock_agent


def _stream_to_frames(raw: List[str]) -> List[dict]:
    frames = []
    for chunk in raw:
        for line in chunk.splitlines():
            line = line.strip()
            if line.startswith("data: "):
                try:
                    frames.append(json.loads(line[len("data: ") :]))
                except json.JSONDecodeError:
                    pass
    return frames


def _set_mode(monkeypatch, mode: str) -> None:
    from application.api.answer.routes import base as base_mod

    monkeypatch.setattr(base_mod.settings, "LITERAL_GUARD_MODE", mode, raising=False)


def _run(flask_app, agent: MagicMock) -> List[dict]:
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


def _answer_text(frames: List[dict]) -> str:
    return "".join(f["answer"] for f in frames if f.get("type") == "answer")


class TestCompleteStreamLiteralGuard:
    def test_enforce_corrects_corrupted_address_in_stream(self, monkeypatch, mock_mongo_db, flask_app):
        _set_mode(monkeypatch, "enforce")
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"The token contract is {CORRUPT_ADDR}.\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[{"text": f"Contract Address {SRC_ADDR}"}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert SRC_ADDR in text
        assert CORRUPT_ADDR not in text

    def test_enforce_scrubs_hallucinated_address(self, monkeypatch, mock_mongo_db, flask_app):
        _set_mode(monkeypatch, "enforce")
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"Send funds to {HALLUCINATED} immediately.\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[{"text": f"the real one is {SRC_ADDR}"}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert HALLUCINATED not in text
        assert _UNVERIFIED_LITERAL_PLACEHOLDER in text

    def test_audit_mode_leaves_bytes_but_records_metadata(self, monkeypatch, mock_mongo_db, flask_app):
        _set_mode(monkeypatch, "audit")
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"Contract {CORRUPT_ADDR}.\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[{"text": f"Contract Address {SRC_ADDR}"}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        # Audit never alters bytes — the corrupted address is still shown.
        assert CORRUPT_ADDR in text

    def test_partial_address_split_across_deltas_corrected(self, monkeypatch, mock_mongo_db, flask_app):
        """A corrupted address streamed in pieces must reassemble and be
        corrected — never emitted half-formed."""
        _set_mode(monkeypatch, "enforce")
        head, tail = CORRUPT_ADDR[:20], CORRUPT_ADDR[20:]
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"addr {head}"},
                {"answer": f"{tail} end\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[{"text": SRC_ADDR}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert SRC_ADDR in text
        assert CORRUPT_ADDR not in text
        # No half-literal leaked in any individual frame.
        for f in frames:
            if f.get("type") == "answer":
                assert CORRUPT_ADDR[:20] not in f["answer"] or SRC_ADDR in text

    def test_tool_returned_address_survives_enforce(self, monkeypatch, mock_mongo_db, flask_app):
        _set_mode(monkeypatch, "enforce")
        agent = _agent_yielding(
            [
                {"sources": [{"title": "networks.md"}]},
                {"answer": f"Latest block minted by {SRC_ADDR}.\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[],
            tool_calls=[{"result_full": f"coinbase {SRC_ADDR}"}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert SRC_ADDR in text
        assert _UNVERIFIED_LITERAL_PLACEHOLDER not in text

    def test_off_mode_is_byte_identical(self, monkeypatch, mock_mongo_db, flask_app):
        _set_mode(monkeypatch, "off")
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"Hallucinated {HALLUCINATED}.\n\n[[cited: 1]]"},
            ],
            retrieved_docs=[{"text": SRC_ADDR}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert HALLUCINATED in text  # disabled → untouched

    def test_abort_path_guards_persisted_partial(self, monkeypatch, mock_mongo_db, flask_app):
        """On client disconnect (GeneratorExit) in enforce mode, the streamed
        bytes were already corrected — the persisted partial response must
        match, not keep the corrupted literal."""
        from application.api.answer.routes import base as base_mod
        from application.api.answer.routes.base import BaseAnswerResource

        _set_mode(monkeypatch, "enforce")
        monkeypatch.setattr(base_mod.LLMCreator, "create_llm", lambda *a, **k: MagicMock())
        # Long answer so the first delta emits (suspending the generator at a
        # yield) with the corrupted address already in response_full.
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"Contract {CORRUPT_ADDR}. " + "padding " * 40},
                {"answer": "tail"},
            ],
            retrieved_docs=[{"text": SRC_ADDR}],
        )
        captured = {}

        with flask_app.app_context():
            resource = BaseAnswerResource()
            resource.conversation_service = MagicMock()
            resource.conversation_service.save_conversation.side_effect = (
                lambda *a, **k: captured.update(response=a[2]) or "cid"
            )
            gen = resource.complete_stream(
                question="Q",
                agent=agent,
                conversation_id="c1",
                user_api_key=None,
                decoded_token={"sub": "u"},
                should_save_conversation=True,
            )
            next(gen)  # advance to the first emitted answer frame
            gen.close()  # trigger GeneratorExit → abort persistence path

        assert "response" in captured, "save_conversation not called on abort"
        assert SRC_ADDR in captured["response"]
        assert CORRUPT_ADDR not in captured["response"]

    def test_enforce_split_between_0_and_x_not_leaked(self, monkeypatch, mock_mongo_db, flask_app):
        """Regression: when the stream split lands exactly between the ``0`` and
        ``x`` of an address (``0`` emitted alone, ``x…`` held back), the rest of
        the literal had no ``0x`` prefix left to match and leaked unguarded. The
        bare-``0`` hold-back must keep the literal whole and correct it."""
        from application.api.answer.routes.base import _CITATION_MARKER_MAX_LEN

        _set_mode(monkeypatch, "enforce")
        # Pad so the marker window forces the first split onto the address's
        # leading ``0`` (split_at == 1).
        pad = "q" * (_CITATION_MARKER_MAX_LEN + 1 - len(CORRUPT_ADDR))
        agent = _agent_yielding(
            [
                {"sources": [{"title": "token.md"}]},
                {"answer": f"{CORRUPT_ADDR}{pad}"},
            ],
            retrieved_docs=[{"text": SRC_ADDR}],
        )
        frames = _run(flask_app, agent)
        text = _answer_text(frames)
        assert SRC_ADDR in text
        assert CORRUPT_ADDR not in text
