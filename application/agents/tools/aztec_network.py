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
from urllib.parse import unquote_plus, urlencode, urlparse

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
# LLM-supplied identifiers (contract addresses, hashes) are interpolated
# into the URL path. Aztec field elements are 32-byte hex (0x + 64 hex);
# allow up to 128 hex chars for headroom. The strict charset rejects path
# delimiters, userinfo, traversal, whitespace, and control chars, so a
# validated id can never alter the authority or escape the path segment.
_HEX_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,128}$")
# Governance proposal states are short alpha labels (Queued / Pending /
# Active / Executed / ...). Sent as a query value.
_GOV_STATE_RE = re.compile(r"^[A-Za-z]{1,32}$")
# Reject control chars in a free-text search query before it is
# urlencoded. (urlencode *escapes* these rather than dropping them, so we
# reject up front to keep the value clean in logs/redaction.)
_FORBIDDEN_QUERY_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_SEARCH_QUERY_LEN = 256
# Cap how many entries we surface from list endpoints so a large upstream
# response can't bloat the tool result the LLM has to read.
_SEARCH_CATEGORY_CAP = 10
_GOVERNANCE_CAP = 20
# Aztecscan's /l2/blocks endpoint rejects a from/to window wider than 20
# blocks (HTTP 400 "Range too wide"); we validate against this client-side
# so the LLM gets a clear message before a wasted request, and cap the
# no-range "latest blocks" response to the same size.
_BLOCKS_WINDOW_CAP = 20
_REORGS_CAP = 10
_BLOCK_TX_HASHES_CAP = 10
# Aztecscan reports validator status as the staking-contract enum ordinal
# (its OpenAPI spec also admits the string labels, so tolerate both).
_VALIDATOR_STATUS_LABELS = {0: "NONE", 1: "VALIDATING", 2: "LIVING", 3: "EXITING"}


class AztecNetworkTool(Tool):
    """Query live Aztec L2 network state via Aztecscan.

    Action surface mirrors the most useful read-only endpoints from the
    Aztecscan OpenAPI spec — chain head, recent block windows,
    finalization status, chain info, validator totals, registered RPC
    nodes. Each action takes a
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
            "aztec_network_get_blocks": self._get_blocks,
            "aztec_network_get_blocks_by_status": self._get_blocks_by_status,
            "aztec_network_get_chain_info": self._get_chain_info,
            "aztec_network_get_validator_totals": self._get_validator_totals,
            "aztec_network_get_rpc_nodes": self._get_rpc_nodes,
            "aztec_network_search": self._search,
            "aztec_network_get_contract_instance": self._get_contract_instance,
            "aztec_network_get_governance_proposals": self._get_governance_proposals,
            "aztec_network_get_tips": self._get_tips,
            "aztec_network_get_tx_effect": self._get_tx_effect,
            "aztec_network_get_block": self._get_block_by_id,
            "aztec_network_get_validator": self._get_validator,
            "aztec_network_get_reorgs": self._get_reorgs,
        }
        if action_name not in actions:
            raise ValueError(f"Unknown action: {action_name}")
        return actions[action_name](**kwargs)

    def _request(
        self, path: str, network: Any, query: dict[str, str] | None = None
    ) -> tuple[int, Any, str | None]:
        """Issue a GET against the Aztecscan base for *network*. Returns
        ``(status_code, parsed_or_text, net)``. ``net`` is the normalized
        network (``None`` on a config error). Exceptions become a 0 status
        with a redacted exception string as the second tuple element.

        *path* is always built internally (callers interpolate only
        charset-validated identifiers via ``_validate_hex_id``); *query*
        is the only way a ``?`` reaches the URL, and its values are
        urlencoded. The path grammar is asserted here so a future caller
        can't smuggle a query/fragment or authority confusion through the
        path and bypass the base-URL SSRF validation."""
        try:
            base_url, api_key, net = self._resolve_settings(network)
        except ValueError as exc:
            return 0, f"configuration error: {exc}", None
        if not path.startswith("/") or _FORBIDDEN_PATH_CHARS_RE.search(path):
            # Our own constant message — no caller value echoed.
            return 0, "internal error: malformed request path", net
        url = f"{base_url}/{api_key}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
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

    def _get_blocks(
        self,
        from_height: Any = None,
        to_height: Any = None,
        network: str = "mainnet",
    ) -> dict[str, Any]:
        """Fetch a window of recent blocks (each with its timestamp).

        With no range, returns the latest blocks (capped at
        ``_BLOCKS_WINDOW_CAP``). With both ``from_height`` and
        ``to_height``, returns that inclusive height window. The
        LLM-facing range is inclusive; Aztecscan's ``/l2/blocks`` uses a
        half-open ``[from, to)`` range, so the upper bound is translated
        to ``to_height + 1``.
        """
        query: dict[str, str] | None = None
        if from_height is not None or to_height is not None:
            # An incomplete range is ambiguous — require both bounds so we
            # never silently widen to "latest" when only one was given.
            if from_height is None or to_height is None:
                return self._err(
                    0,
                    "invalid range: pass both from_height and to_height, or "
                    "neither (to get the latest blocks)",
                )
            try:
                lo = _validate_block_height(from_height, "from_height")
                hi = _validate_block_height(to_height, "to_height")
            except ValueError as exc:
                return self._err(0, f"invalid range: {exc}")
            if lo > hi:
                return self._err(0, "invalid range: from_height must be <= to_height")
            if hi - lo + 1 > _BLOCKS_WINDOW_CAP:
                return self._err(
                    0,
                    f"invalid range: window must be at most {_BLOCKS_WINDOW_CAP} "
                    "blocks (from_height..to_height inclusive)",
                )
            # Translate the inclusive LLM range to the upstream half-open one.
            query = {"from": str(lo), "to": str(hi + 1)}
        status, body, net = self._request("/l2/blocks", network, query=query)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch blocks: {body}", net)
        if not isinstance(body, list):
            return self._err(502, "Unexpected blocks body shape (expected an array)", net)
        entries = [e for e in body if isinstance(e, dict)]
        blocks = []
        for entry in entries[:_BLOCKS_WINDOW_CAP]:
            # Guard both nesting levels: a malformed upstream entry could
            # set header / globalVariables to a truthy non-dict, and an
            # unguarded .get() there would raise mid-stream.
            header = entry.get("header")
            header = header if isinstance(header, dict) else {}
            global_vars = header.get("globalVariables")
            global_vars = global_vars if isinstance(global_vars, dict) else {}
            blocks.append(
                {
                    "height": _coerce_int(entry.get("height")),
                    "timestamp": _coerce_int(global_vars.get("timestamp")),
                    "hash": entry.get("hash"),
                }
            )
        return {
            "status_code": 200,
            "network": net,
            "blocks": blocks,
            "returned_count": len(blocks),
            "truncated": len(entries) > _BLOCKS_WINDOW_CAP,
        }

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

    def _search(self, query: Any = None, network: str = "mainnet") -> dict[str, Any]:
        try:
            q = _validate_search_query(query)
        except ValueError as exc:
            # Validation runs before any request, so the network is not yet
            # resolved. The message names the field but never echoes the
            # rejected value.
            return self._err(0, f"invalid query: {exc}")
        status, body, net = self._request("/l2/search", network, query={"q": q})
        if status != 200:
            return self._err(status or 502, f"Failed to search: {body}", net)
        if not isinstance(body, dict) or not isinstance(body.get("results"), dict):
            return self._err(502, "Unexpected search body shape (expected a results object)", net)
        matches: dict[str, Any] = {}
        total = 0
        truncated = False
        for category, entries in body["results"].items():
            if isinstance(entries, list) and entries:
                total += len(entries)
                if len(entries) > _SEARCH_CATEGORY_CAP:
                    truncated = True
                matches[category] = entries[:_SEARCH_CATEGORY_CAP]
        return {
            "status_code": 200,
            "network": net,
            "search_phrase": body.get("searchPhrase"),
            "matches": matches,
            "total_matches": total,
            "truncated": truncated,
        }

    def _get_contract_instance(self, address: Any = None, network: str = "mainnet") -> dict[str, Any]:
        try:
            addr = _validate_hex_id(address, "address")
        except ValueError as exc:
            return self._err(0, f"invalid address: {exc}")
        status, body, net = self._request(f"/l2/contract-instances/{addr}", network)
        if status == 404:
            return self._err(404, "Contract instance not found", net)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch contract instance: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected contract-instance body shape (expected an object)", net)
        return {
            "status_code": 200,
            "network": net,
            "address": body.get("address"),
            "block_hash": body.get("blockHash"),
            "version": body.get("version"),
            # Live Aztecscan exposes both currentContractClassId and a
            # plain contractClassId; prefer the explicit "current".
            "contract_class_id": body.get("currentContractClassId") or body.get("contractClassId"),
            "original_contract_class_id": body.get("originalContractClassId"),
            "deployer": body.get("deployer"),
            "initialization_hash": body.get("initializationHash"),
            "artifact_contract_name": body.get("artifactContractName"),
            "standard_contract_type": body.get("standardContractType"),
            "source_code_url": body.get("sourceCodeUrl"),
            "is_orphaned": body.get("isOrphaned"),
        }

    def _get_governance_proposals(self, state: Any = None, network: str = "mainnet") -> dict[str, Any]:
        query: dict[str, str] | None = None
        if state is not None:
            try:
                query = {"state": _validate_gov_state(state)}
            except ValueError as exc:
                return self._err(0, f"invalid state: {exc}")
        status, body, net = self._request("/l1/governance/proposals", network, query=query)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch governance proposals: {body}", net)
        if not isinstance(body, list):
            return self._err(502, "Unexpected governance-proposals body shape (expected an array)", net)
        # Filter to well-formed entries BEFORE capping, so a malformed
        # entry in the first _GOVERNANCE_CAP slots can't push a valid
        # proposal out of the result (and the counts stay accurate).
        entries = [e for e in body if isinstance(e, dict)]
        proposals = [
            {
                "proposal_id": entry.get("proposalId"),
                "state": entry.get("state"),
                "cached_state": entry.get("cachedState"),
                "proposer": entry.get("proposer"),
                "payload_address": entry.get("payloadAddress"),
                "created_at": _coerce_int(entry.get("createdAt")),
                "summed_yea": entry.get("summedYea"),
                "summed_nay": entry.get("summedNay"),
            }
            for entry in entries[:_GOVERNANCE_CAP]
        ]
        return {
            "status_code": 200,
            "network": net,
            "proposals": proposals,
            "returned_count": len(proposals),
            "total_count": len(entries),
            "truncated": len(entries) > _GOVERNANCE_CAP,
        }

    def _get_tips(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/tips", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch tips: {body}", net)
        if not isinstance(body, dict) or not isinstance(body.get("tips"), dict):
            return self._err(502, "Unexpected tips body shape (expected a tips object)", net)
        tips = body["tips"]
        out: dict[str, Any] = {"status_code": 200, "network": net}
        for stage in ("proposed", "checkpointed", "proven", "finalized"):
            node = tips.get(stage)
            if not isinstance(node, dict):
                continue
            # 'proposed' is flat {number, hash}; checkpointed/proven/
            # finalized nest the head block under a 'block' key.
            block = node["block"] if isinstance(node.get("block"), dict) else node
            out[f"{stage}_height"] = _coerce_int(block.get("number"))
            out[f"{stage}_hash"] = block.get("hash")
        return out

    def _get_tx_effect(self, tx_hash: Any = None, network: str = "mainnet") -> dict[str, Any]:
        """Resolve a transaction hash to its current state.

        Mined transactions come back from ``/l2/tx-effects/{hash}`` with
        the full receipt. On a 404 we fall through to the pending pool
        (``/l2/txs/{hash}``) and the dropped list
        (``/l2/dropped-txs/{hash}``) so one action answers "did my tx go
        through?" in every state: mined / pending / dropped / not_found.
        """
        try:
            h = _validate_hex_id(tx_hash, "tx_hash")
        except ValueError as exc:
            return self._err(0, f"invalid tx_hash: {exc}")
        status, body, net = self._request(f"/l2/tx-effects/{h}", network)
        if status == 200 and not isinstance(body, dict):
            return self._err(502, "Unexpected tx-effect body shape (expected an object)", net)
        if status == 200:
            revert = body.get("revertCode")
            revert_code = _coerce_int(revert.get("code")) if isinstance(revert, dict) else _coerce_int(revert)
            return {
                "status_code": 200,
                "network": net,
                "state": "mined",
                "tx_hash": body.get("txHash") or h,
                "reverted": bool(revert_code) if revert_code is not None else None,
                "revert_code": revert_code,
                "block_height": _coerce_int(body.get("blockHeight")),
                "block_hash": body.get("blockHash"),
                # True when the containing block was orphaned by a reorg —
                # the tx is no longer on the canonical chain.
                "is_orphaned": body.get("isOrphaned"),
                "transaction_fee": _coerce_int(body.get("transactionFee")),
                "fee_payer": body.get("feePayer"),
                "fee_payment_method": body.get("feePaymentMethod"),
                "initiator": body.get("initiator"),
                "timestamp": _coerce_int(body.get("timestamp")),
                "effect_counts": {
                    key: len(body[field])
                    for key, field in (
                        ("note_hashes", "noteHashes"),
                        ("nullifiers", "nullifiers"),
                        ("l2_to_l1_msgs", "l2ToL1Msgs"),
                        ("public_data_writes", "publicDataWrites"),
                        ("private_logs", "privateLogs"),
                        ("public_logs", "publicLogs"),
                    )
                    if isinstance(body.get(field), list)
                },
            }
        if status != 404:
            return self._err(status or 502, f"Failed to fetch tx effect: {body}", net)

        # Not mined — check the pending pool, then the dropped list. A
        # failed secondary check must not mask the primary finding (the tx
        # is not mined), so non-404 errors here degrade to a note.
        unavailable: list[str] = []
        status, body, net = self._request(f"/l2/txs/{h}", network)
        if status == 200 and isinstance(body, dict):
            return {
                "status_code": 200,
                "network": net,
                "state": "pending",
                "tx_hash": body.get("txHash") or h,
                "birth_timestamp": _coerce_int(body.get("birthTimestamp")),
                "expiration_timestamp": _coerce_int(body.get("expirationTimestamp")),
                "fee_payer": body.get("feePayer"),
                "fee_payment_method": body.get("feePaymentMethod"),
                "initiator": body.get("initiator"),
            }
        if status != 404:
            unavailable.append("pending-pool")
        status, body, net = self._request(f"/l2/dropped-txs/{h}", network)
        if status == 200 and isinstance(body, dict):
            return {
                "status_code": 200,
                "network": net,
                "state": "dropped",
                "tx_hash": body.get("txHash") or h,
                # Shape is undocumented upstream; surface the fields that
                # explain the drop when present.
                "reason": body.get("reason"),
                "dropped_at": _coerce_int(body.get("droppedAt")),
                "created_at": _coerce_int(body.get("createdAt")),
            }
        if status != 404:
            unavailable.append("dropped-list")
        message = "Transaction not found: not mined, not pending, and not in the dropped list"
        if unavailable:
            message = (
                f"Transaction is not mined, but the {' and '.join(unavailable)} "
                "check(s) failed, so it may still be pending or dropped"
            )
        return {
            "status_code": 404,
            "network": net,
            "state": "not_found",
            "tx_hash": h,
            "message": message,
        }

    def _get_block_by_id(self, height_or_hash: Any = None, network: str = "mainnet") -> dict[str, Any]:
        try:
            block_id = _validate_height_or_hash(height_or_hash)
        except ValueError as exc:
            return self._err(0, f"invalid height_or_hash: {exc}")
        status, body, net = self._request(f"/l2/blocks/{block_id}", network)
        if status == 404:
            return self._err(404, "Block not found", net)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch block: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected block body shape (expected an object)", net)
        # Nested fields are upstream-controlled; tolerate any non-dict
        # (fail soft to empty) rather than raising mid-action.
        header = body.get("header")
        header = header if isinstance(header, dict) else {}
        global_vars = header.get("globalVariables")
        global_vars = global_vars if isinstance(global_vars, dict) else {}
        block_body = body.get("body")
        tx_effects = block_body.get("txEffects") if isinstance(block_body, dict) else None
        tx_hashes = [
            entry.get("txHash")
            for entry in (tx_effects if isinstance(tx_effects, list) else [])
            if isinstance(entry, dict) and entry.get("txHash")
        ]
        return {
            "status_code": 200,
            "network": net,
            "hash": body.get("hash"),
            "height": _coerce_int(body.get("height")),
            "finalization_status": body.get("nativeStatus") or body.get("finalizationStatus"),
            # Non-null when the block was reorged out of the canonical chain.
            "is_orphaned": body.get("orphan") is not None,
            "slot_number": _coerce_int(global_vars.get("slotNumber")),
            "timestamp": _coerce_int(global_vars.get("timestamp")),
            "coinbase": global_vars.get("coinbase"),
            "total_fees": _coerce_int(header.get("totalFees")),
            "total_mana_used": _coerce_int(header.get("totalManaUsed")),
            "tx_count": len(tx_hashes),
            "tx_hashes": tx_hashes[:_BLOCK_TX_HASHES_CAP],
            "tx_hashes_truncated": len(tx_hashes) > _BLOCK_TX_HASHES_CAP,
        }

    def _get_validator(self, attester_address: Any = None, network: str = "mainnet") -> dict[str, Any]:
        try:
            addr = _validate_hex_id(attester_address, "attester_address")
        except ValueError as exc:
            return self._err(0, f"invalid attester_address: {exc}")
        status, body, net = self._request(f"/l1/l2-validators/{addr}", network)
        if status == 404:
            return self._err(404, "No validator found with that attester address", net)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch validator: {body}", net)
        if not isinstance(body, dict):
            return self._err(502, "Unexpected validator body shape (expected an object)", net)
        raw_status = body.get("status")
        status_label = _VALIDATOR_STATUS_LABELS.get(_coerce_int(raw_status))
        if status_label is None and isinstance(raw_status, str):
            status_label = raw_status
        stake_wei = _coerce_int(body.get("stake"))
        return {
            "status_code": 200,
            "network": net,
            "attester": body.get("attester"),
            "status": status_label,
            "stake": stake_wei,
            # The stake asset has 18 decimals; pre-divide so the LLM
            # doesn't have to do 10^18 arithmetic on a 24-digit integer.
            "stake_tokens": round(stake_wei / 1e18, 4) if stake_wei is not None else None,
            "withdrawer": body.get("withdrawer"),
            "proposer": body.get("proposer"),
            "rollup_address": body.get("rollupAddress"),
            "first_seen_at": _coerce_int(body.get("firstSeenAt")),
            "latest_seen_change_at": _coerce_int(body.get("latestSeenChangeAt")),
        }

    def _get_reorgs(self, network: str = "mainnet") -> dict[str, Any]:
        status, body, net = self._request("/l2/reorgs", network)
        if status != 200:
            return self._err(status or 502, f"Failed to fetch reorgs: {body}", net)
        if not isinstance(body, list):
            return self._err(502, "Unexpected reorgs body shape (expected an array)", net)
        entries = [e for e in body if isinstance(e, dict)]
        reorgs = [
            {
                "height": _coerce_int(entry.get("height")),
                "orphaned_block_hash": entry.get("orphanedBlockHash"),
                "orphaned_block_count": _coerce_int(entry.get("nbrOfOrphanedBlocks")),
                "timestamp": entry.get("timestamp"),
            }
            for entry in entries[:_REORGS_CAP]
        ]
        return {
            "status_code": 200,
            "network": net,
            "reorgs": reorgs,
            "returned_count": len(reorgs),
            "total_count": len(entries),
            "truncated": len(entries) > _REORGS_CAP,
        }

    def get_actions_metadata(self) -> list[dict[str, Any]]:
        network_param = {
            "type": "string",
            "enum": list(_SUPPORTED_NETWORKS),
            "description": (
                "Which Aztec network to query. 'mainnet' (the live "
                "network, anchored to Ethereum mainnet / l1ChainId 1) or "
                "'testnet' (pre-production, anchored to Sepolia / "
                "l1ChainId 11155111). Defaults to mainnet. Pass 'testnet' "
                "whenever the user is asking about the Aztec testnet — "
                "INCLUDING when the testnet context was established earlier "
                "in the conversation rather than repeated in the current "
                "message. Example: the user asks for the testnet block "
                "number, then pastes a transaction hash to look up — that "
                "lookup is still testnet, so pass network='testnet'. When "
                "the conversation has been about testnet and the network "
                "isn't restated, prefer 'testnet' over the mainnet default. "
                "An explicit 'mainnet' / 'live' / 'production' reference in "
                "the current request overrides earlier testnet context."
            ),
        }

        def params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
            # NOTE on required-ness: the LLM-visible schema is rebuilt from
            # these properties by ToolExecutor._build_tool_parameters, which
            # reads a per-PROPERTY ``required: True`` flag and ignores any
            # top-level ``required`` list. So a required arg must carry the
            # flag on its own property dict (see ``query``/``address``).
            properties: dict[str, Any] = {"network": network_param}
            if extra:
                properties.update(extra)
            return {
                "type": "object",
                "properties": properties,
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
                "name": "aztec_network_get_blocks",
                "description": (
                    "Get a window of recent Aztec L2 blocks, each with its "
                    "height, unix-millisecond timestamp, and hash, newest "
                    "first. Call with NO from_height/to_height to get the "
                    "latest 20 blocks — use this to compute average block "
                    "time or intervals over recent blocks (diff consecutive "
                    "timestamps). To fetch a specific historical window, "
                    "pass BOTH from_height and to_height (inclusive); the "
                    "window is capped at 20 blocks. Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "from_height": {
                            "type": "integer",
                            "description": (
                                "Optional inclusive lower bound of the "
                                "block-height window. Must be passed together "
                                "with to_height; the window cannot exceed 20 "
                                "blocks. Omit both bounds for the latest "
                                "blocks."
                            ),
                        },
                        "to_height": {
                            "type": "integer",
                            "description": (
                                "Optional inclusive upper bound of the "
                                "block-height window. Must be passed together "
                                "with from_height."
                            ),
                        },
                    }
                ),
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
            {
                "name": "aztec_network_search",
                "description": (
                    "Search the Aztec explorer for a block (by hash, "
                    "height, or slot), transaction effect, contract class, "
                    "contract instance, validator, or account. Pass the "
                    "raw identifier (e.g. a 0x… hash/address or a block "
                    "number) as 'query'. Returns the matching entries "
                    "grouped by category. Use this to look up 'what is "
                    "<hash>?' or to resolve a transaction by hash. "
                    "Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "query": {
                            "type": "string",
                            "required": True,
                            "description": (
                                "The identifier to look up: a 0x-prefixed "
                                "hash or address, a block height/number, or "
                                "a slot number."
                            ),
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_contract_instance",
                "description": (
                    "Get a deployed Aztec L2 contract instance by its "
                    "address: contract class id, deployer, initialization "
                    "hash, artifact name, and verification/source metadata. "
                    "Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "address": {
                            "type": "string",
                            "required": True,
                            "description": "The 0x-prefixed contract instance address.",
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_governance_proposals",
                "description": (
                    "List Aztec L1 governance proposals (proposal id, "
                    "state, proposer, payload address, vote tallies). "
                    "Optionally filter by 'state' (e.g. Pending, Active, "
                    "Queued, Executed). Use for 'what governance proposals "
                    "are active/queued?'. Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "state": {
                            "type": "string",
                            "description": (
                                "Optional governance state filter (alpha "
                                "label, e.g. 'Active' or 'Queued'). Omit to "
                                "list all proposals."
                            ),
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_tips",
                "description": (
                    "Get the Aztec L2 chain finality heads: the proposed, "
                    "checkpointed, proven, and finalized block heights and "
                    "hashes. Use to see how far proven/finalized lag the "
                    "chain tip. Defaults to mainnet."
                ),
                "parameters": params(),
            },
            {
                "name": "aztec_network_get_tx_effect",
                "description": (
                    "Look up an Aztec L2 transaction by its hash and report "
                    "its state. If mined, returns the receipt: success or "
                    "reverted (revert_code), block height/hash, fee, fee "
                    "payer, timestamp, and effect counts. If not mined, "
                    "checks the pending pool and the dropped list, so the "
                    "state is one of mined / pending / dropped / not_found. "
                    "Use this to answer 'check this tx hash' or 'did my "
                    "transaction go through?'. Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "tx_hash": {
                            "type": "string",
                            "required": True,
                            "description": "The 0x-prefixed transaction hash to look up.",
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_block",
                "description": (
                    "Get a specific Aztec L2 block by height or block hash: "
                    "finalization stage, timestamp, fees, mana, coinbase, "
                    "whether it was orphaned by a reorg, and the hashes of "
                    "the transactions it contains. Use when the user asks "
                    "about a specific block ('what's in block 12345?'). "
                    "Defaults to mainnet."
                ),
                "parameters": params(
                    {
                        "height_or_hash": {
                            "type": "string",
                            "required": True,
                            "description": (
                                "A block height (decimal number) or a "
                                "0x-prefixed block hash."
                            ),
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_validator",
                "description": (
                    "Get one Aztec L2 validator by its L1 attester address: "
                    "status (VALIDATING / LIVING / EXITING), stake, "
                    "withdrawer and proposer addresses, and first/last seen "
                    "timestamps. Use for 'is my validator in the set?' / "
                    "'what's the status of validator 0x…?'. Defaults to "
                    "mainnet."
                ),
                "parameters": params(
                    {
                        "attester_address": {
                            "type": "string",
                            "required": True,
                            "description": "The validator's 0x-prefixed L1 attester address.",
                        }
                    }
                ),
            },
            {
                "name": "aztec_network_get_reorgs",
                "description": (
                    "List recent Aztec L2 chain reorgs, newest first: the "
                    "height, orphaned block hash, number of orphaned "
                    "blocks, and when each happened. Use for 'did the "
                    "chain reorg?' or to explain why a previously-seen "
                    "block or transaction disappeared. Defaults to "
                    "mainnet."
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


def _validate_hex_id(value: Any, label: str) -> str:
    """Validate an LLM-supplied 0x-hex identifier for URL-path use.

    Raises ``ValueError`` (caught at the action boundary → error dict) on
    a non-string or out-of-charset value. The strict ``0x[0-9a-fA-F]``
    charset rejects path delimiters, ``@``, ``..`` traversal, whitespace
    and control chars, so the validated id is byte-safe to interpolate
    into the request path without altering the authority. The message
    never echoes the rejected value (it can be attacker-controlled and
    flows to logs / the LLM)."""
    if not isinstance(value, str) or not _HEX_ID_RE.match(value):
        raise ValueError(f"{label} must be a 0x-prefixed hex string (0x[0-9a-fA-F], <=128 chars)")
    return value


def _validate_search_query(value: Any) -> str:
    """Validate a free-text search query for use as a urlencoded query
    value. Raises ``ValueError`` on a non-string, empty, over-length, or
    control-char-bearing value. Never echoes the rejected value."""
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    q = value.strip()
    if not q:
        raise ValueError("query must not be empty")
    if len(q) > _MAX_SEARCH_QUERY_LEN:
        raise ValueError(f"query must be at most {_MAX_SEARCH_QUERY_LEN} characters")
    if _FORBIDDEN_QUERY_CHARS_RE.search(q):
        raise ValueError("query must not contain control characters")
    return q


def _validate_block_height(value: Any, label: str) -> int:
    """Validate an LLM-supplied block height: a non-negative integer.

    Accepts an int or an all-digits string; rejects bools (``True`` is an
    ``int`` in Python), floats, negatives, and non-numeric strings. Raises
    ``ValueError`` (caught at the action boundary → error dict). The
    height is sent as a urlencoded query value, not a path segment, so
    this is a type/range check, not a charset-escape guard. Never echoes
    the rejected value."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    if isinstance(value, int):
        height = value
    elif isinstance(value, str) and value.strip().isdigit():
        height = int(value.strip())
    else:
        raise ValueError(f"{label} must be a non-negative integer")
    if height < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return height


def _validate_height_or_hash(value: Any) -> str:
    """Validate an LLM-supplied block identifier for URL-path use.

    Accepts a 0x-hex hash (``_validate_hex_id`` charset), a non-negative
    int, or an all-digits string; returns the canonical path segment.
    Both accepted grammars are path-safe by construction. Raises
    ``ValueError`` (caught at the action boundary → error dict) and never
    echoes the rejected value."""
    if isinstance(value, str) and _HEX_ID_RE.match(value):
        return value
    try:
        return str(_validate_block_height(value, "height_or_hash"))
    except ValueError:
        raise ValueError(
            "height_or_hash must be a block height (non-negative integer) or a 0x-prefixed hex hash"
        )


def _validate_gov_state(value: Any) -> str:
    """Validate an optional governance-state filter. Raises ``ValueError``
    on a non-string or non-alpha value. Never echoes the rejected value."""
    if not isinstance(value, str) or not _GOV_STATE_RE.match(value):
        raise ValueError("state must match [A-Za-z]{1,32}")
    return value


_FORBIDDEN_URL_CHARS_RE = re.compile(r"[\x00-\x20\x7f\\]")
# Same as the URL char ban, plus the query/fragment delimiters: an
# internally-built path must never carry these (only ``_request(query=)``
# may introduce a ``?``). Keeps the dialed path byte-identical to what the
# base-URL SSRF check validated.
_FORBIDDEN_PATH_CHARS_RE = re.compile(r"[\x00-\x20\x7f\\?#]")


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

    Query values are redacted in BOTH their urlencoded form (as they
    appear in the dialed URL) and their decoded form — an upstream error
    or JSON-decode message can echo the decoded value, so redacting only
    the encoded form would miss it. (Today's query values are non-secret
    user input, but this keeps the contract robust if a sensitive query
    param is ever added.)
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
                decoded = unquote_plus(value)
                if len(decoded) >= 4:
                    parts.add(decoded)
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
