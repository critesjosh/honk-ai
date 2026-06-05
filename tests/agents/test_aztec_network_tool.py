"""Tests for application/agents/tools/aztec_network.py"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from application.agents.tools.aztec_network import AztecNetworkTool


@pytest.fixture
def tool():
    return AztecNetworkTool(config={"base_url": "https://api.testnet.aztecscan.xyz/v1", "api_key": "k"})


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
class TestUrlConstruction:
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_url_includes_api_key_in_path(self, mock_get, tool):
        mock_get.return_value = _resp(200, json_body=42)
        tool.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        assert url.endswith("/v1/k/l2/latest-height")

    @patch("application.agents.tools.aztec_network.requests.get")
    def test_base_url_trailing_slash_stripped(self, mock_get):
        t = AztecNetworkTool(config={"base_url": "https://api.testnet.aztecscan.xyz/v1/", "api_key": "k"})
        mock_get.return_value = _resp(200, json_body=42)
        t.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        # Single slash between v1 and api key
        assert "//k" not in url

    def test_invalid_base_url_degrades_to_config_error(self):
        t = AztecNetworkTool(config={"base_url": "ftp://example.com"})
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]

    def test_construction_never_raises(self):
        """ToolManager eagerly instantiates every tool on every tool
        execution. Neither a transient DNS failure nor garbage config /
        env may block the loading of unrelated tools — the constructor
        only stores config; validation happens per request."""
        AztecNetworkTool(
            config={
                "base_url": "https://does-not-resolve-1234567.invalid/v1",
                "api_key": "k",
            }
        )
        AztecNetworkTool(config={"base_url": "ftp://garbage", "api_key": "k/../admin"})

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
        t = AztecNetworkTool(
            config={"base_url": "https://api.testnet.aztecscan.xyz/v1", "api_key": "k/../admin"}
        )
        result = t.execute_action("aztec_network_get_latest_height")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        assert "k/../admin" not in result["message"]

    def test_base_url_query_fragment_degrades_to_config_error(self):
        t = AztecNetworkTool(
            config={"base_url": "https://api.testnet.aztecscan.xyz/v1?evil=1"}
        )
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
        t = AztecNetworkTool(config={"base_url": bad_url, "api_key": "k"})
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
        assert result == {"status_code": 200, "height": 80369}

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
        t = AztecNetworkTool(
            config={"base_url": "https://api.testnet.aztecscan.xyz/v1", "api_key": "SECRETKEY123"}
        )
        mock_get.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.testnet.aztecscan.xyz', port=443): "
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
        t = AztecNetworkTool(
            config={"base_url": "https://api.testnet.aztecscan.xyz/v1", "api_key": "SECRETKEY123"}
        )
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
        # Field names mirror the LIVE Aztecscan response (verified
        # against api.testnet.aztecscan.xyz): nativeStatus, not the
        # earlier-assumed finalizationStatus.
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
                    "l2NetworkId": "TESTNET",
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
    def test_success(self, mock_get, tool):
        mock_get.return_value = _resp(
            200,
            json_body={
                "l2NetworkId": "TESTNET",
                "l1ChainId": 11155111,
                "rollupVersion": "4127419662",
                "l1ContractAddresses": {
                    "rollupAddress": "0xf6d0",
                    "registryAddress": "0xa0bf",
                    "inboxAddress": "0xf1bb",
                    "outboxAddress": "0x5fe6",
                },
            },
        )
        result = tool.execute_action("aztec_network_get_chain_info")
        assert result["l2_network_id"] == "TESTNET"
        assert result["l1_chain_id"] == 11155111
        assert result["rollup_address"] == "0xf6d0"


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
    def test_six_actions(self, tool):
        meta = tool.get_actions_metadata()
        assert len(meta) == 6
        names = [m["name"] for m in meta]
        assert all(n.startswith("aztec_network_") for n in names)

    def test_unknown_action_raises(self, tool):
        with pytest.raises(ValueError, match="Unknown action"):
            tool.execute_action("aztec_network_bogus")

    def test_config_requirements_shape(self, tool):
        reqs = tool.get_config_requirements()
        assert "base_url" in reqs
        assert "api_key" in reqs
        assert reqs["base_url"]["required"] is False


@pytest.mark.unit
class TestEnvOverride:
    @patch.dict(
        "os.environ",
        {"AZTECSCAN_BASE_URL": "https://api.devnet.aztecscan.xyz/v1", "AZTECSCAN_API_KEY": "override"},
    )
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_env_overrides_default(self, mock_get):
        mock_get.return_value = _resp(200, json_body=1)
        t = AztecNetworkTool(config={})
        t.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        assert "devnet" in url
        assert "/override/" in url

    @patch.dict("os.environ", {"AZTECSCAN_BASE_URL": "https://api.devnet.aztecscan.xyz/v1"})
    @patch("application.agents.tools.aztec_network.requests.get")
    def test_config_overrides_env(self, mock_get):
        mock_get.return_value = _resp(200, json_body=1)
        t = AztecNetworkTool(config={"base_url": "https://api.testnet.aztecscan.xyz/v1"})
        t.execute_action("aztec_network_get_latest_height")
        url = mock_get.call_args[0][0]
        assert "testnet" in url
