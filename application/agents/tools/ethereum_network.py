"""Ethereum L1 network-status tool backed by public JSON-RPC endpoints.

We deliberately avoid wrapping the available "Ethereum MCP servers"
(e.g. ``John0n1/ethereum-mcp``) because the underlying payload is
identical JSON-RPC, and a sidecar would either need to be published as
an unauthenticated public Ethereum proxy or have ``mcp_tool``'s SSRF
guard relaxed for an internal compose hostname — both worse than a
direct request. The action surface mirrors what those MCP servers
expose so agent ergonomics are unchanged.

Two networks are supported because the Aztec L2 testnet anchors to
**Sepolia** (``l1ChainId=11155111``) rather than mainnet, so questions
about "the L1" are usually about Sepolia. ``network`` is a parameter
on every action; defaults to ``"mainnet"``.
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

_DEFAULT_MAINNET_RPC = "https://ethereum-rpc.publicnode.com"
_DEFAULT_SEPOLIA_RPC = "https://ethereum-sepolia-rpc.publicnode.com"
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 512 * 1024
_SUPPORTED_NETWORKS = ("mainnet", "sepolia")
_BLOCK_TAG_LITERALS = ("latest", "finalized", "safe", "earliest", "pending")
_HEX_BLOCK_RE = re.compile(r"^0x[0-9a-fA-F]+$")


class EthereumNetworkTool(Tool):
    """Query live Ethereum L1 state via JSON-RPC.

    Each action takes an optional ``network`` ("mainnet" or "sepolia")
    that selects between the two configured upstreams. Hex strings from
    JSON-RPC are decoded to base-10 ints inline so the LLM doesn't have
    to.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        # No validation here — not even structural. ToolManager eagerly
        # instantiates every tool in this package on every tool
        # execution, so a constructor that raises on a malformed env var
        # would break ALL tools for ALL agents, not just this one.
        # Validation (and the SSRF/DNS check) happens per request in
        # _resolve_rpc_url() / _rpc() and degrades to an error dict.
        self.config = config or {}

    def _resolve_rpc_url(self, net: str) -> str:
        """Resolve and structurally validate the RPC URL for *net*.

        Precedence: per-tool config > env var > publicnode default.
        Raises ``ValueError`` (caught by ``_rpc``) on malformed values;
        the message names the offending setting but never echoes its
        value — RPC URLs can embed provider tokens and these strings end
        up in tool results visible to the LLM.
        """
        if net == "mainnet":
            url = (
                self.config.get("mainnet_rpc_url")
                or os.environ.get("ETHEREUM_RPC_URL")
                or _DEFAULT_MAINNET_RPC
            )
            label = "mainnet (mainnet_rpc_url config / ETHEREUM_RPC_URL)"
        else:
            url = (
                self.config.get("sepolia_rpc_url")
                or os.environ.get("ETHEREUM_SEPOLIA_RPC_URL")
                or _DEFAULT_SEPOLIA_RPC
            )
            label = "sepolia (sepolia_rpc_url config / ETHEREUM_SEPOLIA_RPC_URL)"
        if not isinstance(url, str):
            raise ValueError(f"Ethereum {label} RPC URL must be a string")
        _validate_request_url(url, f"Ethereum {label} RPC URL")
        return url

    def execute_action(self, action_name: str, **kwargs: Any) -> dict[str, Any]:
        actions = {
            "ethereum_network_get_block_number": self._get_block_number,
            "ethereum_network_get_gas_price": self._get_gas_price,
            "ethereum_network_get_chain_id": self._get_chain_id,
            "ethereum_network_get_sync_status": self._get_sync_status,
            "ethereum_network_get_block": self._get_block,
        }
        if action_name not in actions:
            raise ValueError(f"Unknown action: {action_name}")
        return actions[action_name](**kwargs)

    def _rpc(self, network: Any, method: str, params: list[Any]) -> tuple[bool, int, Any]:
        """Issue a JSON-RPC POST. Returns ``(ok, status_code, value)``.

        - On success ``(True, 200, result)`` — ``result`` is whatever JSON-RPC
          returned (hex string, dict, bool, ...).
        - On failure ``(False, status_code, message)`` — ``status_code`` is 0
          for client-side validation, the upstream HTTP code for HTTP errors,
          and 200 for JSON-RPC-level errors.
        """
        if not isinstance(network, str) and network is not None:
            return False, 0, f"network must be a string, got {type(network).__name__}"
        net = (network or "mainnet").lower()
        if net not in _SUPPORTED_NETWORKS:
            return False, 0, f"Unsupported network: {network!r} (expected one of {_SUPPORTED_NETWORKS})"
        try:
            url = self._resolve_rpc_url(net)
        except ValueError as exc:
            return False, 0, f"configuration error: {exc}"
        # The RPC URL can embed a provider token (path / query /
        # userinfo). Redact the whole URL and every token-bearing
        # substring from anything that flows to logs or tool results.
        secrets = _redaction_secrets(url)
        try:
            validate_url(url)
        except SSRFError as exc:
            return False, 0, f"SSRF: {exc}"
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            # requests exception strings embed the request URL; redact.
            message = _redact_text(f"{type(exc).__name__}: {exc}", secrets)[:500]
            logger.warning("ethereum rpc transport failure network=%s err=%s", net, message)
            return False, 0, message
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                return False, 0, f"upstream attempted redirect (status {response.status_code})"
            body_bytes = response.raw.read(_MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(body_bytes) > _MAX_RESPONSE_BYTES:
                return False, response.status_code, "upstream response exceeded size cap"
            if response.status_code != 200:
                # Error bodies can echo the request path (e.g. Apache's
                # default 404 page) — redact before surfacing.
                text = body_bytes.decode("utf-8", errors="replace")[:500]
                return False, response.status_code, _redact_text(text, secrets)
            try:
                import json as _json
                body = _json.loads(body_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return False, 200, _redact_text(f"invalid JSON body: {exc}", secrets)
            if not isinstance(body, dict):
                # body is upstream-controlled; a hostile node could echo
                # the request token (e.g. an Infura "invalid project id"
                # string). Redact before it reaches the LLM/tool result.
                return False, 200, _redact_text(f"unexpected RPC body shape: {body!r}", secrets)
            if "error" in body and body["error"] is not None:
                return False, 200, _redact_text(f"{body['error']}", secrets)
            return True, 200, body.get("result")
        finally:
            response.close()

    @staticmethod
    def _err(status_code: int, message: str) -> dict[str, Any]:
        return {"status_code": status_code or 502, "message": message}

    @staticmethod
    def _hex_to_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value, 16) if value.startswith("0x") else int(value)
            except ValueError:
                return None
        return None

    def _get_block_number(self, network: str = "mainnet") -> dict[str, Any]:
        ok, status, value = self._rpc(network, "eth_blockNumber", [])
        if not ok:
            return {"status_code": status, "message": f"eth_blockNumber failed: {value}"}
        height = self._hex_to_int(value)
        if height is None:
            return self._err(200, "eth_blockNumber returned a non-hex value")
        return {"status_code": 200, "network": network, "block_number": height}

    def _get_gas_price(self, network: str = "mainnet") -> dict[str, Any]:
        ok, status, value = self._rpc(network, "eth_gasPrice", [])
        if not ok:
            return {"status_code": status, "message": f"eth_gasPrice failed: {value}"}
        wei = self._hex_to_int(value)
        if wei is None:
            return self._err(200, "eth_gasPrice returned a non-hex value")
        return {
            "status_code": 200,
            "network": network,
            "gas_price_wei": wei,
            "gas_price_gwei": round(wei / 1_000_000_000, 4),
        }

    def _get_chain_id(self, network: str = "mainnet") -> dict[str, Any]:
        ok, status, value = self._rpc(network, "eth_chainId", [])
        if not ok:
            return {"status_code": status, "message": f"eth_chainId failed: {value}"}
        chain_id = self._hex_to_int(value)
        if chain_id is None:
            return self._err(200, "eth_chainId returned a non-hex value")
        return {"status_code": 200, "network": network, "chain_id": chain_id}

    def _get_sync_status(self, network: str = "mainnet") -> dict[str, Any]:
        ok, status, value = self._rpc(network, "eth_syncing", [])
        if not ok:
            return {"status_code": status, "message": f"eth_syncing failed: {value}"}
        if value is False:
            return {"status_code": 200, "network": network, "syncing": False}
        if isinstance(value, dict):
            return {
                "status_code": 200,
                "network": network,
                "syncing": True,
                "current_block": self._hex_to_int(value.get("currentBlock")),
                "highest_block": self._hex_to_int(value.get("highestBlock")),
                "starting_block": self._hex_to_int(value.get("startingBlock")),
            }
        return self._err(200, "eth_syncing returned an unexpected value")

    def _get_block(
        self, block_tag: Any = "latest", network: str = "mainnet",
    ) -> dict[str, Any]:
        """Fetch a trimmed block summary. ``block_tag`` accepts ``latest``,
        ``finalized``, ``safe``, ``earliest``, ``pending``, or a hex
        block number like ``0x1234abcd``. We pass ``False`` for the
        second eth_getBlockByNumber arg so the response excludes the
        full transaction list (we only need header data here)."""
        if block_tag is None:
            tag = "latest"
        elif not isinstance(block_tag, str):
            return {
                "status_code": 0,
                "message": f"block_tag must be a string, got {type(block_tag).__name__}",
            }
        elif block_tag in _BLOCK_TAG_LITERALS:
            tag = block_tag
        elif _HEX_BLOCK_RE.match(block_tag):
            tag = block_tag
        else:
            return {
                "status_code": 0,
                "message": (
                    f"block_tag must be one of {_BLOCK_TAG_LITERALS} or a 0x-prefixed hex number, "
                    f"got {block_tag!r}"
                ),
            }
        ok, status, body = self._rpc(network, "eth_getBlockByNumber", [tag, False])
        if not ok:
            # body is the already-redacted error message from _rpc.
            return {"status_code": status, "message": f"eth_getBlockByNumber failed: {body}"}
        if not isinstance(body, dict):
            # body here is the raw upstream result — don't echo it.
            return self._err(200, "eth_getBlockByNumber returned an unexpected body shape")
        return {
            "status_code": 200,
            "network": network,
            "number": self._hex_to_int(body.get("number")),
            "hash": body.get("hash"),
            "parent_hash": body.get("parentHash"),
            "timestamp": self._hex_to_int(body.get("timestamp")),
            "gas_used": self._hex_to_int(body.get("gasUsed")),
            "gas_limit": self._hex_to_int(body.get("gasLimit")),
            "base_fee_per_gas": self._hex_to_int(body.get("baseFeePerGas")),
            "miner": body.get("miner"),
        }

    def get_actions_metadata(self) -> list[dict[str, Any]]:
        network_param = {
            "type": "string",
            "enum": list(_SUPPORTED_NETWORKS),
            "description": (
                "Which Ethereum network to query. 'mainnet' (chain id 1) "
                "or 'sepolia' (chain id 11155111 — the L1 the Aztec "
                "testnet anchors to). Defaults to mainnet. If the user is "
                "asking in the context of the Aztec testnet — established "
                "now OR earlier in the conversation — use 'sepolia'. An "
                "explicit 'mainnet' / 'live' reference in the current "
                "request overrides earlier testnet context."
            ),
        }
        base_props = {"network": network_param}
        return [
            {
                "name": "ethereum_network_get_block_number",
                "description": (
                    "Get the current Ethereum block height for either "
                    "mainnet or Sepolia. Returns {block_number: int}. "
                    "Sepolia is the L1 the Aztec testnet anchors to — "
                    "use network='sepolia' when the user is asking "
                    "about the L1 underneath an Aztec testnet feature."
                ),
                "parameters": {
                    "type": "object",
                    "properties": base_props,
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ethereum_network_get_gas_price",
                "description": (
                    "Get the current Ethereum gas price (eth_gasPrice). "
                    "Returns both wei and gwei. Use for 'what's gas "
                    "right now' questions. Pass network='sepolia' for "
                    "the L1 underneath the Aztec testnet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": base_props,
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ethereum_network_get_chain_id",
                "description": (
                    "Get the chain id reported by the configured RPC. "
                    "Returns {chain_id: int}. Mostly a sanity check."
                ),
                "parameters": {
                    "type": "object",
                    "properties": base_props,
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ethereum_network_get_sync_status",
                "description": (
                    "Get eth_syncing — false if the node is fully "
                    "synced, otherwise {currentBlock, highestBlock, "
                    "startingBlock}. The public RPC defaults are "
                    "always-synced full nodes; this is most useful "
                    "when an operator points the tool at their own "
                    "node."
                ),
                "parameters": {
                    "type": "object",
                    "properties": base_props,
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ethereum_network_get_block",
                "description": (
                    "Get a trimmed header for a specific block "
                    "(number, hash, timestamp, gas used/limit, base "
                    "fee, miner). Use this when the user asks about a "
                    "specific block. block_tag accepts 'latest', "
                    "'finalized', 'safe', 'earliest', 'pending', or a "
                    "hex block number like '0x12345abc'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **base_props,
                        "block_tag": {
                            "type": "string",
                            "pattern": r"^(latest|finalized|safe|earliest|pending|0x[0-9a-fA-F]+)$",
                            "description": (
                                "One of 'latest', 'finalized', 'safe', "
                                "'earliest', 'pending', or a 0x-prefixed "
                                "hex block number. Defaults to 'latest'."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]

    def get_config_requirements(self) -> dict[str, Any]:
        return {
            "mainnet_rpc_url": {
                "type": "string",
                "label": "Ethereum mainnet RPC URL",
                "description": (
                    "Override mainnet upstream. Defaults to the "
                    "ETHEREUM_RPC_URL env or publicnode.com."
                ),
                "required": False,
                "secret": True,
                "order": 1,
            },
            "sepolia_rpc_url": {
                "type": "string",
                "label": "Ethereum Sepolia RPC URL",
                "description": (
                    "Override Sepolia upstream. Defaults to "
                    "ETHEREUM_SEPOLIA_RPC_URL or publicnode.com."
                ),
                "required": False,
                "secret": True,
                "order": 2,
            },
        }


_FORBIDDEN_URL_CHARS_RE = re.compile(r"[\x00-\x20\x7f\\]")


def _validate_request_url(url: str, label: str) -> None:
    """Reject a request URL where urllib.parse and requests would
    disagree on the authority — an SSRF-validation bypass.

    They normalize backslashes, control characters, whitespace and
    userinfo differently, so the host ``validate_url()`` checks may not
    be the host ``requests`` dials (e.g. ``https://127.0.0.1\\@host``
    validates as ``host`` but dials ``127.0.0.1``). Reject all of those
    up front so the validated URL is byte-for-byte the dialed URL.
    Messages never echo the URL — it can carry a provider token.
    """
    if _FORBIDDEN_URL_CHARS_RE.search(url):
        raise ValueError(f"{label} must not contain control characters, whitespace, or backslashes")
    try:
        parsed = urlparse(url)
        scheme, netloc = parsed.scheme, parsed.netloc
        has_userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        # urlparse property access (port / IDNA host) can raise on
        # hostile input; the exception text embeds the URL, so never
        # surface it.
        raise ValueError(f"{label} is not a valid URL")
    if scheme not in ("http", "https") or not netloc:
        raise ValueError(f"{label} must be an http(s) URL")
    if has_userinfo or "@" in netloc:
        raise ValueError(f"{label} must not contain userinfo")


def _redaction_secrets(url: str) -> tuple[str, ...]:
    """Every substring of *url* that could carry a provider token.

    Providers embed credentials in the path (``infura.io/v3/<token>``),
    the query (``?apikey=<token>``), or userinfo (``user:pass@host``).
    We redact the whole URL, the whole path/query, AND each individual
    path segment / query value / userinfo component — so an upstream that
    echoes only the bare token (not the full path) is still caught. The
    bare hostname is not a secret (defaults are documented). Sorted
    longest-first so a short segment can't pre-empt a longer match.
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
    if parsed.username and len(parsed.username) >= 4:
        parts.add(parsed.username)
    if parsed.password:
        parts.add(parsed.password)
    if parsed.username or parsed.password:
        parts.add(parsed.netloc)
    return tuple(sorted((p for p in parts if p), key=len, reverse=True))


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every value in *secrets* appearing in *text*.

    ``requests`` exception strings embed the request URL (``Max retries
    exceeded with url: /v3/<token>``), and upstream HTTP error bodies can
    echo the request path or token — so neither may flow to warning logs
    or the LLM-visible tool result unredacted.
    """
    for secret in secrets:
        if secret and len(secret) > 1:
            text = text.replace(secret, "<redacted>")
    return text
