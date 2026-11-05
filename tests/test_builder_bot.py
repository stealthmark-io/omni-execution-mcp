import base64
import hashlib
import importlib.util
import json
import re
import unittest
import urllib.parse
from decimal import Decimal
from pathlib import Path

from omni_execution_mcp.builder_bot import hl
from omni_execution_mcp.builder_bot.ledger import Ledger, month_bounds_ms

TRADER_ADDR = "0x" + "ab" * 20
OTHER_ADDR = "0x" + "cd" * 20
PAYOUT_ADDR = "0x" + "EF" * 20
BUILDER_ADDR = "0x" + "1A" * 20

META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        {"name": "LOOM", "szDecimals": 1, "maxLeverage": 3, "isDelisted": True},
        {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
    ]
}
MIDS = {"BTC": "97123.5", "ETH": "3456.78", "kPEPE": "0.012345"}


def _fill(tid, oid, builder_fee="0.01", px="100", sz="1", time_ms=1_000, fee_token="USDC"):
    fill = {
        "tid": tid,
        "oid": oid,
        "coin": "BTC",
        "px": px,
        "sz": sz,
        "time": time_ms,
        "fee": "0.05",
        "feeToken": fee_token,
    }
    if builder_fee is not None:
        fill["builderFee"] = builder_fee
    return fill


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.group = self.ledger.create_group("Señales Cripto AR", admin_tg_id=1)

    def tearDown(self):
        self.ledger.close()

    def test_group_id_is_eight_hex_chars_and_default_share_is_half(self):
        self.assertRegex(self.group.id, r"^[0-9a-f]{8}$")
        self.assertEqual(self.group.share_bps, 5000)
        self.assertEqual(self.ledger.groups_for_admin(1), [self.group])

    def test_create_group_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            self.ledger.create_group("   ", admin_tg_id=1)

    def test_attribution_is_first_touch(self):
        other = self.ledger.create_group("Otro grupo", admin_tg_id=2)
        self.ledger.register_trader(10, self.group.id)
        trader = self.ledger.register_trader(10, other.id)
        self.assertEqual(trader.group_id, self.group.id)

    def test_trader_without_group_gets_first_group_link(self):
        self.ledger.register_trader(11)
        trader = self.ledger.register_trader(11, self.group.id)
        self.assertEqual(trader.group_id, self.group.id)
        self.assertEqual(self.ledger.count_group_traders(self.group.id), 1)

    def test_linking_a_different_wallet_drops_the_agent(self):
        self.ledger.link_wallet(12, TRADER_ADDR.upper().replace("0X", "0x"))
        self.ledger.set_agent(12, OTHER_ADDR, '{"crypto": {}}')
        same = self.ledger.link_wallet(12, TRADER_ADDR)
        self.assertEqual(same.hl_address, TRADER_ADDR)
        self.assertEqual(same.agent_address, OTHER_ADDR)
        changed = self.ledger.link_wallet(12, OTHER_ADDR)
        self.assertEqual(changed.hl_address, OTHER_ADDR)
        self.assertIsNone(changed.agent_address)
        self.assertIsNone(changed.agent_keystore)

    def test_ingest_keeps_only_bot_orders_and_dedupes_by_tid(self):
        self.ledger.record_order(TRADER_ADDR, 77, 10, self.group.id, "BTC", True, 30)
        fills = [_fill(1, 77), _fill(1, 77), _fill(2, 999)]
        self.assertEqual(self.ledger.ingest_fills(TRADER_ADDR.upper().replace("0X", "0x"), fills), 1)
        self.assertEqual(self.ledger.ingest_fills(TRADER_ADDR, fills), 0)
        self.assertEqual(self.ledger.group_trader_addresses(self.group.id), [TRADER_ADDR])

    def test_statement_splits_builder_fees_and_rounds_admin_share_down(self):
        self.ledger.set_payout_address(self.group.id, PAYOUT_ADDR)
        self.ledger.record_order(TRADER_ADDR, 77, 10, self.group.id, "BTC", True, 30)
        self.ledger.ingest_fills(
            TRADER_ADDR,
            [
                _fill(1, 77, builder_fee="0.000003", px="100", sz="2"),
                _fill(2, 77, builder_fee="0.02", px="50", sz="1"),
                _fill(3, 77, builder_fee=None),
            ],
        )
        st = self.ledger.statement(self.group.id, 0, 10_000)
        self.assertEqual(st.fills, 3)
        self.assertEqual(st.volume_usd, Decimal("350"))
        self.assertEqual(st.builder_fees_usd, Decimal("0.020003"))
        self.assertEqual(st.admin_share_usd, Decimal("0.010001"))
        self.assertEqual(st.operator_net_usd, Decimal("0.010002"))
        self.assertEqual(st.admin_share_usd + st.operator_net_usd, st.builder_fees_usd)
        self.assertEqual(st.payout_address, PAYOUT_ADDR.lower())

    def test_statement_filters_period_group_and_non_usdc(self):
        other = self.ledger.create_group("Otro grupo", admin_tg_id=2)
        self.ledger.record_order(TRADER_ADDR, 1, 10, self.group.id, "BTC", True, 30)
        self.ledger.record_order(OTHER_ADDR, 2, 20, other.id, "ETH", False, 30)
        self.ledger.ingest_fills(
            TRADER_ADDR,
            [
                _fill(10, 1, time_ms=500),
                _fill(11, 1, time_ms=5_000),
                _fill(12, 1, time_ms=600, fee_token="HYPE"),
            ],
        )
        self.ledger.ingest_fills(OTHER_ADDR, [_fill(20, 2, time_ms=700)])
        st = self.ledger.statement(self.group.id, 0, 1_000)
        self.assertEqual(st.fills, 1)
        self.assertEqual(st.builder_fees_usd, Decimal("0.01"))
        with self.assertRaises(ValueError):
            self.ledger.statement("deadbeef", 0, 1)

    def test_month_bounds_are_utc_and_wrap_december(self):
        self.assertEqual(month_bounds_ms(2026, 1), (1767225600000, 1769904000000))
        start, end = month_bounds_ms(2026, 12)
        self.assertEqual(end, month_bounds_ms(2027, 1)[0])
        with self.assertRaises(ValueError):
            month_bounds_ms(2026, 13)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, json.loads(body)))
        return json.dumps(self.responses.pop(0)).encode()


class TestHlFeesAndLinks(unittest.TestCase):
    def test_fee_rate_percent_matches_hl_percent_strings(self):
        self.assertEqual(hl.fee_rate_percent(1), "0.001%")
        self.assertEqual(hl.fee_rate_percent(10), "0.01%")
        self.assertEqual(hl.fee_rate_percent(25), "0.025%")
        self.assertEqual(hl.fee_rate_percent(100), "0.1%")

    def test_perp_builder_fee_is_capped_at_10_bps(self):
        for bad in (0, 101, 1000):
            with self.assertRaises(ValueError):
                hl.fee_rate_percent(bad)

    def test_builder_fee_usd(self):
        self.assertEqual(hl.builder_fee_usd(Decimal("1000"), 10), Decimal("0.1"))
        self.assertEqual(hl.builder_fee_usd(Decimal("1000"), 100), Decimal("1"))

    def test_sign_page_url_uses_fragment_with_public_params_only(self):
        url = hl.sign_page_url(
            "https://firmar.example/firmar.html", "testnet", TRADER_ADDR.upper().replace("0X", "0x"),
            builder=BUILDER_ADDR, tenths_bps=30, agent=OTHER_ADDR,
        )
        base, _, fragment = url.partition("#")
        self.assertEqual(base, "https://firmar.example/firmar.html")
        params = dict(urllib.parse.parse_qsl(fragment))
        self.assertEqual(params, {
            "red": "testnet",
            "cuenta": TRADER_ADDR,
            "builder": BUILDER_ADDR.lower(),
            "tasa": "0.03%",
            "agente": OTHER_ADDR,
            "nombre": "omnibot",
        })

    def test_sign_page_url_builder_only(self):
        url = hl.sign_page_url("http://localhost:8000/firmar.html", "mainnet", TRADER_ADDR,
                               builder=BUILDER_ADDR, tenths_bps=10)
        params = dict(urllib.parse.parse_qsl(url.partition("#")[2]))
        self.assertNotIn("agente", params)
        self.assertEqual(params["tasa"], "0.01%")


class TestHlOrders(unittest.TestCase):
    def test_resolve_perp_by_index_case_insensitive_and_skips_delisted(self):
        self.assertEqual(hl.resolve_perp(META, "btc"), hl.PerpAsset(0, "BTC", 5))
        self.assertEqual(hl.resolve_perp(META, "KPEPE"), hl.PerpAsset(3, "kPEPE", 0))
        for bad in ("LOOM", "XYZ"):
            with self.assertRaises(ValueError):
                hl.resolve_perp(META, bad)

    def test_to_wire_normalizes_like_the_sdk(self):
        self.assertEqual(hl.to_wire(Decimal("123.400")), "123.4")
        self.assertEqual(hl.to_wire(Decimal("100")), "100")
        self.assertEqual(hl.to_wire(Decimal("-0")), "0")
        with self.assertRaises(ValueError):
            hl.to_wire(Decimal("0.000000001"))

    def test_open_order_builds_sdk_shaped_action_with_builder_code(self):
        preview = hl.open_order(META, MIDS, "btc", True, Decimal("50"), BUILDER_ADDR, 30)
        self.assertEqual(preview.action, {
            "type": "order",
            "orders": [{"a": 0, "b": True, "p": "98095", "s": "0.00051", "r": False,
                        "t": {"limit": {"tif": "Ioc"}}}],
            "grouping": "na",
            "builder": {"b": BUILDER_ADDR.lower(), "f": 30},
        })
        self.assertEqual(list(preview.action), ["type", "orders", "grouping", "builder"])
        self.assertEqual(list(preview.action["orders"][0]), ["a", "b", "p", "s", "r", "t"])
        self.assertEqual(list(preview.action["builder"]), ["b", "f"])
        self.assertEqual(preview.notional_usd, Decimal("49.532985"))
        self.assertEqual(preview.builder_fee_usd, Decimal("0.014860"))
        self.assertNotIn("x402", json.dumps(preview.action).lower())

    def test_open_order_rounds_price_and_size_per_sz_decimals(self):
        eth = hl.open_order(META, MIDS, "ETH", False, Decimal("100"), BUILDER_ADDR, 10)
        self.assertEqual(eth.action["orders"][0]["p"], "3422.2")
        self.assertEqual(eth.action["orders"][0]["s"], "0.0289")
        self.assertFalse(eth.action["orders"][0]["b"])
        pepe = hl.open_order(META, MIDS, "kpepe", True, Decimal("20"), BUILDER_ADDR, 10)
        self.assertEqual(pepe.action["orders"][0]["a"], 3)
        self.assertEqual(pepe.action["orders"][0]["p"], "0.012468")
        self.assertEqual(pepe.action["orders"][0]["s"], "1620")

    def test_open_order_enforces_hl_minimum_of_10_usd(self):
        with self.assertRaises(ValueError):
            hl.open_order(META, MIDS, "BTC", True, Decimal("9"), BUILDER_ADDR, 30)
        with self.assertRaises(ValueError):
            hl.open_order(META, MIDS, "BTC", False, Decimal("10"), BUILDER_ADDR, 30)
        with self.assertRaises(ValueError):
            hl.open_order(META, MIDS, "BTC", True, Decimal("50"), BUILDER_ADDR, 101)

    def test_close_order_is_reduce_only_in_the_opposite_direction(self):
        state = {"assetPositions": [{"position": {"coin": "ETH", "szi": "-0.5"}}]}
        preview = hl.close_order(META, MIDS, state, "eth", BUILDER_ADDR, 30)
        wire = preview.action["orders"][0]
        self.assertTrue(wire["b"])
        self.assertTrue(wire["r"])
        self.assertEqual(wire["s"], "0.5")
        with self.assertRaises(ValueError):
            hl.close_order(META, MIDS, {"assetPositions": []}, "BTC", BUILDER_ADDR, 30)

    def test_parse_order_response_variants(self):
        def ok(status):
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}
        self.assertEqual(
            hl.parse_order_response(ok({"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 77}})),
            ("filled", 77, "0.02 @ 1891.4"),
        )
        self.assertEqual(hl.parse_order_response(ok({"resting": {"oid": 78}}))[:2], ("resting", 78))
        self.assertEqual(
            hl.parse_order_response(ok({"error": "Builder fee has not been approved."})),
            ("error", None, "Builder fee has not been approved."),
        )
        self.assertEqual(hl.parse_order_response({"status": "err", "response": "boom"}), ("error", None, "boom"))

    def test_exchange_payload_matches_sdk_envelope(self):
        payload = hl.exchange_payload({"type": "order"}, 5, {"r": "0x1", "s": "0x2", "v": 27})
        self.assertEqual(list(payload), ["action", "nonce", "signature", "vaultAddress", "expiresAfter"])
        self.assertIsNone(payload["vaultAddress"])


class TestHlClient(unittest.TestCase):
    def test_info_calls_go_to_the_selected_network(self):
        transport = FakeTransport([META, 30])
        client = hl.HyperliquidClient("testnet", transport)
        self.assertEqual(client.meta(), META)
        self.assertEqual(client.max_builder_fee(TRADER_ADDR, BUILDER_ADDR), 30)
        self.assertEqual(transport.calls[0], ("https://api.hyperliquid-testnet.xyz/info", {"type": "meta"}))
        self.assertEqual(transport.calls[1][1], {"type": "maxBuilderFee", "user": TRADER_ADDR, "builder": BUILDER_ADDR})
        with self.assertRaises(ValueError):
            hl.HyperliquidClient("devnet")

    def test_agent_owner(self):
        transport = FakeTransport([
            {"role": "agent", "data": {"user": TRADER_ADDR.upper().replace("0X", "0x")}},
            {"role": "user"},
            "missing",
        ])
        client = hl.HyperliquidClient("testnet", transport)
        self.assertEqual(client.agent_owner(OTHER_ADDR), TRADER_ADDR)
        self.assertIsNone(client.agent_owner(OTHER_ADDR))
        self.assertIsNone(client.agent_owner(OTHER_ADDR))

    def test_fills_between_pages_by_time(self):
        first = [_fill(i, 1, time_ms=i) for i in range(1, 501)]
        second = [_fill(500, 1, time_ms=500), _fill(501, 1, time_ms=600)]
        transport = FakeTransport([first, second])
        fills = hl.HyperliquidClient("mainnet", transport).fills_between(TRADER_ADDR, 1, 1_000)
        self.assertEqual(len(fills), 502)
        self.assertEqual(transport.calls[1][1]["startTime"], 500)
        self.assertEqual(transport.calls[1][1]["endTime"], 1_000)


class TestAgentSigner(unittest.TestCase):
    def test_requires_a_passphrase(self):
        with self.assertRaises(ValueError):
            hl.AgentSigner("", is_mainnet=False)

    @unittest.skipIf(importlib.util.find_spec("hyperliquid"), "SDK instalado")
    def test_explains_how_to_install_the_sdk(self):
        with self.assertRaises(RuntimeError) as ctx:
            hl.AgentSigner("passphrase-de-prueba", is_mainnet=False)
        self.assertIn("omni-execution-mcp[bot]", str(ctx.exception))


class TestSignPage(unittest.TestCase):
    def setUp(self):
        self.html = (Path(hl.__file__).with_name("static") / "firmar.html").read_text(encoding="utf-8")

    def test_csp_hashes_match_the_inline_style_and_script(self):
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', self.html).group(1)
        for tag, directive in (("style", "style-src"), ("script", "script-src")):
            bodies = re.findall(rf"<{tag}>(.*?)</{tag}>", self.html, re.S)
            self.assertEqual(len(bodies), 1)
            digest = base64.b64encode(hashlib.sha256(bodies[0].encode("utf-8")).digest()).decode()
            self.assertIn(f"{directive} 'sha256-{digest}';", csp)
        self.assertIn("default-src 'none';", csp)
        self.assertIn("connect-src https://api.hyperliquid.xyz https://api.hyperliquid-testnet.xyz;", csp)

    def test_page_never_injects_html_or_loads_remote_code(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "src="):
            self.assertNotIn(banned, self.html)

    def test_page_signs_the_sdk_user_signed_types(self):
        for fragment in (
            '"HyperliquidTransaction:ApproveBuilderFee"',
            '"HyperliquidTransaction:ApproveAgent"',
            '{ name: "maxFeeRate", type: "string" }',
            '{ name: "builder", type: "address" }',
            '{ name: "agentAddress", type: "address" }',
            '{ name: "agentName", type: "string" }',
            '{ name: "nonce", type: "uint64" }',
            'name: "HyperliquidSignTransaction"',
        ):
            self.assertIn(fragment, self.html)


if __name__ == "__main__":
    unittest.main()
