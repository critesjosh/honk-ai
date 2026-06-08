"""Tests for application/agents/tools/aztec_network.py"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from application.agents.tools.aztec_network import AztecNetworkTool

_MAINNET = "https://api.aztecscan.xyz/v1"
_TESTNET = "https://api.testnet.aztecscan.xyz/v1"


@pytest.fixture
def tool():
    return AztecNetworkTool(
        config={"mainnet_base_url": _MAINNET, "testnet_base_url": _TESTNET, "api_key": "k"}
    )


def _resp(status_code: int = 200, json_body=None, text: str | bytes = ""):
    """Mock a streamed HTTP response. The tool reads via ``response.raw.read``
    rather than ``response.json()`` so we can cap body size; the fixture
    mirrors that by encoding the expected body as bytes on ``.raw.read``."""
    r = MagicMock()
    r.status_code = status_code
    if json_body is not None:
        body_bytes = json.dumps(json_body).encode("utf-8")
    elif isinstance(text, bytes):
        body_bytes = text
    else:
        body_bytes = text.encode("utf-8")
    r.raw.read.return_value = body_bytes
    return r


@pytest.mark.unit
class TestNetworkSelection:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_defaults_to_mainnet(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=42)
        result = tool.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        assert url.startswith(_MAINNET + "/k/")
        assert result["network"] == "mainnet"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_testnet_routes_to_testnet_host(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=42)
        result = tool.execute_action("aztec_network_get_latest_height", network="testnet")
        url = mock_get.call_args[0][0]
        assert url.startswith(_TESTNET + "/k/")
        assert result["network"] == "testnet"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_network_is_case_insensitive(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=42)
        tool.execute_action("aztec_network_get_latest_height", network="TESTNET")
        assert mock_get.call_args[0][0].startswith(_TESTNET)

    def test_unsupported_network_fails(self, tool):
        result = tool.execute_action("aztec_network_get_latest_height", network="devnet")
        assert result["status_code"] == 0
        assert "Unsupported network" in result["message"]

    def test_non_string_network_fails(self, tool):
        result = tool.execute_action("aztec_network_get_latest_height", network=1)
        assert result["status_code"] == 0
        assert "network must be a string" in result["message"]


@pytest.mark.unit
class TestUrlConstruction:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_url_includes_api_key_in_path(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=42)
        tool.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        assert url.endswith("/v1/k/l2/latest-height")

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_base_url_trailing_slash_stripped(self, mock_get):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET + "/", "api_key": "k"})
        mock_get.return_value = _resp(200, json_body=42)
        t.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        # Single slash between v1 and api key
        assert "//k" not in url

    def test_invalid_base_url_degrades_to_config_error(self):
        t = AztecNetworkTool(config={"mainnet_base_url": "ftp://example.com"})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]

    def test_construction_never_raises(self):
        """ToolManager eagerly instantiates every tool on every tool
        execution. Neither a transient DNS failure nor garbage config /
        env may block the loading of unrelated tools — the constructor
        only stores config; validation happens per request."""
        AztecNetworkTool(
            config={"mainnet_base_url": "https://does-not-resolve-1234567.invalid/v1", "api_key": "k"}
        )
        AztecNetworkTool(config={"mainnet_base_url": "ftp://garbage", "api_key": "k/../admin"})

    @patch.dict("os.environ", {"AZTECSCAN_API_KEY": "bad key with spaces"})
    def test_bad_env_var_degrades_to_config_error(self):
        """A malformed optional env var must not break tool construction
        (and with it every other tool) — only this tool's actions."""
        t = AztecNetworkTool(config={})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        # The bad value itself is never echoed to the LLM.
        assert "bad key with spaces" not in result["message"]

    def test_api_key_path_injection_degrades_to_config_error(self):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "k/../admin"})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        assert "k/../admin" not in result["message"]

    def test_base_url_query_fragment_degrades_to_config_error(self):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET + "?evil=1"})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "query or fragment" in result["message"]

    @pytest.mark.parametrize(
        "bad_url",
        [
            # urllib parses hostname as example.com; requests dials 127.0.0.1.
            "https://127.0.0.1\\@example.com/v1",
            # userinfo form of the same SSRF bypass.
            "https://127.0.0.1@example.com/v1",
        ],
    )
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_authority_confusion_url_rejected(self, mock_get, bad_url):
        """urllib.parse vs requests disagree on backslash / userinfo
        authorities — that mismatch is an SSRF bypass, so reject pre-flight
        and never issue the request."""
        t = AztecNetworkTool(config={"mainnet_base_url": bad_url, "api_key": "k"})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        mock_get.assert_not_called()


@pytest.mark.unit
class TestLatestHeight:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_success(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=80369)
        result = tool.execute_action("aztec_network_get_latest_height")
        assert result == {"status_code": 200, "network": "mainnet", "height": 80369}

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_non_int_payload(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"oops": True})
        result = tool.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 200
        assert "Unexpected latest-height payload" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_http_error(self, mock_get, tool):
        mock_get.return_value = _resp(429, text="rate limited")
        result = tool.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 429
        assert "Failed" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_transport_failure(self, mock_get, tool):
        mock_get.side_effect = requests.ConnectionError("boom")
        result = tool.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "boom" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_transport_failure_redacts_api_key(self, mock_get):
        """requests exception strings embed the request URL, whose path
        contains the API key — it must never reach the tool result."""
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "SECRETKEY123"})
        mock_get.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.aztecscan.xyz', port=443): "
            "Max retries exceeded with url: /v1/SECRETKEY123/l2/latest-height"
        )
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "SECRETKEY123" not in result["message"]
        assert "<redacted>" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_error_body_redacts_api_key(self, mock_get):
        """Upstream error pages can echo the request path (e.g. Apache's
        default 404) — the API key in that path must be redacted."""
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "SECRETKEY123"})
        mock_get.return_value = _resp(
            404, text="The requested URL /v1/SECRETKEY123/l2/latest-height was not found."
        )
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 404
        assert "SECRETKEY123" not in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_redirect_is_refused(self, mock_get, tool):
        mock_get.return_value = _resp(302, text="moved")
        result = tool.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "redirect" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_response_size_cap(self, mock_get, tool):
        # Body bigger than the 512 KiB cap.
        oversized = b"x" * (520 * 1024)
        mock_get.return_value = _resp(200, text=oversized)
        result = tool.execute_action("aztec_network_get_latest_height")
        # Oversized body is treated as a fetch failure, not a 200 payload.
        assert result["status_code"] == 0
        assert "size cap" in result["message"]


@pytest.mark.unit
class TestLatestBlock:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_success_extracts_summary(self, mock_get, tool):
        # Field names mirror the LIVE Aztecscan response (verified against
        # api.aztecscan.xyz): nativeStatus, not the assumed finalizationStatus.
        mock_get.return_value = _resp(
            200,
            json_body={
                "hash": "0xabc",
                "height": "77344",
                "nativeStatus": "finalized",
                "header": {
                    "totalFees": "1000",
                    "totalManaUsed": "500",
                    "globalVariables": {
                        "timestamp": "1700000000",
                        "coinbase": "0xdef",
                    },
                },
            },
        )
        result = tool.execute_action("aztec_network_get_latest_block")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["hash"] == "0xabc"
        assert result["height"] == 77344
        assert result["finalization_status"] == "finalized"
        assert result["total_fees"] == 1000
        assert result["total_mana_used"] == 500
        assert result["timestamp"] == 1700000000
        assert result["coinbase"] == "0xdef"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_legacy_finalization_status_field_tolerated(self, mock_get, tool):
        mock_get.return_value = _resp(
            200, json_body={"hash": "0xabc", "height": "1", "finalizationStatus": 5}
        )
        result = tool.execute_action("aztec_network_get_latest_block")
        assert result["finalization_status"] == 5

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_missing_header_safe(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"hash": "0xabc"})
        result = tool.execute_action("aztec_network_get_latest_block")
        assert result["status_code"] == 200
        assert result["height"] is None


@pytest.mark.unit
class TestBlocksByStatus:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_groups_by_status(self, mock_get, tool):
        # Live shape: nativeStatus is a string label per stage.
        mock_get.return_value = _resp(
            200,
            json_body=[
                {"nativeStatus": "finalized", "height": "100"},
                {"nativeStatus": "proven", "height": "95"},
                {"nativeStatus": "checkpointed", "height": "80"},
            ],
        )
        result = tool.execute_action("aztec_network_get_blocks_by_status")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["stage_finalized_height"] == 100
        assert result["stage_proven_height"] == 95
        assert result["stage_checkpointed_height"] == 80

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_groups_by_status_legacy_field(self, mock_get, tool):
        mock_get.return_value = _resp(
            200, json_body=[{"finalizationStatus": 1, "height": "100"}]
        )
        result = tool.execute_action("aztec_network_get_blocks_by_status")
        assert result["stage_1_height"] == 100

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_non_list_body(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"error": "noop"})
        result = tool.execute_action("aztec_network_get_blocks_by_status")
        assert result["status_code"] == 502


@pytest.mark.unit
class TestRpcNodes:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_maps_live_field_names(self, mock_get, tool):
        # Live shape (verified): rpcNodeName / nodeVersion / lastSeenAt.
        mock_get.return_value = _resp(
            200,
            json_body=[
                {
                    "rpcNodeName": "http://1.2.3.4:8080",
                    "nodeVersion": "2.1.4",
                    "lastSeenAt": "2025-12-10T09:01:47.762Z",
                    "l2NetworkId": "MAINNET",
                }
            ],
        )
        result = tool.execute_action("aztec_network_get_rpc_nodes")
        assert result["status_code"] == 200
        node = result["nodes"][0]
        assert node["name"] == "http://1.2.3.4:8080"
        assert node["version"] == "2.1.4"
        assert node["last_seen"] == "2025-12-10T09:01:47.762Z"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_maps_legacy_field_names(self, mock_get, tool):
        # Fallback to the older name/version/lastSeen keys.
        mock_get.return_value = _resp(
            200,
            json_body=[{"name": "node-a", "version": "1.0.0", "lastSeen": "2025-01-01T00:00:00Z"}],
        )
        result = tool.execute_action("aztec_network_get_rpc_nodes")
        node = result["nodes"][0]
        assert node["name"] == "node-a"
        assert node["version"] == "1.0.0"
        assert node["last_seen"] == "2025-01-01T00:00:00Z"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_non_list_body(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"error": "noop"})
        result = tool.execute_action("aztec_network_get_rpc_nodes")
        assert result["status_code"] == 502


@pytest.mark.unit
class TestChainInfo:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_mainnet(self, mock_get, tool):
        mock_get.return_value = _resp(
            200,
            json_body={
                "l2NetworkId": "MAINNET",
                "l1ChainId": 1,
                "rollupVersion": "2934756905",
                "l1ContractAddresses": {
                    "rollupAddress": "0xae20",
                    "registryAddress": "0x35b2",
                    "inboxAddress": "0xf1bb",
                    "outboxAddress": "0x5fe6",
                },
            },
        )
        result = tool.execute_action("aztec_network_get_chain_info")
        assert result["network"] == "mainnet"
        assert result["l2_network_id"] == "MAINNET"
        assert result["l1_chain_id"] == 1
        assert result["rollup_address"] == "0xae20"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_testnet(self, mock_get, tool):
        mock_get.return_value = _resp(
            200, json_body={"l2NetworkId": "TESTNET", "l1ChainId": 11155111}
        )
        result = tool.execute_action("aztec_network_get_chain_info", network="testnet")
        assert result["network"] == "testnet"
        assert result["l2_network_id"] == "TESTNET"
        assert result["l1_chain_id"] == 11155111


@pytest.mark.unit
class TestValidatorTotals:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_500_fails_soft(self, mock_get, tool):
        mock_get.return_value = _resp(500, text="server error")
        result = tool.execute_action("aztec_network_get_validator_totals")
        assert result["status_code"] == 500
        assert "Failed" in result["message"]


@pytest.mark.unit
class TestActionsMetadata:
    def test_eleven_actions(self, tool):
        meta = tool.get_actions_metadata()
        assert len(meta) == 11
        names = [m["name"] for m in meta]
        assert all(n.startswith("aztec_network_") for n in names)
        # The curated additions are present.
        for name in (
            "aztec_network_search",
            "aztec_network_get_contract_instance",
            "aztec_network_get_governance_proposals",
            "aztec_network_get_tips",
            "aztec_network_get_blocks",
        ):
            assert name in names

    def test_every_action_exposes_network_enum(self, tool):
        for m in tool.get_actions_metadata():
            net = m["parameters"]["properties"]["network"]
            assert net["enum"] == ["mainnet", "testnet"]

    def test_unknown_action_raises(self, tool):
        with pytest.raises(ValueError, match="Unknown action"):
            tool.execute_action("aztec_network_bogus")

    def test_config_requirements_shape(self, tool):
        reqs = tool.get_config_requirements()
        assert "mainnet_base_url" in reqs
        assert "testnet_base_url" in reqs
        assert "api_key" in reqs
        assert reqs["api_key"]["secret"] is True


@pytest.mark.unit
class TestEnvOverride:
    # validate_url() does a real DNS lookup; patch it out so these
    # precedence tests can use non-resolvable hosts to assert routing.
    @patch.dict(
        "os.environ",
        {
            "AZTECSCAN_MAINNET_BASE_URL": "https://mainnet.example.com/v1",
            "AZTECSCAN_TESTNET_BASE_URL": "https://testnet.example.com/v1",
            "AZTECSCAN_API_KEY": "override",
        },
    )
    @patch("application.agents.tools.aztec_network.validate_url", lambda _u: None)
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_env_overrides_default(self, mock_get):
        mock_get.return_value = _resp(200, json_body=1)
        t = AztecNetworkTool(config={})
        t.execute_action("aztec_network_get_latest_height")
        assert mock_get.call_args[0][0].startswith("https://mainnet.example.com/v1/override/")
        t.execute_action("aztec_network_get_latest_height", network="testnet")
        assert mock_get.call_args[0][0].startswith("https://testnet.example.com/v1/override/")

    @patch.dict("os.environ", {"AZTECSCAN_MAINNET_BASE_URL": "https://env.example.com/v1"})
    @patch("application.agents.tools.aztec_network.validate_url", lambda _u: None)
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_config_overrides_env(self, mock_get):
        mock_get.return_value = _resp(200, json_body=1)
        t = AztecNetworkTool(config={"mainnet_base_url": "https://cfg.example.com/v1"})
        t.execute_action("aztec_network_get_latest_height")
        assert mock_get.call_args[0][0].startswith("https://cfg.example.com/v1/")


# Live-confirmed response shapes (probed against api.aztecscan.xyz and
# api.testnet.aztecscan.xyz with the public temporary-api-key).
_SEARCH_BODY = {
    "searchPhrase": "1",
    "results": {
        "blocks": [{"hash": "0x022f", "blockNumber": 1, "slotNumber": 30650}],
        "txEffects": [],
        "droppedTx": [],
        "pendingTx": [],
        "registeredContractClasses": [],
        "contractInstances": [],
        "validators": [],
        "accounts": [],
    },
}
_CONTRACT_INSTANCE_BODY = {
    "address": "0x14c4",
    "blockHash": "0x19d6",
    "version": 1,
    "salt": "0x0",
    "currentContractClassId": "0x1acd",
    "originalContractClassId": "0x1acd",
    "initializationHash": "0x11fc",
    "deployer": "0x0",
    "artifactContractName": "Token",
    "standardContractType": None,
    "sourceCodeUrl": "https://example/src",
    "isOrphaned": False,
    "publicKeys": {"masterNullifierPublicKey": "0x0149"},
}
_PROPOSALS_BODY = [
    {
        "id": "c829",
        "proposalId": "3",
        "payloadAddress": "0xa156",
        "proposer": "0x06Ef",
        "state": "Queued",
        "cachedState": "Pending",
        "createdAt": 1778568755000,
        "summedYea": "709004000000000000000000000",
        "summedNay": "0",
    }
]
_TIPS_BODY = {
    "tips": {
        "proposed": {"number": 75980, "hash": "0x2acf"},
        "checkpointed": {
            "block": {"number": 75980, "hash": "0x2acf"},
            "checkpoint": {"number": 74732, "hash": "0x0093"},
        },
        "proven": {
            "block": {"number": 75970, "hash": "0x1ec9"},
            "checkpoint": {"number": 74722, "hash": "0x0074"},
        },
        "finalized": {
            "block": {"number": 75938, "hash": "0x2721"},
            "checkpoint": {"number": 74690, "hash": "0x0041"},
        },
    },
    "observedAt": 1780928559793,
    "stale": False,
}
# /l2/blocks returns an array newest-first; each entry carries its
# timestamp under header.globalVariables.timestamp (unix ms). Probed
# values are 72s apart, matching the live testnet cadence.
_BLOCKS_BODY = [
    {
        "hash": "0x15c8",
        "height": "108335",
        "nativeStatus": "checkpointed",
        "header": {"globalVariables": {"blockNumber": 108335, "timestamp": 1780945980000}},
        "body": {"txEffects": []},
    },
    {
        "hash": "0x1f78",
        "height": "108334",
        "nativeStatus": "checkpointed",
        "header": {"globalVariables": {"blockNumber": 108334, "timestamp": 1780945908000}},
    },
    {
        "hash": "0x0c39",
        "height": "108333",
        "nativeStatus": "checkpointed",
        "header": {"globalVariables": {"blockNumber": 108333, "timestamp": 1780945836000}},
    },
]


@pytest.mark.unit
class TestGetBlocks:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_no_range_returns_latest_blocks(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        result = tool.execute_action("aztec_network_get_blocks")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["returned_count"] == 3
        assert result["truncated"] is False
        # Newest-first order is preserved; each block exposes the three
        # fields needed to reason about block intervals.
        assert [b["height"] for b in result["blocks"]] == [108335, 108334, 108333]
        assert result["blocks"][0]["timestamp"] == 1780945980000
        assert result["blocks"][0]["hash"] == "0x15c8"
        # Consecutive timestamps are 72s apart — block-time math works.
        ts = [b["timestamp"] for b in result["blocks"]]
        assert ts[0] - ts[1] == 72000

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_no_range_sends_no_query(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        tool.execute_action("aztec_network_get_blocks")
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l2/blocks"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_inclusive_range_translates_to_half_open(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        tool.execute_action(
            "aztec_network_get_blocks", from_height=108300, to_height=108309
        )
        url = mock_get.call_args[0][0]
        # Inclusive 108300..108309 → upstream half-open from=108300&to=108310.
        assert url == _MAINNET + "/k/l2/blocks?from=108300&to=108310"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_range_accepts_digit_strings(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        tool.execute_action(
            "aztec_network_get_blocks", from_height="100", to_height="109"
        )
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l2/blocks?from=100&to=110"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_single_block_window(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY[:1])
        tool.execute_action("aztec_network_get_blocks", from_height=5, to_height=5)
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l2/blocks?from=5&to=6"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_max_window_allowed(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        # 0..19 inclusive == 20 blocks == the cap; request must go out.
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=0, to_height=19
        )
        assert result["status_code"] == 200
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l2/blocks?from=0&to=20"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_window_too_wide_degrades_without_request(self, mock_get, tool):
        # 0..20 inclusive == 21 blocks == one past the cap.
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=0, to_height=20
        )
        assert result["status_code"] == 0
        assert "at most 20" in result["message"]
        mock_get.assert_not_called()

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_reversed_range_degrades_without_request(self, mock_get, tool):
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=10, to_height=5
        )
        assert result["status_code"] == 0
        assert "from_height must be <= to_height" in result["message"]
        mock_get.assert_not_called()

    @pytest.mark.parametrize("kwargs", [{"from_height": 5}, {"to_height": 5}])
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_partial_range_degrades_without_request(self, mock_get, tool, kwargs):
        result = tool.execute_action("aztec_network_get_blocks", **kwargs)
        assert result["status_code"] == 0
        assert "pass both" in result["message"]
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad", [-1, 1.5, "abc", "0x10", "  ", "1.0", True])
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_invalid_height_degrades_without_request(self, mock_get, tool, bad):
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=bad, to_height=10
        )
        assert result["status_code"] == 0
        assert "invalid range" in result["message"]
        mock_get.assert_not_called()

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_routes_to_testnet(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_BLOCKS_BODY)
        result = tool.execute_action("aztec_network_get_blocks", network="testnet")
        assert mock_get.call_args[0][0] == _TESTNET + "/k/l2/blocks"
        assert result["network"] == "testnet"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_empty_window_returns_empty_list(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=[])
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=99999990, to_height=99999999
        )
        assert result["status_code"] == 200
        assert result["blocks"] == []
        assert result["returned_count"] == 0

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_unexpected_shape(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"not": "a list"})
        result = tool.execute_action("aztec_network_get_blocks")
        assert result["status_code"] == 502

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_upstream_error_surfaced(self, mock_get, tool):
        # A valid client-side range can still 400 upstream (backstop path).
        mock_get.return_value = _resp(
            400, json_body={"message": "Range too wide. Maximum is 20 blocks."}
        )
        result = tool.execute_action(
            "aztec_network_get_blocks", from_height=0, to_height=5
        )
        assert result["status_code"] == 400
        assert "Failed to fetch blocks" in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_oversized_upstream_list_is_capped(self, mock_get, tool):
        # Defensive: if upstream ever returns more than the cap, we trim and
        # flag it rather than bloating the tool result.
        big = [
            {"height": str(i), "header": {"globalVariables": {"timestamp": i}}}
            for i in range(25)
        ]
        mock_get.return_value = _resp(200, json_body=big)
        result = tool.execute_action("aztec_network_get_blocks")
        assert result["returned_count"] == 20
        assert result["truncated"] is True

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_malformed_entries_and_missing_timestamp_tolerated(self, mock_get, tool):
        body = [
            "not-a-dict",
            {"height": "108335", "hash": "0xaa"},  # no header → timestamp None
            # Truthy non-dict header AND globalVariables must not raise.
            {"height": "108334", "header": "garbage", "hash": "0xbb"},
            {"height": "108333", "header": {"globalVariables": "garbage"}, "hash": "0xcc"},
            {"height": "108332", "header": {"globalVariables": {"timestamp": 7}}},
        ]
        mock_get.return_value = _resp(200, json_body=body)
        result = tool.execute_action("aztec_network_get_blocks")
        # The string entry is dropped; the four dicts survive without crashing.
        assert result["returned_count"] == 4
        assert result["blocks"][0] == {"height": 108335, "timestamp": None, "hash": "0xaa"}
        assert result["blocks"][1] == {"height": 108334, "timestamp": None, "hash": "0xbb"}
        assert result["blocks"][2] == {"height": 108333, "timestamp": None, "hash": "0xcc"}
        assert result["blocks"][3]["timestamp"] == 7


@pytest.mark.unit
class TestSearch:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_success_groups_non_empty_categories(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_SEARCH_BODY)
        result = tool.execute_action("aztec_network_search", query="1")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["search_phrase"] == "1"
        # Only the non-empty category survives.
        assert set(result["matches"]) == {"blocks"}
        assert result["matches"]["blocks"][0]["blockNumber"] == 1
        assert result["total_matches"] == 1
        assert result["truncated"] is False

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_query_is_urlencoded_into_query_string(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_SEARCH_BODY)
        tool.execute_action("aztec_network_search", query="a b&c=d?e#f/@x")
        url = mock_get.call_args[0][0]
        # Host + path prefix unchanged; the user value lives only in the
        # urlencoded query string — no extra path segments, no authority.
        assert url.startswith(_MAINNET + "/k/l2/search?q=")
        # Exactly one '?': the user value did not introduce a second query
        # delimiter or any '#'/path segment.
        assert url.count("?") == 1
        assert "#" not in url
        query_part = url.split("?", 1)[1]
        assert query_part.startswith("q=")
        # The raw value is urlencoded, not present literally.
        assert "a b&c=d?e#f/@x" not in url
        assert "a b" not in url  # space encoded to '+'

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_routes_to_testnet(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_SEARCH_BODY)
        result = tool.execute_action("aztec_network_search", query="1", network="testnet")
        assert mock_get.call_args[0][0].startswith(_TESTNET + "/k/l2/search?q=")
        assert result["network"] == "testnet"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_truncates_large_category(self, mock_get, tool):
        big = {"searchPhrase": "x", "results": {"blocks": [{"i": i} for i in range(25)]}}
        mock_get.return_value = _resp(200, json_body=big)
        result = tool.execute_action("aztec_network_search", query="x")
        assert len(result["matches"]["blocks"]) == 10
        assert result["total_matches"] == 25
        assert result["truncated"] is True

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_unexpected_shape(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"no_results": True})
        result = tool.execute_action("aztec_network_search", query="x")
        assert result["status_code"] == 502

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", None, 5, "x" * 257, "has\nnewline", "tab\there", "ctrl\x01char"],
    )
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_invalid_query_degrades_without_request(self, mock_get, tool, bad):
        result = tool.execute_action("aztec_network_search", query=bad)
        assert result["status_code"] == 0
        assert "invalid query" in result["message"]
        mock_get.assert_not_called()

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_invalid_query_does_not_echo_value(self, mock_get, tool):
        result = tool.execute_action("aztec_network_search", query="secret\x01value")
        assert "secret" not in result["message"]
        mock_get.assert_not_called()


@pytest.mark.unit
class TestContractInstance:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_success_extracts_fields(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_CONTRACT_INSTANCE_BODY)
        result = tool.execute_action(
            "aztec_network_get_contract_instance", address="0x14c4"
        )
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["address"] == "0x14c4"
        assert result["contract_class_id"] == "0x1acd"
        assert result["deployer"] == "0x0"
        assert result["artifact_contract_name"] == "Token"
        # Verbose fields are dropped.
        assert "publicKeys" not in result
        assert "salt" not in result

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_address_is_path_segment(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_CONTRACT_INSTANCE_BODY)
        tool.execute_action("aztec_network_get_contract_instance", address="0xABCdef01")
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l2/contract-instances/0xABCdef01"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_contract_class_id_falls_back_to_legacy_field(self, mock_get, tool):
        # No currentContractClassId; the plain contractClassId is used.
        body = {"address": "0x1", "contractClassId": "0xLEGACY"}
        mock_get.return_value = _resp(200, json_body=body)
        result = tool.execute_action("aztec_network_get_contract_instance", address="0x1")
        assert result["contract_class_id"] == "0xLEGACY"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_404_returns_not_found(self, mock_get, tool):
        mock_get.return_value = _resp(404, text="<html>Cannot GET</html>")
        result = tool.execute_action(
            "aztec_network_get_contract_instance", address="0x99"
        )
        assert result["status_code"] == 404
        assert "not found" in result["message"].lower()

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            5,
            "",
            "deadbeef",  # missing 0x
            "0x",  # no hex digits
            "0xGG",  # non-hex
            "0x12/../../admin",  # path traversal
            "0x12/extra",  # path delimiter
            "0x12@evil.com",  # userinfo
            "0x12 34",  # whitespace
            "0x" + "a" * 129,  # over length
        ],
    )
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_invalid_address_degrades_without_request(self, mock_get, tool, bad):
        result = tool.execute_action("aztec_network_get_contract_instance", address=bad)
        assert result["status_code"] == 0
        assert "invalid address" in result["message"]
        mock_get.assert_not_called()


@pytest.mark.unit
class TestGovernanceProposals:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_success_trims_proposals(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_PROPOSALS_BODY)
        result = tool.execute_action("aztec_network_get_governance_proposals")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["returned_count"] == 1
        assert result["total_count"] == 1
        assert result["truncated"] is False
        p = result["proposals"][0]
        assert p["proposal_id"] == "3"
        assert p["state"] == "Queued"
        assert p["created_at"] == 1778568755000

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_no_state_sends_no_query(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_PROPOSALS_BODY)
        tool.execute_action("aztec_network_get_governance_proposals")
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l1/governance/proposals"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_valid_state_is_query_param(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_PROPOSALS_BODY)
        tool.execute_action("aztec_network_get_governance_proposals", state="Active")
        assert mock_get.call_args[0][0] == _MAINNET + "/k/l1/governance/proposals?state=Active"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_truncates_to_cap(self, mock_get, tool):
        body = [{"proposalId": str(i), "state": "Active"} for i in range(30)]
        mock_get.return_value = _resp(200, json_body=body)
        result = tool.execute_action("aztec_network_get_governance_proposals")
        assert result["returned_count"] == 20
        assert result["total_count"] == 30
        assert result["truncated"] is True

    @pytest.mark.parametrize("bad", [5, "has space", "St8te", "x" * 33, "Act-ive"])
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_invalid_state_degrades_without_request(self, mock_get, tool, bad):
        result = tool.execute_action("aztec_network_get_governance_proposals", state=bad)
        assert result["status_code"] == 0
        assert "invalid state" in result["message"]
        mock_get.assert_not_called()

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_non_list_body(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"oops": True})
        result = tool.execute_action("aztec_network_get_governance_proposals")
        assert result["status_code"] == 502

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_malformed_entries_filtered_before_cap(self, mock_get, tool):
        # Non-dict entries must not consume cap slots or skew the counts:
        # 2 junk entries + 1 valid proposal must still surface the proposal.
        body = ["junk", 42, {"proposalId": "7", "state": "Active"}]
        mock_get.return_value = _resp(200, json_body=body)
        result = tool.execute_action("aztec_network_get_governance_proposals")
        assert result["returned_count"] == 1
        assert result["total_count"] == 1
        assert result["proposals"][0]["proposal_id"] == "7"


@pytest.mark.unit
class TestTips:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_flattens_stages(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=_TIPS_BODY)
        result = tool.execute_action("aztec_network_get_tips")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        # 'proposed' is flat; the rest nest under 'block'.
        assert result["proposed_height"] == 75980
        assert result["proposed_hash"] == "0x2acf"
        assert result["checkpointed_height"] == 75980
        assert result["proven_height"] == 75970
        assert result["finalized_height"] == 75938
        assert result["finalized_hash"] == "0x2721"

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_tolerates_absent_stage(self, mock_get, tool):
        mock_get.return_value = _resp(
            200, json_body={"tips": {"proposed": {"number": 5, "hash": "0xaa"}}}
        )
        result = tool.execute_action("aztec_network_get_tips")
        assert result["proposed_height"] == 5
        assert "proven_height" not in result

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_unexpected_shape(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body={"no_tips": 1})
        result = tool.execute_action("aztec_network_get_tips")
        assert result["status_code"] == 502


@pytest.mark.unit
class TestNewActionRedaction:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_search_transport_error_redacts_key(self, mock_get):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "SECRETKEY123"})
        mock_get.side_effect = requests.ConnectionError(
            "Max retries exceeded with url: /v1/SECRETKEY123/l2/search?q=x"
        )
        result = t.execute_action("aztec_network_search", query="x")
        # Transport failure (request status 0) maps to a 502 like the other
        # multi-field actions; the point here is the key never surfaces.
        assert result["status_code"] == 502
        assert "SECRETKEY123" not in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_contract_instance_error_body_redacts_key(self, mock_get):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "SECRETKEY123"})
        mock_get.return_value = _resp(
            500, text="error at /v1/SECRETKEY123/l2/contract-instances/0x1"
        )
        result = t.execute_action("aztec_network_get_contract_instance", address="0x1")
        assert "SECRETKEY123" not in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_get_blocks_transport_error_redacts_key(self, mock_get):
        t = AztecNetworkTool(config={"mainnet_base_url": _MAINNET, "api_key": "SECRETKEY123"})
        mock_get.side_effect = requests.ConnectionError(
            "Max retries exceeded with url: /v1/SECRETKEY123/l2/blocks?from=0&to=10"
        )
        result = t.execute_action(
            "aztec_network_get_blocks", from_height=0, to_height=9
        )
        assert result["status_code"] == 502
        assert "SECRETKEY123" not in result["message"]

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_search_error_body_redacts_decoded_query_value(self, mock_get, tool):
        # The query is urlencoded in the URL (spaces -> '+'), but an
        # upstream error can echo the DECODED form. Both must be redacted.
        mock_get.return_value = _resp(
            400, text="bad query: needle haystack term"
        )
        result = tool.execute_action("aztec_network_search", query="needle haystack term")
        assert "needle haystack term" not in result["message"]


@pytest.mark.unit
class TestRequestPathGuard:
    """The path grammar is asserted inside _request so a future caller
    can't smuggle a query/fragment through the path arg."""

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_malformed_path_degrades(self, mock_get, tool):
        for bad in ("l2/no-leading-slash", "/l2/x?inject=1", "/l2/x#frag", "/l2/x y"):
            result = tool._request(bad, "mainnet")
            assert result[0] == 0
            assert "malformed request path" in result[1]
        mock_get.assert_not_called()


@pytest.mark.unit
class TestLlmSchemaConversion:
    """The LLM-visible function schema is rebuilt from the action metadata
    by ToolExecutor._build_tool_parameters, which honors a per-PROPERTY
    'required' flag (not a top-level 'required' list). Guard that the
    required args actually surface as required through that real path."""

    def _converted(self, action_name):
        from application.agents.tool_executor import ToolExecutor

        meta = {m["name"]: m for m in AztecNetworkTool(config={}).get_actions_metadata()}
        action = {
            "name": action_name,
            "description": meta[action_name]["description"],
            "parameters": meta[action_name]["parameters"],
            "active": True,
        }
        tools_dict = {"0": {"name": "aztec_network", "actions": [action]}}
        schemas = ToolExecutor().prepare_tools_for_llm(tools_dict)
        return next(s["function"] for s in schemas if s["function"]["name"] == action_name)

    def test_search_query_is_required(self):
        fn = self._converted("aztec_network_search")
        assert "query" in fn["parameters"]["properties"]
        assert "query" in fn["parameters"]["required"]
        assert "network" not in fn["parameters"]["required"]

    def test_contract_instance_address_is_required(self):
        fn = self._converted("aztec_network_get_contract_instance")
        assert "address" in fn["parameters"]["required"]

    def test_governance_state_is_optional(self):
        fn = self._converted("aztec_network_get_governance_proposals")
        assert "state" in fn["parameters"]["properties"]
        assert "state" not in fn["parameters"]["required"]

    def test_get_blocks_range_bounds_are_optional(self):
        fn = self._converted("aztec_network_get_blocks")
        props = fn["parameters"]["properties"]
        assert "from_height" in props and "to_height" in props
        assert "from_height" not in fn["parameters"].get("required", [])
        assert "to_height" not in fn["parameters"].get("required", [])
