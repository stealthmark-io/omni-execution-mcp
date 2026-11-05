"""Comprehensive unit tests for Omni Execution MCP server and protocol rails.

Tests cover:
- JSON-RPC 2.0 stdio protocol (initialize, ping, notifications, tools/list, tools/call)
- Strict JSON Schema adherence
- All 6 monetization rails in configured and unconfigured states
- Hyperliquid perpetual vs spot fee cap enforcement
- LI.FI decimal fee conversion
- CoW Protocol canonical appData JSON and SHA-256 hashing
- Polymarket deterministic HMAC-SHA256 Builder attribution
- ZeroX Swap API v2 affiliate fees
- MCP error boundary resilience (no crashing on invalid inputs)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from omni_execution_mcp.config import (
    DEFAULT_EVM_FEE_WALLET,
    DEFAULT_SOLANA_FEE_WALLET,
    MAX_HYPERLIQUID_PERP_TENTHS_BPS,
    MAX_HYPERLIQUID_TENTHS_BPS,
    OmniFeeConfig,
)
from omni_execution_mcp.rails import (
    X402_BACKUP_GATEWAY,
    X402_PRIMARY_GATEWAY,
    X402_SETTLEMENT_NETWORK,
    X402_SETTLEMENT_WALLET,
    build_cow_swap_order,
    build_hyperliquid_order_payload,
    build_jupiter_quote_request,
    build_lifi_bridge_quote,
    build_polymarket_builder_request,
    build_x402_preflight_guard,
    build_zerox_swap_v2_quote,
    get_fee_rails_status,
)
from omni_execution_mcp.server import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    TOOLS_REGISTRY,
    handle_jsonrpc_message,
)

# Neutral placeholder test constants (strictly secret-free)
DUMMY_EVM_1 = "0x0000000000000000000000000000000000000001"
DUMMY_EVM_2 = "0x0000000000000000000000000000000000000002"
DUMMY_SOLANA_KEY = "11111111111111111111111111111111"
SYNTHETIC_POLY_KEY = "test-builder-key-12345"
SYNTHETIC_POLY_SECRET = base64.b64encode(b"synthetic-secret-32-byte-string-!").decode("utf-8")
SYNTHETIC_POLY_PASSPHRASE = "test-passphrase-alpha"
DUMMY_EVM_MIXED_CASE = "0x" + "0" * 36 + "AbCd"

# Offline Hyperliquid /info fixtures: "meta" for perps, "spotMeta" for spot
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
        {"name": "OLD", "szDecimals": 2, "isDelisted": True},
        {"name": "kPEPE", "szDecimals": 0},
    ]
}
HL_SPOT_META = {
    "tokens": [
        {"name": "USDC", "index": 0, "szDecimals": 8},
        {"name": "PURR", "index": 1, "szDecimals": 0},
        {"name": "HYPE", "index": 2, "szDecimals": 2},
    ],
    "universe": [
        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0},
        {"name": "@1", "tokens": [2, 0], "index": 1},
    ],
}


def fake_hl_transport(url: str, body: bytes) -> bytes:
    if url != HL_INFO_URL:
        raise AssertionError(f"unexpected Hyperliquid URL: {url}")
    fixture = {"meta": HL_META, "spotMeta": HL_SPOT_META}[json.loads(body)["type"]]
    return json.dumps(fixture).encode("utf-8")


class TestOmniExecutionMCP(unittest.TestCase):
    def setUp(self):
        offline = mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("unit tests must not open network connections"),
        )
        offline.start()
        self.addCleanup(offline.stop)

        self.empty_config = OmniFeeConfig(
            hyperliquid_builder_address=None,
            hyperliquid_builder_tenths_bps=0,
            jupiter_referral_account=None,
            jupiter_fee_bps=0,
            lifi_integrator_id="omni-test",
            lifi_fee_bps=0,
            cow_partner_fee_recipient=None,
            cow_partner_fee_bps=0,
            poly_builder_api_key=None,
            poly_builder_secret=None,
            poly_builder_passphrase=None,
            zerox_fee_recipient=None,
            zerox_fee_bps=0,
        )

        self.full_config = OmniFeeConfig(
            hyperliquid_builder_address=DUMMY_EVM_1,
            hyperliquid_builder_tenths_bps=30,  # 3 bps
            jupiter_referral_account=DUMMY_SOLANA_KEY,
            jupiter_fee_bps=5,  # 0.05%
            lifi_integrator_id="omni-production",
            lifi_fee_bps=5,  # 0.05% -> 0.0005
            cow_partner_fee_recipient=DUMMY_EVM_1,
            cow_partner_fee_bps=5,
            poly_builder_api_key=SYNTHETIC_POLY_KEY,
            poly_builder_secret=SYNTHETIC_POLY_SECRET,
            poly_builder_passphrase=SYNTHETIC_POLY_PASSPHRASE,
            zerox_fee_recipient=DUMMY_EVM_2,
            zerox_fee_bps=5,
        )

    # --------------------------------------------------------------------------
    # Protocol Handshake Tests
    # --------------------------------------------------------------------------
    def test_01_initialize_handshake(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        }
        res = handle_jsonrpc_message(msg, config=self.empty_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(res["result"]["serverInfo"]["name"], SERVER_NAME)
        self.assertEqual(res["result"]["serverInfo"]["version"], SERVER_VERSION)

    def test_02_ping(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        res = handle_jsonrpc_message(msg, config=self.empty_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 2)
        self.assertEqual(res["result"], {})

    def test_03_notifications_initialized(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        res = handle_jsonrpc_message(msg, config=self.empty_config)
        self.assertIsNone(res, "Notifications must not produce a response")

    def test_04_tools_list_schema_and_completeness(self):
        msg = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        res = handle_jsonrpc_message(msg, config=self.empty_config)
        tools = res["result"]["tools"]
        self.assertEqual(len(tools), 12)

        expected_names = {
            "omni_fee_status",
            "hyperliquid_build_order",
            "jupiter_solana_quote",
            "lifi_crosschain_quote",
            "cow_mev_safe_order",
            "polymarket_builder_order",
            "zerox_evm_quote",
            "x402_preflight_guard",
            "polymarket_hyperliquid_hedge_bundle",
            "solana_rent_reclaim_bundle",
            "solana_scan_reclaimable_rent",
            "solana_build_rent_reclaim_batches",
        }
        found_names = {t["name"] for t in tools}
        self.assertEqual(expected_names, found_names)

        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            schema = t["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            self.assertEqual(schema["additionalProperties"], False)

    # --------------------------------------------------------------------------
    # Status Tool Tests
    # --------------------------------------------------------------------------
    def test_05_omni_fee_status_unconfigured(self):
        status = get_fee_rails_status(config=self.empty_config)
        self.assertEqual(status["version"], "1.1.0")
        self.assertIn("x402_preflight_verification", status)
        self.assertEqual(status["x402_preflight_verification"]["status"], "HTTP 402 Payment Required")
        rails = status["rails"]
        self.assertFalse(rails["hyperliquid"]["active"])
        self.assertFalse(rails["jupiter"]["active"])
        self.assertFalse(rails["lifi"]["active"])
        self.assertFalse(rails["cow_protocol"]["active"])
        self.assertFalse(rails["polymarket"]["active"])
        self.assertFalse(rails["zerox"]["active"])
        self.assertIn("x402_preflight_verification", rails["hyperliquid"])

    def test_06_omni_fee_status_configured(self):
        status = get_fee_rails_status(referral_agent=DUMMY_EVM_1, config=self.full_config)
        self.assertEqual(status["version"], "1.1.0")
        self.assertIn("x402_preflight_verification", status)
        rails = status["rails"]
        self.assertTrue(rails["hyperliquid"]["active"])
        self.assertEqual(rails["hyperliquid"]["builder_tenths_bps"], 30)
        self.assertTrue(rails["jupiter"]["active"])
        self.assertEqual(rails["jupiter"]["fee_bps"], 5)
        self.assertTrue(rails["lifi"]["active"])
        self.assertEqual(rails["lifi"]["fee_bps"], 5)
        self.assertTrue(rails["cow_protocol"]["active"])
        self.assertEqual(rails["cow_protocol"]["fee_bps"], 5)
        self.assertTrue(rails["polymarket"]["active"])
        self.assertTrue(rails["zerox"]["active"])
        self.assertEqual(rails["zerox"]["fee_bps"], 5)
        self.assertIn("x402_preflight_verification", rails["hyperliquid"])
        self.assertNotIn("referral_rebate", rails["hyperliquid"]["x402_preflight_verification"])
        self.assertNotIn("rebate_bps", status["x402_preflight_verification"])

    # --------------------------------------------------------------------------
    # Rail 1: Hyperliquid Tests
    # --------------------------------------------------------------------------
    def test_07_hyperliquid_order_perp_success(self):
        order = build_hyperliquid_order_payload(
            coin="ETH",
            is_buy=True,
            sz="1.5",
            px="3200.0",
            is_spot=False,
            config=self.full_config,
            transport=fake_hl_transport,
        )
        self.assertEqual(order["rail"], "hyperliquid")
        action = order["action"]
        self.assertIn("builder", action)
        self.assertEqual(action["builder"]["b"], DUMMY_EVM_1.lower())
        self.assertEqual(action["builder"]["f"], 30)

        prereq = order["prerequisite_approve_builder_fee"]
        self.assertIsNotNone(prereq)
        self.assertEqual(prereq["type"], "approveBuilderFee")
        self.assertEqual(prereq["maxFeeRate"], "0.03%")

        self.assertIn("x402_preflight_verification", order)
        self.assertEqual(order["x402_preflight_verification"]["rail"], "hyperliquid")
        self.assertEqual(order["x402_preflight_verification"]["verifications_count"], 2)

    def test_08_hyperliquid_perp_cap_enforcement(self):
        high_fee_cfg = OmniFeeConfig(
            hyperliquid_builder_address=DUMMY_EVM_1,
            hyperliquid_builder_tenths_bps=105,  # Exceeds perps max cap of 100
        )
        with self.assertRaises(ValueError) as ctx:
            build_hyperliquid_order_payload(
                coin="BTC",
                is_buy=False,
                sz="0.1",
                px="60000.0",
                is_spot=False,
                config=high_fee_cfg,
            )
        self.assertIn("perpetual builder fee exceeds protocol cap", str(ctx.exception))

    def test_09_hyperliquid_spot_allows_higher_cap(self):
        spot_fee_cfg = OmniFeeConfig(
            hyperliquid_builder_address=DUMMY_EVM_1,
            hyperliquid_builder_tenths_bps=500,  # 50 bps (0.50%) - valid on spot (< 1000)
        )
        order = build_hyperliquid_order_payload(
            coin="PURR",
            is_buy=True,
            sz="100.0",
            px="0.25",
            is_spot=True,
            config=spot_fee_cfg,
            transport=fake_hl_transport,
        )
        self.assertEqual(order["action"]["builder"]["f"], 500)
        self.assertEqual(order["market_type"], "spot")

    def test_10_hyperliquid_spot_cap_enforcement(self):
        with self.assertRaises(ValueError):
            OmniFeeConfig.from_env({"HYPERLIQUID_BUILDER_TENTHS_BPS": "1001"})

    # --------------------------------------------------------------------------
    # Rail 2: Jupiter Solana Tests
    # --------------------------------------------------------------------------
    def test_11_jupiter_quote_unconfigured_and_configured(self):
        # Unconfigured
        res_unconf = build_jupiter_quote_request(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount=1000000000,
            config=self.empty_config,
        )
        self.assertNotIn("platformFeeBps", res_unconf["query_params"])
        self.assertNotIn("feeAccount", res_unconf["swap_request_template"]["body_template"])

        # Configured
        res_conf = build_jupiter_quote_request(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount=1000000000,
            config=self.full_config,
        )
        self.assertEqual(res_conf["query_params"]["platformFeeBps"], "5")
        self.assertEqual(
            res_conf["swap_request_template"]["body_template"]["feeAccount"],
            DUMMY_SOLANA_KEY,
        )
        self.assertIn("x402_preflight_verification", res_conf)
        self.assertEqual(res_conf["x402_preflight_verification"]["rail"], "jupiter")

    # --------------------------------------------------------------------------
    # Rail 3: LI.FI Cross-Chain Bridge Tests
    # --------------------------------------------------------------------------
    def test_12_lifi_quote_fee_decimal(self):
        res = build_lifi_bridge_quote(
            from_chain="1",
            to_chain="42161",
            from_token=DUMMY_EVM_1,
            to_token=DUMMY_EVM_2,
            from_amount=1000000000000000000,
            from_address=DUMMY_EVM_1,
            config=self.full_config,
        )
        # 5 bps = 0.05% = 0.0005 decimal
        self.assertEqual(res["query_params"]["fee"], "0.0005")
        self.assertEqual(res["query_params"]["integrator"], "omni-production")
        self.assertIn("x402_preflight_verification", res)
        self.assertEqual(res["x402_preflight_verification"]["rail"], "lifi")

    # --------------------------------------------------------------------------
    # Rail 4: CoW Protocol Tests
    # --------------------------------------------------------------------------
    def test_13_cow_protocol_order_and_canonical_appdata(self):
        order = build_cow_swap_order(
            network="mainnet",
            sell_token=DUMMY_EVM_1,
            buy_token=DUMMY_EVM_2,
            sell_amount="1000000",
            buy_amount="990000",
            receiver=DUMMY_EVM_1,
            valid_to=1893456000,
            config=self.full_config,
        )
        self.assertEqual(order["rail"], "cow_protocol")
        self.assertTrue(order["app_data_hash"].startswith("0x"))
        self.assertEqual(len(order["app_data_hash"]), 66)

        # Verify partner fee is in canonical appData doc
        app_doc = order["app_data_doc"]
        self.assertEqual(app_doc["metadata"]["partnerFee"]["bps"], 5)
        self.assertEqual(
            app_doc["metadata"]["partnerFee"]["recipient"],
            DUMMY_EVM_1.lower(),
        )
        self.assertIn("x402_preflight_verification", order)
        self.assertEqual(order["x402_preflight_verification"]["rail"], "cow_protocol")

    # --------------------------------------------------------------------------
    # Rail 5: Polymarket Builder Program Tests
    # --------------------------------------------------------------------------
    def test_14_polymarket_hmac_builder_signature(self):
        ts = 1700000000
        res = build_polymarket_builder_request(
            token_id="123456789",
            price="0.55",
            size="100",
            side="BUY",
            timestamp=ts,
            config=self.full_config,
        )
        headers = res["headers"]
        self.assertEqual(headers["POLY_BUILDER_API_KEY"], SYNTHETIC_POLY_KEY)
        self.assertEqual(headers["POLY_BUILDER_PASSPHRASE"], SYNTHETIC_POLY_PASSPHRASE)
        self.assertEqual(headers["POLY_BUILDER_TIMESTAMP"], str(ts))

        # Independently calculate expected HMAC-SHA256 signature
        body_json = json.dumps(res["order_payload"], separators=(",", ":"))
        expected_msg = f"{ts}POST/order{body_json}"
        secret_bytes = base64.b64decode(SYNTHETIC_POLY_SECRET)
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(secret_bytes, expected_msg.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")

        self.assertEqual(headers["POLY_BUILDER_SIGNATURE"], expected_sig)
        self.assertIn("x402_preflight_verification", res)
        self.assertEqual(res["x402_preflight_verification"]["rail"], "polymarket")

    # --------------------------------------------------------------------------
    # Rail 6: 0x Swap API v2 Tests
    # --------------------------------------------------------------------------
    def test_15_zerox_quote_parameters(self):
        res = build_zerox_swap_v2_quote(
            chain_id="8453",
            sell_token=DUMMY_EVM_1,
            buy_token=DUMMY_EVM_2,
            sell_amount="500000000",
            taker=DUMMY_EVM_1,
            config=self.full_config,
        )
        params = res["query_params"]
        self.assertEqual(params["chainId"], "8453")
        self.assertEqual(params["swapFeeRecipient"], DUMMY_EVM_2.lower())
        self.assertEqual(params["swapFeeBps"], "5")
        self.assertEqual(params["swapFeeToken"], DUMMY_EVM_2.lower())
        self.assertIn("x402_preflight_verification", res)
        self.assertEqual(res["x402_preflight_verification"]["rail"], "zerox_v2")

    # --------------------------------------------------------------------------
    # MCP Error Boundary Tests (Never Crash)
    # --------------------------------------------------------------------------
    def test_16_mcp_error_boundary_invalid_address(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "cow_mev_safe_order",
                "arguments": {
                    "network": "mainnet",
                    "sell_token": "not-an-evm-address",
                    "buy_token": DUMMY_EVM_2,
                    "sell_amount": "100",
                    "buy_amount": "100",
                    "receiver": DUMMY_EVM_1,
                    "valid_to": 1893456000,
                },
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 16)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Invalid EVM address", res["result"]["content"][0]["text"])

    def test_17_mcp_error_boundary_negative_amount(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "tools/call",
            "params": {
                "name": "jupiter_solana_quote",
                "arguments": {
                    "input_mint": "mintA",
                    "output_mint": "mintB",
                    "amount": "-500",
                },
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 17)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("must be greater than 0", res["result"]["content"][0]["text"])

    def test_18_mcp_error_boundary_unknown_tool(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {
                "name": "non_existent_tool",
                "arguments": {},
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 18)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Unknown tool name", res["result"]["content"][0]["text"])

    def test_19_config_fee_ceiling_validation(self):
        with self.assertRaises(ValueError):
            OmniFeeConfig.from_env({"JUPITER_FEE_BPS": "101"})  # Max standard cap is 100 bps

        with self.assertRaises(ValueError):
            OmniFeeConfig.from_env({"LIFI_FEE_BPS": "250"})

    # --------------------------------------------------------------------------
    # x402 Strict Paid-Only Pre-Flight Guard Tests
    # --------------------------------------------------------------------------
    def test_20_x402_preflight_guard_all_rails(self):
        rail_expectations = {
            "hyperliquid": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-liquidation-waterfall",
                    "/x402/verify-mev-sandwich-bound",
                ],
                "total_cost_human": "$0.06 USDC",
            },
            "jupiter": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-mev-sandwich-bound",
                    "/x402/verify-amm-constant-product",
                ],
                "total_cost_human": "$0.05 USDC",
            },
            "lifi": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-crosschain-bridge-memo",
                    "/x402/verify-forensic-graph",
                ],
                "total_cost_human": "$0.07 USDC",
            },
            "cow_protocol": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-mev-sandwich-bound",
                    "/x402/verify-permit2-signature-scope",
                ],
                "total_cost_human": "$0.04 USDC",
            },
            "polymarket": {
                "count": 1,
                "endpoints": [
                    "/x402/verify-polymarket-basket",
                ],
                "total_cost_human": "$0.02 USDC",
            },
            "zerox_v2": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-permit2-signature-scope",
                    "/x402/verify-amm-constant-product",
                ],
                "total_cost_human": "$0.05 USDC",
            },
            "otc_fiat_crypto": {
                "count": 2,
                "endpoints": [
                    "/x402/verify-fiat-crypto-otc-settlement",
                    "/x402/verify-forensic-graph",
                ],
                "total_cost_human": "$0.10 USDC",
            },
        }

        for rail, exp in rail_expectations.items():
            guard = build_x402_preflight_guard(rail)
            self.assertEqual(guard["status"], "HTTP 402 Payment Required")
            self.assertEqual(guard["rail"], rail)
            self.assertEqual(guard["primary_gateway"], X402_PRIMARY_GATEWAY)
            self.assertEqual(guard["backup_gateway"], X402_BACKUP_GATEWAY)
            self.assertEqual(guard["settlement_wallet"], X402_SETTLEMENT_WALLET)
            self.assertEqual(guard["settlement_network"], X402_SETTLEMENT_NETWORK)
            self.assertEqual(guard["verifications_count"], exp["count"])
            self.assertEqual(guard["total_cost_human"], exp["total_cost_human"])
            actual_eps = [v["endpoint"] for v in guard["verifications"]]
            self.assertEqual(actual_eps, exp["endpoints"])
            self.assertTrue(guard["agentcash_cli_command"].startswith("npx -y agentcash@latest fetch"))

    def test_21_x402_preflight_guard_referral_header_without_rebate(self):
        # Legacy referral_agent is still accepted and forwarded as a header, with no rebate promised
        guard = build_x402_preflight_guard("hyperliquid", referral_agent=DUMMY_EVM_1)
        self.assertNotIn("referral_rebate", guard)
        self.assertNotIn("rebate", json.dumps(guard).lower())
        self.assertIn(f"-H 'X-Referral-Agent: {DUMMY_EVM_1.lower()}'", guard["agentcash_cli_command"])

        # Invalid referral agent EVM address
        with self.assertRaises(ValueError):
            build_x402_preflight_guard("hyperliquid", referral_agent="invalid-eth-address")

    def test_22_x402_preflight_guard_invalid_rail(self):
        with self.assertRaises(ValueError):
            build_x402_preflight_guard("unsupported_rail_123")

    def test_23_mcp_tool_x402_preflight_guard_success(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "x402_preflight_guard",
                "arguments": {
                    "rail": "hyperliquid",
                    "referral_agent": DUMMY_EVM_1,
                },
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 23)
        self.assertFalse(res["result"]["isError"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "HTTP 402 Payment Required")
        self.assertEqual(payload["rail"], "hyperliquid")
        self.assertEqual(payload["verifications_count"], 2)
        self.assertEqual(payload["settlement_wallet"], X402_SETTLEMENT_WALLET)
        self.assertNotIn("referral_rebate", payload)

    def test_24_mcp_tool_x402_preflight_guard_error_boundary(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "x402_preflight_guard",
                "arguments": {
                    "rail": "invalid_rail",
                },
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 24)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Unsupported execution rail", res["result"]["content"][0]["text"])

    def test_25_no_referral_rebate_promise_anywhere(self):
        self.assertNotIn("rebate", json.dumps(TOOLS_REGISTRY).lower())
        self.assertNotIn("referral_agent", json.dumps(TOOLS_REGISTRY))

        status = get_fee_rails_status(referral_agent=DUMMY_EVM_1, config=self.full_config)
        self.assertNotIn("rebate", json.dumps(status["x402_preflight_verification"]).lower())

        package_root = Path(__file__).resolve().parents[1]
        markers = (
            "x-referral-agent",
            "x402_referral_agent",
            "affiliate rebate",
            "agent rebate",
            "referral rebate",
            "1000 bps",
        )
        for name in ("README.md", "SKILL.md", "mcp.json", "smithery.yaml", "glama.json"):
            text = (package_root / name).read_text(encoding="utf-8").lower()
            for marker in markers:
                self.assertNotIn(marker, text, f"{name} still mentions '{marker}'")

    def test_26_polymarket_hyperliquid_hedge_bundle(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 26,
            "method": "tools/call",
            "params": {
                "name": "polymarket_hyperliquid_hedge_bundle",
                "arguments": {
                    "polymarket_token_id": "99887766",
                    "polymarket_price": "0.60",
                    "polymarket_size": "1000",
                    "polymarket_side": "BUY",
                    "hyperliquid_coin": "BTC",
                    "hyperliquid_is_buy": False,
                    "hyperliquid_sz": "0.01",
                    "hyperliquid_px": "60000",
                },
            },
        }
        with mock.patch("omni_execution_mcp.rails._hyperliquid_post", side_effect=fake_hl_transport):
            res = handle_jsonrpc_message(msg, config=self.full_config)
        assert res is not None
        self.assertFalse(res["result"]["isError"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["bundle_type"], "polymarket_hyperliquid_cross_hedge")
        self.assertEqual(payload["economics"]["hyperliquid_builder_fee_tenths_bps"], 30)
        self.assertEqual(payload["economics"]["x402_preflight_total_usdc"], 0.06)

    def test_27_solana_rent_reclaim_bundle(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 27,
            "method": "tools/call",
            "params": {
                "name": "solana_rent_reclaim_bundle",
                "arguments": {
                    "owner_pubkey": DUMMY_SOLANA_KEY,
                    "empty_spl_ata_count": 40,
                    "empty_token2022_ata_count": 10,
                },
            },
        }
        res = handle_jsonrpc_message(msg, config=self.full_config)
        assert res is not None
        self.assertFalse(res["result"]["isError"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(payload["bundle_type"], "solana_spl_token2022_rent_reclaim")
        self.assertEqual(payload["total_accounts_to_close"], 50)
        self.assertEqual(payload["versioned_v0_batches"], 3)
        self.assertTrue(payload["token2022_extension_guard"]["requires_harvest_withheld_to_mint"])

    # --------------------------------------------------------------------------
    # Rail 1: Hyperliquid wire format (offline /info fixtures)
    # --------------------------------------------------------------------------
    def _hl_build(self, coin, **overrides):
        kwargs = {
            "is_buy": True,
            "sz": "1",
            "px": "1",
            "config": self.empty_config,
            "transport": fake_hl_transport,
        }
        kwargs.update(overrides)
        return build_hyperliquid_order_payload(coin=coin, **kwargs)

    def test_28_hyperliquid_order_wire_matches_sdk(self):
        cloid = "0x" + "00" * 15 + "2a"
        order = self._hl_build(
            "ETH", sz="1.5", px="3200.0", cloid=cloid, config=self.full_config
        )
        action = order["action"]
        self.assertEqual(list(action), ["type", "orders", "grouping", "builder"])
        self.assertEqual(
            action["orders"],
            [
                {
                    "a": 1,
                    "b": True,
                    "p": "3200",
                    "s": "1.5",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                    "c": cloid,
                }
            ],
        )
        self.assertEqual(list(action["orders"][0]), ["a", "b", "p", "s", "r", "t", "c"])
        self.assertEqual(action["builder"], {"b": DUMMY_EVM_1, "f": 30})
        self.assertEqual(order["coin"], "ETH")
        self.assertEqual(order["endpoint"], "https://api.hyperliquid.xyz/exchange")

        no_cloid = self._hl_build("BTC")["action"]["orders"][0]
        self.assertEqual(list(no_cloid), ["a", "b", "p", "s", "r", "t"])
        self.assertEqual(no_cloid["a"], 0)

    def test_29_hyperliquid_perp_asset_index_keeps_case(self):
        kpepe = self._hl_build("kPEPE")
        self.assertEqual(kpepe["action"]["orders"][0]["a"], 3)
        self.assertEqual(kpepe["coin"], "kPEPE")
        self.assertEqual(self._hl_build("KPEPE")["action"]["orders"][0]["a"], 3)
        self.assertEqual(self._hl_build("eth")["coin"], "ETH")
        for unknown in ("OLD", "DOGE"):
            with self.subTest(coin=unknown):
                with self.assertRaisesRegex(ValueError, "Unknown or ambiguous Hyperliquid perp coin"):
                    self._hl_build(unknown)

    def test_30_hyperliquid_spot_asset_ids(self):
        expectations = {
            "PURR": (10000, "PURR/USDC"),
            "purr": (10000, "PURR/USDC"),
            "PURR/USDC": (10000, "PURR/USDC"),
            "HYPE": (10001, "@1"),
            "HYPE/USDC": (10001, "@1"),
            "@1": (10001, "@1"),
        }
        for coin, (asset, name) in expectations.items():
            with self.subTest(coin=coin):
                built = self._hl_build(coin, is_spot=True)
                self.assertEqual(built["action"]["orders"][0]["a"], asset)
                self.assertEqual(built["coin"], name)
                self.assertEqual(built["market_type"], "spot")
        with self.assertRaisesRegex(ValueError, "Unknown or ambiguous Hyperliquid spot coin"):
            self._hl_build("BTC", is_spot=True)

    def test_31_hyperliquid_wire_numbers_match_float_to_wire(self):
        cases = [
            (0.00001, "0.00001"),
            (97000.0, "97000"),
            (0.1 + 0.2, "0.3"),
            ("3200.0", "3200"),
            ("1.50000000", "1.5"),
            (12345678, "12345678"),
        ]
        for value, wire in cases:
            with self.subTest(value=value):
                order_wire = self._hl_build("BTC", sz=value, px=value)["action"]["orders"][0]
                self.assertEqual((order_wire["p"], order_wire["s"]), (wire, wire))

        for too_precise in ("0.000000001", 1e-9, "1.123456789"):
            with self.subTest(value=too_precise):
                with self.assertRaisesRegex(ValueError, "more than 8 decimals"):
                    build_hyperliquid_order_payload(
                        coin="BTC", is_buy=True, sz=too_precise, px="1", config=self.empty_config
                    )

    def test_32_hyperliquid_max_fee_rate_strings(self):
        cases = [
            (1, False, "0.001%"),
            (5, False, "0.005%"),
            (30, False, "0.03%"),
            (MAX_HYPERLIQUID_PERP_TENTHS_BPS, False, "0.1%"),
            (500, True, "0.5%"),
            (MAX_HYPERLIQUID_TENTHS_BPS, True, "1%"),
        ]
        for tenths, is_spot, rate in cases:
            with self.subTest(tenths=tenths, is_spot=is_spot):
                cfg = OmniFeeConfig(
                    hyperliquid_builder_address=DUMMY_EVM_1,
                    hyperliquid_builder_tenths_bps=tenths,
                )
                built = self._hl_build("PURR" if is_spot else "BTC", is_spot=is_spot, config=cfg)
                self.assertEqual(built["prerequisite_approve_builder_fee"]["maxFeeRate"], rate)
                self.assertEqual(built["action"]["builder"], {"b": DUMMY_EVM_1, "f": tenths})

    def test_33_hyperliquid_approve_builder_fee_complete(self):
        cfg = OmniFeeConfig(
            hyperliquid_builder_address=DUMMY_EVM_MIXED_CASE,
            hyperliquid_builder_tenths_bps=30,
        )
        before_ms = int(time.time() * 1000)
        built = self._hl_build("ETH", config=cfg)
        after_ms = int(time.time() * 1000)

        approve = built["prerequisite_approve_builder_fee"]
        self.assertEqual(
            {key: value for key, value in approve.items() if key != "nonce"},
            {
                "type": "approveBuilderFee",
                "hyperliquidChain": "Mainnet",
                "signatureChainId": "0x66eee",
                "maxFeeRate": "0.03%",
                "builder": DUMMY_EVM_MIXED_CASE.lower(),
            },
        )
        self.assertIsInstance(approve["nonce"], int)
        self.assertTrue(before_ms <= approve["nonce"] <= after_ms)
        self.assertEqual(built["action"]["builder"], {"b": DUMMY_EVM_MIXED_CASE.lower(), "f": 30})

    def test_34_hyperliquid_default_transport_posts_info(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                HL_META
            ).encode("utf-8")
            built = build_hyperliquid_order_payload(
                coin="ETH", is_buy=True, sz="1", px="1", config=self.empty_config
            )
        self.assertEqual(built["action"]["orders"][0]["a"], 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, HL_INFO_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"type": "meta"})
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_35_mcp_tool_hyperliquid_build_order(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 35,
            "method": "tools/call",
            "params": {
                "name": "hyperliquid_build_order",
                "arguments": {
                    "coin": "kPEPE",
                    "is_buy": False,
                    "sz": "1000",
                    "px": "0.012",
                    "reduce_only": True,
                },
            },
        }
        with mock.patch("omni_execution_mcp.rails._hyperliquid_post", side_effect=fake_hl_transport):
            res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertFalse(res["result"]["isError"], res["result"]["content"][0]["text"])
        payload = json.loads(res["result"]["content"][0]["text"])
        self.assertEqual(
            payload["action"]["orders"],
            [{"a": 3, "b": False, "p": "0.012", "s": "1000", "r": True, "t": {"limit": {"tif": "Gtc"}}}],
        )
        self.assertEqual(payload["action"]["builder"], {"b": DUMMY_EVM_1, "f": 30})
        self.assertEqual(payload["prerequisite_approve_builder_fee"]["signatureChainId"], "0x66eee")

        msg["params"]["arguments"]["coin"] = "DOGE"
        with mock.patch("omni_execution_mcp.rails._hyperliquid_post", side_effect=fake_hl_transport):
            res = handle_jsonrpc_message(msg, config=self.full_config)
        self.assertTrue(res["result"]["isError"])
        self.assertIn("Unknown or ambiguous Hyperliquid perp coin", res["result"]["content"][0]["text"])

    def test_36_hyperliquid_spot_tokens_keyed_by_index(self):
        shuffled = {
            "tokens": [
                {"name": "USDC", "index": 0, "szDecimals": 8},
                {"name": "HYPE", "index": 2, "szDecimals": 2},
                {"name": "PURR", "index": 1, "szDecimals": 0},
            ],
            "universe": [
                {"name": "PURR/USDC", "tokens": [1, 0], "index": 0},
                {"name": "@107", "tokens": [2, 0], "index": 107},
            ],
        }
        expectations = {"HYPE": (10107, "@107"), "PURR": (10000, "PURR/USDC")}
        for coin, (asset, name) in expectations.items():
            with self.subTest(coin=coin):
                built = self._hl_build(
                    coin,
                    is_spot=True,
                    transport=lambda url, body: json.dumps(shuffled).encode("utf-8"),
                )
                self.assertEqual(built["action"]["orders"][0]["a"], asset)
                self.assertEqual(built["coin"], name)

    def test_37_default_fee_monetization_config_from_empty_env(self):
        # With empty env dict, all Day-0 non-custodial defaults must be active
        cfg = OmniFeeConfig.from_env({})
        self.assertEqual(cfg.hyperliquid_builder_address, DEFAULT_EVM_FEE_WALLET)
        self.assertEqual(cfg.hyperliquid_builder_tenths_bps, 100)
        self.assertEqual(cfg.jupiter_referral_account, DEFAULT_SOLANA_FEE_WALLET)
        self.assertEqual(cfg.jupiter_fee_bps, 35)
        self.assertEqual(cfg.lifi_integrator_id, "omni-execution-mcp")
        self.assertEqual(cfg.lifi_fee_bps, 35)
        self.assertEqual(cfg.cow_partner_fee_recipient, DEFAULT_EVM_FEE_WALLET)
        self.assertEqual(cfg.cow_partner_fee_bps, 35)
        self.assertEqual(cfg.zerox_fee_recipient, DEFAULT_EVM_FEE_WALLET)
        self.assertEqual(cfg.zerox_fee_bps, 35)
        self.assertTrue(cfg.is_hyperliquid_active)
        self.assertTrue(cfg.is_jupiter_active)
        self.assertTrue(cfg.is_lifi_active)
        self.assertTrue(cfg.is_cow_active)
        self.assertTrue(cfg.is_zerox_active)

    def test_38_fee_monetization_explicit_opt_out(self):
        # Test explicit disabling of rails via empty string, "none", "disabled", "off"
        opt_out_env = {
            "HYPERLIQUID_BUILDER_ADDRESS": "none",
            "JUPITER_REFERRAL_ACCOUNT": "off",
            "COW_PARTNER_FEE_RECIPIENT": "disabled",
            "ZEROX_FEE_RECIPIENT": "",
        }
        cfg = OmniFeeConfig.from_env(opt_out_env)
        self.assertIsNone(cfg.hyperliquid_builder_address)
        self.assertEqual(cfg.hyperliquid_builder_tenths_bps, 0)
        self.assertIsNone(cfg.jupiter_referral_account)
        self.assertIsNone(cfg.cow_partner_fee_recipient)
        self.assertIsNone(cfg.zerox_fee_recipient)
        self.assertFalse(cfg.is_hyperliquid_active)
        self.assertFalse(cfg.is_jupiter_active)
        self.assertFalse(cfg.is_cow_active)
        self.assertFalse(cfg.is_zerox_active)


if __name__ == "__main__":
    unittest.main()
