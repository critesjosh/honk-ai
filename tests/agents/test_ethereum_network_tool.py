"""Tests for application/agents/tools/ethereum_network.py"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from application.agents.tools.ethereum_network import EthereumNetworkTool


@pytest.fixture
def tool():
    return EthereumNetworkTool(
        config={
            "mainnet_rpc_url": "https://example.com",
            "sepolia_rpc_url": "https://example.org",
        }
    )


def _rpc_resp(status_code: int = 200, result=None, error=None, raw_text: str | bytes | None = None):
    r = MagicMock()
    r.status_code = status_code
    if raw_text is not None:
        body_bytes = raw_text if isinstance(raw_text, bytes) else raw_text.encode("utf-8")
        r.raw.read.return_value = body_bytes
        return r
    body = {"jsonrpc": "2.0", "id": 1}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    r.raw.read.return_value = json.dumps(body).encode("utf-8")
    return r


@pytest.mark.unit
class TestNetworkSelection:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_mainnet_used_by_default(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0x1")
        tool.execute_action("ethereum_network_get_chain_id")
        assert mock_post.call_args[0][0] == "https://example.com"

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_sepolia_routes_to_sepolia_url(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0xaa36a7")
        tool.execute_action("ethereum_network_get_chain_id", network="sepolia")
        assert mock_post.call_args[0][0] == "https://example.org"

    def test_unsupported_network_fails(self, tool):
        result = tool.execute_action("ethereum_network_get_chain_id", network="polygon")
        assert result["status_code"] == 0
        assert "Unsupported network" in result["message"]

    def test_invalid_rpc_url_degrades_to_config_error(self):
        """Constructor must NOT raise — ToolManager eagerly instantiates
        every tool on every tool execution, so a raising constructor
        would break ALL tools. Bad config surfaces per-action instead."""
        t = EthereumNetworkTool(config={"mainnet_rpc_url": "ftp://example.com"})
        result = t.execute_action("ethereum_network_get_chain_id")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        # The bad value itself is never echoed to the LLM.
        assert "ftp://example.com" not in result["message"]

    def test_construction_never_raises(self):
        """Constructor must not call DNS or validate — ToolManager loads
        every tool eagerly and a transient DNS failure or malformed
        config/env shouldn't break the lot."""
        EthereumNetworkTool(
            config={
                "mainnet_rpc_url": "https://does-not-resolve-1234567.invalid",
                "sepolia_rpc_url": "https://does-not-resolve-7654321.invalid",
            }
        )
        EthereumNetworkTool(config={"mainnet_rpc_url": "not a url at all"})

    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://127.0.0.1\\@example.com",
            "https://127.0.0.1@example.com",
        ],
    )
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_authority_confusion_url_rejected(self, mock_post, bad_url):
        """urllib.parse vs requests disagree on backslash / userinfo
        authorities — that mismatch is an SSRF bypass, so reject pre-flight
        and never issue the request."""
        t = EthereumNetworkTool(config={"mainnet_rpc_url": bad_url})
        result = t.execute_action("ethereum_network_get_chain_id")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        mock_post.assert_not_called()

    @patch.dict("os.environ", {"ETHEREUM_RPC_URL": "garbage-no-scheme"})
    def test_bad_env_var_degrades_to_config_error(self):
        """A malformed optional env var must not break tool construction
        (and with it every other tool) — only this tool's actions. The
        other network keeps working."""
        t = EthereumNetworkTool(config={})
        result = t.execute_action("ethereum_network_get_chain_id")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        with patch("application.agents.tools.ethereum_network.requests.post") as mock_post:
            mock_post.return_value = _rpc_resp(result="0xaa36a7")
            ok = t.execute_action("ethereum_network_get_chain_id", network="sepolia")
        assert ok["status_code"] == 200

    def test_non_string_network_rejected(self, tool):
        result = tool.execute_action("ethereum_network_get_chain_id", network=123)
        assert result["status_code"] == 0
        assert "network must be a string" in result["message"]


@pytest.mark.unit
class TestBlockNumber:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_decodes_hex(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0x17f092e")
        result = tool.execute_action("ethereum_network_get_block_number")
        assert result["status_code"] == 200
        assert result["network"] == "mainnet"
        assert result["block_number"] == 0x17F092E

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_request_body_shape(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0x1")
        tool.execute_action("ethereum_network_get_block_number")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "eth_blockNumber"
        assert payload["params"] == []

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_transport_failure(self, mock_post, tool):
        mock_post.side_effect = requests.Timeout("timeout")
        result = tool.execute_action("ethereum_network_get_block_number")
        assert result["status_code"] == 0
        assert "timeout" in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_transport_failure_redacts_url_token(self, mock_post):
        """requests exception strings embed the request URL — a provider
        token in the path must never reach logs or the tool result."""
        t = EthereumNetworkTool(
            config={"mainnet_rpc_url": "https://mainnet.infura.io/v3/PROVIDER-TOKEN-123"}
        )
        mock_post.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='mainnet.infura.io', port=443): "
            "Max retries exceeded with url: /v3/PROVIDER-TOKEN-123"
        )
        result = t.execute_action("ethereum_network_get_block_number")
        assert result["status_code"] == 0
        assert "PROVIDER-TOKEN-123" not in result["message"]
        assert "<redacted>" in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_transport_failure_redacts_query_token(self, mock_post):
        t = EthereumNetworkTool(
            config={"mainnet_rpc_url": "https://rpc.example.com/?apikey=QUERY-TOKEN-456"}
        )
        mock_post.side_effect = requests.ConnectionError(
            "Max retries exceeded with url: /?apikey=QUERY-TOKEN-456"
        )
        result = t.execute_action("ethereum_network_get_block_number")
        assert "QUERY-TOKEN-456" not in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_error_body_redacts_url_token(self, mock_post):
        """Upstream error pages can echo the request path (e.g. Apache's
        default 404) — a provider token in that path must be redacted."""
        t = EthereumNetworkTool(
            config={"mainnet_rpc_url": "https://mainnet.infura.io/v3/PROVIDER-TOKEN-123"}
        )
        mock_post.return_value = _rpc_resp(
            404, raw_text="The requested URL /v3/PROVIDER-TOKEN-123 was not found."
        )
        result = t.execute_action("ethereum_network_get_block_number")
        assert result["status_code"] == 404
        assert "PROVIDER-TOKEN-123" not in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_jsonrpc_error_redacts_url_token(self, mock_post):
        """A hostile/strict node can echo the request path token in a
        HTTP-200 JSON-RPC error (e.g. Infura 'invalid project id')."""
        t = EthereumNetworkTool(
            config={"mainnet_rpc_url": "https://mainnet.infura.io/v3/PROVIDER-TOKEN-123"}
        )
        # Upstream echoes only the bare token, NOT the full /v3/<token>
        # path — redaction must still catch it.
        mock_post.return_value = _rpc_resp(
            error={"code": -32000, "message": "invalid project id: PROVIDER-TOKEN-123"}
        )
        result = t.execute_action("ethereum_network_get_block_number")
        assert "PROVIDER-TOKEN-123" not in result["message"]

    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://example.com\t/v3/TOKEN",   # tab — requests would %09-encode it
            "https://exa mple.com",             # raw whitespace in authority
            "https://example.com\n",            # newline
        ],
    )
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_control_char_url_rejected(self, mock_post, bad_url):
        t = EthereumNetworkTool(config={"mainnet_rpc_url": bad_url})
        result = t.execute_action("ethereum_network_get_chain_id")
        assert result["status_code"] == 0
        assert "configuration error" in result["message"]
        mock_post.assert_not_called()

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_redirect_is_refused(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(302, raw_text="moved")
        result = tool.execute_action("ethereum_network_get_block_number")
        assert result["status_code"] == 0
        assert "redirect" in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_response_size_cap(self, mock_post, tool):
        oversized = b"x" * (520 * 1024)
        mock_post.return_value = _rpc_resp(200, raw_text=oversized)
        result = tool.execute_action("ethereum_network_get_block_number")
        assert "size cap" in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_jsonrpc_error_branch(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(error={"code": -32046, "message": "blocked"})
        result = tool.execute_action("ethereum_network_get_block_number")
        # HTTP 200 with a JSON-RPC error body — we surface the upstream
        # HTTP status, not 502, since the upstream did respond.
        assert result["status_code"] == 200
        assert "blocked" in result["message"]


@pytest.mark.unit
class TestGasPrice:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_wei_and_gwei(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0x77359400")  # 2e9 wei = 2 gwei
        result = tool.execute_action("ethereum_network_get_gas_price")
        assert result["gas_price_wei"] == 2_000_000_000
        assert result["gas_price_gwei"] == 2.0


@pytest.mark.unit
class TestChainId:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_chain_id_decoded(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result="0xaa36a7")
        result = tool.execute_action("ethereum_network_get_chain_id", network="sepolia")
        assert result["chain_id"] == 11155111
        assert result["network"] == "sepolia"


@pytest.mark.unit
class TestSyncStatus:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_synced(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result=False)
        result = tool.execute_action("ethereum_network_get_sync_status")
        assert result["syncing"] is False

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_actively_syncing(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(
            result={"currentBlock": "0x10", "highestBlock": "0x20", "startingBlock": "0x0"}
        )
        result = tool.execute_action("ethereum_network_get_sync_status")
        assert result["syncing"] is True
        assert result["current_block"] == 16
        assert result["highest_block"] == 32


@pytest.mark.unit
class TestGetBlock:
    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_trims_response(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(
            result={
                "number": "0x1234",
                "hash": "0xaaa",
                "parentHash": "0xbbb",
                "timestamp": "0x60000000",
                "gasUsed": "0x5208",
                "gasLimit": "0x1c9c380",
                "baseFeePerGas": "0x3b9aca00",
                "miner": "0xccc",
                "transactions": ["0x1", "0x2"],  # not surfaced
                "extraData": "0xignored",
            }
        )
        result = tool.execute_action("ethereum_network_get_block")
        assert result["number"] == 4660
        assert result["hash"] == "0xaaa"
        assert result["gas_used"] == 21000
        assert "transactions" not in result
        assert "extraData" not in result

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_block_tag_pass_through(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result={"number": "0x1"})
        tool.execute_action("ethereum_network_get_block", block_tag="finalized")
        params = mock_post.call_args.kwargs["json"]["params"]
        assert params[0] == "finalized"
        assert params[1] is False

    def test_block_tag_rejects_decimal(self, tool):
        # Decimal block numbers would fail upstream — reject pre-call.
        result = tool.execute_action("ethereum_network_get_block", block_tag="12345")
        assert result["status_code"] == 0
        assert "block_tag must be" in result["message"]

    def test_block_tag_rejects_non_string(self, tool):
        result = tool.execute_action("ethereum_network_get_block", block_tag=42)
        assert result["status_code"] == 0
        assert "block_tag must be a string" in result["message"]

    @patch("application.agents.tools.ethereum_network.requests.post")
    def test_block_tag_hex_accepted(self, mock_post, tool):
        mock_post.return_value = _rpc_resp(result={"number": "0xabc"})
        tool.execute_action("ethereum_network_get_block", block_tag="0xabc123")
        params = mock_post.call_args.kwargs["json"]["params"]
        assert params[0] == "0xabc123"


@pytest.mark.unit
class TestMisc:
    def test_unknown_action(self, tool):
        with pytest.raises(ValueError, match="Unknown action"):
            tool.execute_action("ethereum_network_bogus")

    def test_metadata_five_actions(self, tool):
        meta = tool.get_actions_metadata()
        assert len(meta) == 5
        names = [m["name"] for m in meta]
        assert all(n.startswith("ethereum_network_") for n in names)

    def test_config_requirements_marks_secret(self, tool):
        reqs = tool.get_config_requirements()
        # RPC URLs can embed provider tokens; treat as secret in the
        # config UI even though the defaults are public.
        assert reqs["mainnet_rpc_url"]["secret"] is True
        assert reqs["sepolia_rpc_url"]["secret"] is True

    def test_hex_to_int_helpers(self):
        h = EthereumNetworkTool._hex_to_int
        assert h(None) is None
        assert h("0x10") == 16
        assert h(42) == 42
        assert h("not hex") is None
