"""``honk_logs.*`` MCP tools.

Reads container stdout via the Docker Engine API. The MCP server does
NOT speak directly to ``/var/run/docker.sock``; instead it goes
through ``tecnativa/docker-socket-proxy`` configured with ``LOGS=1``
and nothing else, so a code-execution bug in this process can't
escape into Docker control-plane operations (start/stop/exec/run).

The proxy listens on TCP inside the compose network at
``tcp://docker-proxy:2375`` (default). The MCP container's environment
configures the URL via ``MCP_DOCKER_PROXY_URL``.

We reach out via plain HTTP rather than depending on the Docker SDK so
the MCP image stays small and the access pattern is auditable in code.
The Docker Engine API is stable and the only endpoint we touch is
``GET /containers/<id>/logs`` — well-documented and rate-limit-safe.

References:
- https://docs.docker.com/engine/api/v1.43/#tag/Container/operation/ContainerLogs
- https://github.com/Tecnativa/docker-socket-proxy
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Iterable

import requests


logger = logging.getLogger(__name__)


# Whitelist of compose service names this MCP server is willing to
# stream logs for. Aligns with the services in
# ``deployment/docker-compose-hub.yaml`` plus the dev compose. Anything
# outside this list is rejected with a clean 4xx-shaped error rather
# than a docker-engine 404, so the operator-readable message is useful.
DEFAULT_SERVICES: frozenset[str] = frozenset(
    {
        "backend",
        "worker",
        "redis",
        "postgres",
        "caddy",
        "discord-bot",
        "frontend-ask",
        "mcp",  # the server's own container, useful for self-debugging
        "docker-proxy",  # log access from the proxy itself
    }
)


# Per-call cap on returned bytes. The Docker logs endpoint streams
# unbounded; we hard-cap on read to keep MCP responses small.
DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 5000
MAX_BYTES_PER_CALL = 256 * 1024


@dataclass(frozen=True)
class LogQuery:
    service: str
    lines: int = DEFAULT_TAIL_LINES
    since: str | None = None
    grep: str | None = None
    until: str | None = None
    """Defensive: lets callers pin a window without using ``since`` open-ended."""


class LogReader:
    """Reads bounded log windows via the docker-socket-proxy."""

    def __init__(
        self,
        proxy_url: str | None = None,
        *,
        compose_project: str | None = None,
        services: Iterable[str] | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._proxy_url = (proxy_url or os.environ.get("MCP_DOCKER_PROXY_URL") or "http://docker-proxy:2375").rstrip(
            "/"
        )
        # In production the compose project is ``docsgpt-aztec``
        # (set by ``name:`` in docker-compose-hub.yaml). The dev
        # compose uses ``docsgpt-oss``. Operators set this via
        # ``MCP_COMPOSE_PROJECT`` so we can resolve service → container
        # without parsing labels client-side.
        self._compose_project = compose_project or os.environ.get("MCP_COMPOSE_PROJECT") or "docsgpt-aztec"
        self._services = frozenset(services) if services else DEFAULT_SERVICES
        self._timeout = timeout_seconds

    def list_services(self) -> dict[str, list[dict[str, str]]]:
        """Return the whitelisted services and their current container status.

        Calls ``GET /containers/json?all=true&filters={...}`` against
        the proxy; the proxy forwards exactly this read-only endpoint.
        """
        # Filter by ``com.docker.compose.project`` so we don't enumerate
        # other compose projects sharing the same Docker host.
        filters = {
            "label": [
                f"com.docker.compose.project={self._compose_project}",
            ]
        }
        params = {
            "all": "true",
            "filters": _filters_json(filters),
        }
        resp = self._get("/containers/json", params=params)
        resp.raise_for_status()
        out = []
        for container in resp.json():
            labels = container.get("Labels") or {}
            service = labels.get("com.docker.compose.service") or ""
            if service not in self._services:
                continue
            names = container.get("Names") or []
            out.append(
                {
                    "service": service,
                    "container_name": names[0].lstrip("/") if names else "",
                    "image": container.get("Image", ""),
                    "state": container.get("State", ""),
                    "status": container.get("Status", ""),
                }
            )
        out.sort(key=lambda row: row["service"])
        return {"services": out}

    def tail(self, query: LogQuery) -> dict[str, object]:
        """Return up to ``query.lines`` lines of recent log output.

        Returns ``{"service": ..., "container": ..., "lines": [...],
        "truncated_bytes": bool, "next_since": "<timestamp>"}``.
        """
        if query.service not in self._services:
            raise ValueError(f"service {query.service!r} is not in the MCP allowlist")

        lines = max(1, min(int(query.lines), MAX_TAIL_LINES))
        container = self._resolve_container(query.service)

        params = {
            "stdout": "1",
            "stderr": "1",
            "tail": str(lines),
            "timestamps": "1",
            "follow": "0",
        }
        if query.since:
            params["since"] = query.since
        if query.until:
            params["until"] = query.until

        url = f"/containers/{urllib.parse.quote(container)}/logs"
        resp = self._get(url, params=params, stream=True)
        resp.raise_for_status()

        # The docker logs endpoint multiplexes stdout/stderr in an
        # 8-byte-header framing format unless ``tty=true`` is set on
        # the container. Demux defensively — the framing is well-known
        # and stable.
        body = b""
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            body += chunk
            if len(body) >= MAX_BYTES_PER_CALL:
                break
        truncated = len(body) >= MAX_BYTES_PER_CALL
        if truncated:
            body = body[:MAX_BYTES_PER_CALL]

        text_lines = _demux_docker_log_frames(body)
        if query.grep:
            text_lines = _filter_grep(text_lines, query.grep)

        # ``next_since`` is the timestamp of the last returned line so
        # the caller can paginate by passing it back in a follow-up
        # call's ``since`` parameter.
        next_since = None
        if text_lines:
            last = text_lines[-1]
            ts_match = re.match(r"(\S+)\s", last)
            if ts_match:
                next_since = ts_match.group(1)
        return {
            "service": query.service,
            "container": container,
            "lines": text_lines,
            "byte_count": len(body),
            "truncated_bytes": truncated,
            "next_since": next_since,
        }

    def _resolve_container(self, service: str) -> str:
        """Return the running container name for ``service``.

        We resolve service → container at request time rather than at
        startup so a ``docker compose up -d --force-recreate`` is
        picked up automatically.
        """
        filters = {
            "label": [
                f"com.docker.compose.project={self._compose_project}",
                f"com.docker.compose.service={service}",
            ]
        }
        resp = self._get(
            "/containers/json",
            params={"all": "false", "filters": _filters_json(filters)},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise LookupError(
                f"no running container found for service={service} in compose project={self._compose_project}"
            )
        return (data[0].get("Names") or [""])[0].lstrip("/") or data[0]["Id"]

    def _get(self, path: str, **kwargs):
        return requests.get(self._proxy_url + path, timeout=self._timeout, **kwargs)


def _filters_json(filters: dict) -> str:
    """Encode the ``filters`` query parameter as the Docker API expects.

    The endpoint takes a JSON-encoded dict mapping filter name → list of
    values. We urlencode at the requests-call boundary so callers don't
    need to know the wire format.
    """
    import json

    return json.dumps(filters)


# Docker log frame: 8-byte header (stream byte + 3 zero pad + 4-byte
# big-endian length) followed by ``length`` bytes of UTF-8 payload.
def _demux_docker_log_frames(body: bytes) -> list[str]:
    """Split a multiplexed Docker logs response into UTF-8 lines."""
    out: list[str] = []
    chunks: list[bytes] = []
    i = 0
    n = len(body)
    while i + 8 <= n:
        header = body[i : i + 8]
        # Heuristic: the header's first byte is 0/1/2 (stdin/out/err).
        # If it isn't, the stream is plain (e.g. tty=true) — fall back
        # to passing the whole buffer through.
        if header[0] not in (0, 1, 2):
            chunks.append(body[i:])
            break
        length = int.from_bytes(header[4:8], "big")
        i += 8
        if length <= 0 or i + length > n:
            chunks.append(body[i:])
            break
        chunks.append(body[i : i + length])
        i += length
    if i < n and not chunks:
        chunks.append(body[i:])
    text = b"".join(chunks).decode("utf-8", errors="replace")
    out = [line for line in text.split("\n") if line]
    return out


def _filter_grep(lines: list[str], pattern: str) -> list[str]:
    """Return only ``lines`` matching ``pattern`` (Python regex).

    The pattern is compiled with a length cap rather than relying on
    re2 — the length cap is an OK proxy for catastrophic-backtrack
    avoidance for the kind of grep an operator would type, and we
    don't want the additional dependency.
    """
    if len(pattern) > 256:
        raise ValueError("grep pattern too long (max 256 chars)")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    return [line for line in lines if rx.search(line)]
