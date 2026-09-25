"""Tests for the pure-Python helpers in ``application.mcp_server.tools.logs``.

Covers:

* The Docker logs frame demultiplexer — Docker's logs endpoint
  multiplexes stdout/stderr in an 8-byte-header frame format unless
  the container has ``tty=true``.
* The grep regex helper — guards against absurdly long patterns and
  re-raises invalid regex with an operator-readable error.
* The service-allowlist enforcement on the ``LogQuery`` input.

The Docker Engine HTTP path is exercised in integration tests with a
mock ``responses``-backed proxy.
"""

from __future__ import annotations

import pytest

from application.mcp_server.tools.logs import (
    DEFAULT_SERVICES,
    LogQuery,
    LogReader,
    _demux_docker_log_frames,
    _filter_grep,
)


def _frame(stream: int, payload: bytes) -> bytes:
    """Build a single docker-logs frame from a stream+payload pair."""
    length = len(payload)
    header = bytes([stream, 0, 0, 0]) + length.to_bytes(4, "big")
    return header + payload


@pytest.mark.unit
class TestDemuxDockerLogFrames:
    def test_decodes_multiple_frames(self) -> None:
        body = (
            _frame(1, b"2026-05-09T10:00:00Z hello\n")
            + _frame(2, b"2026-05-09T10:00:01Z err line\n")
            + _frame(1, b"2026-05-09T10:00:02Z second stdout\n")
        )
        lines = _demux_docker_log_frames(body)
        assert lines == [
            "2026-05-09T10:00:00Z hello",
            "2026-05-09T10:00:01Z err line",
            "2026-05-09T10:00:02Z second stdout",
        ]

    def test_returns_empty_on_empty_input(self) -> None:
        assert _demux_docker_log_frames(b"") == []

    def test_falls_back_when_header_unrecognised(self) -> None:
        # First byte not in {0,1,2}: assume tty=true plain stream.
        plain = b"plain text without framing\n"
        assert _demux_docker_log_frames(plain) == ["plain text without framing"]


@pytest.mark.unit
class TestFilterGrep:
    def test_filters_matching_lines(self) -> None:
        lines = [
            "2026 INFO normal",
            "2026 ERROR boom",
            "2026 WARN something",
            "2026 ERROR another",
        ]
        assert _filter_grep(lines, "ERROR") == [
            "2026 ERROR boom",
            "2026 ERROR another",
        ]

    def test_supports_regex(self) -> None:
        lines = ["a1", "a22", "b1", "ab"]
        assert _filter_grep(lines, r"^a\d+$") == ["a1", "a22"]

    def test_rejects_invalid_regex(self) -> None:
        with pytest.raises(ValueError):
            _filter_grep(["x"], "(unbalanced")

    def test_rejects_overlong_pattern(self) -> None:
        with pytest.raises(ValueError):
            _filter_grep(["x"], "x" * 257)


@pytest.mark.unit
class TestServiceAllowlist:
    def test_default_services_match_compose(self) -> None:
        # Sanity: the compose service names that operators expect to
        # tail must all appear in DEFAULT_SERVICES so the default
        # construction of LogReader serves them.
        for s in (
            "backend",
            "worker",
            "postgres",
            "redis",
            "caddy",
            "discord-bot",
            "frontend-ask",
        ):
            assert s in DEFAULT_SERVICES

    def test_unknown_service_rejected(self) -> None:
        # Construct a reader without touching the network; tail() must
        # reject the service before any HTTP call is attempted.
        reader = LogReader(proxy_url="http://unused:0")
        with pytest.raises(ValueError):
            reader.tail(LogQuery(service="not-a-service"))
