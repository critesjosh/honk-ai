"""Aztec L2 network-status tool backed by the public Aztecscan REST API.

Aztec now runs both a **mainnet** (anchored to Ethereum mainnet,
``l1ChainId=1``) and a **testnet** (anchored to Sepolia,
``l1ChainId=11155111``). Each action takes a ``network`` argument
(``"mainnet"`` / ``"testnet"``) that selects between the two Aztecscan
hosts; it defaults to ``"mainnet"`` (the live network).

The Aztecscan API does not yet have a sign-up flow; the placeholder key
``temporary-api-key`` is the documented public default. The key is sent
in the URL path (``/v1/{apiKey}/...``), not a header.

Per-network base URLs and the API key are operator-configurable per
``user_tools`` row, falling back to ``AZTECSCAN_MAINNET_BASE_URL`` /
``AZTECSCAN_TESTNET_BASE_URL`` / ``AZTECSCAN_API_KEY`` env vars, then to
the defaults below.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests

from application.agents.tools.base import Tool
from application.core.url_validation import SSRFError, validate_url

logger = logging.getLogger(__name__)

_DEFAULT_MAINNET_BASE_URL = "https://api.aztecscan.xyz/v1"
_DEFAULT_TESTNET_BASE_URL = "https://api.testnet.aztecscan.xyz/v1"
_DEFAULT_API_KEY = "temporary-api-key"
_SUPPORTED_NETWORKS = ("mainnet", "testnet")
_REQUEST_TIMEOUT_SECONDS = 15
# Cap upstream body size — Aztecscan can return large block lists and a
# malicious upstream could return arbitrary bytes. 512 KiB is comfortable
# for the largest block summary we surface (~5 KiB) with room to spare.
_MAX_RESPONSE_BYTES = 512 * 1024
# Aztec API key goes into the URL path. Reject anything containing
# path/query/fragment delimiters or control chars to avoid escapes.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


class AztecNetworkTool(Tool):
    """Query live Aztec L2 network state via Aztecscan.

    Action surface mirrors the most useful read-only endpoints from the
    Aztecscan OpenAPI spec — chain head, finalization status, chain info,
    validator totals, registered RPC nodes. Each action takes a
    ``network`` ("mainnet" / "testnet", default "mainnet") and returns a
    small dict shaped ``{status_code, network, ...data}`` or
    ``{status_code, message}`` on failure.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        # No validation here — not even structural. ToolManager eagerly
        # instantiates every tool in this package on every tool
        # execution, so a constructor that raises on a malformed env var
        # would break ALL tools for ALL agents, not just this one.
        # Validation (and the SSRF/DNS check) happens per request in
        # _resolve_settings() / _request() and degrades to an error dict.
        self.config = config or {}

    def _resolve_settings(self, network: Any) -> tuple[str, str, str]:
        """Resolve ``(base_url, api_key, net)`` for *network*.

        Precedence: per-tool config > env var > default. Raises
        ``ValueError`` (caught by ``_request``) on an unsupported network
        or malformed setting; the message names the offending setting but
        never echoes its value — these strings end up in tool results
        visible to the LLM.
        """
        if not isinstance(network, str) and network is not None:
            raise ValueError(f"network must be a string, got {type(network).__name__}")
        net = (network or "mainnet").lower()
        if net not in _SUPPORTED_NETWORKS:
            raise ValueError(f"Unsupported network: {network!r} (expected one of {_SUPPORTED_NETWORKS})")

        if net == "mainnet":
            base_url = (
                self.config.get("mainnet_base_url")
                or os.environ.get("AZTECSCAN_MAINNET_BASE_URL")
                or _DEFAULT_MAINNET_BASE_URL
            )
            label = "Aztecscan mainnet base URL (mainnet_base_url config / AZTECSCAN_MAINNET_BASE_URL)"
        else:
            base_url = (
                self.config.get("testnet_base_url")
                or os.environ.get("AZTECSCAN_TESTNET_BASE_URL")
                or _DEFAULT_TESTNET_BASE_URL
            )
            label = "Aztecscan testnet base URL (testnet_base_url config / AZTECSCAN_TESTNET_BASE_URL)"

        api_key = (
            self.config.get("api_key")
            or os.environ.get("AZTECSCAN_API_KEY")
            or _DEFAULT_API_KEY
        )
        if not isinstance(base_url, str):
            raise ValueError(f"{label} must be a string")
        # Reject the urllib-vs-requests authority divergences (backslash,
        # control chars, whitespace, userinfo) up front so the URL
        # validate_url() checks is byte-for-byte the URL requests dials.
        _validate_request_url(base_url, label)
        parsed = urlparse(base_url)
        if parsed.query or parsed.fragment:
            raise ValueError(f"{label} must not contain query or fragment")
        if not isinstance(api_key, str) or not _API_KEY_RE.match(api_key):
            raise ValueError(
                "Aztecscan API key (api_key config / AZTECSCAN_API_KEY) must "
                "match [A-Za-z0-9._-]{1,128} (it is interpolated into the URL path)"
            )
        return base_url.rstrip("/"), api_key, net

    def execute_action(self, action_name: str, **kwargs: Any) -> dict[str, Any]:
        actions = {
            "aztec_network_get_latest_height": self._get_latest_height,
            "aztec_network_get_latest_block": self._get_latest_block,
            "aztec_network_get_blocks_by_status": self._get_blocks_by_status,
            "aztec_network_get_chain_info": self._get_chain_info,
            "aztec_network_get_validator_totals": self._get_validator_totals,
            "aztec_network_get_rpc_nodes": self._get_rpc_nodes,
        }
        if action_name not in actions:
            raise ValueError(f"Unknown action: {action_name}")
        return actions[action_name](**kwargs)

    def _request(self, path: str, network: Any) -> tuple[int, Any, str | None]:
        """Issue a GET against the Aztecscan base for *network*. Returns
        ``(status_code, parsed_or_text, net)``. ``net`` is the normalized
        network (``None`` on a config error). Exceptions become a 0 status
        with a redacted exception string as the second tuple element."""
        try:
            base_url, api_key, net = self._resolve_settings(network)
        except ValueError as exc:
            return 0, f"configuration error: {exc}", None
        url = f"{base_url}/{api_key}{path}"
        # The full URL (and api_key path segment) is redacted from every
        # message that could surface upstream text or the request URL.
        secrets = _redaction_secrets(url) + (api_key,)
        # SSRF check is deferred to request time and re-runs on every
        # call: DNS rebinding mitigation, and resilient to transient
        # resolution failures at tool-construction time.
        try:
            validate_url(base_url)
        except SSRFError as exc:
            return 0, f"SSRF: {exc}", net
        try:
            response = requests.get(
                url,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                # A malicious / misconfigured public upstream could 302
                # to localhost or metadata. Disable redirects entirely
                # rather than re-validate each hop.
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            # requests exception strings embed the request URL — which
            # contains the API key as a path segment. Redact before the
            # message reaches logs or the LLM-visible tool result.
            message = _redact_text(f"{type(exc).__name__}: {exc}", secrets)[:500]
            logger.warning("aztecscan request failed network=%s path=%s err=%s", net, path, message)
            return 0, message, net
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                return 0, f"upstream attempted redirect (status {response.status_code})", net
            body_bytes = response.raw.read(_MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(body_bytes) > _MAX_RESPONSE_BYTES:
                # Mark as a fetch failure (status 0) — not a 200 with a
                # sentinel body, which an action would otherwise try to
                # parse as the real payload. The message is our own
                # constant, so it's safe to surface verbatim.
                return 0, "upstream response exceeded size cap", net
            if response.status_code != 200:
                # Error bodies can echo the request path (e.g. Apache's
                # default 404 page), and the path carries the API key.
                text = body_bytes.decode("utf-8", errors="replace")[:500]
                return response.status_code, _redact_text(text, secrets), net
            try:
                import json as _json
                return 200, _json.loads(body_bytes.decode("utf-8")), net
            except (ValueError, UnicodeDecodeError) as exc:
                # JSONDecodeError str embeds a body snippet; redact secrets.
                message = _redact_text(f"invalid JSON body: {exc}", secrets)
                logger.warning("aztecscan JSON decode failed network=%s path=%s err=%s", net, path, message)
                return 200, message, net
        finally:
            response.close()

    @staticmethod
    def _err(status_code: int, message: str, network: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"status_code": status_code, "message": message}
        if network is not None:
            out["network"] = network
        return out

    def _get_latest_height(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/latest-height", network)
        if status != 200:
            return self._err(status, f"Failed to fetch latest height: {body}", net)
        try:
            height = int(body)
        except (TypeError, ValueError):
            return self._err(200, "Unexpected latest-height payload (expected an integer)", net)
        return {"status_code": 200, "network": net, "height": height}

    def _get_latest_block(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/blocks/latest", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch latest block: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected latest-block body shape (expected an object)", net)
        header = body.get("header") or {}
        global_vars = header.get("globalVariables") or {}
        return {
            "status_code": 200,
            "network": net,
            "hash": body.get("hash"),
            "height": _coerce_int(body.get("height")),
            # Live Aztecscan reports this as ``nativeStatus`` (a string
            # like "finalized" / "proven" / "checkpointed"); tolerate the
            # older ``finalizationStatus`` name too.
            "finalization_status": body.get("nativeStatus") or body.get("finalizationStatus"),
            "total_fees": _coerce_int(header.get("totalFees")),
            "total_mana_used": _coerce_int(header.get("totalManaUsed")),
            "timestamp": _coerce_int(global_vars.get("timestamp")),
            "coinbase": global_vars.get("coinbase"),
        }

    def _get_blocks_by_status(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/blocks/by-status", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch blocks-by-status: {body}", net)
        if not isinstance(body, list):
            return self._err(502, "Unexpected blocks-by-status body shape (expected an array)", net)
        summary: dict[str, Any] = {"status_code": 200, "network": net}
        for entry in body:
            if not isinstance(entry, dict):
                continue
            # Live Aztecscan keys this as ``nativeStatus`` (a string like
            # "finalized" / "proven" / "checkpointed"); tolerate the older
            # numeric ``finalizationStatus`` too. Keep the raw stage label
            # so the LLM can reason without us pinning the enum semantics.
            stage = entry.get("nativeStatus")
            if stage is None:
                stage = entry.get("finalizationStatus")
            height = _coerce_int(entry.get("height"))
            if stage is None or height is None:
                continue
            summary[f"stage_{stage}_height"] = height
        return summary

    def _get_chain_info(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/info", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch chain info: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected chain-info body shape (expected an object)", net)
        l1_contracts = body.get("l1ContractAddresses") or {}
        return {
            "status_code": 200,
            "network": net,
            "l2_network_id": body.get("l2NetworkId"),
            "l1_chain_id": body.get("l1ChainId"),
            "rollup_version": body.get("rollupVersion"),
            "rollup_address": l1_contracts.get("rollupAddress"),
            "registry_address": l1_contracts.get("registryAddress"),
            "inbox_address": l1_contracts.get("inboxAddress"),
            "outbox_address": l1_contracts.get("outboxAddress"),
        }

    def _get_validator_totals(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l1/l2-validators/totals", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch validator totals: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected validator-totals body shape (expected an object)", net)
        return {"status_code": 200, "network": net, "totals": body}

    def _get_rpc_nodes(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/rpc-nodes", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch RPC nodes: {body}", net)
        if not isinstance(body, list):
            return self._err(502, "Unexpected rpc-nodes body shape (expected an array)", net)
        nodes = []
        for entry in body:
            if not isinstance(entry, dict):
                continue
            # Live Aztecscan keys these as rpcNodeName / nodeVersion /
            # lastSeenAt; tolerate the older name/version/lastSeen too.
            nodes.append(
                {
                    "name": entry.get("rpcNodeName") or entry.get("name"),
                    "version": entry.get("nodeVersion") or entry.get("version"),
                    "last_seen": (
                        entry.get("lastSeenAt")
                        or entry.get("lastSeen")
                        or entry.get("last_seen")
                    ),
                }
            )
        return {"status_code": 200, "network": net, "nodes": nodes}

    def get_actions_metadata(self) -> list[dict[str, Any]]:
        network_param = {
            "type": "string",
            "enum": list(_SUPPORTED_NETWORKS),
            "description": (
                "Which Aztec network to query. 'mainnet' (the live "
                "network, anchored to Ethereum mainnet / l1ChainId 1) or "
                "'testnet' (pre-production, anchored to Sepolia / "
                "l1ChainId 11155111). Defaults to mainnet — only pass "
                "'testnet' when the user explicitly asks about the Aztec "
                "testnet."
            ),
        }

        def params() -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {"network": network_param},
                "required": [],
                "additionalProperties": False,
            }

        return [
            {
                "name": "aztec_network_get_latest_height",
                "description": (
                    "Get the current Aztec L2 block height. Returns "
                    "{network, height}. Use this for 'what block is the "
                    "Aztec network on right now?'. Defaults to mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_latest_block",
                "description": (
                    "Get a summary of the most recent Aztec L2 block "
                    "(hash, height, finalization stage, fees, mana, "
                    "timestamp, coinbase). Use for detail on the chain "
                    "head. Defaults to mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_blocks_by_status",
                "description": (
                    "Get one block per L2 finalization stage so you can "
                    "see how far behind proven and L1-finalized blocks "
                    "are versus the chain head. Defaults to mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_chain_info",
                "description": (
                    "Get Aztec L2 chain metadata: network id, L1 chain "
                    "id (1 for mainnet, Sepolia 11155111 for testnet), "
                    "rollup version, and the L1 contract addresses "
                    "(rollup, registry, inbox, outbox). Defaults to "
                    "mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_validator_totals",
                "description": (
                    "Get the count of L2 validators by status (active, "
                    "pending, exited). The upstream endpoint is "
                    "occasionally unavailable; on failure the tool "
                    "returns a status_code/message pair. Defaults to "
                    "mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_rpc_nodes",
                "description": (
                    "List Aztec L2 RPC nodes with their last-seen "
                    "timestamp and version. Use to answer 'are RPC "
                    "nodes healthy?' questions. Defaults to mainnet."
                ),
                "parameters": params(),
            },
        ]

    def get_config_requirements(self) -> dict[str, Any]:
        return {
            "mainnet_base_url": {
                "type": "string",
                "label": "Aztecscan mainnet base URL",
                "description": (
                    "Override the Aztecscan mainnet API base. Defaults "
                    "to the AZTECSCAN_MAINNET_BASE_URL env or "
                    "api.aztecscan.xyz."
                ),
                "required": False,
                "secret": False,
                "order": 1,
            },
            "testnet_base_url": {
                "type": "string",
                "label": "Aztecscan testnet base URL",
                "description": (
                    "Override the Aztecscan testnet API base. Defaults "
                    "to the AZTECSCAN_TESTNET_BASE_URL env or "
                    "api.testnet.aztecscan.xyz."
                ),
                "required": False,
                "secret": False,
                "order": 2,
            },
            "api_key": {
                "type": "string",
                "label": "Aztecscan API key",
                "description": (
                    "Currently 'temporary-api-key' is the documented "
                    "public placeholder. Override via AZTECSCAN_API_KEY."
                ),
                "required": False,
                # The current default is a public placeholder, but a real
                # key is interpolated into the URL path and is redacted
                # everywhere else — keep the config UI consistent with that.
                "secret": True,
                "order": 3,
            },
        }


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_FORBIDDEN_URL_CHARS_RE = re.compile(r"[\x00-\x20\x7f\\]")


def _validate_request_url(url: str, label: str) -> None:
    """Reject a request URL where urllib.parse and requests would
    disagree on the authority — an SSRF-validation bypass.

    They normalize backslashes, control characters, whitespace and
    userinfo differently, so the host ``validate_url()`` checks may not
    be the host ``requests`` dials (e.g. ``https://127.0.0.1\\@host``
    validates as ``host`` but dials ``127.0.0.1``). Reject all of those
    up front so the validated URL is byte-for-byte the dialed URL.
    Messages never echo the URL — it can carry the API key.
    """
    if _FORBIDDEN_URL_CHARS_RE.search(url):
        raise ValueError(f"{label} must not contain control characters, whitespace, or backslashes")
    try:
        parsed = urlparse(url)
        scheme, netloc = parsed.scheme, parsed.netloc
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        # urlparse property access (port / IDNA host) can raise on
        # hostile input; the exception text embeds the URL, never show it.
        raise ValueError(f"{label} is not a valid URL")
    if scheme not in ("http", "https") or not netloc:
        raise ValueError(f"{label} must be an http(s) URL")
    if has_userinfo or "@" in netloc:
        raise ValueError(f"{label} must not contain userinfo")


def _redaction_secrets(url: str) -> tuple[str, ...]:
    """Every substring of *url* that could carry the API key.

    The Aztecscan key is a path segment (``/v1/<key>/...``). We redact
    the whole URL, the whole path, AND each individual path segment /
    query value — so an upstream that echoes only the bare key (not the
    full path) is still caught. The bare hostname is not a secret
    (defaults are documented). Sorted longest-first so a short segment
    can't pre-empt a longer match.
    """
    parsed = urlparse(url)
    parts: set[str] = {url}
    if parsed.path and parsed.path != "/":
        parts.add(parsed.path)
        parts.update(seg for seg in parsed.path.split("/") if len(seg) >= 4)
    if parsed.query:
        parts.add(parsed.query)
        for pair in parsed.query.split("&"):
            value = pair.split("=", 1)[-1]
            if len(value) >= 4:
                parts.add(value)
    return tuple(sorted((p for p in parts if p), key=len, reverse=True))


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every value in *secrets* appearing in *text*.

    ``requests`` exception messages embed the request URL (e.g.
    ``Max retries exceeded with url: /v1/<api-key>/l2/...``), and
    upstream HTTP error bodies can echo the request path or key — so a
    raw ``str(exc)`` or error body would leak the Aztecscan API key into
    warning logs and into the tool result the LLM (and ultimately the
    user) sees.
    """
    for secret in secrets:
        if secret and len(secret) > 1:
            text = text.replace(secret, "<redacted>")
    return text
